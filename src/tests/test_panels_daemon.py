"""Daemon-side panel tests: flatten/round-trip, placement translate,
listing, and migration.  Leaf node types only, so no PipeWire starts."""

import json
import os

import panels
from main import PatchBayDaemon


def _daemon(tmp_path):
    pdir = tmp_path / "panels"
    pdir.mkdir()
    root = tmp_path / "root.json"
    d = PatchBayDaemon(
        panel_dirs=[(str(pdir), True)],
        root_panel_path=str(root),
    )
    return d, str(root), str(pdir)


def _write_initial_tree(root, pdir):
    kit = panels.Panel(
        id="kit", parent="", label="Kitchen", color="#112233",
        mode="read-write", x=100, y=50, w=300, h=200,
        path=pdir + "/kit.json", writable=True,
        config={"nodes": {"a": {"type": "regex_input",
                                "params": {"x": 10, "y": 20, "pattern": ".*"}}},
                "edges": [], "panels": [], "groups": []},
    )
    panels.write_file(pdir + "/kit.json", kit)
    rootp = panels.Panel(
        id="", parent=None, label="root", color="#ffffff", mode="read-write",
        writable=True,
        config={"nodes": {"top": {"type": "regex_output",
                                  "params": {"x": 0, "y": 0, "pattern": ".*"}}},
                "edges": [{"from": "kit::a", "to": "top"}],
                "panels": ["kit"], "groups": []},
    )
    panels.write_file(root, rootp)


def test_flatten_panels_qualifies_ids_and_folds_origin(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.panels = d._load_panels_tree()

    config = d._flatten_panels(d.panels)
    assert set(config["nodes"]) == {"kit::a", "top"}
    # Node x/y are panel-relative in the file, absolute after flatten.
    assert config["nodes"]["kit::a"]["params"]["x"] == 110
    assert config["nodes"]["kit::a"]["params"]["y"] == 70
    assert config["nodes"]["top"]["params"]["x"] == 0
    assert config["nodes"]["kit::a"]["params"]["declarative"] is True
    assert config["nodes"]["top"]["params"]["declarative"] is False
    assert config["edges"][0]["from"] == "kit::a"


def test_build_panels_round_trips_through_space(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.panels = d._load_panels_tree()
    d._load_session(d._flatten_panels(d.panels), declarative=True)

    tree = d._build_panels_from_space()
    assert set(tree) == {"", "kit"}
    assert set(tree["kit"].nodes) == {"a"}
    assert set(tree[""].nodes) == {"top"}
    # Serialized back to panel-relative coordinates.
    assert tree["kit"].nodes["a"]["params"]["x"] == 10.0
    assert tree["kit"].nodes["a"]["params"]["y"] == 20.0
    # The cross-panel edge is owned by the root, not the kit panel.
    assert tree["kit"].config["edges"] == []
    assert any(e["from"] == "kit::a" and e["to"] == "top"
               for e in tree[""].config["edges"])


def test_set_panel_layout_translates_subtree(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.panels = d._load_panels_tree()
    d._load_session(d._flatten_panels(d.panels), declarative=True)

    before = d.space.nodes["kit::a"].x
    d._cmd_set_panel_layout({"panel_id": "kit", "x": 200})
    assert d.panels["kit"].x == 200
    assert d.space.nodes["kit::a"].x == before + 100


def test_list_panels(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.panels = d._load_panels_tree()
    resp = d._cmd_list_panels({})
    assert resp["status"] == "ok"
    ids = {f["id"] for f in resp["files"]}
    assert "kit" in ids
    kit = next(f for f in resp["files"] if f["id"] == "kit")
    assert kit["mode"] == "read-write"
    assert kit["nodes"] == ["kit::a"]


def test_legacy_session_cache_migrates_to_root_panel(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    # A bare old autosave.
    with open(root, "w") as f:
        json.dump({
            "nodes": {"a": {"type": "regex_input", "params": {"x": 5, "y": 6,
                                                              "pattern": ".*"}}},
            "edges": [],
            "groups": [],
        }, f)
    tree = d._load_panels_tree()
    assert panels.ROOT_ID in tree
    assert "a" in tree[panels.ROOT_ID].nodes
    # The root file was rewritten in the panel shape.
    raw = json.load(open(root))
    assert raw.get("type") == panels.TYPE_PANEL


def test_create_and_delete_panel(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})

    resp = d._cmd_create_panel({"name": "Kitchen", "node_ids": ["a"]})
    assert resp["status"] == "ok", resp
    assert resp["panel_id"] == "Kitchen"
    assert "Kitchen::a" in d.space.nodes
    assert os.path.isfile(os.path.join(pdir, "Kitchen.json"))

    listing = d._cmd_list_panels({})
    assert any(f["id"] == "Kitchen" for f in listing["files"])

    assert d._cmd_delete_panel({"panel_id": "Kitchen"})["status"] == "ok"
    assert "Kitchen::a" not in d.space.nodes
    assert not os.path.exists(os.path.join(pdir, "Kitchen.json"))


def test_move_nodes_refused_from_readonly_panel(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    frozen = panels.Panel(
        id="frozen", parent="", label="Frozen", color="#abc", mode="read-only",
        path=os.path.join(pdir, "frozen.json"), writable=True,
        config={"nodes": {"x": {"type": "regex_input",
                                "params": {"x": 0, "y": 0, "pattern": ".*"}}},
                "edges": [], "panels": [], "groups": []},
    )
    panels.write_file(os.path.join(pdir, "frozen.json"), frozen)
    panels.write_file(root, panels.Panel(
        id="", parent=None, label="root", color="#fff", mode="read-write",
        writable=True,
        config={"nodes": {}, "edges": [], "panels": ["frozen"], "groups": []},
    ))
    d.panels = d._load_panels_tree()
    assert d.panels["frozen"].is_readonly
    d._load_session(d._flatten_panels(d.panels), declarative=True)
    assert "frozen::x" in d.space.nodes

    resp = d._cmd_move_nodes({"panel_id": "", "node_ids": ["frozen::x"]})
    assert resp["status"] == "ok"
    assert resp["moved"] == []
    assert "frozen::x" in resp["refused"]
    assert "frozen::x" in d.space.nodes


def test_reset_panel_reverts_subtree_scoped(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    frozen = panels.Panel(
        id="frozen", parent="", label="F", color="#abc", mode="read-only",
        path=os.path.join(pdir, "frozen.json"), writable=True,
        config={"nodes": {"x": {"type": "regex_input",
                                "params": {"x": 10, "y": 10, "pattern": "a"}}},
                "edges": [], "panels": [], "groups": []},
    )
    panels.write_file(os.path.join(pdir, "frozen.json"), frozen)
    panels.write_file(root, panels.Panel(
        id="", parent=None, label="root", color="#fff", mode="read-write",
        writable=True,
        config={"nodes": {"y": {"type": "regex_output",
                                "params": {"x": 500, "y": 10, "pattern": "b"}}},
                "edges": [{"from": "frozen::x", "to": "y"}],
                "panels": ["frozen"], "groups": []},
    ))
    d._install_panels(d._load_panels_tree())
    d._load_session(d._flatten_panels(d.panels), declarative=True)

    # Runtime edits in the read-only panel.
    with d._lock:
        d.space.nodes["frozen::x"].x = 999
        d.space.nodes["frozen::x"].pattern = "changed"
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "frozen::z", "config": {"pattern": "z"}})

    resp = d._cmd_reset_panel({"panel_id": "frozen"})
    assert resp["status"] == "ok", resp
    # Snapshot state restored...
    assert d.space.nodes["frozen::x"].x == 10
    assert d.space.nodes["frozen::x"].pattern == "a"
    assert "frozen::z" not in d.space.nodes
    # ...while the imperative cross-panel edge survives.
    assert "frozen::x->y" in d.space.edges
    assert "y" in d.space.nodes
