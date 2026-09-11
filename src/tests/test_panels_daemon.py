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


def test_set_panel_layout_moves_nodes_with_the_panel(tmp_path):
    # Moving a panel shifts its nodes by the same delta so their positions
    # relative to the panel stay put (the GUI's own node layout is the same,
    # so re-sending it is idempotent).
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.panels = d._load_panels_tree()
    d._load_session(d._flatten_panels(d.panels), declarative=True)

    before = d.space.nodes["kit::a"].x
    d._cmd_set_panel_layout({"panel_id": "kit", "x": 200})
    assert d.panels["kit"].x == 200
    assert d.space.nodes["kit::a"].x == before + 100


def test_panel_and_node_layout_round_trip_relative(tmp_path):
    # Moving a panel keeps its node's serialized relative position unchanged.
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.panels = d._load_panels_tree()
    d._load_session(d._flatten_panels(d.panels), declarative=True)

    node = d.space.nodes["kit::a"]
    assert (node.x, node.y) == (110, 70)  # 100/50 origin + 10/20 local
    d._cmd_set_panel_layout({"panel_id": "kit", "x": 200, "y": 90})

    tree = d._build_panels_from_space()
    rel = tree["kit"].config["nodes"]["a"]["params"]
    assert (rel["x"], rel["y"]) == (10, 20)


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


def test_create_panel_honours_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    resp = d._cmd_create_panel(
        {"name": "kit", "x": 10, "y": 20, "w": 333, "h": 222}
    )
    assert resp["status"] == "ok", resp
    panel = d.panels["kit"]
    assert (panel.x, panel.y, panel.w, panel.h) == (10.0, 20.0, 333.0, 222.0)
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["placement"] == {
        "x": 10.0, "y": 20.0, "w": 333.0, "h": 222.0, "anchored": False,
    }


def test_edit_panel_changes_label_and_color(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    assert d._cmd_create_panel({"name": "kit"})["status"] == "ok"

    resp = d._cmd_edit_panel(
        {"panel_id": "kit", "label": "Kitchen", "color": "#112233"}
    )
    assert resp["status"] == "ok", resp
    assert d.panels["kit"].label == "Kitchen"
    assert d.panels["kit"].color == "#112233"
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["label"] == "Kitchen"
    assert raw["color"] == "#112233"

    d.panels["ro"] = panels.Panel(
        id="ro", parent="", label="RO", color="#000000", mode="read-only",
        path=os.path.join(pdir, "ro.json"), writable=True,
        config={"nodes": {}, "edges": [], "panels": [], "groups": []},
    )
    assert d._cmd_edit_panel({"panel_id": "ro", "label": "nope"})["status"] == "error"


def test_move_nodes_carries_group_membership(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d.handle_command({"command": "add_node", "node_type": "regex_output",
                      "node_id": "b", "config": {"pattern": ".*"}})
    d.handle_command({"command": "add_group", "group_id": "g", "label": "G",
                      "nodes": ["a", "b"]})

    resp = d._cmd_create_panel({"name": "kit", "node_ids": ["a", "b"]})
    assert resp["status"] == "ok", resp
    # The group's members were re-qualified with the new panel...
    assert set(d.groups["g"]["nodes"]) == {"kit::a", "kit::b"}
    # ...and the rebuilt tree nests the group under the panel.
    tree = d._build_panels_from_space()
    assert tree["kit"].config["groups"]
    assert tree["kit"].config["groups"][0]["nodes"] == ["a", "b"]
    listing = d._cmd_list_panels({})
    kit = next(f for f in listing["files"] if f["id"] == "kit")
    assert set(kit["nodes"]) == {"kit::a", "kit::b"}


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


def test_delete_panel_with_nodes_removes_them(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"

    resp = d._cmd_delete_panel({"panel_id": "kit"})
    assert resp["status"] == "ok", resp
    assert "kit::a" not in d.space.nodes
    assert not os.path.exists(os.path.join(pdir, "kit.json"))


def test_delete_panel_keep_nodes_moves_them_to_parent(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    assert "kit::a" in d.space.nodes

    resp = d._cmd_delete_panel({"panel_id": "kit", "keep_nodes": True})
    assert resp["status"] == "ok", resp
    assert "a" in d.space.nodes
    assert "kit::a" not in d.space.nodes
    assert not os.path.exists(os.path.join(pdir, "kit.json"))
    # The node is now owned by the root panel.
    tree = d._build_panels_from_space()
    assert "a" in tree[panels.ROOT_ID].config["nodes"]
    listing = d._cmd_list_panels({})
    assert all(f["id"] != "kit" for f in listing["files"])


def test_export_panel_returns_live_state(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    # A runtime edit not yet persisted.
    d.space.nodes["kit::a"].pattern = "edited"
    resp = d._cmd_export_panel({"panel_id": "kit"})
    assert resp["status"] == "ok", resp
    assert resp["payload"]["type"] == "panel"
    assert resp["payload"]["config"]["nodes"]["a"]["params"]["pattern"] == "edited"


def test_clone_panel_creates_a_new_live_panel(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"

    resp = d._cmd_clone_panel({"panel_id": "kit", "name": "Kit Copy"})
    assert resp["status"] == "ok", resp
    stem = resp["panel_id"]
    assert os.path.isfile(os.path.join(pdir, stem + ".json"))
    assert stem in d.panels
    # The clone's node is live and namespaced under the new panel.
    assert f"{stem}::a" in d.space.nodes
    assert "kit::a" in d.space.nodes


def test_auto_load_false_panel_is_not_loaded_at_start(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    panels.write_file(
        os.path.join(pdir, "hidden.json"),
        panels.Panel(
            id="hidden", parent="", label="Hidden", color="#123456",
            mode="read-write", path=os.path.join(pdir, "hidden.json"),
            writable=True, auto_load=False,
            config={"nodes": {}, "edges": [], "panels": [], "groups": []},
        ),
    )
    panels.write_file(root, panels.Panel(
        id="", parent=None, label="root", color="#ffffff", mode="read-write",
        writable=True,
        config={"nodes": {}, "edges": [], "panels": [], "groups": []},
    ))
    tree = d._load_panels_tree()
    assert "hidden" not in tree


def test_edit_panel_sets_auto_load(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    assert d._cmd_create_panel({"name": "kit"})["status"] == "ok"
    assert d.panels["kit"].auto_load is False
    resp = d._cmd_edit_panel({"panel_id": "kit", "auto_load": True})
    assert resp["status"] == "ok", resp
    assert d.panels["kit"].auto_load is True
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["auto_load"] is True


def test_edit_mode_gates_param_persistence(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    kit_path = os.path.join(pdir, "kit.json")

    def file_pattern():
        raw = json.load(open(kit_path))
        return raw["config"]["nodes"]["a"]["params"]["pattern"]

    # Outside edit mode, a runtime tweak is not written.
    d.space.nodes["kit::a"].pattern = "live"
    d._write_panels()
    assert file_pattern() == "orig"

    # Entering edit mode refreshes to the file value.
    resp = d._cmd_set_panel_edit_mode({"panel_id": "kit", "enabled": True})
    assert resp["status"] == "ok", resp
    assert d.space.nodes["kit::a"].pattern == "orig"

    # While editing, changes are persisted.
    d.space.nodes["kit::a"].pattern = "committed"
    d._write_panels()
    assert file_pattern() == "committed"

    # Leaving edit mode stops persisting further tweaks.
    assert d._cmd_set_panel_edit_mode(
        {"panel_id": "kit", "enabled": False}
    )["status"] == "ok"
    d.space.nodes["kit::a"].pattern = "live2"
    d._write_panels()
    assert file_pattern() == "committed"


def test_edit_mode_refused_for_readonly(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.panels["ro"] = panels.Panel(
        id="ro", parent="", label="RO", color="#000", mode="read-only",
        path=os.path.join(pdir, "ro.json"), writable=True,
        config={"nodes": {}, "edges": [], "panels": [], "groups": []},
    )
    resp = d._cmd_set_panel_edit_mode({"panel_id": "ro", "enabled": True})
    assert resp["status"] == "error"


def test_move_panel_nests_and_preserves_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "b", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "fx", "node_ids": ["b"]})["status"] == "ok"

    before = d._panel_origin(d.panels, "fx")
    resp = d._cmd_move_panel({"panel_id": "fx", "parent_id": "kit"})
    assert resp["status"] == "ok", resp
    assert resp["panel_id"] == "kit::fx"
    assert d.panels["kit::fx"].parent == "kit"
    assert "kit::fx::b" in d.space.nodes
    assert "fx::b" not in d.space.nodes
    # Absolute placement preserved.
    assert d._panel_origin(d.panels, "kit::fx") == before
    # Parent link lists updated.
    assert "fx" in d.panels["kit"].children
    assert "fx" not in d.panels[panels.ROOT_ID].children
    # Survives a save/reload round-trip.
    d._write_panels()
    tree = d._load_panels_tree()
    assert "kit::fx" in tree
    assert "b" in tree["kit::fx"].nodes


def test_move_panel_refuses_cycle(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d._cmd_create_panel({"name": "kit"})
    d._cmd_create_panel({"name": "fx"})
    d._cmd_move_panel({"panel_id": "fx", "parent_id": "kit"})
    resp = d._cmd_move_panel({"panel_id": "kit", "parent_id": "kit::fx"})
    assert resp["status"] == "error"


def test_place_panel_adds_a_second_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})["status"] == "ok"

    resp = d._cmd_place_panel({"stem": "kit"})
    assert resp["status"] == "ok", resp
    name = resp["name"]
    assert name != "kit"
    assert name in d.panels
    assert d.panels[name].stem == "kit"
    # Both placements have their own live nodes from the shared file.
    assert "kit::a" in d.space.nodes
    assert f"{name}::a" in d.space.nodes
    # The file is shared.
    assert d.panels[name].path == d.panels["kit"].path


def test_list_panel_files_and_autoload_toggle(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    assert d._cmd_create_panel({"name": "kit"})["status"] == "ok"

    listing = d._cmd_list_panel_files({})
    assert listing["status"] == "ok"
    stems = {f["stem"]: f for f in listing["files"]}
    assert "kit" in stems
    assert stems["kit"]["auto_load"] is False

    assert d._cmd_set_panel_file_autoload(
        {"stem": "kit", "enabled": True}
    )["status"] == "ok"
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["auto_load"] is True
    listing = d._cmd_list_panel_files({})
    assert next(f for f in listing["files"] if f["stem"] == "kit")["auto_load"] is True


def test_placements_sync_structure(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_panel({"stem": "kit"})["name"]
    assert "kit::a" in d.space.nodes and f"{other}::a" in d.space.nodes

    # Add a node to one placement; the shared file syncs the other.
    d.handle_command({"command": "add_node", "node_type": "regex_output",
                      "node_id": "kit::b", "config": {"pattern": ".*"}})
    d._write_panels()
    assert f"{other}::b" in d.space.nodes


def test_placements_sync_positions(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*", "x": 10, "y": 20}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_panel({"stem": "kit"})["name"]

    rel = d.space.nodes["kit::a"]
    rel.x += 100
    rel.y += 40
    d._write_panels()
    o = d.space.nodes[f"{other}::a"]
    # Same relative offset within each placement's own origin.
    off_k = d._panel_origin(d.panels, "kit")
    off_o = d._panel_origin(d.panels, other)
    assert abs((rel.x - off_k[0]) - (o.x - off_o[0])) < 1e-6
    assert abs((rel.y - off_k[1]) - (o.y - off_o[1])) < 1e-6


def test_remove_panel_placement_keeps_file(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_panel({"stem": "kit"})["name"]

    resp = d._cmd_remove_panel_placement({"panel_id": other})
    assert resp["status"] == "ok", resp
    assert other not in d.panels
    assert f"{other}::a" not in d.space.nodes
    # The file and the other placement survive.
    assert os.path.isfile(os.path.join(pdir, "kit.json"))
    assert "kit" in d.panels and "kit::a" in d.space.nodes


def test_moving_a_placement_does_not_rebuild_siblings(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*", "x": 10, "y": 20}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_panel({"stem": "kit"})["name"]

    a_obj = d.space.nodes["kit::a"]
    b_obj = d.space.nodes[f"{other}::a"]
    d._cmd_set_panel_layout({"panel_id": "kit", "x": 500, "y": 100})
    d._write_panels()
    # Live nodes untouched (no rebuild), only positions shifted.
    assert d.space.nodes["kit::a"] is a_obj
    assert d.space.nodes[f"{other}::a"] is b_obj
    assert a_obj.x == 510


def test_moving_a_node_syncs_positions_without_rebuild(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*", "x": 10, "y": 20}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_panel({"stem": "kit"})["name"]

    b_obj = d.space.nodes[f"{other}::a"]
    d.space.nodes["kit::a"].x += 100
    d._write_panels()
    assert d.space.nodes[f"{other}::a"] is b_obj  # no rebuild
    off_k = d._panel_origin(d.panels, "kit")
    off_o = d._panel_origin(d.panels, other)
    assert abs((d.space.nodes["kit::a"].x - off_k[0]) - (b_obj.x - off_o[0])) < 1e-6


def test_placements_keep_their_own_positions(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_panel({"stem": "kit", "x": 42, "y": 24})["name"]

    d._cmd_set_panel_layout({"panel_id": "kit", "x": 500, "y": 100})
    d._cmd_set_panel_layout({"panel_id": other, "x": -300, "y": 800})
    d._write_panels()

    tree = d._load_panels_tree()
    assert (tree["kit"].x, tree["kit"].y) == (500, 100)
    assert (tree[other].x, tree[other].y) == (-300, 800)


def test_place_panel_honours_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d._cmd_create_panel({"name": "kit"})
    other = d._cmd_place_panel({"stem": "kit", "x": 42, "y": 24})["name"]
    assert (d.panels[other].x, d.panels[other].y) == (42, 24)
    d._write_panels()
    tree = d._load_panels_tree()
    assert (tree[other].x, tree[other].y) == (42, 24)


def test_enter_edit_mode_refreshes_params_without_recreating_nodes(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig"}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})

    obj = d.space.nodes["kit::a"]
    d.space.nodes["kit::a"].pattern = "live"
    resp = d._cmd_set_panel_edit_mode({"panel_id": "kit", "enabled": True})
    assert resp["status"] == "ok", resp
    # Same live node (no reload), but its params are back to the file's.
    assert d.space.nodes["kit::a"] is obj
    assert d.space.nodes["kit::a"].pattern == "orig"


def test_panel_port_nodes_round_trip(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d._cmd_create_panel({"name": "kit"})
    for nid, ntype, name in (
        ("kit::mic", "panel_in", "Mic"),
        ("kit::out", "panel_out", "Out"),
        ("kit::gate", "bool_panel_in", "Gate"),
    ):
        resp = d.handle_command(
            {"command": "add_node", "node_type": ntype, "node_id": nid,
             "config": {"port_name": name, "label": name, "anchored": True,
                        "description": name + " port", "x": 5, "y": 5}}
        )
        assert resp["status"] == "ok", resp
        assert d.space.nodes[nid].port_name == name
        assert d.space.nodes[nid].label == name
        assert d.space.nodes[nid].description == name + " port"
        assert d.space.nodes[nid].anchored is True
    assert d.space.nodes["kit::mic"].is_transparent()
    assert d.space.nodes["kit::gate"].port_kind("out", "out") == "boolean"

    d._write_panels()
    tree = d._load_panels_tree()
    assert tree["kit"].nodes["mic"]["params"]["port_name"] == "Mic"
    assert tree["kit"].nodes["gate"]["params"]["port_name"] == "Gate"


def test_edit_mode_params_sync_across_placements(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.panels = d._load_panels_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig", "x": 10, "y": 20}})
    d._cmd_create_panel({"name": "kit", "node_ids": ["a"]})
    d._write_panels()
    other = d._cmd_place_panel({"stem": "kit"})["name"]

    d._cmd_set_panel_edit_mode({"panel_id": other, "enabled": True})
    d.space.nodes[f"{other}::a"].pattern = "edited"
    d._write_panels()
    # The edit is mirrored onto the sibling...
    assert d.space.nodes["kit::a"].pattern == "edited"
    # ...and repeated writes don't clobber it back.
    d._write_panels()
    assert d.space.nodes["kit::a"].pattern == "edited"
    assert d.space.nodes[f"{other}::a"].pattern == "edited"
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["config"]["nodes"]["a"]["params"]["pattern"] == "edited"
