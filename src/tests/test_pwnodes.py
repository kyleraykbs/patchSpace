"""Tests for PatchSpace reconciliation + supervision, using a fake
in-memory PipeWire graph (no real pw-dump / pw-cli involved)."""

import time

import pytest

from pwproc import Backoff
from pwmatch import INTERNAL_MEDIA_CLASS, SOURCE_MEDIA_CLASSES
from pwnodes import (
    PatchSpace,
    InputNode,
    OutputNode,
    BackedNode,
    GateNode,
    SwitcherNode,
    InverseSwitcherNode,
    BooleanSourceNode,
    BooleanSplitterNode,
    BooleanInvertNode,
    BooleanAndNode,
    BooleanOrNode,
    WarpInNode,
    WarpOutNode,
    BooleanWarpInNode,
    BooleanWarpOutNode,
    NoiseCancelNode,
    SplitterNode,
    VolumeProcessNode,
    VirtualSpeakerNode,
    DeviceOutputNode,
    _base_node_name,
    Node,
    LINK_CONFIRM_TIMEOUT_S,
    device_profile_name,
    pick_auto_a2dp_profile,
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

    def add_source(
        self,
        node_id,
        name,
        media_class="Stream/Output/Audio",
        channels=("FL", "FR"),
        app=None,
    ):
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

    def add_sink(self, node_id, name, media_class="Audio/Sink", channels=("FL", "FR")):
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
        for pid in [
            p
            for p, d in self._ports.items()
            if d["info"]["props"].get("node.id") == node_id
        ]:
            self._ports.pop(pid, None)
        self._links = [
            (o, i) for o, i in self._links if o in self._ports and i in self._ports
        ]

    # -- accessors used by pwmatch / PatchSpace ----------------------------

    def nodes(self):
        return dict(self._nodes)

    def ports(self):
        return dict(self._ports)

    def ports_for_node(self, node_id):
        return {
            pid: d
            for pid, d in self._ports.items()
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


class AsyncFakeGraph(FakeGraph):
    """FakeGraph whose freshly-made links are only visible to
    linked_pairs() after flush() - mimicking a real pw-dump snapshot,
    which lags a just-issued pw-link."""

    def __init__(self):
        super().__init__()
        self._pending_links = []

    def connect(self, out_port, in_port):
        if (out_port, in_port) in self._links or (
            out_port,
            in_port,
        ) in self._pending_links:
            return False
        self._pending_links.append((out_port, in_port))
        return True

    def flush(self):
        for pair in self._pending_links:
            if pair not in self._links:
                self._links.append(pair)
        self._pending_links.clear()

    def linked_pairs(self):
        return set(self._links)


class WiringBacked(BackedNode):
    def __init__(self, node_id, backing_node_name="fx"):
        super().__init__(node_id, backing_node_name)

    def ensure_structural(self):
        pass

    def structural_ok(self):
        return True

    def input_identity(self, port="in"):
        return {"name": "dummy_in"}

    def internal_links(self):
        return [({"nodeName": "src_dsp"}, {"name": "dsp_in"})]


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


class NamedSink(OutputNode):
    def __init__(self, node_id, name):
        super().__init__(node_id)
        self._name = name

    def sink_filters(self):
        return [{"name": self._name}]


def test_switcher_only_routes_the_selected_output():
    g = FakeGraph()
    src_ports = g.add_source(10, "app1")
    on_ports = g.add_sink(20, "sinkA")
    off_ports = g.add_sink(30, "sinkB")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SwitcherNode("sw", output=1))  # output 1 = "on"
    space.add_node(NamedSink("on", "sinkA"))
    space.add_node(NamedSink("off", "sinkB"))
    space.add_edge("src", "sw")
    space.add_edge("sw", "on", from_port="on")
    space.add_edge("sw", "off", from_port="off")
    space.sync()

    # "on" selected: only the on branch carries audio.
    assert (src_ports["FL"], on_ports["FL"]) in g.linked_pairs()
    assert (src_ports["FL"], off_ports["FL"]) not in g.linked_pairs()

    # Flip to "off": on is torn down, off comes up.
    space.nodes["sw"].output = 0
    space.sync()
    assert (src_ports["FL"], off_ports["FL"]) in g.linked_pairs()
    assert (src_ports["FL"], on_ports["FL"]) not in g.linked_pairs()


def test_switcher_state_propagates_through_downstream_transparent_nodes():
    g = FakeGraph()
    src_ports = g.add_source(10, "app1")
    on_ports = g.add_sink(20, "sinkA")
    off_ports = g.add_sink(30, "sinkB")
    space = make_space(g)
    space.mark_graph_loaded()
    for node in (
        SrcNode("src"),
        SwitcherNode("sw", output=1),
        GateNode("ga"),
        GateNode("gb"),
        NamedSink("on", "sinkA"),
        NamedSink("off", "sinkB"),
    ):
        space.add_node(node)
    space.add_edge("src", "sw")
    space.add_edge("sw", "ga", from_port="on")
    space.add_edge("ga", "on")
    space.add_edge("sw", "gb", from_port="off")
    space.add_edge("gb", "off")
    space.sync()
    assert (src_ports["FL"], on_ports["FL"]) in g.linked_pairs()
    assert (src_ports["FL"], off_ports["FL"]) not in g.linked_pairs()

    space.nodes["sw"].output = 0
    space.sync()
    assert (src_ports["FL"], off_ports["FL"]) in g.linked_pairs()
    assert (src_ports["FL"], on_ports["FL"]) not in g.linked_pairs()


class NamedSource(InputNode):
    def __init__(self, node_id, name):
        super().__init__(node_id)
        self._name = name

    def source_filters(self):
        return [{"nodeName": self._name}]


def test_inverse_switcher_only_passes_the_selected_input():
    g = FakeGraph()
    a_ports = g.add_source(10, "srcA")
    b_ports = g.add_source(20, "srcB")
    sink_ports = g.add_sink(30, "sink")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(NamedSource("sa", "srcA"))
    space.add_node(NamedSource("sb", "srcB"))
    space.add_node(InverseSwitcherNode("inv", output=1))  # "on"
    space.add_node(NamedSink("snk", "sink"))
    space.add_edge("sa", "inv", to_port="on")
    space.add_edge("sb", "inv", to_port="off")
    space.add_edge("inv", "snk")
    space.sync()

    # "on" input selected: only srcA reaches the sink.
    assert (a_ports["FL"], sink_ports["FL"]) in g.linked_pairs()
    assert (b_ports["FL"], sink_ports["FL"]) not in g.linked_pairs()

    space.nodes["inv"].output = 0
    space.sync()
    assert (b_ports["FL"], sink_ports["FL"]) in g.linked_pairs()
    assert (a_ports["FL"], sink_ports["FL"]) not in g.linked_pairs()


def test_inverse_switcher_accepts_two_inputs_but_gate_does_not():
    space = make_space(FakeGraph())
    space.mark_graph_loaded()
    space.add_node(NamedSource("sa", "srcA"))
    space.add_node(NamedSource("sb", "srcB"))
    space.add_node(InverseSwitcherNode("inv"))
    space.add_node(GateNode("gate"))
    space.add_edge("sa", "inv", to_port="on")
    space.add_edge("sb", "inv", to_port="off")  # allowed: selectable inputs
    space.add_edge("sa", "gate")
    with pytest.raises(ValueError):
        space.add_edge("sb", "gate")


def test_edges_with_same_nodes_but_different_source_ports_are_distinct():
    space = make_space(FakeGraph())
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SwitcherNode("sw"))
    space.add_node(SinkNode("snk"))
    id_a = space.add_edge("sw", "snk", from_port="on")
    id_b = space.add_edge("sw", "snk", from_port="off")
    assert id_a != id_b
    assert len(space.edges) == 2


def test_wiring_issues_nonconflicting_links_in_one_pass():
    g = AsyncFakeGraph()
    src_ports = g.add_source(10, "app1")
    sink_ports = g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")

    space.sync()
    # Both channel pairs touch distinct ports, so both are issued at
    # once rather than one-per-pass.
    assert set(g._pending_links) == {
        (src_ports["FL"], sink_ports["FL"]),
        (src_ports["FR"], sink_ports["FR"]),
    }
    assert g.linked_pairs() == set()

    # Another pass while they are unconfirmed must not pile on more.
    space.sync()
    assert len(g._pending_links) == 2

    g.flush()
    space.sync()
    assert len(g.linked_pairs()) == 2
    assert g._pending_links == []


def test_wiring_connects_internal_dsp_links_before_user_edges():
    g = AsyncFakeGraph()
    g.add_source(10, "app1")  # user edge source
    dsp_ports = g.add_source(30, "src_dsp")  # internal link source
    dsp_in_ports = g.add_sink(40, "dsp_in")  # internal link target
    g.add_sink(50, "dummy_in")  # user edge target
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(WiringBacked("n"))
    space.add_edge("src", "n")

    space.sync()
    # The internal sandwich link is issued first (so the module side
    # exists before a user edge attaches to the dummy); the user edge
    # may follow in the same pass because it touches different ports.
    assert g._pending_links[0] == (dsp_ports["FL"], dsp_in_ports["FL"])


def test_unconfirmed_link_times_out_and_pacing_continues():
    g = AsyncFakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")

    space.sync()
    assert len(g._pending_links) == 2

    # Pretend a link has been waiting past the confirm timeout; the next
    # pass must retire it and carry on rather than block forever.
    space._inflight_links[(999, 998)] = (
        "ghost",
        time.monotonic() - LINK_CONFIRM_TIMEOUT_S - 1,
    )
    space.sync()
    assert (999, 998) not in space._inflight_links


def test_unconfirmable_link_backs_off_instead_of_retrying_forever(monkeypatch):
    """A connect that returns success but never shows up in the live graph
    (a Bluetooth sink rejecting the source's format) must not be re-issued
    on every pass forever.  It backs off, then retries once the backoff
    elapses."""
    monkeypatch.setattr("pwnodes.LINK_CONFIRM_TIMEOUT_S", 0.0)
    g = AsyncFakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    now = [1000.0]
    space._link_gate = Backoff(
        initial_s=5.0, max_s=30.0, clock=lambda: now[0]
    )
    connects = []
    orig_connect = g.connect

    def counting_connect(o, i):
        connects.append((o, i))
        return orig_connect(o, i)

    g.connect = counting_connect
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")

    space.sync()
    assert len(connects) == 2  # FL + FR issued, both pending/never live

    # Timeout -> backoff recorded, and the next passes must NOT re-issue.
    space.sync()
    assert len(connects) == 2
    space.sync()
    assert len(connects) == 2

    # Past the backoff, a retry is allowed again.
    now[0] += 100.0
    space.sync()
    assert len(connects) == 4


def test_removing_edge_forgets_link_backoff(monkeypatch):
    """Dropping the edge clears the pair's backoff so a deliberate re-add
    retries immediately rather than waiting out the old backoff."""
    monkeypatch.setattr("pwnodes.LINK_CONFIRM_TIMEOUT_S", 0.0)
    g = AsyncFakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")

    space.sync()
    space.sync()  # times the pending pair out, records backoff
    assert space._link_gate._state, "expected a backed-off pair"

    space.remove_edge("src->snk")
    space.sync()
    assert space._link_gate._state == {}


def test_removing_edge_with_inflight_link_does_not_stall():
    g = AsyncFakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")

    space.sync()
    assert space._inflight_links
    space.remove_edge("src->snk")
    assert space._inflight_links == {}
    assert space._orphan_disconnects
    space.sync()  # must not raise or block on the dead link


def test_node_internals_wired_tracks_half_connected_effect():
    g = AsyncFakeGraph()
    g.add_source(30, "src_dsp")
    g.add_sink(40, "dsp_in")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(WiringBacked("n"))

    # Nothing derived yet -> not wired.
    assert not space.node_internals_wired("n")

    space.sync()
    # The internal connect is issued but hasn't shown up live yet.
    assert not space.node_internals_wired("n")

    g.flush()
    space.sync()
    assert space.node_internals_wired("n")


def test_node_internals_wired_true_for_no_internal_links():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    # A plain (non-backed) node, and a backed node with no interior,
    # both count as wired - there is nothing to be half-connected.
    assert space.node_internals_wired("src")
    assert space.node_internals_wired("nonexistent")


def test_drop_edge_links_forces_a_fresh_reconnect():
    g = FakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(SrcNode("src"))
    space.add_node(SinkNode("snk"))
    space.add_edge("src", "snk")
    space.sync()
    assert len(g.linked_pairs()) == 2

    # Drop the edge's live pairs the way session load's re-link pass does.
    space.drop_edge_links("src->snk")
    assert g.linked_pairs() == set()
    assert "src->snk" not in space._edge_links

    # The next sync re-derives and re-creates them.
    space.sync()
    assert len(g.linked_pairs()) == 2


def test_internal_media_class_hides_plumbing_but_keeps_it_routable():
    # An Audio/Sink subclass, so the adapter still creates the sink and
    # monitor ports (a custom top-level class yields zero ports)...
    assert INTERNAL_MEDIA_CLASS.startswith("Audio/Sink")
    # ...but not the exact string pipewire-pulse exposes as a sink.
    assert INTERNAL_MEDIA_CLASS != "Audio/Sink"
    # And the engine still treats it as a routable source.
    assert INTERNAL_MEDIA_CLASS in SOURCE_MEDIA_CLASSES

    # Splitters hide; things an app selects or wpctl drives stay visible.
    assert SplitterNode("s").MEDIA_CLASS == INTERNAL_MEDIA_CLASS
    assert VolumeProcessNode("v", "v").MEDIA_CLASS == "Audio/Sink"
    assert VirtualSpeakerNode("vs", "vs").MEDIA_CLASS == "Audio/Sink"


def test_chain_effect_dummy_sinks_use_internal_media_class():
    node = NoiseCancelNode("nc", "nc")
    recorded = []
    node._prune_dead = lambda *a, **k: None
    node._ensure_null_sink = lambda *a, **k: recorded.append(k.get("media_class"))
    node._ensure_feed = lambda *a, **k: None
    node._ensure_drain = lambda *a, **k: None
    node.ensure_structural()
    assert recorded  # the two dummies
    assert all(cls == INTERNAL_MEDIA_CLASS for cls in recorded)


def test_device_renamed_with_suffix_is_rebound():
    g = FakeGraph()
    g.add_sink(20, "sink1.3")  # the live hardware object, suffixed
    space = make_space(g)
    space.mark_graph_loaded()
    node = DeviceOutputNode("out", "sink1")  # stores the unsuffixed name
    node.apply_device_settings = lambda: None  # no wpctl in tests
    space.add_node(node)

    space._sync_device_bindings()

    assert node.device_name == "sink1.3"
    assert node.live_node_id == 20
    # And sync() now links the source into the renamed sink.
    assert _base_node_name("bluez_output.38_FB.1") == "bluez_output.38_FB"


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


def test_detach_nodes_collects_backings_without_destroying():
    """Batch teardown: detach_nodes must remove the nodes from the model
    and hand back their backings for the caller to destroy in parallel,
    so a graph full of effects costs the slowest process instead of the
    sum of every node's teardown."""
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(EchoBacked("n"))
    space.add_node(EchoBacked("m"))

    class Backing:
        def __init__(self):
            self.destroyed = False

        def destroy(self):
            self.destroyed = True

    a, b = Backing(), Backing()
    space.nodes["n"].backings.append(a)
    space.nodes["m"].backings.append(b)

    doomed = space.detach_nodes(["n", "m"])
    assert set(doomed) == {a, b}
    assert not a.destroyed and not b.destroyed  # destruction is the caller's job
    assert "n" not in space.nodes and "m" not in space.nodes


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
    g.add_source(10, "srcnode")  # internal-link source
    g.add_sink(20, "sinknode")  # internal-link target
    g.add_source(30, "app1")  # user-edge source
    g.add_sink(40, "inbound_sink")  # user-edge target
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


# ---------------------------------------------------------------------------
# boolean control signals
# ---------------------------------------------------------------------------


def test_boolean_source_drives_gate_state():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()

    # Gate's stored default says "closed", but a wired boolean source
    # overrides it.
    src = BooleanSourceNode("b", output=1)
    gate = GateNode("g", enabled=False)
    space.add_node(src)
    space.add_node(gate)
    space.add_edge("b", "g", "ctrl")

    space._refresh_boolean_states()
    assert gate.gate_open() is True

    # Flip the source off; the gate follows.
    src.output = 0
    space._refresh_boolean_states()
    assert gate.gate_open() is False

    # Removing the control edge falls back to the gate's own default.
    space.remove_edge(space._edge_id("b", "g", "ctrl"))
    space._refresh_boolean_states()
    assert gate.gate_open() is False  # stored enabled=False


def test_boolean_splitter_forwards_to_both_outputs():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()

    space.add_node(BooleanSourceNode("b", output=1))
    space.add_node(BooleanSplitterNode("s"))
    space.add_node(GateNode("g1", enabled=False))
    space.add_node(SwitcherNode("g2", output=0))
    space.add_edge("b", "s", "in")
    space.add_edge("s", "g1", "ctrl", from_port="out1")
    space.add_edge("s", "g2", "ctrl", from_port="out2")

    space._refresh_boolean_states()
    assert space.nodes["g1"].gate_open() is True
    # true -> switcher channel "on".
    assert space.nodes["g2"].active_output() == "on"


def test_mixed_kind_edge_is_rejected():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(BooleanSourceNode("b"))
    space.add_node(GateNode("g"))
    space.add_node(SrcNode("src"))

    # boolean output -> audio input
    with pytest.raises(ValueError):
        space.add_edge("b", "g", "in")
    # audio output -> boolean input
    with pytest.raises(ValueError):
        space.add_edge("src", "g", "ctrl")


def test_boolean_input_accepts_only_one_driver():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(BooleanSourceNode("b1"))
    space.add_node(BooleanSourceNode("b2"))
    space.add_node(GateNode("g"))
    space.add_edge("b1", "g", "ctrl")
    with pytest.raises(ValueError):
        space.add_edge("b2", "g", "ctrl")


def test_boolean_edge_never_becomes_a_pipewire_link():
    g = FakeGraph()
    g.add_source(10, "app1")
    g.add_sink(20, "sink1")
    space = make_space(g)
    space.mark_graph_loaded()

    space.add_node(SrcNode("src"))
    space.add_node(GateNode("gate"))
    space.add_node(SinkNode("out"))
    space.add_node(BooleanSourceNode("b", output=1))
    space.add_edge("src", "gate", "in")
    audio_eid = space.add_edge("gate", "out", "in")
    bool_eid = space.add_edge("b", "gate", "ctrl")

    space.sync()
    # The audio edge out of the (open) gate got a real pair; the boolean
    # control edge never gets one.
    assert space._edge_links[audio_eid].pairs
    assert bool_eid not in space._edge_links


def test_boolean_invert_negates_its_input():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(BooleanSourceNode("b", output=1))
    space.add_node(BooleanInvertNode("n"))
    space.add_node(GateNode("g", enabled=True))
    space.add_edge("b", "n", "in")
    space.add_edge("n", "g", "ctrl")

    space._refresh_boolean_states()
    assert space.nodes["g"].gate_open() is False  # inverted true -> false

    space.nodes["b"].output = 0
    space._refresh_boolean_states()
    assert space.nodes["g"].gate_open() is True  # inverted false -> true

    # An unwired inverter emits no value, so downstream keeps its default.
    space.remove_edge(space._edge_id("n", "g", "ctrl"))
    space._refresh_boolean_states()
    assert space.nodes["g"].gate_open() is True  # stored enabled=True


def test_boolean_and_or_gates_combine_two_inputs():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(BooleanSourceNode("s1", output=1))
    space.add_node(BooleanSourceNode("s2", output=1))
    space.add_node(BooleanAndNode("and"))
    space.add_node(BooleanOrNode("or"))
    space.add_node(GateNode("ga", enabled=False))
    space.add_node(GateNode("go", enabled=False))
    space.add_edge("s1", "and", "a")
    space.add_edge("s2", "and", "b")
    space.add_edge("and", "ga", "ctrl")
    space.add_edge("s1", "or", "a")
    space.add_edge("s2", "or", "b")
    space.add_edge("or", "go", "ctrl")

    def states():
        space._refresh_boolean_states()
        return space.nodes["ga"].gate_open(), space.nodes["go"].gate_open()

    assert states() == (True, True)  # 1 AND 1 | 1 OR 1
    space.nodes["s1"].output = 0
    assert states() == (False, True)  # 0 AND 1 | 0 OR 1
    space.nodes["s2"].output = 0
    assert states() == (False, False)  # 0 AND 0 | 0 OR 0
    space.nodes["s1"].output = 1
    assert states() == (False, True)  # 1 AND 0 | 1 OR 0


def test_boolean_logic_single_input_passes_through_and_empty_emits_nothing():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(BooleanSourceNode("s", output=1))
    space.add_node(BooleanAndNode("and"))
    space.add_node(GateNode("gate", enabled=False))
    space.add_edge("s", "and", "a")  # only "a" wired; "b" left open
    space.add_edge("and", "gate", "ctrl")

    space._refresh_boolean_states()
    assert space.nodes["gate"].gate_open() is True  # passes the one input
    space.nodes["s"].output = 0
    space._refresh_boolean_states()
    assert space.nodes["gate"].gate_open() is False

    # With no inputs wired at all the gate emits nothing, so downstream
    # falls back to its own stored default.
    space.remove_edge(space._edge_id("s", "and", "a"))
    space._refresh_boolean_states()
    assert space.nodes["gate"].gate_open() is False  # stored enabled=False


# ---------------------------------------------------------------------------
# warps
# ---------------------------------------------------------------------------


def test_audio_warp_mixes_multiple_publishers():
    g = FakeGraph()
    src_a = g.add_source(10, "appA")
    src_b = g.add_source(11, "appB")
    sink = g.add_sink(20, "sink")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(NamedSource("sa", "appA"))
    space.add_node(NamedSource("sb", "appB"))
    space.add_node(WarpInNode("wi1", "foo"))
    space.add_node(WarpInNode("wi2", "foo"))
    space.add_node(WarpOutNode("wo", "foo"))
    space.add_node(NamedSink("snk", "sink"))
    space.add_edge("sa", "wi1", to_port="in")
    space.add_edge("sb", "wi2", to_port="in")
    space.add_edge("wo", "snk", from_port="out")
    space.sync()

    # Both publishers are linked into the same sink ports -> summed.
    pairs = g.linked_pairs()
    assert (src_a["FL"], sink["FL"]) in pairs
    assert (src_b["FL"], sink["FL"]) in pairs


def test_warp_name_mismatch_yields_no_link():
    g = FakeGraph()
    g.add_source(10, "appA")
    g.add_sink(20, "sink")
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(NamedSource("sa", "appA"))
    space.add_node(WarpInNode("wi", "foo"))
    space.add_node(WarpOutNode("wo", "bar"))  # different name
    space.add_node(NamedSink("snk", "sink"))
    space.add_edge("sa", "wi", to_port="in")
    space.add_edge("wo", "snk", from_port="out")
    space.sync()
    assert g.linked_pairs() == set()


def test_boolean_warp_uses_first_publisher_and_separate_namespace():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(BooleanSourceNode("b1", output=1))
    space.add_node(BooleanSourceNode("b2", output=0))
    space.add_node(BooleanWarpInNode("wi1", "m"))
    space.add_node(BooleanWarpInNode("wi2", "m"))  # duplicate name
    space.add_node(BooleanWarpOutNode("wo", "m"))
    space.add_node(GateNode("gate", enabled=False))
    space.add_edge("b1", "wi1", "in")
    space.add_edge("b2", "wi2", "in")
    space.add_edge("wo", "gate", "ctrl")
    space._refresh_boolean_states()
    # First publisher (b1 = true) wins; the gate opens.
    assert space.nodes["gate"].gate_open() is True

    # A boolean warp must not be reachable from an audio warp of the
    # same name (separate namespaces).
    space.add_node(WarpOutNode("awo", "m"))
    assert space._resolve_warp_audio(space.nodes["awo"], set()) == []


def test_audio_warp_cycle_resolves_safely():
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    # warp "x" is fed by warp "y", and warp "y" by warp "x": a loop.
    space.add_node(WarpInNode("wi_x", "x"))
    space.add_node(WarpOutNode("wo_y", "y"))
    space.add_node(WarpInNode("wi_y", "y"))
    space.add_node(WarpOutNode("wo_x", "x"))
    space.add_edge("wo_y", "wi_x", "in")
    space.add_edge("wo_x", "wi_y", "in")
    # A downstream consumer forces resolution of the loop.
    space.add_node(WarpOutNode("wo_x2", "x"))
    space.add_node(SinkNode("snk"))
    space.add_edge("wo_x2", "snk")
    space.sync()  # must not hang
    assert g.linked_pairs() == set()


def test_handle_node_removed_ignores_reused_node_id():
    """PipeWire reuses node ids: a removal event whose object name is not
    ours must not tear down our backing (it only clears the stale id)."""
    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()

    node = EchoBacked("n")

    class FakeOwned:
        owns_process = True
        is_alive = True
        node_id = 55
        name = "our_node"
        destroyed = False

        def destroy(self):
            self.destroyed = True

    owned = FakeOwned()
    node.backings.append(owned)
    space.add_node(node)

    space.handle_node_removed(55, {"info": {"props": {"node.name": "some_other_node"}}})
    assert owned.destroyed is False
    assert owned in node.backings
    assert owned.node_id is None  # stale id forgotten so it re-resolves

    # A genuine removal (name matches) still restarts the backing.
    owned.node_id = 55
    space.handle_node_removed(55, {"info": {"props": {"node.name": "our_node"}}})
    assert owned.destroyed is True
    assert owned not in node.backings


def _device(params):
    return {"id": 300, "type": "PipeWire:Interface:Device", "info": {"params": params}}


def test_device_profile_name_reads_current_profile():
    dev = _device({"Profile": [{"index": 0, "name": "off"}]})
    assert device_profile_name(dev) == "off"
    assert device_profile_name(None) is None
    assert device_profile_name({}) is None


def test_pick_auto_a2dp_profile_prefers_highest_priority_sink():
    dev = _device({
        "Profile": [{"index": 0, "name": "off"}],
        "EnumProfile": [
            {"index": 0, "name": "off", "available": "yes", "priority": 0},
            {"index": 131073, "name": "a2dp-sink-sbc", "available": "yes", "priority": 132},
            {"index": 131076, "name": "a2dp-sink", "available": "yes", "priority": 133},
            {"index": 9, "name": "a2dp-sink-hq", "available": "no", "priority": 999},
        ],
    })
    # Sink: best available a2dp-sink, ignoring unavailable/off.
    assert pick_auto_a2dp_profile(dev, True) == (131076, "a2dp-sink")
    # No a2dp-source on this device -> no source pick.
    assert pick_auto_a2dp_profile(dev, False) is None


def test_pick_auto_a2dp_profile_source_falls_back_to_headset():
    dev = _device({
        "EnumProfile": [
            {"index": 0, "name": "off", "available": "yes", "priority": 0},
            {"index": 196865, "name": "headset-head-unit", "available": "yes", "priority": 6},
        ]
    })
    assert pick_auto_a2dp_profile(dev, False) == (196865, "headset-head-unit")
    assert pick_auto_a2dp_profile(dev, True) is None
def test_rename_preserves_internal_link_bookkeeping():
    """Renaming a node must re-key its id-derived internal-link entries,
    not drop them.  Dropping them makes the next sync() disconnect the
    whole effect interior and re-make it, which stalls timing-sensitive
    modules like RNNoise (the 'noise cancel kills audio' wedge)."""
    from pwnodes import _DesiredLinks

    g = FakeGraph()
    space = make_space(g)
    space.mark_graph_loaded()
    space.add_node(Node("old"))

    space._edge_links["__internal__:old:0"] = _DesiredLinks(pairs={(1, 2)})
    space._edge_links["__internal__:old:1"] = _DesiredLinks(pairs={(3, 4)})
    space._edge_links["user->edge"] = _DesiredLinks(pairs={(5, 6)})
    space._inflight_links[(1, 2)] = ("__internal__:old:0", 123.0)

    space.rename_node("old", "new")

    assert "__internal__:old:0" not in space._edge_links
    assert "__internal__:old:1" not in space._edge_links
    assert space._edge_links["__internal__:new:0"].pairs == {(1, 2)}
    assert space._edge_links["__internal__:new:1"].pairs == {(3, 4)}
    # Non-internal entries are untouched.
    assert space._edge_links["user->edge"].pairs == {(5, 6)}
    # In-flight bookkeeping follows the re-key too.
    assert space._inflight_links[(1, 2)][0] == "__internal__:new:0"
