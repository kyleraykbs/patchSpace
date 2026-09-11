"""Unit tests for panels.py: namespacing, LCA edge ownership, the panel
tree loader, and migration from the legacy session/declarative shapes."""

import json
import os

import panels
from panels import (
    MODE_RO,
    MODE_RW,
    ROOT_ID,
    Panel,
    edge_owner,
    is_descendant,
    lca,
    local_of,
    make_id,
    panel_of,
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

    assert panel_of("kit::eq::band1") == "kit::eq"
    assert local_of("kit::eq::band1") == "band1"
    assert relative_id("kit", "kit::eq::band1") == "eq::band1"
    assert relative_id("", "kit::mic") == "kit::mic"
    assert relative_id("kit::eq", "kit::fx::x") == "kit::fx::x"


def test_lca_and_edge_owner():
    assert lca("kit", "kit") == "kit"
    assert lca("kit::eq", "kit::fx") == "kit"
    assert lca("kit::eq", "other") == ""
    assert lca("", "kit") == ""

    # Same panel -> that panel owns the edge.
    assert edge_owner("kit::a", "kit::b") == "kit"
    # Different sub-panels of kit -> kit owns it.
    assert edge_owner("kit::eq::a", "kit::fx::b") == "kit"
    # Different top-level panels -> root owns it.
    assert edge_owner("kit::a", "other::b") == ""
    # Root-level node to a top-level panel node -> root owns it.
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

    # kit panel with a nested eq panel and one node each.
    (rw / "kit.json").write_text(json.dumps({
        "type": "panel",
        "mode": "read-write",
        "label": "Kitchen",
        "color": "#123456",
        "placement": {"x": 100, "y": 50, "w": 500, "h": 300, "anchored": True},
        "config": {
            "nodes": {"mic": {"type": "splitter", "params": {"x": 10, "y": 20}}},
            "edges": [],
            "panels": ["eq"],
        },
    }))
    (rw / "eq.json").write_text(json.dumps({
        "type": "panel",
        "mode": "read-only",
        "label": "EQ",
        "config": {
            "nodes": {"band1": {"type": "splitter", "params": {}}},
            "edges": [{"from": "band1", "to": "band1"}],
        },
    }))
    root.write_text(json.dumps({
        "type": "panel",
        "mode": "read-write",
        "label": "root",
        "config": {"nodes": {"top": {"type": "splitter", "params": {}}},
                   "edges": [], "panels": ["kit"]},
    }))

    tree = panels.load_tree(str(root), [str(rw)], {str(rw): True})
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


def test_readonly_dir_marks_panel_unwritable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    root = tmp_path / "root.json"
    (ro / "fx.json").write_text(json.dumps({
        "type": "panel",
        "config": {"nodes": {}, "edges": [], "panels": []},
    }))
    root.write_text(json.dumps({
        "type": "panel",
        "config": {"nodes": {}, "edges": [], "panels": ["fx"]},
    }))
    tree = panels.load_tree(str(root), [str(ro)], {str(ro): False})
    assert tree["fx"].writable is False


def test_write_file_round_trips_panel(tmp_path):
    panel = Panel(
        id="kit",
        parent=ROOT_ID,
        label="Kitchen",
        color="#abcdef",
        mode=MODE_RW,
        x=10, y=20, w=300, h=200, anchored=True,
        config={"nodes": {"a": {"type": "splitter", "params": {}}},
                "edges": [], "panels": [], "groups": []},
    )
    path = tmp_path / "kit.json"
    panels.write_file(str(path), panel)
    back = panels.load_panel(str(path), "kit", ROOT_ID, True)
    assert back.label == "Kitchen"
    assert back.x == 10 and back.y == 20 and back.w == 300 and back.anchored
    assert list(back.nodes) == ["a"]


def test_migrate_legacy_session():
    raw = {
        "nodes": {"a": {"type": "splitter", "params": {}}},
        "edges": [{"from": "a", "to": "a"}],
        "groups": [{"id": "g", "label": "G", "color": "#fff", "nodes": ["a"]}],
    }
    panel = panels.migrate_legacy_session(raw)
    assert panel.id == ROOT_ID and panel.is_root
    assert list(panel.nodes) == ["a"]
    assert panel.edges == raw["edges"]
    assert panel.groups == raw["groups"]


def test_migrate_declarative_file_modes():
    rw = panels.migrate_declarative_file("kit", {
        "label": "Kitchen",
        "config": {"nodes": {"mic": {"type": "splitter", "params": {}}},
                   "edges": [], "groups": []},
    })
    assert rw.id == "kit" and rw.mode == MODE_RW

    ro = panels.migrate_declarative_file("fx", {
        "readonly": True,
        "config": {"nodes": {}, "edges": [], "groups": []},
    })
    assert ro.mode == MODE_RO
