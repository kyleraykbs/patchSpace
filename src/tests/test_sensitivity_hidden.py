"""The Sensitivity gate's gain-staging Volume nodes are daemon-owned
and hidden: the user never creates or wires them, get_nodes/export never
show them, and the user's edges are transparently routed through them.

These tests drive the daemon command layer directly (like
test_daemon_protocol), with the pw-cli process classes faked out so no
real PipeWire server is touched."""

import pytest

import pwnodes
from main import PatchSpaceDaemon


class FakeCli:
    instances = []

    def __init__(self, name, command=("pw-cli",), settle=0.0, **kw):
        self.name = name
        self.node_id = None
        self.alive = True
        self.owns_process = True
        self.set_params = []
        FakeCli.instances.append(self)

    def create(self, line):
        self.alive = True
        return True

    @property
    def is_alive(self):
        return self.alive

    def stuck(self, grace_s):
        return False

    def resolve(self, node_id):
        self.node_id = node_id

    def set_param(self, iface, body):
        self.set_params.append((iface, body))

    def destroy(self):
        self.alive = False


class FakeProc(FakeCli):
    def create(self, command, quiet=False):
        self.alive = True
        return True


@pytest.fixture(autouse=True)
def _fake_processes(monkeypatch):
    FakeCli.instances.clear()
    monkeypatch.setattr(pwnodes, "OwnedPwNode", FakeCli)
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)


def fresh_daemon():
    return PatchSpaceDaemon()


def _add(d, node_type, node_id, config=None):
    resp = d.handle_command(
        {
            "command": "add_node",
            "node_type": node_type,
            "node_id": node_id,
            "config": config or {},
        }
    )
    assert resp["status"] == "ok", resp
    return resp


def test_sensitivity_gets_hidden_bracketing_volume_nodes():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")

    pre, post = "__sens_pre__sens", "__sens_post__sens"
    assert pre in d.space.nodes
    assert post in d.space.nodes
    # They are not public, so the GUI never sees them...
    assert pre not in d.space.public_nodes
    assert post not in d.space.public_nodes
    assert set(d.handle_command({"command": "get_nodes"})["nodes"]) == {"sens"}
    # ...but the internal links that put them in the signal path exist.
    assert f"{pre}->sens" in d.space.edges
    assert f"sens->{post}" in d.space.edges
    # The gate itself moves its own live threshold now, so the hidden
    # pre/post are pinned unity pass-throughs (kept only so the routing
    # and saved sessions don't change).
    assert d.space.nodes[pre].volume_min == 1.0
    assert d.space.nodes[pre].volume_max == 1.0
    assert d.space.nodes[post].volume_min == 1.0
    assert d.space.nodes[post].volume_max == 1.0


def test_user_edges_route_through_hidden_nodes_and_stay_logical():
    d = fresh_daemon()
    _add(d, "regex_input", "src", {"pattern": ".*"})
    _add(d, "sensitivity_gate", "sens")
    _add(d, "description_output", "out", {"description": "speakers"})
    assert (
        d.handle_command(
            {"command": "add_edge", "from_node": "src", "to_node": "sens"}
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command(
            {"command": "add_edge", "from_node": "sens", "to_node": "out"}
        )["status"]
        == "ok"
    )

    # Physically, the audio goes src -> pre -> sens -> post -> out ...
    assert "src->__sens_pre__sens" in d.space.edges
    assert "__sens_post__sens->out" in d.space.edges
    # ... but the GUI sees only the logical edges.
    edges = d.handle_command({"command": "get_nodes"})["edges"]
    assert set(edges) == {"src->sens", "sens->out"}
    assert edges["src->sens"]["from_node"] == "src"
    assert edges["sens->out"]["to_node"] == "out"


def test_removing_logical_edge_removes_routed_edge():
    d = fresh_daemon()
    _add(d, "regex_input", "src", {"pattern": ".*"})
    _add(d, "sensitivity_gate", "sens")
    d.handle_command({"command": "add_edge", "from_node": "src", "to_node": "sens"})

    resp = d.handle_command({"command": "remove_edge", "edge_id": "src->sens"})
    assert resp["status"] == "ok", resp
    assert "src->__sens_pre__sens" not in d.space.edges
    assert d.handle_command({"command": "get_nodes"})["edges"] == {}


def test_sensitivity_property_moves_gate_threshold_live():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    node = d.space.nodes["sens"]
    pre = d.space.nodes["__sens_pre__sens"]
    post = d.space.nodes["__sens_post__sens"]

    """Sliding sensitivity moves the gate's load-time threshold and
    schedules a debounced interior reload (the live set-param path is not
    reliable through the daemon's pw-cli session)."""
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "sens",
                "property": "sensitivity",
                "value": 1.0,
            }
        )["status"]
        == "ok"
    )
    assert node.sensitivity == 1.0
    assert node.level == 0.0
    assert node._reload_due is not None
    assert '"threshold" = 0.005623' in node._module_command_args()

    # Slider 0.0 = least sensitive -> level 100.
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "sens",
                "property": "sensitivity",
                "value": 0.0,
            }
        )["status"]
        == "ok"
    )
    assert node.level == 100.0

    # The hidden nodes stay unity pass-throughs regardless of the slider.
    for n in (pre, post):
        assert n.volume_min == 1.0 and n.volume_max == 1.0

    # Out-of-range values clamp rather than error.
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "sens",
                "property": "sensitivity",
                "value": 5.0,
            }
        )["status"]
        == "ok"
    )
    assert d.space.nodes["sens"].sensitivity == 1.0


def test_removing_sensitivity_node_removes_hidden_children():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    assert (
        d.handle_command({"command": "remove_node", "node_id": "sens"})["status"]
        == "ok"
    )
    assert "__sens_pre__sens" not in d.space.nodes
    assert "__sens_post__sens" not in d.space.nodes


def test_renaming_sensitivity_gate_carries_hidden_children_and_edges():
    d = fresh_daemon()
    _add(d, "regex_input", "src", {"pattern": ".*"})
    _add(d, "sensitivity_gate", "sens")
    d.handle_command({"command": "add_edge", "from_node": "src", "to_node": "sens"})

    resp = d.handle_command(
        {
            "command": "rename_node",
            "old_node_id": "sens",
            "new_node_id": "sens2",
        }
    )
    assert resp["status"] == "ok", resp
    assert "__sens_pre__sens2" in d.space.nodes
    assert "__sens_post__sens2" in d.space.nodes
    assert "__sens_pre__sens" not in d.space.nodes
    edges = d.handle_command({"command": "get_nodes"})["edges"]
    assert set(edges) == {"src->sens2"}


def test_reset_clears_hidden_sensitivity_nodes():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    d.handle_command({"command": "reset"})
    assert d.handle_command({"command": "get_nodes"})["nodes"] == {}
    assert not any(nid.startswith("__sens_") for nid in d.space.nodes)


def test_export_hides_hidden_nodes_and_round_trips_sensitivity():
    d1 = fresh_daemon()
    _add(d1, "regex_input", "src", {"pattern": ".*"})
    _add(d1, "sensitivity_gate", "sens", {"sensitivity": 0.5})
    d1.handle_command({"command": "add_edge", "from_node": "src", "to_node": "sens"})
    d1.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sens",
            "property": "sensitivity",
            "value": 0.5,
        }
    )
    export = d1.handle_command({"command": "export_config"})["config"]
    assert set(export["nodes"]) == {"src", "sens"}
    assert export["nodes"]["sens"]["params"]["sensitivity"] == 0.5
    assert export["edges"] == [{"from": "src", "to": "sens"}]

    d2 = fresh_daemon()
    for node_id, node_cfg in export["nodes"].items():
        _add(d2, node_cfg["type"], node_id, node_cfg["params"])
    for edge in export["edges"]:
        d2.handle_command(
            {
                "command": "add_edge",
                "from_node": edge["from"],
                "to_node": edge["to"],
                "to_port": edge.get("to_port", "in"),
            }
        )
    nodes = d2.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["sens"]["sensitivity"] == 0.5
    # The hidden nodes are unity pass-throughs now, so nothing about the
    # slider's value lives on them.
    assert d2.space.nodes["__sens_pre__sens"].volume_max == 1.0


def test_load_stages_hidden_nodes_so_a_mid_load_tick_cannot_prune_them(monkeypatch):
    """Regression: a big session load holds the daemon lock for as long as
    it takes to build every node's structural pieces.  While the lock is
    held the graph's node-created callbacks cannot resolve any backing, so
    a hidden sensitivity pre/post created early in the load looks 'stuck'
    (alive but unresolved past RESOLVE_GRACE_S) the instant a supervision
    tick runs, and gets torn down and rebuilt - the pre/post thrash that
    killed the gate's audio.  The load must stage every backed node,
    including those hidden ones, until its own bring-up turn."""

    # Every fake backing looks stuck: alive but never resolving, exactly
    # as the hidden pre/post look while the load holds the lock.
    class StuckCli(FakeCli):
        def stuck(self, grace_s):
            return True

    monkeypatch.setattr(pwnodes, "OwnedPwNode", StuckCli)
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)

    d = fresh_daemon()

    # Record which nodes a supervision tick (as the periodic ticker would
    # run mid-load) actually supervises.
    capturing = {"on": False}
    supervised = []
    real_supervise_node = d.space._supervise_node

    def spy_supervise_node(node):
        if capturing["on"]:
            supervised.append(node.id)
        return real_supervise_node(node)

    monkeypatch.setattr(d.space, "_supervise_node", spy_supervise_node)

    ticked = {"done": False}

    def spy_bring_up(node):
        if not ticked["done"]:
            ticked["done"] = True
            capturing["on"] = True
            try:
                with d._lock:
                    d.space.supervise()
            finally:
                capturing["on"] = False
        return True

    monkeypatch.setattr(d, "_bring_node_up", spy_bring_up)

    d._load_session(
        {
            "nodes": {"sens": {"type": "sensitivity_gate", "params": {}}},
            "edges": [],
        }
    )

    assert ticked["done"], "the load never reached its bring-up loop"
    assert "__sens_pre__sens" not in supervised
    assert "__sens_post__sens" not in supervised
    # And they survived the load.
    assert "__sens_pre__sens" in d.space.nodes
    assert "__sens_post__sens" in d.space.nodes


def test_fresh_sensitivity_level_and_sensitivity_agree():
    """Regression: a fresh Sensitivity node used to default to level=25
    with sensitivity=0.0 (which implies level 100), so the Settings dialog
    showed "25" while the node's inline 0..1 slider sat empty.  The two
    are now derived from whichever one the config supplies, so they can't
    disagree."""
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    node = d.space.nodes["sens"]
    assert node.level == 25.0
    assert node.sensitivity == pytest.approx(0.75)
    assert node.sensitivity_to_level(node.sensitivity) == pytest.approx(node.level)

    # A config carrying only the legacy `level` still round-trips.
    d2 = fresh_daemon()
    _add(d2, "sensitivity_gate", "sens", {"level": 40.0})
    n2 = d2.space.nodes["sens"]
    assert n2.level == 40.0
    assert n2.sensitivity == pytest.approx(0.6)


def test_sensitivity_tuning_controls_are_settable_and_clamped():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    node = d.space.nodes["sens"]
    assert (
        node.ratio,
        node.attack_ms,
        node.release_ms,
        node.knee_db,
        node.makeup,
        node.range_db,
    ) == (4.0, 5.0, 2000.0, 6.0, 1.0, -96.0)

    for prop, value in (
        ("ratio", 8.0),
        ("attack_ms", 12.0),
        ("release_ms", 350.0),
        ("knee_db", 3.0),
        ("makeup", 2.5),
        ("range_db", -60.0),
    ):
        resp = d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "sens",
                "property": prop,
                "value": value,
            }
        )
        assert resp["status"] == "ok", resp
        assert getattr(node, prop) == value
        assert node._reload_due is not None

    # Out-of-range values clamp; non-numbers are rejected.
    d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sens",
            "property": "ratio",
            "value": 999.0,
        }
    )
    assert node.ratio == node.RATIO_MAX
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "sens",
                "property": "makeup",
                "value": "loud",
            }
        )["status"]
        == "error"
    )

    # The values are baked into the module graph and survive export.
    assert f'"ratio" = {node.RATIO_MAX:.2f}' in node._module_command_args()
    export = d.handle_command({"command": "export_config"})["config"]
    assert export["nodes"]["sens"]["params"]["makeup"] == 2.5
    assert export["nodes"]["sens"]["params"]["range_db"] == -60.0


def test_sensitivity_gate_closes_to_silence_by_default():
    """Regression: Calf Gate's `range` (max gain reduction) defaults to
    -24 dB, so an inactive sensitivity node still bled audible background
    noise.  The node now pins it to Calf's minimum (~-96 dB) so a closed
    gate is effectively silent."""
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    node = d.space.nodes["sens"]
    assert node.range_db == -96.0
    # 10**(-96/20) == 1.5849e-05, exactly Calf's `range` minimum.
    assert '"range" = 0.00001585' in node._module_command_args()
    assert 'plugin = "http://calf.sourceforge.net/plugins/Gate"' in (
        node._module_command_args()
    )
    # A config can override it (e.g. a gentler -24 dB duck).
    d2 = fresh_daemon()
    _add(d2, "sensitivity_gate", "sens", {"range_db": -24.0})
    assert d2.space.nodes["sens"].range_db == -24.0


def test_creating_panel_moves_hidden_children_live(tmp_path):
    """A live panel move must rename a Sensitivity gate's hidden pre/post
    companions with it, or the gate's signal path breaks."""
    pdir = tmp_path / "panels"
    pdir.mkdir()
    d = PatchSpaceDaemon(
        panel_dirs=[(str(pdir), True)],
        root_panel_path=str(tmp_path / "root.json"),
    )
    _add(d, "sensitivity_gate", "sens")

    # Seed an interior-link entry under the old id; the move must re-key
    # it with the node rather than dropping it (dropping forces the next
    # sync to tear the interior down and re-make it, which can stall the
    # module).
    from pwnodes import _DesiredLinks

    d.space._edge_links["__internal__:sens:0"] = _DesiredLinks(pairs=set())

    export = d.handle_command(
        {"command": "create_panel", "name": "kit", "node_ids": ["sens"]}
    )
    assert export["status"] == "ok", export
    assert "__internal__:sens:0" not in d.space._edge_links
    assert "__internal__:kit::sens:0" in d.space._edge_links
    assert "kit::sens" in d.space.nodes
    assert "sens" not in d.space.nodes
    assert "__sens_pre__kit::sens" in d.space.nodes
    assert "__sens_post__kit::sens" in d.space.nodes
    assert "__sens_pre__sens" not in d.space.nodes
    assert "__sens_post__sens" not in d.space.nodes
    assert "__sens_pre__kit::sens->kit::sens" in d.space.edges
    assert "kit::sens->__sens_post__kit::sens" in d.space.edges


def test_create_panel_runs_standard_careful_setup_for_moved_nodes(tmp_path):
    """A panel move must go through the same per-node setup + careful
    bring-up as a normal add/load (not just supervise()), so a moved
    finicky effect can't be left half-configured or half-wired."""
    pdir = tmp_path / "panels"
    pdir.mkdir()
    d = PatchSpaceDaemon(
        panel_dirs=[(str(pdir), True)],
        root_panel_path=str(tmp_path / "root.json"),
    )
    _add(d, "sensitivity_gate", "sens")

    careful = []
    d._careful_bring_up = lambda n: careful.append(n.id)

    export = d.handle_command(
        {"command": "create_panel", "name": "kit", "node_ids": ["sens"]}
    )
    assert export["status"] == "ok", export
    assert "kit::sens" in d.space.nodes
    # The standard careful bring-up ran for the renamed node.
    assert "kit::sens" in careful
