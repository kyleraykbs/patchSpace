"""Unit tests for spaces.py: namespacing, LCA edge ownership, the space
tree loader, and migration from the legacy session/declarative shapes."""

import json
import os

from central import spaces
from central.spaces import (
    MODE_RO,
    MODE_RW,
    ROOT_ID,
    Space,
    edge_owner,
    is_descendant,
    lca,
    local_of,
    make_id,
    space_of,
    relative_id,
    split_id,
)


def test_namespacing_round_trip():
    assert make_id("", "mic") == "mic"
    assert make_id("kit", "mic") == "kit::mic"
    assert make_id("kit::eq", "band1") == "kit::eq::band1"

    assert split_id("mic") == ("", "mic")
    assert split_id("kit::mic") == ("kit", "mic")
    assert split_id("kit::eq::band1") == ("kit::eq", "band1")

    assert space_of("kit::eq::band1") == "kit::eq"
    assert local_of("kit::eq::band1") == "band1"
    assert relative_id("kit", "kit::eq::band1") == "eq::band1"
    assert relative_id("", "kit::mic") == "kit::mic"
    assert relative_id("kit::eq", "kit::fx::x") == "kit::fx::x"


def test_lca_and_edge_owner():
    assert lca("kit", "kit") == "kit"
    assert lca("kit::eq", "kit::fx") == "kit"
    assert lca("kit::eq", "other") == ""
    assert lca("", "kit") == ""

    # Same space -> that space owns the edge.
    assert edge_owner("kit::a", "kit::b") == "kit"
    # Different sub-spaces of kit -> kit owns it.
    assert edge_owner("kit::eq::a", "kit::fx::b") == "kit"
    # Different top-level spaces -> root owns it.
    assert edge_owner("kit::a", "other::b") == ""
    # Root-level node to a top-level space node -> root owns it.
    assert edge_owner("topnode", "kit::a") == ""


def test_is_descendant():
    assert is_descendant("kit::eq", "kit")
    assert is_descendant("kit::eq", "")
    assert is_descendant("kit", "kit")
    assert not is_descendant("kit", "kit::eq")
    assert not is_descendant("other", "kit")


def test_write_read_and_load_tree(tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    root = tmp_path / "root.json"

    # kit space with a nested eq space and one node each.
    (rw / "kit.json").write_text(json.dumps({
        "type": "space",
        "mode": "read-write",
        "label": "Kitchen",
        "color": "#123456",
        "placement": {"x": 100, "y": 50, "w": 500, "h": 300, "anchored": True},
        "config": {
            "nodes": {"mic": {"type": "splitter", "params": {"x": 10, "y": 20}}},
            "edges": [],
            "spaces": ["eq"],
        },
    }))
    (rw / "eq.json").write_text(json.dumps({
        "type": "space",
        "mode": "read-only",
        "label": "EQ",
        "config": {
            "nodes": {"band1": {"type": "splitter", "params": {}}},
            "edges": [{"from": "band1", "to": "band1"}],
        },
    }))
    root.write_text(json.dumps({
        "type": "space",
        "mode": "read-write",
        "label": "root",
        "config": {"nodes": {"top": {"type": "splitter", "params": {}}},
                   "edges": [], "spaces": ["kit"]},
    }))

    tree = spaces.load_tree(str(root), [str(rw)], {str(rw): True})
    assert set(tree) == {"", "kit", "kit::eq"}

    kit = tree["kit"]
    assert kit.parent == ROOT_ID
    assert kit.label == "Kitchen"
    assert kit.mode == MODE_RW
    assert kit.writable is True
    assert kit.x == 100 and kit.anchored is True
    assert kit.children == ["eq"]

    eq = tree["kit::eq"]
    assert eq.parent == "kit"
    assert eq.mode == MODE_RO
    assert list(eq.nodes) == ["band1"]


def test_readonly_dir_marks_space_unwritable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    root = tmp_path / "root.json"
    (ro / "fx.json").write_text(json.dumps({
        "type": "space",
        "config": {"nodes": {}, "edges": [], "spaces": []},
    }))
    root.write_text(json.dumps({
        "type": "space",
        "config": {"nodes": {}, "edges": [], "spaces": ["fx"]},
    }))
    tree = spaces.load_tree(str(root), [str(ro)], {str(ro): False})
    assert tree["fx"].writable is False


def test_write_file_round_trips_space(tmp_path):
    space = Space(
        id="kit",
        parent=ROOT_ID,
        label="Kitchen",
        color="#abcdef",
        mode=MODE_RW,
        x=10, y=20, w=300, h=200, anchored=True,
        config={"nodes": {"a": {"type": "splitter", "params": {}}},
                "edges": [], "spaces": [], "groups": []},
    )
    path = tmp_path / "kit.json"
    spaces.write_file(str(path), space)
    back = spaces.load_space(str(path), "kit", ROOT_ID, True)
    assert back.label == "Kitchen"
    assert back.x == 10 and back.y == 20 and back.w == 300 and back.anchored
    assert list(back.nodes) == ["a"]


def test_writing_a_space_keeps_the_previous_content_as_a_backup(tmp_path):
    """A space file *is* the user's graph and every autosave overwrites it
    in place, so the previous content has to survive one write as
    ``<file>.json.bak`` - it is what recovers an interrupted reload (or any
    bad write) instead of losing the whole graph."""
    path = tmp_path / "kit.json"
    first = Space(
        id="kit", parent=ROOT_ID, label="Kitchen", color="#abcdef",
        mode=MODE_RW,
        config={"nodes": {"a": {"type": "splitter", "params": {}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(str(path), first)
    original = path.read_text()

    second = Space(
        id="kit", parent=ROOT_ID, label="Kitchen", color="#abcdef",
        mode=MODE_RW,
        config={"nodes": {"a": {"type": "splitter", "params": {}},
                          "b": {"type": "volume", "params": {}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(str(path), second)

    backup = tmp_path / "kit.json.bak"
    assert backup.exists()
    assert backup.read_text() == original
    assert path.read_text() != original
    # The .bak is not a space file: the scanner must ignore it.
    assert not backup.name.endswith(spaces.SPACE_SUFFIX)


def test_migrate_legacy_session():
    raw = {
        "nodes": {"a": {"type": "splitter", "params": {}}},
        "edges": [{"from": "a", "to": "a"}],
        "groups": [{"id": "g", "label": "G", "color": "#fff", "nodes": ["a"]}],
    }
    space = spaces.migrate_legacy_session(raw)
    assert space.id == ROOT_ID and space.is_root
    assert list(space.nodes) == ["a"]
    assert space.edges == raw["edges"]
    assert space.groups == raw["groups"]


def test_migrate_declarative_file_modes():
    rw = spaces.migrate_declarative_file("kit", {
        "label": "Kitchen",
        "config": {"nodes": {"mic": {"type": "splitter", "params": {}}},
                   "edges": [], "groups": []},
    })
    assert rw.id == "kit" and rw.mode == MODE_RW

    ro = spaces.migrate_declarative_file("fx", {
        "readonly": True,
        "config": {"nodes": {}, "edges": [], "groups": []},
    })
    assert ro.mode == MODE_RO


def test_an_old_spelling_space_file_loads_and_is_resaved_new(tmp_path):
    """S0 data migration: a file written before the rename (`type: "panel"`,
    the child list under `panels`) still loads as a space, and the first save
    rewrites it under the new spelling (`type: "space"`, `spaces`).

    The other direction matters as much: the old marker must read as *a space
    file*, not as a legacy session - otherwise a pre-rename root would be
    rewritten as an empty migrated session and the graph would be gone."""
    rw = tmp_path / "rw"
    rw.mkdir()
    root = tmp_path / "root.json"
    (rw / "kit.json").write_text(json.dumps({
        "type": "panel",
        "mode": "read-write",
        "label": "Kitchen",
        "placement": {"x": 100, "y": 50, "w": 300, "h": 200,
                      "anchored": False},
        "config": {
            "nodes": {"pin": {"type": "panel_in",
                              "params": {"x": 10, "y": 20}}},
            "edges": [],
            "panels": [],
        },
    }))
    root.write_text(json.dumps({
        "type": "panel",
        "mode": "read-write",
        "label": "root",
        "config": {"nodes": {}, "edges": [], "panels": ["kit"], "groups": []},
    }))

    tree = spaces.load_tree(str(root), [str(rw)], {str(rw): True})
    assert set(tree) == {"", "kit"}                 # the child list was read
    assert tree[""].children == ["kit"]
    assert tree["kit"].x == 100 and tree["kit"].w == 300
    assert list(tree["kit"].nodes) == ["pin"]

    # Save it back: only the new spelling is ever written.
    spaces.write_file(str(root), tree[""])
    spaces.write_file(str(rw / "kit.json"), tree["kit"])

    root_raw = json.loads(root.read_text())
    assert root_raw["type"] == spaces.TYPE_SPACE
    assert root_raw["config"]["spaces"] == ["kit"]
    assert "panels" not in root_raw["config"]

    kit_raw = json.loads((rw / "kit.json").read_text())
    assert kit_raw["type"] == spaces.TYPE_SPACE
    assert "panels" not in kit_raw["config"]
    # The port id itself is canonicalized by the daemon's node registry, not
    # by the file layer - so the file keeps what it read (see
    # test_spaces_daemon for the re-save that re-spells it).
    assert kit_raw["config"]["nodes"]["pin"]["type"] == "panel_in"

    # Both spellings in one dict: the new one wins.
    dual = {"config": {"nodes": {}, "edges": [],
                       "spaces": ["new"], "panels": ["old"]}}
    assert spaces.config_from_raw(dual)["spaces"] == ["new"]
