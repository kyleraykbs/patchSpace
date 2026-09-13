"""Tests for the force-layout overlap resolver (gui/force_layout.py)."""

from gui.force_layout import ForceLayout


def _make(padding):
    # No forces at all, so the only thing that moves the rects is the
    # overlap-resolution pass.
    return ForceLayout(
        repulsion=0.0,
        spring_length=0.0,
        spring_k=0.0,
        center_k=0.0,
        flow_gap=0.0,
        flow_k=0.0,
        max_speed=0.0,
        overlap_padding=padding,
    )


def _axis_gap(lo1, hi1, lo2, hi2):
    if hi1 <= lo2:
        return lo2 - hi1
    if hi2 <= lo1:
        return lo1 - hi2
    return -1.0


def _gap_after_step(padding, pinned):
    layout = _make(padding)
    positions = {"a": (0.0, 0.0), "b": (50.0, 0.0)}
    sizes = {"a": (100.0, 40.0), "b": (100.0, 40.0)}
    layout.step(["a", "b"], positions, sizes, [], pinned)
    ax, ay = positions["a"]
    aw, ah = sizes["a"]
    bx, by = positions["b"]
    bw, bh = sizes["b"]
    gx = _axis_gap(ax, ax + aw, bx, bx + bw)
    gy = _axis_gap(ay, ay + ah, by, by + bh)
    return max(gx, gy), positions


def test_overlap_resolution_leaves_the_padding_gap():
    gap, _ = _gap_after_step(20.0, set())
    assert gap >= 20.0 - 1e-6


def test_overlap_resolution_ignores_pinned_pairs():
    _gap, positions = _gap_after_step(20.0, {"a", "b"})
    assert positions["a"] == (0.0, 0.0)
    assert positions["b"] == (50.0, 0.0)
