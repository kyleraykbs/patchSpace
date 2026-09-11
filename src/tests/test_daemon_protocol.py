"""Daemon-level protocol tests using leaf (non-backed) node types only,
so nothing touches a real PipeWire process.  Exercises the same command
layer and serialization shapes the GUI depends on."""

import socket
import threading

from main import PatchBayDaemon
from pwnodes import BackedNode, Node
import main as main_mod


def fresh_daemon():
    # Constructing a daemon only builds the graph monitor object; no
    # pw-dump / pw-cli subprocess is started until start().
    return PatchBayDaemon()


def test_add_node_and_serialize_leaf_types():
    d = fresh_daemon()
    for cmd in [
        {
            "command": "add_node",
            "node_type": "regex_input",
            "node_id": "in1",
            "config": {"pattern": ".*", "label": "Everything"},
        },
        {
            "command": "add_node",
            "node_type": "gate",
            "node_id": "g1",
            "config": {"enabled": True},
        },
        {
            "command": "add_node",
            "node_type": "exclude_filter",
            "node_id": "x1",
            "config": {"pattern": "Discord"},
        },
        {
            "command": "add_node",
            "node_type": "description_output",
            "node_id": "out1",
            "config": {"description": "speakers"},
        },
        {"command": "add_edge", "from_node": "in1", "to_node": "g1"},
        {"command": "add_edge", "from_node": "g1", "to_node": "out1"},
    ]:
        resp = d.handle_command(cmd)
        assert resp["status"] == "ok", resp

    resp = d.handle_command({"command": "get_nodes"})
    assert resp["status"] == "ok"
    nodes = resp["nodes"]
    assert nodes["in1"]["type"] == "regex_input"
    assert nodes["in1"]["pattern"] == ".*"
    assert nodes["in1"]["label"] == "Everything"
    assert nodes["g1"]["enabled"] is True
    assert nodes["x1"]["pattern"] == "Discord"
    assert nodes["out1"]["description"] == "speakers"
    assert "out1->" not in nodes

    edges = resp["edges"]
    assert "in1->g1" in edges
    assert edges["in1->g1"]["from_node"] == "in1"


def test_export_import_round_trip():
    d1 = fresh_daemon()
    for cmd in [
        {
            "command": "add_node",
            "node_type": "regex_input",
            "node_id": "in1",
            "config": {"pattern": "Firefox", "label": "Web"},
        },
        {
            "command": "add_node",
            "node_type": "gate",
            "node_id": "g1",
            "config": {"enabled": False},
        },
        {
            "command": "add_node",
            "node_type": "media_class_output",
            "node_id": "out1",
            "config": {"media_class": "Audio/Sink"},
        },
        {"command": "add_edge", "from_node": "in1", "to_node": "g1"},
        {"command": "add_edge", "from_node": "g1", "to_node": "out1"},
    ]:
        assert d1.handle_command(cmd)["status"] == "ok"

    export = d1.handle_command({"command": "export_config"})
    assert export["status"] == "ok"
    config = export["config"]
    assert set(config["nodes"]) == {"in1", "g1", "out1"}
    assert config["nodes"]["in1"]["type"] == "regex_input"
    assert len(config["edges"]) == 2

    # Replay into a fresh daemon - import must reproduce the graph.
    d2 = fresh_daemon()
    for node_id, node_cfg in config["nodes"].items():
        resp = d2.handle_command(
            {
                "command": "add_node",
                "node_type": node_cfg["type"],
                "node_id": node_id,
                "config": node_cfg["params"],
            }
        )
        assert resp["status"] == "ok"
    for edge in config["edges"]:
        resp = d2.handle_command(
            {
                "command": "add_edge",
                "from_node": edge["from"],
                "to_node": edge["to"],
                "to_port": edge.get("to_port", "in"),
            }
        )
        assert resp["status"] == "ok"

    state2 = d2.handle_command({"command": "get_nodes"})
    n2 = state2["nodes"]
    assert n2["in1"]["pattern"] == "Firefox"
    assert n2["in1"]["label"] == "Web"
    assert n2["g1"]["enabled"] is False
    assert n2["out1"]["media_class"] == "Audio/Sink"
    assert set(state2["edges"]) == set(state2["edges"])  # stable


def test_switcher_toggles_output_and_ports_round_trip():
    d = fresh_daemon()
    for cmd in [
        {"command": "add_node", "node_type": "switcher", "node_id": "sw", "config": {}},
        {
            "command": "add_node",
            "node_type": "description_output",
            "node_id": "a",
            "config": {"description": "A"},
        },
        {
            "command": "add_node",
            "node_type": "description_output",
            "node_id": "b",
            "config": {"description": "B"},
        },
        {"command": "add_edge", "from_node": "sw", "to_node": "a", "from_port": "a"},
        {"command": "add_edge", "from_node": "sw", "to_node": "b", "from_port": "b"},
    ]:
        assert d.handle_command(cmd)["status"] == "ok", cmd

    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["sw"]["type"] == "switcher"
    assert nodes["sw"]["output"] == 0

    edges = d.handle_command({"command": "get_nodes"})["edges"]
    assert edges["sw->a@a"]["from_port"] == "a"
    assert edges["sw->b@b"]["from_port"] == "b"

    resp = d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sw",
            "property": "output",
            "value": 1,
        }
    )
    assert resp["status"] == "ok"
    assert d.handle_command({"command": "get_nodes"})["nodes"]["sw"]["output"] == 1

    # Export keeps the source port so a replay rewires the same branch.
    export = d.handle_command({"command": "export_config"})["config"]
    ports = {e["from_port"] for e in export["edges"]}
    assert ports == {"a", "b"}


def test_inverse_switcher_registered_and_toggles():
    d = fresh_daemon()
    resp = d.handle_command(
        {
            "command": "add_node",
            "node_type": "inverse_switcher",
            "node_id": "inv",
            "config": {},
        }
    )
    assert resp["status"] == "ok"
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["inv"]["type"] == "inverse_switcher"
    assert nodes["inv"]["output"] == 0

    resp = d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "inv",
            "property": "output",
            "value": 1,
        }
    )
    assert resp["status"] == "ok"
    assert d.handle_command({"command": "get_nodes"})["nodes"]["inv"]["output"] == 1


def test_node_layout_persists_through_export_and_reimport():
    d1 = fresh_daemon()
    d1.handle_command(
        {
            "command": "add_node",
            "node_type": "gate",
            "node_id": "g1",
            "config": {"label": "G"},
        }
    )
    resp = d1.handle_command(
        {
            "command": "set_node_layout",
            "layout": {
                "g1": {"x": 123.5, "y": -40.0, "anchored": True},
            },
        }
    )
    assert resp["status"] == "ok"

    nodes = d1.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["g1"]["x"] == 123.5
    assert nodes["g1"]["y"] == -40.0
    assert nodes["g1"]["anchored"] is True

    export = d1.handle_command({"command": "export_config"})["config"]
    params = export["nodes"]["g1"]["params"]
    assert params["x"] == 123.5
    assert params["y"] == -40.0
    assert params["anchored"] is True

    # Replay the export into a fresh daemon, as apply_config.py does.
    d2 = fresh_daemon()
    for node_id, cfg in export["nodes"].items():
        resp = d2.handle_command(
            {
                "command": "add_node",
                "node_type": cfg["type"],
                "node_id": node_id,
                "config": cfg["params"],
            }
        )
        assert resp["status"] == "ok"
    restored = d2.handle_command({"command": "get_nodes"})["nodes"]["g1"]
    assert restored["x"] == 123.5
    assert restored["y"] == -40.0
    assert restored["anchored"] is True


def test_groups_add_update_export_and_prune():
    d = fresh_daemon()
    for nid in ("a", "b"):
        d.handle_command(
            {"command": "add_node", "node_type": "gate", "node_id": nid, "config": {}}
        )
    resp = d.handle_command(
        {
            "command": "add_group",
            "group_id": "g1",
            "label": "Drums",
            "color": "#33d17a",
            "nodes": ["a", "b"],
        }
    )
    assert resp["status"] == "ok"

    groups = d.handle_command({"command": "get_nodes"})["groups"]
    assert groups == [
        {"id": "g1", "label": "Drums", "color": "#33d17a", "nodes": ["a", "b"]}
    ]

    # Rename, recolor, and drop a member.
    assert (
        d.handle_command(
            {
                "command": "set_group",
                "group_id": "g1",
                "new_group_id": "g2",
                "label": "Perc",
                "color": "#e01b24",
                "nodes": ["a"],
            }
        )["status"]
        == "ok"
    )
    groups = d.handle_command({"command": "get_nodes"})["groups"]
    assert groups[0]["id"] == "g2"
    assert groups[0]["label"] == "Perc"
    assert groups[0]["nodes"] == ["a"]

    # Export carries groups, and removing a node prunes it from them.
    d.handle_command({"command": "add_group", "group_id": "g3", "nodes": ["b"]})
    export = d.handle_command({"command": "export_config"})["config"]
    assert {g["id"] for g in export["groups"]} == {"g2", "g3"}
    d.handle_command({"command": "remove_node", "node_id": "b"})
    groups = {g["id"]: g for g in d.handle_command({"command": "get_nodes"})["groups"]}
    assert groups["g3"]["nodes"] == []


def test_no_two_groups_with_identical_members():
    """Groups may overlap freely, but two groups can never hold exactly
    the same node set - enforced daemon-side as the safety net behind the
    GUI's own guard."""
    d = fresh_daemon()
    for nid in ("a", "b", "c"):
        d.handle_command(
            {"command": "add_node", "node_type": "gate", "node_id": nid, "config": {}}
        )

    assert (
        d.handle_command(
            {"command": "add_group", "group_id": "g1", "nodes": ["a", "b"]}
        )["status"]
        == "ok"
    )
    # Same members, different order -> still an exact duplicate.
    assert (
        d.handle_command(
            {"command": "add_group", "group_id": "g2", "nodes": ["b", "a"]}
        )["status"]
        == "error"
    )
    # Overlapping groups are fine.
    assert (
        d.handle_command(
            {"command": "add_group", "group_id": "g2", "nodes": ["a", "b", "c"]}
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command({"command": "add_group", "group_id": "g3", "nodes": ["a"]})[
            "status"
        ]
        == "ok"
    )

    # Editing a group so it matches another is rejected...
    assert (
        d.handle_command(
            {"command": "set_group", "group_id": "g3", "nodes": ["a", "b"]}
        )["status"]
        == "error"
    )
    # ...an overlapping edit is fine...
    assert (
        d.handle_command({"command": "set_group", "group_id": "g3", "nodes": ["c"]})[
            "status"
        ]
        == "ok"
    )
    # ...and it can't be made identical to another later either.
    assert (
        d.handle_command(
            {"command": "set_group", "group_id": "g1", "nodes": ["a", "b", "c"]}
        )["status"]
        == "error"
    )

    groups = {
        g["id"]: set(g["nodes"])
        for g in d.handle_command({"command": "get_nodes"})["groups"]
    }
    assert groups == {"g1": {"a", "b"}, "g2": {"a", "b", "c"}, "g3": {"c"}}


def test_set_property_and_rename():
    d = fresh_daemon()
    d.handle_command(
        {
            "command": "add_node",
            "node_type": "regex_input",
            "node_id": "n1",
            "config": {"pattern": ".*"},
        }
    )
    resp = d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "n1",
            "property": "pattern",
            "value": "OBS",
        }
    )
    assert resp["status"] == "ok"
    assert d.handle_command({"command": "get_nodes"})["nodes"]["n1"]["pattern"] == "OBS"

    resp = d.handle_command(
        {"command": "rename_node", "old_node_id": "n1", "new_node_id": "n2"}
    )
    assert resp["status"] == "ok"
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert "n2" in nodes and "n1" not in nodes
    assert nodes["n2"]["pattern"] == "OBS"


def test_remove_node_and_reset():
    d = fresh_daemon()
    d.handle_command(
        {"command": "add_node", "node_type": "gate", "node_id": "g1", "config": {}}
    )
    d.handle_command(
        {"command": "add_node", "node_type": "gate", "node_id": "g2", "config": {}}
    )
    assert (
        d.handle_command({"command": "remove_node", "node_id": "g1"})["status"] == "ok"
    )
    assert "g1" not in d.handle_command({"command": "get_nodes"})["nodes"]

    d.handle_command({"command": "reset"})
    assert d.handle_command({"command": "get_nodes"})["nodes"] == {}
    assert d.handle_command({"command": "get_nodes"})["edges"] == {}


def test_rebuild_captures_then_tears_down_and_reloads(monkeypatch):
    """The rebuild command is "turn it off and on again": it snapshots the
    current graph, tears the public nodes down, and stages the snapshot
    back in on a background thread (same one-in-flight-protocol reason as
    load_session).  Nothing may be lost in the round trip."""
    d = fresh_daemon()
    for cmd in [
        {
            "command": "add_node",
            "node_type": "regex_input",
            "node_id": "in1",
            "config": {"pattern": ".*", "label": "In"},
        },
        {
            "command": "add_node",
            "node_type": "gate",
            "node_id": "g1",
            "config": {"enabled": False},
        },
        {"command": "add_edge", "from_node": "in1", "to_node": "g1"},
    ]:
        assert d.handle_command(cmd)["status"] == "ok", cmd

    reloaded = {}
    done = threading.Event()

    def fake_load(config):
        reloaded["config"] = config
        done.set()

    monkeypatch.setattr(d, "_load_session", fake_load)

    resp = d.handle_command({"command": "rebuild"})
    assert resp == {"status": "ok", "started": True}
    assert done.wait(2.0), "rebuild never reached the reload step"

    cfg = reloaded["config"]
    assert set(cfg["nodes"]) == {"in1", "g1"}
    assert cfg["nodes"]["in1"]["params"]["pattern"] == ".*"
    assert cfg["nodes"]["g1"]["params"]["enabled"] is False
    assert cfg["edges"] == [{"from": "in1", "to": "g1"}]

    # The daemon tore the graph down before staging the reload, so the
    # (stubbed) load starts from a clean space.
    assert d.handle_command({"command": "get_nodes"})["nodes"] == {}


def test_shutdown_command_reports_ok_and_stops_the_daemon():
    """The GUI's Stop/Restart menu and its on-close cleanup send this;
    it must reply before flipping the run flag (the reply is what lets
    the caller wait for a graceful exit rather than racing it)."""
    d = fresh_daemon()
    d._running = True
    resp = d.handle_command({"command": "shutdown"})
    assert resp["status"] == "ok"
    assert d._running is False


def test_every_node_type_has_a_description_and_setting_tooltips():
    """Every node spec carries hover-ready text: a one-line description
    (shown on the add-node panel and a long hover on the canvas) and a
    tooltip for every Settings row (falling back to the label)."""
    from gui import node_specs as ns

    missing_desc = [t for t, spec in ns.NODE_TYPE_SPECS.items() if not spec.description]
    assert missing_desc == []
    for node_type, spec in ns.NODE_TYPE_SPECS.items():
        for row in spec.settings or []:
            attr, label = row[0], row[1]
            assert ns.setting_tooltip(node_type, attr, label), (node_type, attr)


def test_unknown_type_and_bad_command_error():
    d = fresh_daemon()
    resp = d.handle_command(
        {"command": "add_node", "node_type": "nope", "node_id": "x", "config": {}}
    )
    assert resp["status"] == "error"
    assert d.handle_command({"command": "frobnicate"})["status"] == "error"


class _StubBacking:
    """A process-owning backing whose liveness we can flip by hand."""

    def __init__(self, alive=True, resolved=True):
        self.node_id = 1 if resolved else None
        self.owns_process = True
        self.is_alive = alive
        self.name = "fx_module"

    def destroy(self):
        pass

    def stuck(self, grace_s):
        return False


class _FakeEffect(BackedNode):
    """A backed node with a live module, used to exercise _node_health
    without spawning a real PipeWire process."""

    def __init__(self, node_id="fx"):
        super().__init__(node_id, "fx")
        self.backings = [_StubBacking()]

    def structural_ok(self):
        return True

    def has_module(self):
        return True

    def module_backing(self):
        return self.backings[0] if self.backings else None

    def module_ok(self):
        b = self.module_backing()
        return bool(b is not None and b.is_alive)

    def internal_links(self):
        return [({"nodeName": "src"}, {"name": "dsp"})]

    def ensure_structural(self):
        pass

    def ensure_module(self):
        pass


def test_node_health_ok_starting_and_dead():
    d = fresh_daemon()
    node = _FakeEffect("fx")
    d.space.nodes["fx"] = node
    d.space.public_nodes.add("fx")
    d.space.node_internals_wired = lambda nid: True
    assert d._node_health(node) == "ok"

    # Interior never connected: structurally ready but acoustically dead.
    d.space.node_internals_wired = lambda nid: False
    assert d._node_health(node) == "dead"

    # Module process gone -> dead regardless of the interior check.
    d.space.node_internals_wired = lambda nid: True
    node.backings[0].is_alive = False
    assert d._node_health(node) == "dead"

    # No module at all yet -> not ready, but not dead either.
    node.backings = []
    assert d._node_health(node) == "starting"

    # The serialized shape carries it through to the GUI.
    node.backings = [_StubBacking()]
    assert d.handle_command({"command": "get_nodes"})["nodes"]["fx"]["health"] == "ok"


class _LeafStub(Node):
    def __init__(self, node_id, backing_node_name=None, **_kw):
        super().__init__(node_id)


class _RegularStub(BackedNode):
    def __init__(self, node_id, backing_node_name="reg", **_kw):
        super().__init__(node_id, backing_node_name)

    def structural_ok(self):
        return True

    def ensure_structural(self):
        pass


class _FinickyStub(BackedNode):
    def __init__(self, node_id, backing_node_name="fx", **_kw):
        super().__init__(node_id, backing_node_name)

    def structural_ok(self):
        return True

    def ensure_structural(self):
        pass


def test_load_session_wires_finicky_node_inputs_one_at_a_time(monkeypatch):
    monkeypatch.setitem(main_mod.NODE_TYPE_REGISTRY, "leaf_stub", _LeafStub)
    monkeypatch.setitem(main_mod.NODE_TYPE_REGISTRY, "regular_stub", _RegularStub)
    monkeypatch.setitem(main_mod.NODE_TYPE_REGISTRY, "finicky_stub", _FinickyStub)
    monkeypatch.setattr(main_mod, "_CAREFUL_NODE_TYPES", (_FinickyStub,))

    d = fresh_daemon()
    # The careful path only runs against a live graph; mark it loaded so
    # these assertions still exercise the staged bring-up.
    d.space.mark_graph_loaded()
    monkeypatch.setattr(
        d,
        "_create_node",
        lambda node_type, node_id, config: main_mod.NODE_TYPE_REGISTRY[node_type](
            node_id, config.get("backing_node_name") or f"patchbay_{node_id}"
        ),
    )
    bringups = []
    monkeypatch.setattr(
        d, "_bring_node_up", lambda node: (bringups.append(node.id), True)[1]
    )
    internals = []
    monkeypatch.setattr(
        d,
        "_wait_node_internals_wired",
        lambda nid: (internals.append(nid), True)[1],
    )
    wired = []
    relinked = []
    real_store = d._store_session_edge

    def fake_wire(edge):
        wired.append(f"{edge['from']}->{edge['to']}")
        return real_store(edge)

    monkeypatch.setattr(d, "_wire_edge_carefully", fake_wire)

    def fake_relink(edge):
        relinked.append(f"{edge['from']}->{edge['to']}")
        return ("", None, False, None)

    monkeypatch.setattr(d, "_relink_edge_carefully", fake_relink)

    config = {
        "nodes": {
            "src": {"type": "leaf_stub", "params": {}},
            "fx": {"type": "finicky_stub", "params": {}},
            "dst": {"type": "leaf_stub", "params": {}},
            "src2": {"type": "leaf_stub", "params": {}},
            "reg": {"type": "regular_stub", "params": {}},
        },
        "edges": [
            # Output edge listed first on purpose - the careful pass must
            # still wire the finicky node's INPUT before its output.
            {"from": "fx", "to": "dst"},
            {"from": "src", "to": "fx"},
            {"from": "src2", "to": "reg"},
        ],
    }
    result = d._load_session(config)

    assert result["status"] == "ok"
    # The regular backed node came up in the first pass; the finicky one
    # was held back for the dedicated careful pass.
    assert bringups == ["reg", "fx"]
    assert internals == ["fx"]
    assert wired == ["src->fx", "fx->dst"]
    # And the downstream chain (fx->dst) is re-linked once the finicky
    # node's interior is confirmed live.
    assert relinked == ["fx->dst"]
    assert "src2->reg" in result["edges_created"]
    assert set(d.space.edges) == {"src->fx", "fx->dst", "src2->reg"}


def test_downstream_edges_walks_signal_order_through_switches():
    d = fresh_daemon()
    edges = [
        {"from": "fx", "to": "sw"},  # index 0 - directly downstream
        {"from": "sw", "to": "a"},  # index 1
        {"from": "sw", "to": "bypass"},  # index 2
        {"from": "a", "to": "device"},  # index 3 - furthest downstream
        {"from": "upstream", "to": "fx"},  # index 4 - upstream, excluded
        {"from": "elsewhere", "to": "x"},  # index 5 - unrelated, excluded
    ]
    result = d._downstream_edges(["fx"], edges)
    # Everything reachable from fx via from->to, closest first, then by
    # config order; upstream/unrelated edges are not included.
    assert result == [edges[0], edges[1], edges[2], edges[3]]


def test_light_noise_cancel_shares_echo_backing_but_hides_probe():
    from pwnodes import EchoCancelNode, LightNoiseCancelNode
    from gui import node_specs

    assert main_mod.NODE_TYPE_REGISTRY["light_noise_cancel"] is LightNoiseCancelNode
    # Same backing/machinery as echo cancel, so the careful-node
    # treatment (isinstance against _CAREFUL_NODE_TYPES) picks it up too.
    assert issubclass(LightNoiseCancelNode, EchoCancelNode)
    assert isinstance(LightNoiseCancelNode("x", "x"), EchoCancelNode)

    spec = node_specs.spec_for("light_noise_cancel")
    assert spec.label == "Light Noise Cancel"
    assert spec.inputs == ["mic"]  # probe deliberately not exposed

    # The existing denoiser is now presented as AI Noise Cancel.
    assert node_specs.spec_for("noise_cancel").label == "AI Noise Cancel"
    assert (
        node_specs.normalize_node_type("LightNoiseCancelNode") == "light_noise_cancel"
    )
    assert (
        "Light Noise Cancel",
        "light_noise_cancel",
    ) in node_specs.ADD_NODE_MENU_ITEMS


def test_add_node_stages_and_waits_on_finicky_nodes(monkeypatch):
    """A finicky node created at runtime gets the same staged, careful
    bring-up a session load gives it - so the edges the GUI wires next
    can't attach before its module streams are live."""
    monkeypatch.setitem(main_mod.NODE_TYPE_REGISTRY, "leaf_stub", _LeafStub)
    monkeypatch.setitem(main_mod.NODE_TYPE_REGISTRY, "finicky_stub", _FinickyStub)
    monkeypatch.setattr(main_mod, "_CAREFUL_NODE_TYPES", (_FinickyStub,))

    d = fresh_daemon()
    # The careful path only runs against a live graph; mark it loaded so
    # these assertions still exercise the staged bring-up.
    d.space.mark_graph_loaded()
    monkeypatch.setattr(
        d,
        "_create_node",
        lambda node_type, node_id, config: main_mod.NODE_TYPE_REGISTRY[node_type](
            node_id, config.get("backing_node_name") or f"patchbay_{node_id}"
        ),
    )
    bringups = []
    staged_during_bringup = []

    def fake_bring_up(node):
        bringups.append(node.id)
        staged_during_bringup.append(node.id in d.space._staging)
        return True

    monkeypatch.setattr(d, "_bring_node_up", fake_bring_up)
    internals = []
    monkeypatch.setattr(
        d,
        "_wait_node_internals_wired",
        lambda nid: (internals.append(nid), True)[1],
    )
    relinked = []
    monkeypatch.setattr(
        d,
        "_relink_edge_carefully",
        lambda edge: (
            relinked.append(f"{edge['from']}->{edge['to']}"),
            ("", None, False, None),
        )[1],
    )

    # A plain leaf node is created normally, no careful pass.
    assert (
        d.handle_command(
            {"command": "add_node", "node_type": "leaf_stub", "node_id": "src"}
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command(
            {"command": "add_node", "node_type": "finicky_stub", "node_id": "fx"}
        )["status"]
        == "ok"
    )
    # It was staged for the whole bring-up, brought up, and its interior
    # waited on - and left unstaged afterward.
    assert bringups == ["fx"]
    assert staged_during_bringup == [True]
    assert internals == ["fx"]
    assert "fx" not in d.space._staging

    # An edge touching the finicky node is wired the careful way; one
    # that doesn't is left to the normal sync.
    assert (
        d.handle_command({"command": "add_edge", "from_node": "src", "to_node": "fx"})[
            "status"
        ]
        == "ok"
    )
    assert relinked == ["src->fx"]
    assert (
        d.handle_command(
            {"command": "add_node", "node_type": "leaf_stub", "node_id": "dst"}
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command({"command": "add_edge", "from_node": "src", "to_node": "dst"})[
            "status"
        ]
        == "ok"
    )
    assert relinked == ["src->fx"]


def test_load_session_only_reaps_backings_of_new_nodes(monkeypatch):
    """Re-importing the current session must not reap the live objects
    of nodes that already exist - doing so kills their owning processes
    and leaves them structurally present but silent (the re-import
    thrash). Only backings for nodes this load will create are swept."""
    monkeypatch.setitem(main_mod.NODE_TYPE_REGISTRY, "regular_stub", _RegularStub)

    d = fresh_daemon()
    monkeypatch.setattr(
        d,
        "_create_node",
        lambda node_type, node_id, config: main_mod.NODE_TYPE_REGISTRY[node_type](
            node_id, config.get("backing_node_name") or f"patchbay_{node_id}"
        ),
    )
    monkeypatch.setattr(d, "_bring_node_up", lambda node: True)
    monkeypatch.setattr(d, "_wait_node_internals_wired", lambda nid: True)
    monkeypatch.setattr(
        d, "_wire_edge_carefully", lambda edge: d._store_session_edge(edge)
    )
    monkeypatch.setattr(
        d, "_relink_edge_carefully", lambda edge: ("", None, False, None)
    )

    # An already-present, healthy node whose backing must be left alone.
    d.space.nodes["keep"] = _RegularStub("keep", "keep_backing")
    d.space.public_nodes.add("keep")

    reaped = []
    monkeypatch.setattr(
        d.graph,
        "reap_stale_for_names",
        lambda markers: reaped.append(list(markers)) or 0,
    )

    d._load_session(
        {
            "nodes": {
                "keep": {
                    "type": "regular_stub",
                    "params": {"backing_node_name": "keep_backing"},
                },
                "fresh": {
                    "type": "regular_stub",
                    "params": {"backing_node_name": "fresh_backing"},
                },
            },
            "edges": [],
        }
    )
    assert reaped == [["fresh_backing"]]


def test_speaker_mic_lines_share_builtin_volume_and_lock():
    from pwnodes import (
        PatchBayDeviceNode,
        VirtualMicNode,
        VirtualSpeakerNode,
    )

    d = fresh_daemon()
    # Stand in for the daemon's built-in devices (start() creates these).
    d.builtin_sink = VirtualSpeakerNode("__builtin_sink__", "PatchBay")
    d.builtin_mic = VirtualMicNode("__builtin_mic__", "PatchBay Mic")

    for node_id in ("spk1", "spk2"):
        resp = d.handle_command(
            {
                "command": "add_node",
                "node_type": "patchbay_device",
                "node_id": node_id,
                "config": {"label": "Speaker Line"},
            }
        )
        assert resp["status"] == "ok", resp
    assert (
        d.handle_command(
            {
                "command": "add_node",
                "node_type": "patchbay_mic_device",
                "node_id": "mic1",
                "config": {"label": "Mic Line"},
            }
        )["status"]
        == "ok"
    )

    # Several Speaker Lines coexist and all point at the one built-in
    # sink (same source/sink identities), not separate devices.
    assert isinstance(d.space.nodes["spk1"], PatchBayDeviceNode)
    assert d._line_volume_target(d.space.nodes["spk1"]) is d.builtin_sink
    assert d._line_volume_target(d.space.nodes["spk2"]) is d.builtin_sink
    assert d._line_volume_target(d.space.nodes["mic1"]) is d.builtin_mic
    assert (
        d.space.nodes["spk1"].source_filters() == d.space.nodes["spk2"].source_filters()
    )

    # One line's slider drives the shared device and mirrors to siblings.
    resp = d.handle_command(
        {"command": "set_device_volume", "node_id": "spk1", "volume": 0.25}
    )
    assert resp["status"] == "ok", resp
    assert abs(d.builtin_sink.device_volume - 0.25) < 1e-9
    assert abs(d.space.nodes["spk1"].device_volume - 0.25) < 1e-9
    assert abs(d.space.nodes["spk2"].device_volume - 0.25) < 1e-9
    # The mic line is a different device and is untouched.
    assert abs(d.builtin_mic.device_volume - 1.0) < 1e-9

    # Lock is shared per device and mirrors to every line.
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "spk2",
                "property": "volume_locked",
                "value": False,
            }
        )["status"]
        == "ok"
    )
    assert d.builtin_sink.volume_locked is False
    assert d.space.nodes["spk1"].volume_locked is False
    assert d.space.nodes["spk2"].volume_locked is False
    assert d.builtin_mic.volume_locked is True

    # And the state round-trips through serialization for the GUI.
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert abs(nodes["spk1"]["device_volume"] - 0.25) < 1e-9
    assert nodes["spk1"]["volume_locked"] is False


def test_device_volume_lock_gates_constant_override(monkeypatch):
    import pwnodes
    from pwnodes import DeviceOutputNode

    calls = []
    monkeypatch.setattr(pwnodes, "_run_wpctl", lambda *a, **k: calls.append(a) or True)

    node = DeviceOutputNode("out", "dev", device_volume=0.5)
    node.live_node_id = 123
    node.live_props = {}

    # Locked (default): the configured value is re-asserted every apply.
    calls.clear()
    node.apply_device_settings()
    assert ("set-volume", 123, 0.5) in calls

    # Unlocked: the tick no longer overwrites the device...
    node.volume_locked = False
    calls.clear()
    node.apply_device_settings()
    assert calls == []

    # ...but an explicit user drag still pushes immediately.
    calls.clear()
    node.apply_device_settings(push_volume=True)
    assert ("set-volume", 123, 0.5) in calls


def test_normalize_registry_spec_and_control_clamp():
    from pwnodes import NormalizeNode
    from gui import node_specs as ns

    assert main_mod.NODE_TYPE_REGISTRY["normalize"] is NormalizeNode
    assert NormalizeNode in main_mod._CAREFUL_NODE_TYPES
    spec = ns.spec_for("normalize")
    assert spec.label == "Normalize"
    assert spec.control == "gain"
    assert ns.normalize_node_type("NormalizeNode") == "normalize"
    assert ("Normalize", "normalize") in ns.ADD_NODE_MENU_ITEMS

    d = fresh_daemon()
    node = NormalizeNode("nz", "norm")
    d.space.nodes["nz"] = node
    d.space.public_nodes.add("nz")

    # Out-of-range values are clamped to the plugin's safe bounds and a
    # (debounced) interior reload is scheduled for the load-time change.
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "nz",
                "property": "boost_db",
                "value": 999,
            }
        )["status"]
        == "ok"
    )
    assert node.boost_db == NormalizeNode.BOOST_MAX_DB
    assert node._reload_due is not None

    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "nz",
                "property": "ceiling_db",
                "value": -999,
            }
        )["status"]
        == "ok"
    )
    assert node.ceiling_db == NormalizeNode.CEILING_MIN_DB

    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "nz",
                "property": "leveling",
                "value": False,
            }
        )["status"]
        == "ok"
    )
    assert node.leveling is False

    # Serialized for the GUI (and the inline boost slider reads boost_db).
    data = d.handle_command({"command": "get_nodes"})["nodes"]["nz"]
    assert data["type"] == "normalize"
    assert data["boost_db"] == NormalizeNode.BOOST_MAX_DB
    assert data["leveling"] is False


def test_boolean_nodes_registry_specs_and_output_control():
    from pwnodes import (
        BooleanSourceNode,
        BooleanSplitterNode,
        BooleanInvertNode,
        BooleanAndNode,
        BooleanOrNode,
    )
    from gui import node_specs as ns

    assert main_mod.NODE_TYPE_REGISTRY["boolean_switch"] is BooleanSourceNode
    assert main_mod.NODE_TYPE_REGISTRY["boolean_splitter"] is BooleanSplitterNode
    assert main_mod.NODE_TYPE_REGISTRY["boolean_invert"] is BooleanInvertNode
    assert main_mod.NODE_TYPE_REGISTRY["boolean_and"] is BooleanAndNode
    assert main_mod.NODE_TYPE_REGISTRY["boolean_or"] is BooleanOrNode
    assert ns.spec_for("boolean_and").inputs == ["a", "b"]
    assert ns.spec_for("boolean_and").boolean_inputs == {"a", "b"}
    assert ns.spec_for("boolean_or").boolean_outputs == {"out"}
    assert ns.spec_for("boolean_switch").inputs == []
    assert ns.spec_for("boolean_switch").boolean_outputs == {"out"}
    assert ns.spec_for("gate").boolean_inputs == {"ctrl"}
    assert ns.spec_for("gate").control == "fallback_onoff"
    assert ns.spec_for("switcher").inputs == ["in", "ctrl"]
    assert ns.spec_for("switcher").outputs == ["on", "off"]
    assert ns.port_kind("gate", "ctrl", "in") == "boolean"
    assert ns.port_kind("gate", "in", "in") == "audio"
    assert ns.port_kind("switcher", "on", "out") == "audio"
    assert ns.port_kind("boolean_splitter", "out1", "out") == "boolean"
    assert ns.port_kind("boolean_splitter", "in", "in") == "boolean"
    assert ns.port_kind("boolean_invert", "out", "out") == "boolean"
    assert ("On/Off", "boolean_switch") in ns.ADD_NODE_MENU_ITEMS
    assert ("Bool Splitter", "boolean_splitter") in ns.ADD_NODE_MENU_ITEMS
    assert ("Invert", "boolean_invert") in ns.ADD_NODE_MENU_ITEMS
    assert ("AND", "boolean_and") in ns.ADD_NODE_MENU_ITEMS
    assert ("OR", "boolean_or") in ns.ADD_NODE_MENU_ITEMS

    d = fresh_daemon()
    d.space.mark_graph_loaded()
    assert (
        d.handle_command(
            {
                "command": "add_node",
                "node_type": "boolean_switch",
                "node_id": "bo",
                "config": {},
            }
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command(
            {"command": "add_node", "node_type": "gate", "node_id": "g", "config": {}}
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command(
            {
                "command": "add_edge",
                "from_node": "bo",
                "to_node": "g",
                "to_port": "ctrl",
            }
        )["status"]
        == "ok"
    )

    # Flipping the On/Off source drives the wired gate's pass state.
    assert d.space.nodes["g"].gate_open() is False  # output default 0
    d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "bo",
            "property": "output",
            "value": 1,
        }
    )
    assert d.space.nodes["g"].gate_open() is True

    # get_nodes must surface the *effective* state and whether a ctrl
    # signal is wired, so the GUI can draw the on/off switch read-only
    # and white showing the value actually in effect (not the gate's
    # stored default).
    g = d.handle_command({"command": "get_nodes"})["nodes"]["g"]
    assert g["bool_driven"] is True
    assert g["bool_state"] is True
    d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "bo",
            "property": "output",
            "value": 0,
        }
    )
    g = d.handle_command({"command": "get_nodes"})["nodes"]["g"]
    assert g["bool_state"] is False

    # An unwired gate falls back to its own default: nothing is driving
    # it, so it reports no effective value.
    d.handle_command(
        {"command": "add_node", "node_type": "gate", "node_id": "g2", "config": {}}
    )
    g2 = d.handle_command({"command": "get_nodes"})["nodes"]["g2"]
    assert g2["bool_driven"] is False
    assert g2["bool_state"] is None

    # A boolean output can't be wired into an audio input.
    assert (
        d.handle_command(
            {"command": "add_edge", "from_node": "bo", "to_node": "g", "to_port": "in"}
        )["status"]
        == "error"
    )


def test_warp_nodes_registry_specs_and_name_roundtrip():
    from pwnodes import (
        WarpInNode,
        WarpOutNode,
        BooleanWarpInNode,
        BooleanWarpOutNode,
    )
    from gui import node_specs as ns

    assert main_mod.NODE_TYPE_REGISTRY["warp_in"] is WarpInNode
    assert main_mod.NODE_TYPE_REGISTRY["warp_out"] is WarpOutNode
    assert main_mod.NODE_TYPE_REGISTRY["bool_warp_in"] is BooleanWarpInNode
    assert main_mod.NODE_TYPE_REGISTRY["bool_warp_out"] is BooleanWarpOutNode
    assert ns.spec_for("warp_in").field == "warp_name"
    assert ns.spec_for("warp_out").field == "warp_name"
    assert ns.port_kind("warp_in", "in", "in") == "audio"
    assert ns.port_kind("warp_out", "out", "out") == "audio"
    assert ns.port_kind("bool_warp_in", "in", "in") == "boolean"
    assert ns.port_kind("bool_warp_out", "out", "out") == "boolean"
    for label, key in (
        ("Warp In", "warp_in"),
        ("Warp Out", "warp_out"),
        ("Bool Warp In", "bool_warp_in"),
        ("Bool Warp Out", "bool_warp_out"),
    ):
        assert (label, key) in ns.ADD_NODE_MENU_ITEMS

    d = fresh_daemon()
    assert (
        d.handle_command(
            {
                "command": "add_node",
                "node_type": "warp_in",
                "node_id": "wi",
                "config": {"warp_name": "foo"},
            }
        )["status"]
        == "ok"
    )
    assert (
        d.handle_command(
            {
                "command": "add_node",
                "node_type": "warp_out",
                "node_id": "wo",
                "config": {"warp_name": "foo"},
            }
        )["status"]
        == "ok"
    )

    # The name is serialized (so it round-trips through export/import)...
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["wi"]["warp_name"] == "foo"
    assert nodes["wo"]["warp_name"] == "foo"
    # ...and editable at runtime.
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "wi",
                "property": "warp_name",
                "value": "bar",
            }
        )["status"]
        == "ok"
    )
    assert d.space.nodes["wi"].warp_name == "bar"


def test_get_logs_serves_recent_output():
    import logging
    from main import _LogRingHandler

    main_mod._log_buffer.clear()
    root = logging.getLogger()
    handler = _LogRingHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(handler)
    try:
        root.warning("console hello")
        d = fresh_daemon()
        resp = d.handle_command({"command": "get_logs", "since": 0})
        assert resp["status"] == "ok"
        assert any("console hello" in line["text"] for line in resp["lines"])
        # Polling again from the returned high-water mark yields nothing.
        resp2 = d.handle_command({"command": "get_logs", "since": resp["last_seq"]})
        assert resp2["lines"] == []
    finally:
        root.removeHandler(handler)


def test_reverb_spec_and_control_clamp():
    from pwnodes import ReverbNode
    from gui import node_specs as ns

    spec = ns.spec_for("reverb")
    assert spec.control == "wetdry"
    settings_attrs = {row[0] for row in spec.settings}
    assert {
        "decay_time",
        "room_size",
        "diffusion",
        "hf_damp",
        "predelay",
        "plugin_uri",
    } <= settings_attrs

    d = fresh_daemon()
    node = ReverbNode("rev", "reverb_node_rev")
    d.space.nodes["rev"] = node
    d.space.public_nodes.add("rev")
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "rev",
                "property": "decay_time",
                "value": 999,
            }
        )["status"]
        == "ok"
    )
    assert node.decay_time == ReverbNode.DECAY_MAX_S
    assert node._reload_due is not None
    assert (
        d.handle_command(
            {
                "command": "set_node_property",
                "node_id": "rev",
                "property": "room_size",
                "value": -5,
            }
        )["status"]
        == "ok"
    )
    assert node.room_size == ReverbNode.ROOM_MIN

    data = d.handle_command({"command": "get_nodes"})["nodes"]["rev"]
    assert data["type"] == "reverb"
    assert data["decay_time"] == ReverbNode.DECAY_MAX_S


def test_install_log_ring_is_idempotent():
    import logging
    from main import _LogRingHandler

    root = logging.getLogger()
    for h in list(root.handlers):
        if isinstance(h, _LogRingHandler):
            root.removeHandler(h)
    main_mod._log_ring_installed = False
    try:
        main_mod._install_log_ring()
        main_mod._install_log_ring()
        ring = [h for h in root.handlers if isinstance(h, _LogRingHandler)]
        assert len(ring) == 1
    finally:
        for h in list(root.handlers):
            if isinstance(h, _LogRingHandler):
                root.removeHandler(h)
        main_mod._log_ring_installed = False


def test_assert_defaults_reclaims_builtin_when_stolen(monkeypatch):
    """Default promotion must not be one-shot: if something else takes
    the default source/sink after the builtin resolved, a later tick
    must put the builtin back, or apps silently record a hardware source
    instead of the processed Mic Line."""
    d = fresh_daemon()
    calls = []
    live = {"sink": 99, "source": 99}
    monkeypatch.setattr(d, "_builtin_resolved_ids", lambda: (11, 22))
    monkeypatch.setattr(
        d,
        "_read_default_id",
        lambda token: live["sink"] if "SINK" in token else live["source"],
    )
    monkeypatch.setattr(
        d, "_set_default", lambda token, node_id: calls.append((token, node_id))
    )

    # Builtin isn't default yet -> promote both immediately.
    d._default_check_at = 0.0
    d._assert_defaults()
    assert calls == [
        ("@DEFAULT_AUDIO_SINK@", 11),
        ("@DEFAULT_AUDIO_SOURCE@", 22),
    ]

    # Already the default -> no redundant set-default calls.
    live["sink"] = 11
    live["source"] = 22
    d._default_check_at = 0.0
    d._assert_defaults()
    assert len(calls) == 2

    # Something stole them again -> re-promote.
    live["sink"] = 99
    live["source"] = 99
    d._default_check_at = 0.0
    d._assert_defaults()
    assert calls[-2:] == [
        ("@DEFAULT_AUDIO_SINK@", 11),
        ("@DEFAULT_AUDIO_SOURCE@", 22),
    ]


def test_assert_defaults_is_throttled(monkeypatch):
    """The live-default re-check shells out to wpctl, so it must not run
    on every tick."""
    d = fresh_daemon()
    calls = []
    monkeypatch.setattr(d, "_builtin_resolved_ids", lambda: (11, 22))
    monkeypatch.setattr(d, "_read_default_id", lambda token: 99)
    monkeypatch.setattr(
        d, "_set_default", lambda token, node_id: calls.append(token)
    )

    d._default_check_at = 1e18  # far in the future
    d._assert_defaults()
    assert calls == []


def test_validate_session_reports_without_mutating(monkeypatch):
    """The dry-run session validator returns the issue/fix lists and
    never touches the live graph."""
    d = fresh_daemon()
    d.handle_command(
        {"command": "add_node", "node_type": "regex_input", "node_id": "in1",
         "config": {}}
    )
    d.handle_command(
        {"command": "add_node", "node_type": "regex_output", "node_id": "out1",
         "config": {}}
    )
    d.handle_command({"command": "add_edge", "from_node": "in1", "to_node": "out1"})

    # Inject a legacy-port edge directly into the space (the add_edge
    # command would normalize/reject it, so this simulates an old saved
    # session).
    d.handle_command(
        {"command": "add_node", "node_type": "inverse_switcher", "node_id": "tog",
         "config": {}}
    )
    edge = d.space.add_edge("in1", "tog", "a")
    assert edge in d.space.edges

    before = set(d.space.edges)
    resp = d.handle_command({"command": "validate_session"})
    assert resp["status"] == "ok"
    assert resp["applied"] is False
    assert any(f.startswith("normalized ports") for f in resp["fixes"])
    assert set(d.space.edges) == before


def test_another_daemon_running_detects_live_listener(monkeypatch, tmp_path):
    """A live listener on the socket counts; a bare leftover socket file
    (crashed run) does not."""
    path = str(tmp_path / "patchbay.sock")
    monkeypatch.setattr(main_mod, "SOCKET_PATH", path)
    assert main_mod.PatchBayDaemon._another_daemon_running() is False

    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    try:
        assert main_mod.PatchBayDaemon._another_daemon_running() is True
    finally:
        srv.close()


def test_start_refuses_when_another_daemon_is_listening(monkeypatch, tmp_path):
    """A second daemon must not unlink the live socket and start up; it
    should bail out before touching the graph."""
    path = str(tmp_path / "patchbay.sock")
    monkeypatch.setattr(main_mod, "SOCKET_PATH", path)
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(1)
    try:
        d = fresh_daemon()
        d.start()
        assert d._running is False
        assert d._ticker is None
        assert d.builtin_sink is None
    finally:
        srv.close()


def _device_snapshot(profile, enum_profiles):
    return {
        "id": 300,
        "type": "PipeWire:Interface:Device",
        "info": {"params": {"Profile": [profile], "EnumProfile": enum_profiles}},
    }


def test_ensure_device_profiles_applies_a2dp_when_off(monkeypatch):
    """A configured Bluetooth sink whose profile is 'off' exports no live
    node; the daemon should pick and apply an A2DP profile (and pin it so
    the normal tick keeps re-asserting it)."""
    from pwnodes import DeviceOutputNode

    d = fresh_daemon()
    node = DeviceOutputNode("tozo", device_name="bluez_output.XX.1")
    node.live_props = {"device.id": 300}
    node.live_node_id = None
    d.space.add_node(node)

    dev = _device_snapshot(
        {"index": 0, "name": "off"},
        [
            {"index": 0, "name": "off", "available": "yes", "priority": 0},
            {"index": 131073, "name": "a2dp-sink-sbc", "available": "yes", "priority": 132},
            {"index": 131076, "name": "a2dp-sink", "available": "yes", "priority": 133},
        ],
    )
    monkeypatch.setattr(d.graph, "all_objects", lambda: {300: dev})
    calls = []
    monkeypatch.setattr(
        main_mod, "_run_wpctl", lambda *a, **k: (calls.append(a), True)[1]
    )

    d._ensure_device_profiles()
    assert node.profile_index == 131076
    assert node.profile_description == "a2dp-sink"
    assert ("set-profile", 300, 131076) in calls


def test_ensure_device_profiles_reasserts_pinned_profile_when_off(monkeypatch):
    """A profile the user (or auto-pick) pinned must be re-applied if the
    device flips back to 'off' without its id changing."""
    from pwnodes import DeviceOutputNode

    d = fresh_daemon()
    node = DeviceOutputNode(
        "tozo", device_name="bluez_output.XX.1",
        profile_index=131076, profile_description="a2dp-sink",
    )
    node.live_props = {"device.id": 300}
    node.live_node_id = None
    d.space.add_node(node)
    dev = _device_snapshot(
        {"index": 0, "name": "off"},
        [{"index": 0, "name": "off", "available": "yes", "priority": 0}],
    )
    monkeypatch.setattr(d.graph, "all_objects", lambda: {300: dev})
    calls = []
    monkeypatch.setattr(
        main_mod, "_run_wpctl", lambda *a, **k: (calls.append(a), True)[1]
    )

    d._ensure_device_profiles()
    assert ("set-profile", 300, 131076) in calls
    assert node.profile_index == 131076


def test_ensure_device_profiles_warns_once_when_unroutable(monkeypatch, caplog):
    import logging

    from pwnodes import DeviceOutputNode

    d = fresh_daemon()
    node = DeviceOutputNode("tozo", device_name="bluez_output.XX.1")
    node.live_props = {"device.id": 300}
    node.live_node_id = None
    d.space.add_node(node)

    # No suitable profile to pick -> warn, once, rather than retrying the
    # message every tick (and never pin a bogus profile).
    dev = _device_snapshot(
        {"index": 0, "name": "off"},
        [{"index": 0, "name": "off", "available": "yes", "priority": 0}],
    )
    monkeypatch.setattr(d.graph, "all_objects", lambda: {300: dev})
    monkeypatch.setattr(main_mod, "_run_wpctl", lambda *a, **k: True)

    with caplog.at_level(logging.WARNING):
        d._ensure_device_profiles()
        d._ensure_device_profiles()

    warned = [
        r for r in caplog.records if "has no live audio node" in r.getMessage()
    ]
    assert len(warned) == 1
    assert node.profile_index is None


def test_ensure_device_profiles_clears_warning_when_routable(monkeypatch, caplog):
    import logging

    from pwnodes import DeviceOutputNode

    d = fresh_daemon()
    node = DeviceOutputNode("tozo", device_name="bluez_output.XX.1")
    node.live_props = {"device.id": 300}
    node.live_node_id = None
    d.space.add_node(node)
    dev = _device_snapshot(
        {"index": 0, "name": "off"},
        [{"index": 0, "name": "off", "available": "yes", "priority": 0}],
    )
    monkeypatch.setattr(d.graph, "all_objects", lambda: {300: dev})
    monkeypatch.setattr(main_mod, "_run_wpctl", lambda *a, **k: True)

    with caplog.at_level(logging.WARNING):
        d._ensure_device_profiles()
        # Device comes back with a live node + active profile.
        node.live_node_id = 99
        dev["info"]["params"]["Profile"] = [{"index": 131076, "name": "a2dp-sink"}]
        d._ensure_device_profiles()
        # ...and goes away again: the warning is allowed to fire again.
        node.live_node_id = None
        dev["info"]["params"]["Profile"] = [{"index": 0, "name": "off"}]
        d._ensure_device_profiles()

    warned = [
        r for r in caplog.records if "has no live audio node" in r.getMessage()
    ]
    assert len(warned) == 2

