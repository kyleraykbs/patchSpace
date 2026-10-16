"""Daemon-side space tests: flatten/round-trip, placement translate,
listing, and migration.  Leaf node types only, so no PipeWire starts."""

import json
import os
from pathlib import Path

from central import spaces
from central.main import PatchSpaceDaemon
from modules.pipewire.pwnodes import SpaceInNode


def _daemon(tmp_path):
    pdir = tmp_path / "spaces"
    pdir.mkdir()
    root = tmp_path / "root.json"
    d = PatchSpaceDaemon(
        space_dirs=[(str(pdir), True)],
        root_space_path=str(root),
    )
    return d, str(root), str(pdir)


def _write_initial_tree(root, pdir):
    kit = spaces.Space(
        id="kit", parent="", label="Kitchen", color="#112233",
        mode="read-write", x=100, y=50, w=300, h=200,
        path=pdir + "/kit.json", writable=True,
        config={"nodes": {"a": {"type": "regex_input",
                                "params": {"x": 10, "y": 20, "pattern": ".*"}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(pdir + "/kit.json", kit)
    rootp = spaces.Space(
        id="", parent=None, label="root", color="#ffffff", mode="read-write",
        writable=True,
        config={"nodes": {"top": {"type": "regex_output",
                                  "params": {"x": 0, "y": 0, "pattern": ".*"}}},
                "edges": [{"from": "kit::a", "to": "top"}],
                "spaces": ["kit"], "groups": []},
    )
    spaces.write_file(root, rootp)


def test_flatten_spaces_qualifies_ids_and_folds_origin(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.spaces = d._load_spaces_tree()

    config = d._flatten_spaces(d.spaces)
    assert set(config["nodes"]) == {"kit::a", "top"}
    # Node x/y are space-relative in the file, absolute after flatten.
    assert config["nodes"]["kit::a"]["params"]["x"] == 110
    assert config["nodes"]["kit::a"]["params"]["y"] == 70
    assert config["nodes"]["top"]["params"]["x"] == 0
    assert config["nodes"]["kit::a"]["params"]["declarative"] is True
    assert config["nodes"]["top"]["params"]["declarative"] is False
    assert config["edges"][0]["from"] == "kit::a"


def test_build_spaces_round_trips_through_space(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.spaces = d._load_spaces_tree()
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)

    tree = d._build_spaces_from_space()
    assert set(tree) == {"", "kit"}
    assert set(tree["kit"].nodes) == {"a"}
    assert set(tree[""].nodes) == {"top"}
    # Serialized back to space-relative coordinates.
    assert tree["kit"].nodes["a"]["params"]["x"] == 10.0
    assert tree["kit"].nodes["a"]["params"]["y"] == 20.0
    # The cross-space edge is owned by the root, not the kit space.
    assert tree["kit"].config["edges"] == []
    assert any(e["from"] == "kit::a" and e["to"] == "top"
               for e in tree[""].config["edges"])


def test_set_space_layout_moves_nodes_with_the_space(tmp_path):
    # Moving a space shifts its nodes by the same delta so their positions
    # relative to the space stay put (the GUI's own node layout is the same,
    # so re-sending it is idempotent).
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.spaces = d._load_spaces_tree()
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)

    before = d.space.nodes["kit::a"].x
    d._cmd_set_space_layout({"space_id": "kit", "x": 200})
    assert d.spaces["kit"].x == 200
    assert d.space.nodes["kit::a"].x == before + 100


def test_space_and_node_layout_round_trip_relative(tmp_path):
    # Moving a space keeps its node's serialized relative position unchanged.
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.spaces = d._load_spaces_tree()
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)

    node = d.space.nodes["kit::a"]
    assert (node.x, node.y) == (110, 70)  # 100/50 origin + 10/20 local
    d._cmd_set_space_layout({"space_id": "kit", "x": 200, "y": 90})

    tree = d._build_spaces_from_space()
    rel = tree["kit"].config["nodes"]["a"]["params"]
    assert (rel["x"], rel["y"]) == (10, 20)


def test_list_spaces(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    _write_initial_tree(root, pdir)
    d.spaces = d._load_spaces_tree()
    resp = d._cmd_list_spaces({})
    assert resp["status"] == "ok"
    ids = {f["id"] for f in resp["files"]}
    assert "kit" in ids
    kit = next(f for f in resp["files"] if f["id"] == "kit")
    assert kit["mode"] == "read-write"
    assert kit["nodes"] == ["kit::a"]


def test_legacy_session_cache_migrates_to_root_space(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    # A bare old autosave.
    with open(root, "w") as f:
        json.dump({
            "nodes": {"a": {"type": "regex_input", "params": {"x": 5, "y": 6,
                                                              "pattern": ".*"}}},
            "edges": [],
            "groups": [],
        }, f)
    tree = d._load_spaces_tree()
    assert spaces.ROOT_ID in tree
    assert "a" in tree[spaces.ROOT_ID].nodes
    # The root file was rewritten in the space shape.
    raw = json.load(open(root))
    # The file's `type` key carries the space's *kind* since S1 (`live`);
    # `space`/`panel` are the pre-S1 spellings of a live space.
    assert raw.get("type") == spaces.LIVE


def test_create_and_delete_space(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})

    resp = d._cmd_create_space({"name": "Kitchen", "node_ids": ["a"]})
    assert resp["status"] == "ok", resp
    assert resp["space_id"] == "Kitchen"
    assert "Kitchen::a" in d.space.nodes
    assert os.path.isfile(os.path.join(pdir, "Kitchen.json"))

    listing = d._cmd_list_spaces({})
    assert any(f["id"] == "Kitchen" for f in listing["files"])

    assert d._cmd_delete_space({"space_id": "Kitchen"})["status"] == "ok"
    assert "Kitchen::a" not in d.space.nodes
    assert not os.path.exists(os.path.join(pdir, "Kitchen.json"))


def test_move_nodes_refused_from_readonly_space(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    frozen = spaces.Space(
        id="frozen", parent="", label="Frozen", color="#abc", mode="read-only",
        path=os.path.join(pdir, "frozen.json"), writable=True,
        config={"nodes": {"x": {"type": "regex_input",
                                "params": {"x": 0, "y": 0, "pattern": ".*"}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(os.path.join(pdir, "frozen.json"), frozen)
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode="read-write",
        writable=True,
        config={"nodes": {}, "edges": [], "spaces": ["frozen"], "groups": []},
    ))
    d.spaces = d._load_spaces_tree()
    assert d.spaces["frozen"].is_readonly
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)
    assert "frozen::x" in d.space.nodes

    resp = d._cmd_move_nodes({"space_id": "", "node_ids": ["frozen::x"]})
    assert resp["status"] == "ok"
    assert resp["moved"] == []
    assert "frozen::x" in resp["refused"]
    assert "frozen::x" in d.space.nodes


def test_create_space_honours_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    resp = d._cmd_create_space(
        {"name": "kit", "x": 10, "y": 20, "w": 333, "h": 222}
    )
    assert resp["status"] == "ok", resp
    space = d.spaces["kit"]
    assert (space.x, space.y, space.w, space.h) == (10.0, 20.0, 333.0, 222.0)
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    # New spaces default to pinned/paused (physics off for them), so the
    # placement records anchored True even though the caller didn't ask.
    assert raw["placement"] == {
        "x": 10.0, "y": 20.0, "w": 333.0, "h": 222.0, "anchored": True,
    }


def test_edit_space_changes_label_and_set_space_meta_the_color(tmp_path):
    """The space's name is the rename path (`edit_space`); everything S1
    added about it - color, kind, permanence, the default view - is
    `set_space_meta`.  One writer per field."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    assert d._cmd_create_space({"name": "kit"})["status"] == "ok"

    resp = d._cmd_edit_space({"space_id": "kit", "label": "Kitchen"})
    assert resp["status"] == "ok", resp
    assert d.spaces["kit"].label == "Kitchen"
    resp = d._cmd_set_space_meta({"space_id": "kit", "color": "#112233"})
    assert resp["status"] == "ok", resp
    resp = d._cmd_edit_space({"space_id": "kit", "color": "#112233"})
    assert resp["status"] == "ok", resp
    assert "color" not in resp              # edit_space no longer touches it
    assert d.spaces["kit"].color == "#112233"
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["label"] == "Kitchen"
    assert raw["color"] == "#112233"

    d.spaces["ro"] = spaces.Space(
        id="ro", parent="", label="RO", color="#000000", mode="read-only",
        path=os.path.join(pdir, "ro.json"), writable=True,
        config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    )
    assert d._cmd_edit_space({"space_id": "ro", "label": "nope"})["status"] == "error"


def test_move_nodes_carries_group_membership(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d.handle_command({"command": "add_node", "node_type": "regex_output",
                      "node_id": "b", "config": {"pattern": ".*"}})
    d.handle_command({"command": "add_group", "group_id": "g", "label": "G",
                      "nodes": ["a", "b"]})

    resp = d._cmd_create_space({"name": "kit", "node_ids": ["a", "b"]})
    assert resp["status"] == "ok", resp
    # The group's members were re-qualified with the new space...
    assert set(d.groups["g"]["nodes"]) == {"kit::a", "kit::b"}
    # ...and the rebuilt tree nests the group under the space.
    tree = d._build_spaces_from_space()
    assert tree["kit"].config["groups"]
    assert tree["kit"].config["groups"][0]["nodes"] == ["a", "b"]
    listing = d._cmd_list_spaces({})
    kit = next(f for f in listing["files"] if f["id"] == "kit")
    assert set(kit["nodes"]) == {"kit::a", "kit::b"}


def test_reset_space_reverts_subtree_scoped(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    frozen = spaces.Space(
        id="frozen", parent="", label="F", color="#abc", mode="read-only",
        path=os.path.join(pdir, "frozen.json"), writable=True,
        config={"nodes": {"x": {"type": "regex_input",
                                "params": {"x": 10, "y": 10, "pattern": "a"}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(os.path.join(pdir, "frozen.json"), frozen)
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode="read-write",
        writable=True,
        config={"nodes": {"y": {"type": "regex_output",
                                "params": {"x": 500, "y": 10, "pattern": "b"}}},
                "edges": [{"from": "frozen::x", "to": "y"}],
                "spaces": ["frozen"], "groups": []},
    ))
    d._install_spaces(d._load_spaces_tree())
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)

    # Runtime edits in the read-only space.
    with d._lock:
        d.space.nodes["frozen::x"].x = 999
        d.space.nodes["frozen::x"].pattern = "changed"
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "frozen::z", "config": {"pattern": "z"}})

    resp = d._cmd_reset_space({"space_id": "frozen"})
    assert resp["status"] == "ok", resp
    # Snapshot state restored...
    assert d.space.nodes["frozen::x"].x == 10
    assert d.space.nodes["frozen::x"].pattern == "a"
    assert "frozen::z" not in d.space.nodes
    # ...while the imperative cross-space edge survives.
    assert "frozen::x->y:audio" in d.space.edges
    assert "y" in d.space.nodes


def test_delete_space_with_nodes_removes_them(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"

    resp = d._cmd_delete_space({"space_id": "kit"})
    assert resp["status"] == "ok", resp
    assert "kit::a" not in d.space.nodes
    assert not os.path.exists(os.path.join(pdir, "kit.json"))


def test_delete_space_keep_nodes_moves_them_to_parent(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    assert "kit::a" in d.space.nodes

    resp = d._cmd_delete_space({"space_id": "kit", "keep_nodes": True})
    assert resp["status"] == "ok", resp
    assert "a" in d.space.nodes
    assert "kit::a" not in d.space.nodes
    assert not os.path.exists(os.path.join(pdir, "kit.json"))
    # The node is now owned by the root space.
    tree = d._build_spaces_from_space()
    assert "a" in tree[spaces.ROOT_ID].config["nodes"]
    listing = d._cmd_list_spaces({})
    assert all(f["id"] != "kit" for f in listing["files"])


def test_export_space_returns_live_state(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    # A runtime edit not yet persisted.
    d.space.nodes["kit::a"].pattern = "edited"
    resp = d._cmd_export_space({"space_id": "kit"})
    assert resp["status"] == "ok", resp
    assert resp["payload"]["type"] == spaces.LIVE
    assert resp["payload"]["config"]["nodes"]["a"]["params"]["pattern"] == "edited"


def test_clone_space_creates_a_new_live_space(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"

    resp = d._cmd_clone_space({"space_id": "kit", "name": "Kit Copy"})
    assert resp["status"] == "ok", resp
    stem = resp["space_id"]
    assert os.path.isfile(os.path.join(pdir, stem + ".json"))
    assert stem in d.spaces
    # A fresh clone starts pinned/paused, like a newly created space.
    assert d.spaces[stem].anchored is True
    # The clone's node is live and namespaced under the new space.
    assert f"{stem}::a" in d.space.nodes
    assert "kit::a" in d.space.nodes


def test_auto_load_false_space_is_not_loaded_at_start(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    spaces.write_file(
        os.path.join(pdir, "hidden.json"),
        spaces.Space(
            id="hidden", parent="", label="Hidden", color="#123456",
            mode="read-write", path=os.path.join(pdir, "hidden.json"),
            writable=True, auto_load=False,
            config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
        ),
    )
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#ffffff", mode="read-write",
        writable=True,
        config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    ))
    tree = d._load_spaces_tree()
    assert "hidden" not in tree


def test_edit_space_sets_auto_load(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    assert d._cmd_create_space({"name": "kit"})["status"] == "ok"
    assert d.spaces["kit"].auto_load is False
    resp = d._cmd_edit_space({"space_id": "kit", "auto_load": True})
    assert resp["status"] == "ok", resp
    assert d.spaces["kit"].auto_load is True
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["auto_load"] is True


def test_edit_mode_gates_param_persistence(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    kit_path = os.path.join(pdir, "kit.json")

    def file_pattern():
        raw = json.load(open(kit_path))
        return raw["config"]["nodes"]["a"]["params"]["pattern"]

    # Outside edit mode, a runtime tweak is not written.
    d.space.nodes["kit::a"].pattern = "live"
    d._write_spaces()
    assert file_pattern() == "orig"

    # Entering edit mode refreshes to the file value.
    resp = d._cmd_set_space_edit_mode({"space_id": "kit", "enabled": True})
    assert resp["status"] == "ok", resp
    assert d.space.nodes["kit::a"].pattern == "orig"

    # While editing, changes are persisted.
    d.space.nodes["kit::a"].pattern = "committed"
    d._write_spaces()
    assert file_pattern() == "committed"

    # Leaving edit mode stops persisting further tweaks.
    assert d._cmd_set_space_edit_mode(
        {"space_id": "kit", "enabled": False}
    )["status"] == "ok"
    d.space.nodes["kit::a"].pattern = "live2"
    d._write_spaces()
    assert file_pattern() == "committed"


def test_edit_mode_refused_for_readonly(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.spaces["ro"] = spaces.Space(
        id="ro", parent="", label="RO", color="#000", mode="read-only",
        path=os.path.join(pdir, "ro.json"), writable=True,
        config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    )
    resp = d._cmd_set_space_edit_mode({"space_id": "ro", "enabled": True})
    assert resp["status"] == "error"


def test_move_space_nests_and_preserves_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "b", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "fx", "node_ids": ["b"]})["status"] == "ok"

    before = d._space_origin(d.spaces, "fx")
    resp = d._cmd_move_space({"space_id": "fx", "parent_id": "kit"})
    assert resp["status"] == "ok", resp
    assert resp["space_id"] == "kit::fx"
    assert d.spaces["kit::fx"].parent == "kit"
    assert "kit::fx::b" in d.space.nodes
    assert "fx::b" not in d.space.nodes
    # Absolute placement preserved.
    assert d._space_origin(d.spaces, "kit::fx") == before
    # Parent link lists updated.
    assert "fx" in d.spaces["kit"].children
    assert "fx" not in d.spaces[spaces.ROOT_ID].children
    # Survives a save/reload round-trip.
    d._write_spaces()
    tree = d._load_spaces_tree()
    assert "kit::fx" in tree
    assert "b" in tree["kit::fx"].nodes


def test_move_space_refuses_cycle(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "kit"})
    d._cmd_create_space({"name": "fx"})
    d._cmd_move_space({"space_id": "fx", "parent_id": "kit"})
    resp = d._cmd_move_space({"space_id": "kit", "parent_id": "kit::fx"})
    assert resp["status"] == "error"


def test_place_space_adds_a_second_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"

    resp = d._cmd_place_space({"stem": "kit"})
    assert resp["status"] == "ok", resp
    name = resp["name"]
    assert name != "kit"
    assert name in d.spaces
    assert d.spaces[name].stem == "kit"
    # A fresh placement starts pinned/paused, like a newly created space.
    assert d.spaces[name].anchored is True
    # Both placements have their own live nodes from the shared file.
    assert "kit::a" in d.space.nodes
    assert f"{name}::a" in d.space.nodes
    # The file is shared.
    assert d.spaces[name].path == d.spaces["kit"].path


def test_delete_space_file_removes_file_and_placements(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    other = d._cmd_place_space({"stem": "kit"})["name"]
    assert "kit::a" in d.space.nodes
    assert f"{other}::a" in d.space.nodes

    # The side-view listing carries the per-file node/child counts.
    listing = d._cmd_list_space_files({})
    entry = next(f for f in listing["files"] if f["stem"] == "kit")
    assert entry["node_count"] == 1
    assert entry["children"] == []

    resp = d._cmd_delete_space_file({"stem": "kit"})
    assert resp["status"] == "ok", resp
    assert not os.path.exists(os.path.join(pdir, "kit.json"))
    assert "kit" not in d.spaces and other not in d.spaces
    assert "kit::a" not in d.space.nodes
    assert f"{other}::a" not in d.space.nodes


def test_delete_space_file_refuses_readonly(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    assert d._cmd_create_space({"name": "kit"})["status"] == "ok"
    # The file's directory is read-only from the daemon's point of view.
    d.space_dirs = [(pdir, False)]
    resp = d._cmd_delete_space_file({"stem": "kit"})
    assert resp["status"] == "error"
    assert os.path.exists(os.path.join(pdir, "kit.json"))


def test_space_files_in_folders_are_listed_and_deleted(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    sub = os.path.join(pdir, "fx")
    os.makedirs(sub)
    kit = spaces.Space(
        id="kit", parent="", label="Kit", color="#112233",
        mode="read-write", path=os.path.join(sub, "kit.json"), writable=True,
        config={"nodes": {"a": {"type": "regex_input",
                                "params": {"x": 0, "y": 0, "pattern": ".*"}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(os.path.join(sub, "kit.json"), kit)

    listing = d._cmd_list_space_files({})
    entry = next(f for f in listing["files"] if f["stem"] == "kit")
    assert entry["folder"] == "fx"
    assert entry["node_count"] == 1
    # Lookup is recursive, so a nested file can still be placed/deleted by
    # stem.
    path, writable = d._space_file_path("kit")
    assert path == os.path.join(sub, "kit.json") and writable

    resp = d._cmd_delete_space_file({"stem": "kit"})
    assert resp["status"] == "ok", resp
    assert not os.path.exists(os.path.join(sub, "kit.json"))
    # The now-empty sub-folder is pruned.
    assert not os.path.exists(sub)
    assert os.path.isdir(pdir)


def test_list_space_files_and_autoload_toggle(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    assert d._cmd_create_space({"name": "kit"})["status"] == "ok"

    listing = d._cmd_list_space_files({})
    assert listing["status"] == "ok"
    stems = {f["stem"]: f for f in listing["files"]}
    assert "kit" in stems
    assert stems["kit"]["auto_load"] is False

    assert d._cmd_set_space_file_autoload(
        {"stem": "kit", "enabled": True}
    )["status"] == "ok"
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["auto_load"] is True
    listing = d._cmd_list_space_files({})
    assert next(f for f in listing["files"] if f["stem"] == "kit")["auto_load"] is True


def test_a_blueprint_change_reaches_its_instances_on_reload(tmp_path):
    """A change to the *blueprint* (edit mode writes the file) is what other
    placements follow - on reload, not by leaking live.  A local change in one
    placement is that placement's own (see the divergence tests below)."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_space({"stem": "kit"})["name"]
    assert "kit::a" in d.space.nodes and f"{other}::a" in d.space.nodes

    # A node added to the blueprint in edit mode is written to the file...
    d._cmd_set_space_edit_mode({"space_id": "kit", "enabled": True})
    d.handle_command({"command": "add_node", "node_type": "regex_output",
                      "node_id": "kit::b", "config": {"pattern": ".*"}})
    d._write_spaces()
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert "b" in raw["config"]["nodes"]

    # ...and the other placement picks it up when the tree is reloaded.
    d.spaces = d._load_spaces_tree()
    assert "b" in d.spaces[other].nodes


def test_a_local_move_stays_in_its_placement(tmp_path):
    """Two instances of one blueprint diverge: a node moved in one keeps the
    blueprint's position in the other, and the move comes back after a reload
    (it is that placement's diff)."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*", "x": 10, "y": 20}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_space({"stem": "kit"})["name"]

    moved = d.space.nodes["kit::a"]
    sibling = d.space.nodes[f"{other}::a"]
    moved.x += 100
    moved.y += 40
    d._write_spaces()

    # The sibling is untouched (no fan-out, no rebuild)...
    assert d.space.nodes[f"{other}::a"] is sibling
    off_o = d._space_origin(d.spaces, other)
    assert abs((sibling.x - off_o[0]) - 10) < 1e-6
    # ...the move is recorded as *this* placement's diff...
    assert "kit" in d._placement_diffs
    assert d._placement_diffs["kit"]["nodes"]["a"]["params"]["x"] == 110.0
    # ...and the blueprint file is untouched.
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["config"]["nodes"]["a"]["params"]["x"] == 10

    # The diff rode into the session file, and a reload replays it.
    stored = json.load(open(os.path.join(os.path.dirname(root), "placement_diffs.json")))
    assert stored["placements"]["kit"]["nodes"]["a"]["params"]["x"] == 110.0
    d._install_spaces(d._load_spaces_tree())
    config = d._flatten_spaces(d.spaces)
    assert config["nodes"]["kit::a"]["params"]["x"] == 110.0
    assert config["nodes"][f"{other}::a"]["params"]["x"] != 110.0


def test_a_local_parameter_change_is_not_written_to_the_blueprint(tmp_path):
    """The volume-slider case: turning a control in one instance keeps it in
    that instance; the file and the other instance are unaffected."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig"}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_space({"stem": "kit"})["name"]

    d.space.nodes["kit::a"].pattern = "local"
    d._write_spaces()

    assert d.space.nodes[f"{other}::a"].pattern == "orig"
    assert d._placement_diffs["kit"]["nodes"]["a"]["params"]["pattern"] == "local"
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["config"]["nodes"]["a"]["params"]["pattern"] == "orig"


def test_remove_space_placement_keeps_file(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_space({"stem": "kit"})["name"]

    resp = d._cmd_remove_space_placement({"space_id": other})
    assert resp["status"] == "ok", resp
    assert other not in d.spaces
    assert f"{other}::a" not in d.space.nodes
    # The file and the other placement survive.
    assert os.path.isfile(os.path.join(pdir, "kit.json"))
    assert "kit" in d.spaces and "kit::a" in d.space.nodes


def test_moving_a_placement_does_not_rebuild_siblings(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*", "x": 10, "y": 20}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_space({"stem": "kit"})["name"]

    a_obj = d.space.nodes["kit::a"]
    b_obj = d.space.nodes[f"{other}::a"]
    d._cmd_set_space_layout({"space_id": "kit", "x": 500, "y": 100})
    d._write_spaces()
    # Live nodes untouched (no rebuild), only positions shifted.
    assert d.space.nodes["kit::a"] is a_obj
    assert d.space.nodes[f"{other}::a"] is b_obj
    assert a_obj.x == 510


def test_placements_keep_their_own_positions(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    other = d._cmd_place_space({"stem": "kit", "x": 42, "y": 24})["name"]

    d._cmd_set_space_layout({"space_id": "kit", "x": 500, "y": 100})
    d._cmd_set_space_layout({"space_id": other, "x": -300, "y": 800})
    d._write_spaces()

    tree = d._load_spaces_tree()
    assert (tree["kit"].x, tree["kit"].y) == (500, 100)
    assert (tree[other].x, tree[other].y) == (-300, 800)


def test_place_space_honours_placement(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "kit"})
    other = d._cmd_place_space({"stem": "kit", "x": 42, "y": 24})["name"]
    assert (d.spaces[other].x, d.spaces[other].y) == (42, 24)
    d._write_spaces()
    tree = d._load_spaces_tree()
    assert (tree[other].x, tree[other].y) == (42, 24)


def test_enter_edit_mode_refreshes_params_without_recreating_nodes(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig"}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})

    obj = d.space.nodes["kit::a"]
    d.space.nodes["kit::a"].pattern = "live"
    resp = d._cmd_set_space_edit_mode({"space_id": "kit", "enabled": True})
    assert resp["status"] == "ok", resp
    # Same live node (no reload), but its params are back to the file's.
    assert d.space.nodes["kit::a"] is obj
    assert d.space.nodes["kit::a"].pattern == "orig"


def test_space_port_nodes_round_trip(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "kit"})
    for nid, ntype, name in (
        ("kit::mic", "space_in", "Mic"),
        ("kit::out", "space_out", "Out"),
        ("kit::gate", "bool_space_in", "Gate"),
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

    d._write_spaces()
    tree = d._load_spaces_tree()
    assert tree["kit"].nodes["mic"]["params"]["port_name"] == "Mic"
    assert tree["kit"].nodes["gate"]["params"]["port_name"] == "Gate"


def test_an_edit_mode_change_writes_the_blueprint(tmp_path):
    """In edit mode the change *is* the blueprint: it lands in the file, and
    the placement that made it keeps no diff of its own."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "orig", "x": 10, "y": 20}})
    d._cmd_create_space({"name": "kit", "node_ids": ["a"]})
    d._write_spaces()
    other = d._cmd_place_space({"stem": "kit"})["name"]

    d._cmd_set_space_edit_mode({"space_id": other, "enabled": True})
    d.space.nodes[f"{other}::a"].pattern = "edited"
    d._write_spaces()

    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["config"]["nodes"]["a"]["params"]["pattern"] == "edited"
    # Committed, so the placement carries no diff for it.
    assert "pattern" not in (
        d._placement_diffs.get(other, {}).get("nodes", {}).get("a", {}).get("params", {})
    )
    # Repeated writes do not clobber it back.
    d._write_spaces()
    assert json.load(open(os.path.join(pdir, "kit.json")))["config"]["nodes"]["a"]["params"]["pattern"] == "edited"


def test_create_space_nests_into_given_parent(tmp_path):
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    d.handle_command({"command": "add_node", "node_type": "regex_output",
                      "node_id": "kit::b", "config": {"pattern": ".*", "x": 5, "y": 6}})

    resp = d._cmd_create_space(
        {"name": "sub", "node_ids": ["kit::b"], "parent_id": "kit",
         "x": 5, "y": 6}
    )
    assert resp["status"] == "ok", resp
    assert resp["space_id"] == "kit::sub"
    assert "kit::sub::b" in d.space.nodes
    assert d.spaces["kit::sub"].parent == "kit"
    assert "sub" in d.spaces["kit"].children
    assert "sub" not in d.spaces[spaces.ROOT_ID].children
    # Placement is parent-relative (kit is at the origin here).
    assert (d.spaces["kit::sub"].x, d.spaces["kit::sub"].y) == (5.0, 6.0)

    d._write_spaces()
    tree = d._load_spaces_tree()
    assert "kit::sub" in tree
    assert "b" in tree["kit::sub"].nodes


def test_an_auto_load_space_in_a_read_only_dir_is_adopted(tmp_path):
    """A space file that opts in with `auto_load` and is not referenced by the
    root is spawned at the root on start-up - which is how a Nix-generated
    space dir (read-only, nobody references it) shows up at all.

    The adoption used to be a no-op: the new child reference was appended to
    the in-memory root, then a *fresh read from disk* (which cannot see it)
    was written back - so the reference was lost on both sides and the space
    never loaded, in that run or any later one."""
    rw = tmp_path / "spaces"
    rw.mkdir()
    ro = tmp_path / "store-spaces"
    ro.mkdir()
    (ro / "main.json").write_text(json.dumps({
        "type": "space", "label": "Main", "color": "#445566",
        "mode": "read-only", "auto_load": True,
        "config": {"nodes": {"paul": {"type": "gate", "params": {}}},
                   "edges": [], "spaces": [], "groups": []},
    }))
    root = tmp_path / "root.json"
    root.write_text(json.dumps({
        "type": "space", "label": "root", "color": "#000000",
        "mode": "read-write",
        "config": {"nodes": {}, "edges": [], "spaces": [], "groups": []},
    }))
    d = PatchSpaceDaemon(
        space_dirs=[(str(rw), True), (str(ro), False)],
        root_space_path=str(root),
    )
    tree = d._load_spaces_tree()
    assert "main" in tree, "the auto_load space was not adopted"
    assert set(tree["main"].config["nodes"]) == {"paul"}

    # …and the root space now references it, so the next start loads it too.
    written = json.loads(root.read_text())
    stems = {
        e if isinstance(e, str) else e.get("stem")
        for e in written["config"]["spaces"]
    }
    assert "main" in stems, written["config"]["spaces"]

    # Idempotent: a second load changes nothing and still sees the space.
    before = root.read_text()
    tree2 = d._load_spaces_tree()
    assert "main" in tree2
    assert root.read_text() == before


def test_an_unpositioned_space_is_placed_beside_the_placed_ones(tmp_path):
    """A declarative space need not carry coordinates: the module leaves them
    unset, and the daemon drops the space beside the spaces that *are* placed
    - to their right, top-aligned - instead of leaving it at the origin, which
    is typically nowhere near the graph the user arranged."""
    rw = tmp_path / "spaces"
    rw.mkdir()
    root = tmp_path / "root.json"
    root.write_text(json.dumps({
        "type": "space", "label": "root", "color": "#000000", "mode": "read-write",
        "config": {
            "nodes": {}, "edges": [], "groups": [],
            "spaces": [{"name": "kit", "stem": "kit",
                        "placement": {"x": 1000, "y": 400, "w": 300, "h": 200}}],
        },
    }))
    (rw / "kit.json").write_text(json.dumps({
        "type": "space", "label": "Kitchen", "color": "#112233", "mode": "read-write",
        "placement": {"x": 1000, "y": 400, "w": 300, "h": 200},
        "config": {"nodes": {}, "edges": [], "spaces": [], "groups": []},
    }))
    # An auto_load space with no placement of its own, in the read-only dir.
    ro = tmp_path / "store"
    ro.mkdir()
    (ro / "widgets.json").write_text(json.dumps({
        "type": "space", "label": "Widgets", "color": "#445566",
        "mode": "read-only", "auto_load": True,
        "config": {"nodes": {"w": {"type": "gate", "params": {}}},
                   "edges": [], "spaces": [], "groups": []},
    }))
    d = PatchSpaceDaemon(
        space_dirs=[(str(rw), True), (str(ro), False)],
        root_space_path=str(root),
    )
    tree = d._load_spaces_tree()
    placed = tree["kit"]
    widgets = tree["widgets"]
    assert widgets.placed, "the unpositioned space was not placed"
    assert placed.placed
    assert widgets.x >= placed.x + placed.w, (widgets.x, placed.x, placed.w)
    assert widgets.y == placed.y, (widgets.y, placed.y)
    # …and something placed by hand at the origin is left exactly there.
    assert (placed.x, placed.y) == (1000.0, 400.0)
    # The placement is remembered: it is a session value, so the next
    # autosave writes it into the root space and the space stops being
    # "unplaced" (a user drag does exactly the same thing).
    assert d._dirty, "the placement was not marked for the session autosave"


def test_a_space_with_its_own_coordinates_is_left_alone(tmp_path):
    rw = tmp_path / "spaces"
    rw.mkdir()
    root = tmp_path / "root.json"
    root.write_text(json.dumps({
        "type": "space", "label": "root", "color": "#000000", "mode": "read-write",
        "config": {"nodes": {}, "edges": [], "groups": [], "spaces": ["kit"]},
    }))
    (rw / "kit.json").write_text(json.dumps({
        "type": "space", "label": "Kitchen", "color": "#112233", "mode": "read-write",
        "placement": {"x": 7, "y": 9, "w": 300, "h": 200},
        "config": {"nodes": {}, "edges": [], "spaces": [], "groups": []},
    }))
    (rw / "other.json").write_text(json.dumps({
        "type": "space", "label": "Other", "color": "#654321", "mode": "read-write",
        "auto_load": True,
        "config": {"nodes": {}, "edges": [], "spaces": [], "groups": []},
    }))
    d = PatchSpaceDaemon(
        space_dirs=[(str(rw), True)],
        root_space_path=str(root),
    )
    tree = d._load_spaces_tree()
    assert (tree["kit"].x, tree["kit"].y) == (7.0, 9.0)
    assert tree["other"].placed and tree["other"].x > 7.0


def test_exclude_is_written_live_inside_a_space(tmp_path):
    """Kyle: "the exclude state on filter nodes isn't saved inside of spaces
    too."

    Toggling Include/Exclude is the filter's *membership* - which streams it
    lets through - rather than a knob a runtime tweak turns, so it is written
    with the layout instead of frozen with the other params.  The same toggle
    at the root level always stuck, which is what the "too" pointed at."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "filter",
                      "node_id": "f", "config": {}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["f"]})["status"] == "ok"
    kit_path = os.path.join(pdir, "kit.json")

    node = d.space.nodes["kit::f"]
    assert node.exclude is False
    # A live flip, exactly as the body's Include/Exclude switch does it.
    node.exclude = True
    d._write_spaces()

    params = json.load(open(kit_path))["config"]["nodes"]["f"]["params"]
    assert params["exclude"] is True


def test_a_replay_buffers_window_is_saved_live_inside_a_space(tmp_path):
    """Kyle: "it should persist across reboots & be stored in the node."

    The window is the node's own setting - edited from its body while the graph
    runs - so it is written with the layout rather than frozen with the params
    the snapshot holds, the same as a filter's exclude switch."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "replay_buffer",
                      "node_id": "rb", "config": {"window": 60.0}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["rb"]})["status"] == "ok"
    kit_path = os.path.join(pdir, "kit.json")

    node = d.space.nodes["kit::rb"]
    assert node.window == 60.0, "the default is a minute"
    node.window = 12.5
    d._write_spaces()

    params = json.load(open(kit_path))["config"]["nodes"]["rb"]["params"]
    assert params["window"] == 12.5
    # And it comes back: the file is what a reboot reads.
    assert d._export_node_params("kit::rb", node)["params"]["window"] == 12.5


def test_an_old_spelling_space_file_loads_and_resaves_under_the_new_one(tmp_path):
    """S0 data migration, end to end: a space file as the pre-rename daemon
    wrote it - `type: "panel"`, the child list under `panels`, the input port
    under `panel_in` - loads unchanged, and the first save writes it back
    under the new spelling (`type: "space"`, `spaces`, `space_in`).

    The node type id is a persistent contract, so an old file must keep
    loading *and* re-export canonically; the child key and the file marker are
    the file layer's half of the same rename."""
    d, root, pdir = _daemon(tmp_path)
    with open(root, "w") as f:
        json.dump({
            "type": "panel",
            "mode": "read-write",
            "label": "root",
            "color": "#ffffff",
            "config": {"nodes": {}, "edges": [], "panels": ["kit"],
                       "groups": []},
        }, f)
    with open(os.path.join(pdir, "kit.json"), "w") as f:
        json.dump({
            "type": "panel",
            "mode": "read-write",
            "label": "Kitchen",
            "color": "#112233",
            "placement": {"x": 100, "y": 50, "w": 300, "h": 200,
                          "anchored": False},
            "config": {
                "nodes": {"pin": {"type": "panel_in",
                                  "params": {"x": 10, "y": 20}}},
                "edges": [],
                "panels": [],
                "groups": [],
            },
        }, f)

    d.spaces = d._load_spaces_tree()
    # Loaded as a space tree - the old marker was not mistaken for a bare
    # session cache (which would have rewritten the root empty).
    assert set(d.spaces) == {"", "kit"}
    assert d.spaces[""].children == ["kit"]
    assert list(d.spaces["kit"].nodes) == ["pin"]

    d._load_session(d._flatten_spaces(d.spaces), declarative=True)
    # The old port id resolved through the registry's legacy alias to the
    # canonical class (a persistent contract: type ids are read, never
    # rewritten in place).
    assert isinstance(d.space.nodes["kit::pin"], SpaceInNode)
    d._write_spaces()

    root_raw = json.load(open(root))
    assert root_raw["type"] == "live"
    # The child reference is re-written from the built tree: the placement it
    # carries (geometry on the reference, since the file is shared) comes back
    # as a dict, the reference by stem.
    assert [spaces.child_name(c) for c in root_raw["config"]["spaces"]] == ["kit"]
    assert [spaces.child_stem(c) for c in root_raw["config"]["spaces"]] == ["kit"]
    assert "panels" not in root_raw["config"]

    kit_raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert kit_raw["type"] == "live"
    assert "panels" not in kit_raw["config"]
    assert kit_raw["config"]["nodes"]["pin"]["type"] == "space_in"
    # The placement survived the round trip (space-relative, as stored).
    assert (kit_raw["placement"]["x"], kit_raw["placement"]["y"]) == (100.0, 50.0)
    assert kit_raw["config"]["nodes"]["pin"]["params"]["x"] == 10.0


def test_the_pre_rename_dir_flag_still_works():
    """The daemon accepts `--panel-dir` and `--root-panel` (the pre-rename
    spellings) onto the same destinations as `--space-dir`/`--root-space`.

    A user service or a launcher written before the rename passes the old
    flag; it has to keep working, and the environment keeps its old name too
    (main.DEFAULT_SPACE_DIR reads both).  The aliases go away in S1."""
    from central import main as main_mod

    parser = main_mod.build_parser()
    args = parser.parse_args([
        "--panel-dir", str(Path("/tmp/old") / "spaces:ro"),
        "--panel-dir", "/tmp/new",
        "--root-panel", "/tmp/old_root.json",
    ])
    assert main_mod.parse_space_dirs(args.space_dir) == [
        ("/tmp/old/spaces", False), ("/tmp/new", True)
    ]
    assert args.root_space == "/tmp/old_root.json"

    # The new spelling lands in the same place (one parser, one destination).
    args = parser.parse_args(["--space-dir", "/tmp/new:ro"])
    assert main_mod.parse_space_dirs(args.space_dir) == [("/tmp/new", False)]
    assert main_mod.parse_space_dirs(None) is None


def test_the_pre_rename_command_names_are_gone(tmp_path, caplog):
    """S1 removed the protocol's one-release window for the S0 rename: the
    command names *and* the argument keys are no longer accepted (they fall
    through to an unknown command / are ignored), and no deprecation line is
    logged.  The *data* aliases (old files, the old directory, the old flags
    and the old type ids) stay - those are data, not protocol."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()

    with caplog.at_level("WARNING"):
        listing = d.handle_command({"command": "list_panels"})
    assert listing["status"] == "error"
    assert "Unknown command" in listing["message"]
    assert "deprecation" not in caplog.text

    caplog.clear()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": ".*"}})
    with caplog.at_level("WARNING"):
        # The old command name is not a command any more...
        resp = d.handle_command({"command": "create_panel", "name": "Kitchen",
                                 "panel_id": "", "node_ids": ["a"]})
    assert resp["status"] == "error", resp
    assert "Unknown command" in resp["message"]
    assert "deprecation" not in caplog.text
    # ...and the old argument key is not honoured either: `move_nodes` with
    # only `panel_id` names no space, so nothing moves.
    caplog.clear()
    with caplog.at_level("WARNING"):
        moved = d.handle_command({"command": "move_nodes", "panel_id": "",
                                  "node_ids": ["a"]})
    assert moved["status"] == "ok", moved
    assert moved["moved"] == [], moved          # the old key was ignored
    assert "deprecation" not in caplog.text


# ---------------------------------------------------------------------------
# S1: space type (template/live), permanence, the face
# ---------------------------------------------------------------------------


def test_a_templates_interior_is_not_spawned(tmp_path):
    """A template is a definition: loading it brings up no module instance,
    no live node and no placement - and a node added inside one is refused
    rather than silently spawned and then lost."""
    d, root, pdir = _daemon(tmp_path)
    template = spaces.Space(
        id="tmpl", parent="", label="Filter", color="@blue",
        type=spaces.TEMPLATE,
        path=os.path.join(pdir, "tmpl.json"), writable=True, stem="tmpl",
        config={"nodes": {"in": {"type": "space_in",
                                 "params": {"port_name": "In", "x": 5, "y": 5}},
                          "fx": {"type": "regex_input",
                                 "params": {"pattern": "x", "x": 5, "y": 60}}},
                "edges": [{"from": "in", "to": "fx"}],
                "spaces": [], "groups": []},
    )
    spaces.write_file(os.path.join(pdir, "tmpl.json"), template)
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode="read-write",
        writable=True,
        config={"nodes": {}, "edges": [], "spaces": ["tmpl"], "groups": []},
    ))
    d.spaces = d._load_spaces_tree()
    assert d.spaces["tmpl"].is_template
    assert d._flatten_spaces(d.spaces)["nodes"] == {}
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)
    assert d.space.nodes == {}                     # not spawned

    # The node ids still come back from the *definition*.
    assert set(d.spaces["tmpl"].nodes) == {"in", "fx"}
    # Its record carries the face derived from those definition nodes.
    record = next(s for s in d._serialize_spaces() if s["id"] == "tmpl")
    assert [e["key"] for e in record["face"]] == ["in"]
    assert record["face"][0]["kind"] == "audio"

    resp = d.handle_command({"command": "add_node", "node_type": "regex_input",
                             "node_id": "tmpl::extra",
                             "config": {"pattern": "y"}})
    assert resp["status"] == "error", resp
    assert "template" in resp["message"]
    assert "tmpl::extra" not in d.space.nodes


def test_instantiate_template_makes_a_nested_live_space(tmp_path):
    """Dropping a template in materializes it: a *nested live* space whose
    nodes are spawned and whose `template` names the source."""
    d, root, pdir = _daemon(tmp_path)
    template = spaces.Space(
        id="tmpl", parent="", label="Filter", color="@blue",
        type=spaces.TEMPLATE,
        path=os.path.join(pdir, "tmpl.json"), writable=True, stem="tmpl",
        config={"nodes": {"fx": {"type": "regex_input",
                                 "params": {"pattern": "x", "x": 5, "y": 60}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(os.path.join(pdir, "tmpl.json"), template)
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode="read-write",
        writable=True,
        config={"nodes": {}, "edges": [], "spaces": ["tmpl"], "groups": []},
    ))
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "Host"})

    resp = d.handle_command(
        {"command": "instantiate_template", "template_id": "tmpl",
         "parent_space": "Host"}
    )
    assert resp["status"] == "ok", resp
    new_id = resp["space_id"]
    # Nested in the parent, named after the template but with a file of its
    # own (the template's file is already called `tmpl`).
    assert new_id.startswith("Host::") and "tmpl" in new_id
    inst = d.spaces[new_id]
    assert inst.path != d.spaces["tmpl"].path
    assert inst.type == spaces.LIVE
    assert inst.template == "tmpl"
    assert inst.permanent is True
    # Spawned: the definition's node is live inside the new space.
    assert f"{new_id}::fx" in d.space.nodes
    assert d.space.nodes[f"{new_id}::fx"].pattern == "x"
    # The template itself is still a definition (nothing of it runs).
    assert "tmpl::fx" not in d.space.nodes

    record = next(s for s in d._serialize_spaces() if s["id"] == new_id)
    assert record["type"] == "live"
    assert record["template"] == "tmpl"

    # Round-trips: the template's file is still the definition (untouched
    # by the instantiation), and the instance is its own live space file.
    raw = json.load(open(os.path.join(pdir, "tmpl.json")))
    assert raw["type"] == "template"
    assert set(raw["config"]["nodes"]) == {"fx"}   # the definition, untouched
    inst_path = d.spaces[new_id].path
    raw = json.load(open(inst_path))
    assert raw["type"] == "live"
    assert raw["template"] == "tmpl"
    assert "fx" in raw["config"]["nodes"]

    tree = d._load_spaces_tree()
    assert tree[new_id].template == "tmpl"
    assert tree[new_id].type == spaces.LIVE

    # A live space is not a template, and a missing one is an error.
    assert d._cmd_instantiate_template(
        {"template_id": "Host", "parent_space": ""}
    )["status"] == "error"
    assert d._cmd_instantiate_template(
        {"template_id": "nope", "parent_space": ""}
    )["status"] == "error"


def test_switching_a_space_to_template_freezes_and_despawns_it(tmp_path):
    """The "Space Types" switch: flipping a live space to Template captures
    its interior as the definition and takes it out of the running graph;
    flipping back materializes it again."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "a", "config": {"pattern": "x", "x": 3, "y": 4}})
    assert d._cmd_create_space({"name": "kit", "node_ids": ["a"]})["status"] == "ok"
    assert "kit::a" in d.space.nodes

    resp = d.handle_command(
        {"command": "set_space_meta", "space_id": "kit", "type": "template"}
    )
    assert resp["status"] == "ok", resp
    assert resp["type"] == "template"
    assert "kit::a" not in d.space.nodes          # despawned
    d._write_spaces()
    raw = json.load(open(os.path.join(pdir, "kit.json")))
    assert raw["type"] == "template"
    assert raw["config"]["nodes"]["a"]["params"]["pattern"] == "x"   # kept

    resp = d.handle_command(
        {"command": "set_space_meta", "space_id": "kit", "type": "live"}
    )
    assert resp["status"] == "ok", resp
    assert "kit::a" in d.space.nodes              # materialized again
    assert d.space.nodes["kit::a"].pattern == "x"

    # The root is the session: it may not become a template.
    resp = d._cmd_set_space_meta({"space_id": "", "type": "template"})
    assert resp["status"] == "error", resp
    assert "root" in resp["message"]
    # An unknown space, and an unknown kind, are refused too.
    assert d._cmd_set_space_meta({"space_id": "nope"})["status"] == "error"
    assert d._cmd_set_space_meta(
        {"space_id": "kit", "type": "sideways"}
    )["status"] == "error"


def test_a_non_permanent_space_is_written_nowhere(tmp_path):
    """Persistence is filtered per space: a non-permanent space - and its
    nodes, edges and placement - reaches no file, while a permanent
    neighbour is fully written.  Its layout is still accepted in memory."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "Keep"})
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "Keep::k",
                      "config": {"pattern": "keep", "x": 1, "y": 1}})
    # An app/module-created space: not part of the session at all.
    resp = d._cmd_create_space({"name": "App", "permanent": False})
    assert resp["status"] == "ok", resp
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "App::a",
                      "config": {"pattern": "app", "x": 2, "y": 2}})
    # A cross-space edge: it is *owned* by the root, so it must be dropped
    # with the space its endpoint lives in.
    d.handle_command({"command": "add_edge", "from_node": "App::a",
                      "to_node": "Keep::k"})
    d._write_spaces()

    assert not os.path.exists(os.path.join(pdir, "App.json"))
    raw = json.load(open(root))
    assert [spaces.child_name(c) for c in raw["config"]["spaces"]] == ["Keep"]
    assert raw["config"]["edges"] == []
    assert raw["config"]["nodes"] == {}
    # The permanent neighbour is fully written.
    keep = json.load(open(os.path.join(pdir, "Keep.json")))
    assert keep["type"] == "live"
    assert keep["permanent"] is True
    assert set(keep["config"]["nodes"]) == {"k"}
    # And nothing of the non-permanent space reached the diff sidecar.
    diffs = json.load(open(os.path.join(tmp_path, "placement_diffs.json")))
    assert "App" not in diffs["placements"]

    # Its layout is still accepted and applied in memory (the app owns it).
    resp = d._cmd_set_space_layout({"space_id": "App", "x": 400, "y": 50})
    assert resp["status"] == "ok", resp
    assert (d.spaces["App"].x, d.spaces["App"].y) == (400.0, 50.0)
    assert "App::a" in d.space.nodes              # live while it exists
    d._write_spaces()
    assert not os.path.exists(os.path.join(pdir, "App.json"))
    assert json.load(open(root))["config"]["nodes"] == {}

    # Flipping it permanent brings it back into the session.
    assert d._cmd_set_space_meta(
        {"space_id": "App", "permanent": True}
    )["status"] == "ok"
    d._write_spaces()
    assert os.path.isfile(os.path.join(pdir, "App.json"))
    raw = json.load(open(root))
    assert sorted(spaces.child_name(c) for c in raw["config"]["spaces"]) == [
        "App", "Keep"
    ]


def test_a_non_permanent_parent_hides_its_whole_subtree(tmp_path):
    """Outermost first: a permanent space nested inside a non-permanent one
    is written to nothing either."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "App", "permanent": False})
    d._cmd_create_space({"name": "Inner", "parent_id": "App"})
    d.handle_command({"command": "add_node", "node_type": "regex_input",
                      "node_id": "App::Inner::i", "config": {"pattern": "i"}})
    assert d.spaces["App::Inner"].permanent is True     # the child itself is
    d._write_spaces()
    assert not os.path.exists(os.path.join(pdir, "App.json"))
    assert not os.path.exists(os.path.join(pdir, "Inner.json"))
    raw = json.load(open(root))
    assert raw["config"].get("spaces") or [] == []

    tree = d._load_spaces_tree()
    assert sorted(tree) == [""]                          # nothing came back


def test_set_space_meta_and_set_space_input_round_trip(tmp_path):
    """The whole S1 record survives save -> load: the space's kind, its
    permanence, color and default view, and the face input's configuration
    (stored on the port node)."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "Kitchen"})
    d.handle_command({"command": "add_node", "node_type": "space_in",
                      "node_id": "Kitchen::mic",
                      "config": {"port_name": "Mic", "label": "Mic",
                                 "x": 5, "y": 5}})
    d.handle_command({"command": "add_node", "node_type": "space_out",
                      "node_id": "Kitchen::out",
                      "config": {"port_name": "Out", "x": 5, "y": 100}})

    # Over the wire: a command carrying a `type` key of its own must still
    # be read as a command (only the module API keys off a bare `type`).
    resp = d.handle_command({
        "command": "set_space_meta", "space_id": "Kitchen", "color": "@green",
        "node_view": True, "permanent": True, "type": "live",
    })
    assert resp["status"] == "ok", resp
    resp = d.handle_command({
        "command": "set_space_input", "space_id": "Kitchen", "key": "mic",
        "mode": "value", "kind": "choice", "method": "field",
        "choices": ["a", "b"],
    })
    assert resp["status"] == "ok", resp
    assert [e for e in resp["face"] if e["key"] == "mic"] == [{
        "key": "mic", "label": "Mic", "direction": "in", "mode": "value",
        "kind": "choice", "method": "field", "choices": ["a", "b"],
    }]
    # A partial update leaves the rest of the input alone.
    assert d.handle_command(
        {"command": "set_space_input", "space_id": "Kitchen", "key": "mic",
         "method": "edge"}
    )["status"] == "ok"
    d._write_spaces()

    raw = json.load(open(os.path.join(pdir, "Kitchen.json")))
    assert raw["type"] == "live"
    assert raw["permanent"] is True
    assert raw["node_view"] is True
    assert raw["color"] == "@green"
    params = raw["config"]["nodes"]["mic"]["params"]
    assert (params["mode"], params["kind"], params["method"]) == (
        "value", "choice", "edge"
    )
    assert params["choices"] == ["a", "b"]
    # An output has no face configuration at all.
    assert "mode" not in raw["config"]["nodes"]["out"]["params"]

    # Bring it back up from the file and check the node holds it.
    tree = d._load_spaces_tree()
    assert tree["Kitchen"].color == "@green"
    assert tree["Kitchen"].node_view is True
    d._load_session(d._flatten_spaces(tree), declarative=True)
    node = d.space.nodes["Kitchen::mic"]
    assert (node.mode, node.kind, node.method) == ("value", "choice", "edge")
    assert node.choices == ["a", "b"]
    # And the record still derives the same face from the live node.
    record = next(s for s in d._serialize_spaces() if s["id"] == "Kitchen")
    assert [e["key"] for e in record["face"]] == ["mic", "out"]
    assert record["face"][1] == {
        "key": "out", "label": "Out", "direction": "out",
    }


def test_a_face_inputs_configuration_is_refused_when_it_contradicts(tmp_path):
    """Audio is node-only: it is neither an externalized value nor driven by
    a field/edge.  An unknown key, mode, kind or method is refused, and a
    refusal changes nothing."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "Kitchen"})
    d.handle_command({"command": "add_node", "node_type": "space_in",
                      "node_id": "Kitchen::mic",
                      "config": {"port_name": "Mic"}})

    def set_input(**kw):
        return d.handle_command(
            {"command": "set_space_input", "space_id": "Kitchen", "key": "mic",
             **kw}
        )

    assert set_input(mode="value", kind="audio")["status"] == "error"
    assert set_input(kind="audio", method="field")["status"] == "error"
    assert set_input(kind="audio", method="edge")["status"] == "error"
    assert set_input(mode="sideways")["status"] == "error"
    assert set_input(kind="colour")["status"] == "error"
    assert set_input(method="wizard")["status"] == "error"
    assert set_input(choices="a")["status"] == "error"        # not a list
    assert d.handle_command(
        {"command": "set_space_input", "space_id": "Kitchen", "key": "out"}
    )["status"] == "error"                                    # no such input
    assert d.handle_command(
        {"command": "set_space_input", "space_id": "nope", "key": "mic"}
    )["status"] == "error"
    # None of that changed anything.
    node = d.space.nodes["Kitchen::mic"]
    assert (node.mode, node.kind, node.method, node.choices) == (
        "node", "audio", "", None
    )
    # A number input may be an edge-driven value, and a boolean port is a
    # boolean by default.
    assert set_input(mode="value", kind="number", method="edge")["status"] == "ok"
    assert set_input(mode="value", kind="boolean")["status"] == "ok"


def test_the_space_record_carries_the_s1_fields(tmp_path):
    """What a display reads: kind, permanence, default view, the template it
    came from and the derived face."""
    d, root, pdir = _daemon(tmp_path)
    d.spaces = d._load_spaces_tree()
    d._cmd_create_space({"name": "kit", "node_view": True, "color": "@red"})
    record = next(s for s in d._serialize_spaces() if s["id"] == "kit")
    assert record["type"] == "live"
    assert record["permanent"] is True
    assert record["node_view"] is True
    assert record["color"] == "@red"
    assert record["template"] is None
    assert record["face"] == []
    root_record = next(s for s in d._serialize_spaces() if s["id"] == "")
    assert root_record["type"] == "live"
    assert root_record["permanent"] is True
