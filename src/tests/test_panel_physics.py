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


def _widget(anchored=True, panel_wh=(420.0, 260.0), nodes=None, panels=None):
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
    edges = {
        f"{a}->{b}": {"from_node": a, "to_node": b,
                      "to_port": "in", "from_port": "out"}
        for a, b in _chain(sorted(payload))
    }
    if panels is None:
        panels = [{
            "id": "p1", "parent": "", "label": "P1", "color": "#3584e4",
            "mode": "read-write", "x": 100.0, "y": 100.0,
            "w": panel_wh[0], "h": panel_wh[1],
            "anchored": anchored, "writable": True, "readonly": False,
        }]
    w.update_from_daemon({"nodes": payload, "edges": edges, "panels": panels})
    return w


def _chain(order):
    """Consecutive pairs of ids sharing a panel - enough wiring for the
    layout to have forces to resolve."""
    out = []
    for prev, nxt in zip(order, order[1:]):
        if prev.rsplit("::", 1)[0] == nxt.rsplit("::", 1)[0]:
            out.append((prev, nxt))
    return out


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
    """A cloud of unanchored members (what an imported panel's nodes look
    like) is settled by physics, and the wall keeps them inside the room the
    panel's placement allows - so the box the user sees stays bounded too."""
    w = _widget(anchored=True, panel_wh=(420.0, 260.0), nodes={
        "p1::a": {"type": "volume", "x": 0.0, "y": 0.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 4000.0, "y": 0.0, "label": "b"},
    })
    for _ in range(200):
        w._hierarchical_step()
    _rect = w._panel_rect_base("p1")
    assert _rect is not None
    # The members are inside the room, so the fitted box is too (plus the
    # padding it draws around them).
    room_w = 420.0 + 2 * w.PANEL_PHYSICS_GROW
    room_h = 260.0 + 2 * w.PANEL_PHYSICS_GROW
    assert _rect[2] <= room_w + 2 * w.PANEL_PADDING + 1.0, _rect
    assert _rect[3] <= room_h + 2 * w.PANEL_PADDING + 1.0, _rect


def test_two_pinned_neighbours_never_cross():
    """Neither panel may be moved by the panel pass (both pinned), so
    containment has to come from the room itself: members stop short of the
    neighbour, and the two fitted boxes can't overlap however hard physics
    pushes their contents apart."""
    w = _widget(anchored=True, nodes={
        "p1::a": {"type": "volume", "x": 150.0, "y": 300.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 400.0, "y": 300.0, "label": "b"},
        "p2::a": {"type": "volume", "x": 520.0, "y": 300.0, "label": "a"},
        "p2::b": {"type": "volume", "x": 560.0, "y": 300.0, "label": "b"},
    }, panels=[
        {"id": "p1", "parent": "", "label": "P1", "color": "#3584e4",
         "mode": "read-write", "x": 150.0, "y": 200.0, "w": 420.0, "h": 260.0,
         "anchored": True, "writable": True, "readonly": False},
        {"id": "p2", "parent": "", "label": "P2", "color": "#e5a50a",
         "mode": "read-write", "x": 560.0, "y": 200.0, "w": 420.0, "h": 260.0,
         "anchored": True, "writable": True, "readonly": False},
    ])

    for _ in range(200):
        w._hierarchical_step()

    a, b = w._panel_rect_base("p1"), w._panel_rect_base("p2")
    assert not (
        a[0] < b[0] + b[2] and b[0] < a[0] + a[2]
        and a[1] < b[1] + b[3] and b[1] < a[1] + a[3]
    ), (a, b)
    # …and every member is still inside its own panel's box.
    for nid, node in w.nodes.items():
        r = w._panel_rect_base(nid.rsplit("::", 1)[0])
        assert r[0] - 1.0 <= node["x"] <= r[0] + r[2] + 1.0, (nid, node["x"], r)
        assert r[1] - 1.0 <= node["y"] <= r[1] + r[3] + 1.0, (nid, node["y"], r)


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


def test_the_fit_is_not_capped_by_the_declared_placement():
    """The placement (w/h) is a room for the physics, not a limit on the
    drawn box: it is a value the widget never rewrites, so it is usually just
    the size the panel was created with.  Contents that legitimately stick
    out of it - a wide layout, a panel dragged away - must be enclosed, not
    clipped, or the panel under-fits its own graph."""
    w = _widget(anchored=False, panel_wh=(420.0, 260.0), nodes={
        "p1::a": {"type": "volume", "x": 0.0, "y": 0.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 2000.0, "y": 0.0, "label": "b"},
    })
    rect = w._panel_rect_base("p1")
    assert rect[2] > 420.0 + 2 * w.PANEL_PHYSICS_GROW, rect
    for nid, node in w.nodes.items():
        assert rect[0] <= node["x"], (nid, rect)
        assert node["x"] + w.node_width(nid) <= rect[0] + rect[2] + 1.0, (nid, rect)


def test_a_panel_encloses_a_child_panel_that_sits_away_from_it():
    """A parent folds its children's boxes into its own fit, and those boxes
    are in canvas coordinates (nodes carry absolute x/y; `_draw_panel_boxes`
    draws every rect raw).  A child that has moved away from a parent whose
    placement is still the default 420x260 - the reported under-fit - must
    still end up inside the parent."""
    w = _widget(anchored=True, nodes={
        "p1::a": {"type": "volume", "x": 50.0, "y": 50.0, "label": "a"},
        "p1::c::b": {"type": "volume", "x": 20.0, "y": 20.0, "label": "b"},
    }, panels=[
        {"id": "p1", "parent": "", "label": "P1", "color": "#3584e4",
         "mode": "read-write", "x": 0.0, "y": 0.0, "w": 420.0, "h": 260.0,
         "anchored": True, "writable": True, "readonly": False,
         "children": ["p1::c"]},
        {"id": "p1::c", "parent": "p1", "label": "C", "color": "#e5a50a",
         "mode": "read-write", "x": 3000.0, "y": 1500.0, "w": 320.0, "h": 320.0,
         "anchored": True, "writable": True, "readonly": False,
         "children": []},
    ])
    outer = w._panel_rect_base("p1")
    cx, cy, cw, ch = w._panel_rect_base("p1::c")
    assert outer[0] <= cx and outer[1] <= cy, (outer, (cx, cy, cw, ch))
    assert cx + cw <= outer[0] + outer[2] + 1.0, (outer, (cx, cy, cw, ch))
    assert cy + ch <= outer[1] + outer[3] + 1.0, (outer, (cx, cy, cw, ch))


def test_a_stale_placement_never_clips_a_member():
    """Panels are created with a default 320x320 placement and the widget
    never rewrites w/h, so a panel that has since grown has a placement that
    says nothing about its contents.  A member the user placed (node-
    anchored) sits where it sits; the box has to enclose it."""
    w = _widget(anchored=True, panel_wh=(320.0, 320.0), nodes={
        "p1::a": {"type": "volume", "x": 900.0, "y": 600.0, "label": "a"},
        "p1::b": {"type": "volume", "x": 20.0, "y": 20.0, "label": "b"},
    })
    w.anchored_nodes.add("p1::a")
    rect = w._panel_rect_base("p1")
    assert rect[0] + rect[2] >= 900.0 + w.node_width("p1::a") - 1.0, rect
    assert rect[1] + rect[3] >= 600.0 + w.node_height("p1::a") - 1.0, rect
