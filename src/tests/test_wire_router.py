"""Tests for the pure-geometry wire router (no GTK needed)."""

from gui import wire_router as wr


def test_segment_blocked_detects_rect_crossing():
    assert wr.segment_blocked(0, 0, 100, 0, [(40, -10, 60, 10)], pad=0)
    assert not wr.segment_blocked(0, 0, 100, 0, [(40, 20, 60, 30)], pad=0)
    # A near miss becomes a hit once the obstacle is inflated.
    assert wr.segment_blocked(0, 0, 100, 0, [(40, 12, 60, 30)], pad=20)


def test_route_goes_around_an_obstacle_and_stays_square():
    obs = [(80, -20, 120, 20)]
    path = wr.route(0, 0, 200, 0, obs)
    assert path is not None
    assert path[0] == (0, 0)
    assert path[-1] == (200, 0)
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        # Every segment is axis-aligned (square routing).
        assert abs(ax - bx) < 1e-6 or abs(ay - by) < 1e-6
        # ...and clear of the obstacle.
        assert not wr.segment_blocked(ax, ay, bx, by, obs, pad=0)


def test_route_returns_none_when_boxed_in():
    obs = [(-10000, -10000, 10000, 10000)]
    assert wr.route(0, 0, 200, 0, obs, margin=50) is None


def test_orthogonalize_inserts_elbows_for_diagonal_hops():
    pts = wr.orthogonalize([(0, 0), (50, 30), (50, 100)])
    # The diagonal first hop gains an elbow; all segments axis-aligned.
    assert pts[0] == (0, 0)
    assert pts[-1] == (50, 100)
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        assert abs(ax - bx) < 1e-6 or abs(ay - by) < 1e-6


def test_polyline_rects_wraps_each_segment_with_spacing():
    rects = wr.polyline_rects([(0, 0), (100, 0), (100, 50)], half=5)
    assert rects == [
        (-5.0, -5.0, 105.0, 5.0),
        (95.0, -5.0, 105.0, 55.0),
    ]


def test_route_keeps_clear_of_a_prior_wire_obstacle():
    # A thin vertical obstacle standing in for an earlier wire.
    prior = wr.polyline_rects([(100, -100), (100, 100)], half=wr.SPACING)
    path = wr.route(0, 0, 200, 0, prior)
    assert path is not None
    rect = prior[0]
    for (ax, ay), (bx, by) in zip(path, path[1:]):
        assert not wr._segment_hits_rect(ax, ay, bx, by, *rect)

