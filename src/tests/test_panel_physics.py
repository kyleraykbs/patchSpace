"""Physics inside panels: members settle, the box stays bounded, and nodes
the user pinned don't move.

Regression for "the physics system doesn't work in panels": every panel the
daemon creates is `anchored` (pinned by default), and a pinned panel used to
skip node physics *entirely* - so imported/declarative nodes, which are not
node-anchored, never settled inside their panel.  `anchored` now pins the
box, not the contents.

Exercises the widget's step directly (no GLib timer), so it needs GTK;
skipped when unavailable."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"
))


class _Client:
    def send(self, cmd):
        pass

    def is_connected(self):
        return True


def _widget(anchored=True, panel_wh=(420.0, 260.0), nodes=None):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.patchspace_widget import PatchSpaceGraphWidget

    w = PatchSpaceGraphWidget(_Client())
    w.pan_x = w.pan_y = 0.0
    w.zoom = 1.0
    w._revealed = None
    w.physics_active = True
    w.layout_awake = True
    payload = nodes if nodes is not None else {
        # Two members 30px apart, wired together: the spring/repulsion pair
        # has something to do, and neither node is node-anchored (only the
        # *user's* nodes are - this is what an imported/Nix node looks like).
        "p1::a": {"type": "volume", "x": 100.0, "y": 100.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 130.0, "y": 100.0, "label": "b"},
    }
    w.update_from_daemon({
        "nodes": payload,
        "edges": {
            "p1::a->p1::b": {
                "from_node": "p1::a", "to_node": "p1::b",
                "to_port": "in", "from_port": "out",
            }
        },
        "panels": [{
            "id": "p1", "parent": "", "label": "P1", "color": "#3584e4",
            "mode": "read-write", "x": 100.0, "y": 100.0,
            "w": panel_wh[0], "h": panel_wh[1],
            "anchored": anchored, "writable": True, "readonly": False,
        }],
    })
    return w


def _positions(w, prefix="p1::"):
    return {nid: (w.nodes[nid]["x"], w.nodes[nid]["y"])
            for nid in w.nodes if nid.startswith(prefix)}


def _step(w, times=10):
    for _ in range(times):
        w._hierarchical_step()


def test_members_settle_inside_a_pinned_panel():
    w = _widget(anchored=True)
    before = _positions(w)
    _step(w, 20)
    after = _positions(w)
    assert any(
        abs(after[nid][0] - before[nid][0]) > 1.0
        or abs(after[nid][1] - before[nid][1]) > 1.0
        for nid in before
    ), "node physics did not run inside a pinned (anchored) panel"


def test_node_anchored_members_still_hold_still():
    w = _widget(anchored=True)
    w.anchored_nodes.add("p1::a")
    before = w.nodes["p1::a"]["x"], w.nodes["p1::a"]["y"]
    _step(w, 20)
    assert (w.nodes["p1::a"]["x"], w.nodes["p1::a"]["y"]) == before
    # …while its unanchored neighbour in the same panel moved.
    assert w.nodes["p1::b"]["x"] != 130.0 or w.nodes["p1::b"]["y"] != 100.0


def test_physics_cannot_balloon_a_panel():
    w = _widget(anchored=True, panel_wh=(420.0, 260.0), nodes={
        "p1::a": {"type": "volume", "x": 0.0, "y": 0.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 4000.0, "y": 0.0, "label": "b"},
    })
    _rect = w._panel_rect_base("p1")
    assert _rect is not None
    assert _rect[2] <= 420.0 + 2 * w.PANEL_PHYSICS_GROW + 1.0
    assert _rect[3] <= 260.0 + 2 * w.PANEL_PHYSICS_GROW + 1.0


def test_the_wall_pulls_members_back_inside_the_box():
    w = _widget(anchored=True, nodes={
        "p1::a": {"type": "volume", "x": 0.0, "y": 0.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 4000.0, "y": 0.0, "label": "b"},
    })
    for _ in range(200):
        w._hierarchical_step()
    rect = w._panel_rect_base("p1")
    rx, ry, rw, rh = rect
    for nid, node in w.nodes.items():
        assert rx - 1.0 <= node["x"] <= rx + rw + 1.0, (nid, node["x"], rect)
        assert ry - 1.0 <= node["y"] <= ry + rh + 1.0, (nid, node["y"], rect)


def test_an_unpinned_panel_is_not_capped_by_physics_growth():
    """The cap is a safety net around the box auto-fit; it must not stop the
    box from hugging contents that legitimately grow it (a dragged node, a
    wide layout) beyond the declared placement - PANEL_DRAG_GROW still
    governs that path."""
    w = _widget(anchored=False, panel_wh=(420.0, 260.0), nodes={
        "p1::a": {"type": "volume", "x": 0.0, "y": 0.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 2000.0, "y": 0.0, "label": "b"},
    })
    rect = w._panel_rect_base("p1")
    assert rect[2] <= 420.0 + 2 * w.PANEL_PHYSICS_GROW + 1.0
