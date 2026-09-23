"""Regression: a socket whose straight outward stub has no room because a
node sits right in front of it must still leave the port sideways (via the
perpendicular "escape" jog) instead of collapsing to a zero-length stub that
the cleanup passes erase - which left the wire diving straight into the port.

Exercises the widget's routing, so it needs GTK; skipped when unavailable."""

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


def _widget():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.patchspace_widget import PatchSpaceGraphWidget
    w = PatchSpaceGraphWidget(_Client())
    w.pan_x = 0.0
    w.pan_y = 0.0
    w.zoom = 1.0
    w._revealed = None
    return w


def test_blocked_output_stub_still_leaves_sideways():
    w = _widget()
    # a's output socket has "bypass" sitting ~10px in front of it, so the
    # rightward stub cannot reach the grid-step minimum.
    nodes = {
        "a": {"type": "warp_out", "x": 100.0, "y": 100.0, "label": "a"},
        "b": {"type": "volume", "x": 100.0, "y": 400.0, "label": "b"},
        "c": {"type": "volume", "x": 290.0, "y": 80.0, "label": "bypass"},
    }
    edges = {
        "a->b": {
            "from_node": "a", "to_node": "b",
            "to_port": "in", "from_port": "out",
        }
    }
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()

    pts = w._wire_routes["a->b"]
    assert pts
    sx, sy = w._socket_position("a", "out", 0)
    assert pts[0][0] == pytest.approx(sx)
    assert pts[0][1] == pytest.approx(sy)
    # The first segment leaves the output horizontally (not straight down).
    assert abs(pts[1][1] - sy) < 1.0
    assert pts[1][0] > sx + 4.0

    from gui.wire_router import segment_blocked
    c = (290.0, 80.0, 290.0 + w.node_width("c"), 80.0 + w.node_height("c"))
    for p, q in zip(pts, pts[1:]):
        assert not segment_blocked(p[0], p[1], q[0], q[1], [c], pad=0)


def test_connected_nodes_close_together_do_not_collapse_the_stub():
    # "Bool Warp Out" with its destination "bypass" right next to its output:
    # the destination must not clamp the stub to nothing (which the cleanup
    # passes then erased, leaving a degenerate hug/loop).
    w = _widget()
    nodes = {
        "a": {"type": "warp_out", "x": 100.0, "y": 100.0, "label": "Bool Warp Out"},
        "b": {"type": "volume", "x": 290.0, "y": 80.0, "label": "bypass"},
    }
    edges = {
        "a->b": {"from_node": "a", "to_node": "b",
                 "to_port": "in", "from_port": "out"},
    }
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    pts = w._wire_routes["a->b"]
    assert pts
    sx, sy = w._socket_position("a", "out", 0)
    ex, ey = w._socket_position("b", "in", 0)
    assert pts[0] == pytest.approx((sx, sy))
    assert pts[-1] == pytest.approx((ex, ey))
    ys = [p[1] for p in pts]
    xs = [p[0] for p in pts]
    # The route stays local: it must not dive past the target row and hook
    # back (the pre-fix bug overshot ~70px above) or wander inside a node.
    assert min(ys) > min(sy, ey) - 30.0
    assert max(ys) < max(sy, ey) + 30.0
    assert max(xs) < max(sx, ex) + 30.0


def test_cached_route_is_rejected_when_a_wire_moved_too_close():
    # Wire-vs-wire spacing must be re-checked on a cached route, or a stale
    # wire is never pushed off by a neighbour that moved onto it afterwards.
    w = _widget()
    edge = {"from_node": "a", "to_node": "b", "to_port": "in", "from_port": "out"}
    points = [(0.0, 0.0), (100.0, 0.0)]
    on_top = [(-10.0, -5.0, 110.0, 5.0)]
    assert not w._route_still_valid(
        points, edge, 0.0, 0.0, 100.0, 0.0, {}, (), spacing_rects=on_top
    )
    clear = [(0.0, 50.0, 100.0, 60.0)]
    assert w._route_still_valid(
        points, edge, 0.0, 0.0, 100.0, 0.0, {}, (), spacing_rects=clear
    )


def test_shared_endpoint_wires_receive_bundle_strips():
    # A fan-out's second wire must still get a (small) keep-out strip from
    # the first - a full exclusion let bundled wires sit exactly on top of
    # each other.
    w = _widget()
    nodes = {
        "src": {"type": "volume", "x": 50.0, "y": 200.0, "label": "src"},
        "t1": {"type": "volume", "x": 500.0, "y": 80.0, "label": "t1"},
        "t2": {"type": "volume", "x": 500.0, "y": 320.0, "label": "t2"},
    }
    edges = {
        "src->t1": {"from_node": "src", "to_node": "t1",
                    "to_port": "in", "from_port": "out"},
        "src->t2": {"from_node": "src", "to_node": "t2",
                    "to_port": "in", "from_port": "out"},
    }
    captured = {}
    orig = w._wire_points

    def spy(edge, *args, **kwargs):
        captured[(edge["from_node"], edge["to_node"])] = kwargs.get(
            "extra_obstacles"
        )
        return orig(edge, *args, **kwargs)

    w._wire_points = spy
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    # The second (shared-source) edge must have been routed with some
    # keep-out from the first, not with an empty extra list.
    assert captured[("src", "t2")]


def test_congested_stack_does_not_detour_far_past_the_target():
    # Dense vertical stack with several wires fanning out/in: the router must
    # not satisfy clearance by diving far past the target row and hooking
    # back (it used to overshoot by >450px here).
    w = _widget()
    pos = [(111.0, 120.0), (111.0, 240.0), (111.0, 360.0), (111.0, 480.0),
           (665.0, 280.0), (475.0, 246.0)]
    nodes = {
        f"n{i}": {"type": "volume", "x": x, "y": y, "label": f"n{i}"}
        for i, (x, y) in enumerate(pos)
    }
    edges = {
        "n0->n4": {"from_node": "n0", "to_node": "n4",
                   "to_port": "in", "from_port": "out"},
        "n1->n5": {"from_node": "n1", "to_node": "n5",
                   "to_port": "in", "from_port": "out"},
        "n2->n5": {"from_node": "n2", "to_node": "n5",
                   "to_port": "in", "from_port": "out"},
        "n3->n4": {"from_node": "n3", "to_node": "n4",
                   "to_port": "in", "from_port": "out"},
    }
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    for eid in edges:
        pts = w._wire_routes.get(eid) or []
        if not pts:
            continue
        sy = w._socket_position(edges[eid]["from_node"], "out", 0)[1]
        ey = w._socket_position(edges[eid]["to_node"], "in", 0)[1]
        ys = [p[1] for p in pts]
        over = (max(0.0, min(sy, ey) - min(ys))
                + max(0.0, max(ys) - max(sy, ey)))
        assert over < 300.0, (eid, over, pts)


def _count_route_calls(w, fn):
    import gui.patchspace_widget as M
    calls = {"n": 0}
    orig = M.route_wire

    def counted(*args, **kwargs):
        calls["n"] += 1
        return orig(*args, **kwargs)

    M.route_wire = counted
    try:
        fn()
    finally:
        M.route_wire = orig
    return calls["n"]


def test_routing_is_skipped_when_nothing_changed():
    # Routing is the expensive part of a frame; an idle repaint (pan, zoom,
    # hover, selection) must not re-run it.
    w = _widget()
    nodes = {
        "a": {"type": "volume", "x": 50.0, "y": 50.0, "label": "a"},
        "b": {"type": "volume", "x": 500.0, "y": 400.0, "label": "b"},
    }
    edges = {"a->b": {"from_node": "a", "to_node": "b",
                      "to_port": "in", "from_port": "out"}}
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    assert _count_route_calls(w, w._route_all_wires) == 0


def test_moving_one_node_only_reroutes_nearby_wires():
    w = _widget()
    nodes = {}
    for i in range(12):
        nodes[f"n{i}"] = {"type": "volume",
                          "x": 60.0 + (i % 4) * 500.0,
                          "y": 60.0 + (i // 4) * 400.0,
                          "label": f"n{i}"}
    edges = {
        f"n{i}->n{i + 1}": {"from_node": f"n{i}", "to_node": f"n{i + 1}",
                            "to_port": "in", "from_port": "out"}
        for i in range(11)
    }
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    w.nodes["n0"]["x"] += 4.0
    total = len(edges)
    n = _count_route_calls(w, w._route_all_wires)
    assert 0 < n < total  # not every edge re-routes


def test_dehairpin_strips_a_180_reversal():
    # A route that reaches a stub tip from the socket side and then runs back
    # out along the exit leg doubles over itself; the reversal vertex must go
    # or the rounded corners draw it as a self-crossing loop.
    w = _widget()
    assert w._dehairpin(
        [(0.0, 0.0), (40.0, 0.0), (20.0, 0.0), (20.0, 50.0)]
    ) == [(0.0, 0.0), (20.0, 0.0), (20.0, 50.0)]
    # A normal L (and a straight run) are untouched.
    assert w._dehairpin([(0.0, 0.0), (100.0, 0.0), (100.0, 50.0)]) == [
        (0.0, 0.0), (100.0, 0.0), (100.0, 50.0)
    ]
    assert w._dehairpin([(0.0, 0.0), (50.0, 0.0), (100.0, 0.0)]) == [
        (0.0, 0.0), (50.0, 0.0), (100.0, 0.0)
    ]


def test_dehairpin_strips_a_collinear_spike():
    w = _widget()
    # A long retrace down the same line is redundant however long it is.
    assert w._dehairpin(
        [(0.0, 0.0), (0.0, 100.0), (0.0, 50.0), (100.0, 50.0)]
    ) == [(0.0, 0.0), (0.0, 50.0), (100.0, 50.0)]
    # A short non-collinear hook becomes an orthogonal elbow, not a diagonal.
    pts = w._dehairpin(
        [(0.0, 0.0), (0.0, -50.0), (-10.0, -45.0), (50.0, -45.0)]
    )
    assert pts == [(0.0, 0.0), (0.0, -45.0), (50.0, -45.0)]


def test_tight_span_stubs_meet_instead_of_overshooting():
    # Two connected nodes closer than two stubs: each side's exit must be
    # capped at half the span (`want`), so the legs meet/leave a gap rather
    # than overshooting each other (which hooked the wire).
    w = _widget()
    nodes = {
        "a": {"type": "volume", "x": 0.0, "y": 0.0, "label": "a"},
        "b": {"type": "volume", "x": 150.0, "y": 150.0, "label": "b"},
    }
    edges = {"a->b": {"from_node": "a", "to_node": "b",
                      "to_port": "in", "from_port": "out"}}
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    pts = w._wire_routes["a->b"]
    assert pts
    sx, _sy = w._socket_position("a", "out", 0)
    ex, _ey = w._socket_position("b", "in", 0)
    want = min(44.0, abs(ex - sx) * 0.5)
    xs = [p[0] for p in pts]
    # A small margin for the rounded/elbow cleanup passes.
    assert min(xs) >= min(sx, ex) - want - 5.0
    assert max(xs) <= max(sx, ex) + want + 5.0


def test_stubbed_fallback_always_exits_right_and_enters_left():
    # Even when the target sits behind the source, the last-resort Z must
    # leave the output rightwards and enter the input from the left - never
    # cut back through the source/target node.
    w = _widget()
    same_row = w._stubbed_fallback(200.0, 100.0, 50.0, 100.0)
    assert same_row[1][0] > same_row[0][0]
    assert same_row[-1][0] > same_row[-2][0]
    other_row = w._stubbed_fallback(200.0, 100.0, 50.0, 300.0)
    assert other_row[1][0] > other_row[0][0]
    assert other_row[-1][0] > other_row[-2][0]


def test_removed_edge_retracts_instead_of_blinking_out():
    w = _widget()
    nodes = {
        "a": {"type": "volume", "x": 50.0, "y": 50.0, "label": "a"},
        "b": {"type": "volume", "x": 500.0, "y": 400.0, "label": "b"},
    }
    edges = {"a->b": {"from_node": "a", "to_node": "b",
                      "to_port": "in", "from_port": "out"}}
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    assert w._wire_routes.get("a->b")
    # The poll no longer reports the edge: it must leave a retract ghost with
    # the last drawn path rather than vanish.
    w.update_from_daemon({"nodes": nodes, "edges": {}, "panels": []})
    assert "a->b" not in w.edges
    ghost = w._edge_ghosts.get("a->b")
    assert ghost is not None and ghost["points"]


def test_close_pair_wire_jogs_in_the_gap_not_on_a_border():
    """Two sockets close enough that the stubs are dropped must still jog
    *between* the nodes.

    With the endpoints that close the A* grid collapses to one cell and there
    are two equal-cost Ls; either puts its perpendicular run on an endpoint's
    border (x == the source's or the target's socket x), where the node -
    painted after the wires - covers it.  The wire then reads as "stops at
    the box" instead of entering the socket, and which L the heap tie-break
    returns can flip on any re-route.  The route must instead cross the gap.
    """
    w = _widget()
    # 30px between the sockets, 47px of vertical offset: exactly the case the
    # old code reduced to one hidden L.
    nodes = {
        "btn": {"type": "button", "x": 40.0, "y": 120.0, "label": "Button"},
        "fx": {"type": "sound_effect", "x": 250.0, "y": 160.0, "label": "FX"},
    }
    edges = {"btn->fx": {"from_node": "btn", "to_node": "fx",
                         "to_port": "in", "from_port": "out"}}
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()

    pts = w._wire_routes["btn->fx"]
    x1, y1 = w._socket_position("btn", "out", 0)
    x2, y2 = w._socket_position("fx", "in", 0)
    assert abs(pts[0][0] - x1) < 0.5 and abs(pts[0][1] - y1) < 0.5
    assert abs(pts[-1][0] - x2) < 0.5 and abs(pts[-1][1] - y2) < 0.5

    # The jog has to happen *in the open gap*: some point of the route must
    # sit strictly between the two socket columns, at a height strictly
    # between the two socket rows.  Either L (whatever the tie-break picks)
    # has no such point - its perpendicular run is on a border - so this is
    # the property that separates "enters the socket" from "stops at the box".
    lo, hi = min(x1, x2), max(x1, x2)
    assert any(
        lo + 0.5 < px < hi - 0.5 and y1 + 0.5 < py < y2 - 0.5
        for px, py in pts
    ), pts
