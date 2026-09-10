"""Tests for PatchSpace reconciliation + supervision, using a fake
in-memory PipeWire graph (no real pw-dump / pw-cli involved)."""

import time

import pytest

from pwproc import Backoff
from pwnodes import (
    PatchSpace,
    InputNode,
    OutputNode,
    BackedNode,
    GateNode,
    Node,
)


class FakeGraph:
    """Minimal in-memory stand-in for pwgraph.PipewireGraph, exposing
    exactly the surface PatchSpace.sync() / pwmatch touch."""

    def __init__(self):
        self._nodes = {}
        self._ports = {}
        self._links = []
        self._next_port = 1

    # -- population helpers ------------------------------------------------

    def add_source(self, node_id, name, media_class="Stream/Output/Audio",
                   channels=("FL", "FR"), app=None):
        props = {"node.name": name, "media.class": media_class}
        if app:
            props["application.name"] = app
        self._nodes[node_id] = {"info": {"props": props}}
        out_ports = {}
        for ch in channels:
            pid = self._next_port
            self._next_port += 1
            self._ports[pid] = {
                "info": {
                    "props": {
                        "node.id": node_id,
                        "port.direction": "out",
                        "port.name": f"out_{ch}",
                        "audio.channel": ch,
                    }
                }
            }
            out_ports[ch] = pid
        return out_ports

    def add_sink(self, node_id, name, media_class="Audio/Sink",
                 channels=("FL", "FR")):
        self._nodes[node_id] = {
            "info": {"props": {"node.name": name, "media.class": media_class}}
        }
        in_ports = {}
        for ch in channels:
            pid = self._next_port
            self._next_port += 1
            self._ports[pid] = {
                "info": {
                    "props": {
                        "node.id": node_id,
                        "port.direction": "in",
                        "port.name": f"in_{ch}",
                        "audio.channel": ch,
                    }
                }
            }
            in_ports[ch] = pid
        return in_ports

    def remove_node(self, node_id):
        self._nodes.pop(node_id, None)
        for pid in [p for p, d in self._ports.items()
                    if d["info"]["props"].get("node.id") == node_id]:
            self._ports.pop(pid, None)
        self._links = [(o, i) for o, i in self._links
                       if o in self._ports and i in self._ports]

    # -- accessors used by pwmatch / PatchSpace ----------------------------

    def nodes(self):
        return dict(self._nodes)

    def ports(self):
        return dict(self._ports)

    def ports_for_node(self, node_id):
        return {
            pid: d for pid, d in self._ports.items()
            if d["info"]["props"].get("node.id") == node_id
        }

    def linked_pairs(self):
        return set(self._links)

    def node_id_by_name(self, name):
        for nid, d in self._nodes.items():
            if d["info"]["props"].get("node.name") == name:
                return nid
        return None

    def has_node_with_name(self, name):
        return self.node_id_by_name(name) is not None

    def connect(self, out_port, in_port):
        if (out_port, in_port) in self._links:
            return False
        self._links.append((out_port, in_port))
        return True

    def disconnect(self, out_port, in_port):
        if (out_port, in_port) in self._links:
            self._links.remove((out_port, in_port))
            return True
        return False


class SrcNode(InputNode):
    def source_filters(self):
        return [{"nodeName": "app1"}]


class SinkNode(OutputNode):
    def sink_filters(self):
        return [{"name": "sink1"}]


class EchoBacked(BackedNode):
    """A BackedNode for testing; 'structural' pieces are plain fake
    backings that don't spawn anything."""

    def __init__(self, node_id, backing_node_name="b"):
        super().__init__(node_id, backing_node_name)
        self.struct_broken = False
        self.module_broken = False
        self.spawn_succeeds = True
        self.struct_repairs = 0
        self.module_spawns = 0
        self.module_reloads = 0
        self._fake_module = None

    class _FakeOwned:
        def __init__(self, alive=True):
            self.node_id = 7
            self._alive = alive
            self.owns_process = True

        @property
        def is_alive(self):
            return self._alive

        def destroy(self):
            self._alive = False

    def has_module(self):
        return True

    def module_backing(self):
        if self._fake_module is None:
            return None
        return self._fake_module

    def structural_ok(self):
        return not self.struct_broken

    def module_ok(self):
        if self.module_broken:
            return False
        return self._fake_module is not None and self._fake_module.is_alive

    def ensure_structural(self):
        self.struct_repairs += 1
        if self.spawn_succeeds:
            self.struct_broken = False

    def ensure_module(self):
        self.module_spawns += 1
        if self.spawn_succeeds:
            self._fake_module = self._FakeOwned()
            self.module_broken = False

    def reload_module(self):
        self.module_reloads += 1
        self._fake_module = self._FakeOwned()
        self.module_broken = False

    def teardown_backing(self):
        self._fake_module = None
        self.backings.clear()


def make_space(graph, **kw):
    return PatchSpace(graph, **kw)


# ---------------------------------------------------------------------------
# sync / reconciliation
# ---------------------------------------------------------------------------


def test_sync_connects_matching_channels():
    g = FakeGraph()
    src_ports = g.add_source(10, "app1")
    sink_ports = g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()

    src = SrcNode("src")
    snk = SinkNode("snk")
    space.add_node(src)
    space.add_node(snk)
    space.add_edge("src", "snk")
    space.sync()

    # Every shared channel between the two nodes got linked.
    assert (src_ports["FL"], sink_ports["FL"]) in g.linked_pairs()
    assert (src_ports["FR"], sink_ports["FR"]) in g.linked_pairs()


def test_sync_idempotent_second_pass_does_not_relink():
    g = FakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")
    space.sync()
    count_after_first = len(g._links)
    space.sync()
    assert len(g._links) == count_after_first


def test_gate_close_disconnects_downstream():
    g = FakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(GateNode("gate"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "gate")
    space.add_edge("gate", "snk")
    space.sync()
    assert len(g.linked_pairs()) == 2

    # Close the gate: every edge downstream of it must be torn down.
    space.nodes["gate"].enabled = False
    space.sync()
    assert len(g.linked_pairs()) == 0

    # Re-open: links come back.
    space.nodes["gate"].enabled = True
    space.sync()
    assert len(g.linked_pairs()) == 2


def test_removing_node_tears_down_and_unlinks():
    g = FakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")
    space.sync()
    assert g.linked_pairs()

    space.remove_node("snk")
    space.sync()
    assert not g.linked_pairs()


# ---------------------------------------------------------------------------
# supervision
# ---------------------------------------------------------------------------


def test_supervise_repairs_dead_structural():
    g = FakeGraph()
    space = make_space(g, repair_gate=Backoff(initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = EchoBacked("n")
    node.ensure_module()  # healthy from the start
    space.add_node(node)

    node.struct_broken = True
    repairs_before = node.struct_repairs
    space.supervise()
    assert node.struct_repairs == repairs_before + 1
    assert not node.struct_broken


def test_supervise_spawns_missing_module():
    g = FakeGraph()
    space = make_space(g, repair_gate=Backoff(initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = EchoBacked("n")
    node.module_broken = True  # module never came up
    space.add_node(node)
    assert node.module_spawns == 0

    space.supervise()
    assert node.module_spawns == 1
    assert node.module_ok()


def test_supervise_reloads_crashed_module():
    g = FakeGraph()
    space = make_space(g, repair_gate=Backoff(initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = EchoBacked("n")
    node.ensure_module()
    space.add_node(node)

    # module process died on its own
    node._fake_module._alive = False
    space.supervise()
    assert node.module_reloads == 1
    assert node.module_ok()


def test_supervise_performs_due_reload():
    g = FakeGraph()
    space = make_space(g, repair_gate=Backoff(initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = EchoBacked("n")
    node.ensure_module()
    space.add_node(node)

    node.schedule_reload()
    space.supervise()
    assert node.module_reloads == 1


def test_failing_module_is_not_thrashed_every_tick():
    """A module that keeps failing to spawn must be retried with backoff,
    not re-attempted on every single supervision tick."""
    g = FakeGraph()
    gate = Backoff(initial_s=5.0, jitter_fraction=0)
    space = make_space(g, repair_gate=gate)
    space.mark_graph_loaded()
    node = EchoBacked("n")
    node.module_broken = True
    node.spawn_succeeds = False
    space.add_node(node)

    space.supervise()  # first attempt
    assert node.module_spawns == 1
    space.supervise()  # should be gated by backoff
    space.supervise()
    assert node.module_spawns == 1


def test_healthy_node_clears_prior_backoff():
    g = FakeGraph()
    space = make_space(g, repair_gate=Backoff(initial_s=5.0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = EchoBacked("n")
    node.module_broken = True
    node.spawn_succeeds = False
    space.add_node(node)
    space.supervise()
    assert node.module_spawns == 1

    # node becomes healthy by other means; a later failure starts fresh
    node.spawn_succeeds = True
    space._repair_gate.record_success(("n", "module"))
    space._repair_gate.record_success(("n", "structural"))
    node.module_broken = True
    space.supervise()
    assert node.module_spawns == 2


def test_sync_reconnects_externally_dropped_link():
    """If a link we own disappears out from under us (external destroy,
    module reload), the next sync must re-make it - the reconciliation
    keys off the live graph, not our own bookkeeping."""
    g = FakeGraph()
    src_ports = g.add_source(10, "app1")
    sink_ports = g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")
    space.sync()

    g._links.remove((src_ports["FL"], sink_ports["FL"]))  # link vanishes
    assert len(g.linked_pairs()) == 1
    space.sync()
    assert len(g.linked_pairs()) == 2


def test_staged_node_links_are_not_derived_until_unstaged():
    """sync_locked() must leave a staged node alone: a caller
    (_load_session) is bringing it up one-at-a-time and its module may be
    about to be re-spawned, so deriving/committing internal links or
    inbound edges here would wire against streams that vanish. Once
    unstaged, the next sync wires both the user edge and the interior."""
    g = FakeGraph()
    g.add_source(10, "srcnode")          # internal-link source
    g.add_sink(20, "sinknode")           # internal-link target
    g.add_source(30, "app1")             # user-edge source
    g.add_sink(40, "inbound_sink")       # user-edge target
    space = make_space(g)
    space.mark_graph_loaded()

    class LinkedBacked(BackedNode):
        def __init__(self, node_id, backing_node_name="b"):
            super().__init__(node_id, backing_node_name)

        def ensure_structural(self):
            pass

        def structural_ok(self):
            return True

        def input_identity(self, port="in"):
            return {"name": "inbound_sink"}

        def internal_links(self):
            return [({"nodeName": "srcnode"}, {"name": "sinknode"})]

    space.add_node(LinkedBacked("n"))
    space.add_node(SrcNode("src"))
    space.add_edge("src", "n")

    space.stage(["n"])
    space.sync()
    assert not g.linked_pairs()  # staged: nothing derived

    space.unstage("n")
    space.sync()
    # FL+FR for the user edge plus FL+FR for the internal link.
    assert len(g.linked_pairs()) == 4


def test_handle_node_removed_restarts_orphaned_backing():
    """A live object disappearing while its owning process is still
    alive (someone pw-cli destroy'd our node) must drop the owner so the
    supervisor respawns it, rather than leaving a process that owns
    nothing."""
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()

    node = EchoBacked("n")

    class FakeOwned:
        owns_process = True
        is_alive = True
        node_id = 55
        name = "fake_owned"
        destroyed = False

        def destroy(self):
            self.destroyed = True

    owned = FakeOwned()
    node.backings.append(owned)
    space.add_node(node)

    space.handle_node_removed(55)
    assert owned.destroyed
    assert owned not in node.backings


def test_one_broken_node_does_not_stop_others():
    g = FakeGraph()
    space = make_space(g, repair_gate=Backoff(initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()

    class BoomNode(EchoBacked):
        def ensure_structural(self):
            raise RuntimeError("boom")

    bad = BoomNode("bad")
    good = EchoBacked("good")
    good.module_broken = True
    space.add_node(bad)
    space.add_node(good)

    space.supervise()  # must not raise, and must still repair `good`
    assert good.module_spawns == 1
