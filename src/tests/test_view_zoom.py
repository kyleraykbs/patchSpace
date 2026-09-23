"""Pan/zoom input: the wheel, and a two-finger pinch.

A real touchscreen can't be driven from a test, so this checks the two halves
that can go wrong without one: the zoom-about-a-point math (which the wheel
and the pinch share) and the fact that the pinch gesture is installed at all -
it was missing entirely, so pinching did nothing.

Needs GTK; skipped when unavailable."""

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
    w.pan_x, w.pan_y = 40.0, 25.0
    w.zoom = 1.0
    w.track_pointer(300.0, 200.0)
    return w


class _FakePinch:
    """Just enough of a Gtk.GestureZoom for the handler."""

    def __init__(self, centre):
        self._centre = centre

    def get_bounding_box_center(self):
        return (True, *self._centre)


def test_a_pinch_zoom_gesture_is_installed():
    w = _widget()
    names = {type(c).__name__ for c in w.observe_controllers()}
    assert "GestureZoom" in names, names


def test_zooming_about_a_point_keeps_that_point_still():
    w = _widget()
    wx, wy = w.to_world(300.0, 200.0)
    assert w.zoom_about(300.0, 200.0, 2.0)
    assert w.zoom == pytest.approx(2.0)
    # The world point under (300, 200) is the same one as before.
    assert w.to_world(300.0, 200.0) == pytest.approx((wx, wy))


def test_a_pinch_zooms_about_the_point_between_the_touches():
    w = _widget()
    w.track_pointer(0.0, 0.0)          # a pointer somewhere else entirely
    centre = (500.0, 350.0)
    wx, wy = w.to_world(*centre)
    w._on_pinch_begin(_FakePinch(centre))
    w._on_pinch_zoom(_FakePinch(centre), 1.0)   # no-op initial update
    assert w.zoom == pytest.approx(1.0)
    w._on_pinch_zoom(_FakePinch(centre), 1.5)
    assert w.zoom == pytest.approx(1.5)
    assert w.to_world(*centre) == pytest.approx((wx, wy))


def test_a_pinch_scale_is_cumulative_not_compounding():
    """::scale-changed reports the ratio since the gesture *began*; applying
    it to the live zoom each time would compound (1.5 -> 2.25 -> ...)."""
    w = _widget()
    centre = (100.0, 100.0)
    w._on_pinch_begin(_FakePinch(centre))
    w._on_pinch_zoom(_FakePinch(centre), 2.0)
    w._on_pinch_zoom(_FakePinch(centre), 2.0)
    assert w.zoom == pytest.approx(2.0)     # not 4.0


def test_a_pinch_without_touch_points_uses_the_pointer():
    """A trackpad's zoom gesture has no touches to take a centre from."""
    w = _widget()
    wx, wy = w.to_world(300.0, 200.0)
    w._on_pinch_begin(_FakePinch((0.0, 0.0)))

    class _NoCentre(_FakePinch):
        def get_bounding_box_center(self):
            return (False, 0.0, 0.0)

    w._on_pinch_zoom(_NoCentre((0.0, 0.0)), 2.0)
    assert w.zoom == pytest.approx(2.0)
    assert w.to_world(300.0, 200.0) == pytest.approx((wx, wy))
