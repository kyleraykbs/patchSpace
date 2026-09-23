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


# ---------------------------------------------------------------- wires ---

def _wire_widget(nodes, edges):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.patchspace_widget import PatchSpaceGraphWidget

    w = PatchSpaceGraphWidget(_Client())
    w.update_from_daemon({"nodes": nodes, "edges": edges, "panels": []})
    w._route_all_wires()
    return w


def test_a_short_jog_between_nearly_level_sockets_becomes_a_curve():
    """Two sockets almost level: the router has to step those few pixels
    somewhere, and a stubby straight step there reads as a hard little kink -
    so it is blended into a sigmoid (see _sigmoid_short_segments).  The tell
    is a diagonal hop in an otherwise strictly orthogonal path."""
    w = _wire_widget(
        {"a": {"type": "volume", "x": 0.0, "y": 100.0, "label": "a"},
         "b": {"type": "volume", "x": 400.0, "y": 108.0, "label": "b"}},
        {"a->b": {"from_node": "a", "to_node": "b",
                  "to_port": "in", "from_port": "out"}},
    )
    pts = w._wire_routes["a->b"]
    diagonals = [
        (a, b) for a, b in zip(pts, pts[1:])
        if abs(a[0] - b[0]) > 1e-6 and abs(a[1] - b[1]) > 1e-6
    ]
    assert diagonals, f"the short step stayed a hard kink: {pts}"
    # …and the curve still starts and ends on the sockets.
    assert pts[0] == w._socket_position("a", "out", 0)
    assert pts[-1] == w._socket_position("b", "in", 0)


def test_a_wires_bends_all_share_one_radius():
    """A corner beside a short segment used to round tightly while the corner
    at the other end of the same wire rounded generously; every bend of a wire
    is drawn with the same radius now - the smallest that fits them all."""
    from render_utils import CORNER_RADIUS, square_path_radius

    # Two bends: one with a long run either side, one whose outgoing segment is
    # short, so per-vertex radii would differ.
    pts = [(0.0, 0.0), (300.0, 0.0), (300.0, 12.0), (340.0, 12.0)]
    r = square_path_radius(pts)
    # The short (12px) segment caps it: (12 - MIN_STRAIGHT) / 2, not 14.
    assert 0.0 < r < CORNER_RADIUS
    # A wire with nothing but generous bends gets the full radius.
    assert square_path_radius([(0.0, 0.0), (300.0, 0.0), (300.0, 300.0)]) == CORNER_RADIUS
    # And no bends at all rounds nothing.
    assert square_path_radius([(0.0, 0.0), (300.0, 0.0)]) == 0.0


def test_wrapped_text_is_the_same_at_every_zoom():
    """Text used to wrap at draw time on the zoom-scaled Cairo context, so a
    marginal word could fit at one zoom and wrap at another (and the wrap
    could disagree with the height the node reserved).  The break is decided
    once now, in world units, and the drawn height matches the reserved one
    whatever the context is scaled by."""
    import cairo
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from render_utils import draw_text_wrapped, wrap_text_lines, wrapped_text_height

    w = _widget()
    text = "Mic Boost Noise Cancel"
    width, size = 60.0, 12

    lines, _lh = wrap_text_lines(w, text, width, size)
    assert len(lines) > 1, lines
    assert "".join(lines).replace(" ", "") == text.replace(" ", "")

    reserved = wrapped_text_height(w, text, width, size)
    for zoom in (0.5, 1.0, 3.0):
        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 400)
        cr = cairo.Context(surface)
        cr.scale(zoom, zoom)
        drawn = draw_text_wrapped(
            cr, 10.0, 20.0, text, width, size, (1.0, 1.0, 1.0), widget=w
        )
        assert drawn == reserved, (zoom, drawn, reserved)
        # And the same breaks at that zoom.
        assert wrap_text_lines(w, text, width, size)[0] == lines


def test_an_icon_is_drawn_the_same_size_at_any_zoom():
    """A symbolic icon comes back from GTK at its *natural* size (a 16px
    request can yield 14px).  Deriving the draw scale from the request instead
    of from the pixbuf made the drawn size depend on the zoom: the panel
    buttons' icons shrank as you zoomed in.  The placement is computed from
    the pixbuf alone, so the zoom cancels out."""
    gi = pytest.importorskip("gi")
    gi.require_version("GdkPixbuf", "2.0")
    from gi.repository import GdkPixbuf
    # No display needed: the placement math is pure.
    from gui.patchspace_widget import PatchSpaceGraphWidget

    for px in (8, 14, 16, 32):
        pixbuf = GdkPixbuf.Pixbuf.new(
            GdkPixbuf.Colorspace.RGB, True, 8, px, px
        )
        ox, oy, scale = PatchSpaceGraphWidget.icon_placement(
            100.0, 200.0, 22.0, pixbuf
        )
        # The pixbuf occupies exactly `size` world units, whatever it came
        # back as and whatever the zoom is.
        assert pixbuf.get_width() * scale == pytest.approx(22.0)
        assert pixbuf.get_height() * scale == pytest.approx(22.0)
        assert ox == 100.0 and oy == pytest.approx(200.0)
