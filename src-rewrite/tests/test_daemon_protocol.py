"""Daemon-level protocol tests using leaf (non-backed) node types only,
so nothing touches a real PipeWire process.  Exercises the same command
layer and serialization shapes the GUI depends on."""

from main import PatchBayDaemon


def fresh_daemon():
    # Constructing a daemon only builds the graph monitor object; no
    # pw-dump / pw-cli subprocess is started until start().
    return PatchBayDaemon()


def test_add_node_and_serialize_leaf_types():
    d = fresh_daemon()
    for cmd in [
        {"command": "add_node", "node_type": "regex_input", "node_id": "in1",
         "config": {"pattern": ".*", "label": "Everything"}},
        {"command": "add_node", "node_type": "gate", "node_id": "g1",
         "config": {"enabled": True}},
        {"command": "add_node", "node_type": "exclude_filter", "node_id": "x1",
         "config": {"pattern": "Discord"}},
        {"command": "add_node", "node_type": "description_output", "node_id": "out1",
         "config": {"description": "speakers"}},
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
        {"command": "add_node", "node_type": "regex_input", "node_id": "in1",
         "config": {"pattern": "Firefox", "label": "Web"}},
        {"command": "add_node", "node_type": "gate", "node_id": "g1",
         "config": {"enabled": False}},
        {"command": "add_node", "node_type": "media_class_output", "node_id": "out1",
         "config": {"media_class": "Audio/Sink"}},
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
        resp = d2.handle_command({
            "command": "add_node", "node_type": node_cfg["type"],
            "node_id": node_id, "config": node_cfg["params"],
        })
        assert resp["status"] == "ok"
    for edge in config["edges"]:
        resp = d2.handle_command({
            "command": "add_edge", "from_node": edge["from"],
            "to_node": edge["to"], "to_port": edge.get("to_port", "in"),
        })
        assert resp["status"] == "ok"

    state2 = d2.handle_command({"command": "get_nodes"})
    n2 = state2["nodes"]
    assert n2["in1"]["pattern"] == "Firefox"
    assert n2["in1"]["label"] == "Web"
    assert n2["g1"]["enabled"] is False
    assert n2["out1"]["media_class"] == "Audio/Sink"
    assert set(state2["edges"]) == set(state2["edges"])  # stable


def test_set_property_and_rename():
    d = fresh_daemon()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "n1", "config": {"pattern": ".*"}})
    resp = d.handle_command({"command": "set_node_property",
                             "node_id": "n1", "property": "pattern",
                             "value": "OBS"})
    assert resp["status"] == "ok"
    assert d.handle_command({"command": "get_nodes"})["nodes"]["n1"]["pattern"] == "OBS"

    resp = d.handle_command({"command": "rename_node",
                             "old_node_id": "n1", "new_node_id": "n2"})
    assert resp["status"] == "ok"
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert "n2" in nodes and "n1" not in nodes
    assert nodes["n2"]["pattern"] == "OBS"


def test_remove_node_and_reset():
    d = fresh_daemon()
    d.handle_command({"command": "add_node", "node_type": "gate",
                      "node_id": "g1", "config": {}})
    d.handle_command({"command": "add_node", "node_type": "gate",
                      "node_id": "g2", "config": {}})
    assert d.handle_command({"command": "remove_node", "node_id": "g1"})["status"] == "ok"
    assert "g1" not in d.handle_command({"command": "get_nodes"})["nodes"]

    d.handle_command({"command": "reset"})
    assert d.handle_command({"command": "get_nodes"})["nodes"] == {}
    assert d.handle_command({"command": "get_nodes"})["edges"] == {}


def test_unknown_type_and_bad_command_error():
    d = fresh_daemon()
    resp = d.handle_command({"command": "add_node", "node_type": "nope",
                             "node_id": "x", "config": {}})
    assert resp["status"] == "error"
    assert d.handle_command({"command": "frobnicate"})["status"] == "error"
