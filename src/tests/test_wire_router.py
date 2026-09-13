"""Tests for the pure orthogonal wire router (gui/wire_router.py)."""

from gui.wire_router import (
    _segment_hits_rect,
    route,
    segment_blocked,
    simplify,
)


def test_segment_hits_rect_basic():
    rect = (100.0, 100.0, 200.0, 200.0)
    assert _segment_hits_rect(0, 150, 300, 150, *rect) is True
    assert _segment_hits_rect(0, 20, 300, 20, *rect) is False


def test_segment_blocked_with_padding():
    rect = (100.0, 100.0, 200.0, 200.0)
    # A horizontal line just below the rect is clear, but blocked once the
    # routing padding is applied.
    assert segment_blocked(0, 205, 300, 205, [rect], pad=0.0) is False
    assert segment_blocked(0, 205, 300, 205, [rect], pad=12.0) is True


def test_simplify_drops_collinear_midpoints():
    assert simplify(
        [(0, 0), (10, 0), (20, 0), (20, 10)]
    ) == [(0, 0), (20, 0), (20, 10)]


def test_route_goes_around_an_obstacle():
    # A tall wall directly between the endpoints.
    wall = (180.0, -200.0, 220.0, 200.0)
    path = route(0.0, 0.0, 400.0, 0.0, [wall])
    assert path is not None
    # The route is grid-aligned, so its ends sit within a cell of the
    # requested points (the caller connects the exact sockets).
    assert abs(path[0][0] - 0.0) <= 40 and abs(path[0][1] - 0.0) <= 40
    assert abs(path[-1][0] - 400.0) <= 40 and abs(path[-1][1] - 0.0) <= 40
    # No segment of the returned path may cut through the obstacle.
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        assert _segment_hits_rect(
            ax, ay, bx, by, *wall
        ) is False, (ax, ay, bx, by)
    # It must actually detour (not just the straight line).
    assert len(path) > 2


def test_route_between_same_cell_is_a_short_segment():
    assert route(0.0, 0.0, 1.0, 1.0, []) == [(0.0, 0.0), (1.0, 1.0)]
