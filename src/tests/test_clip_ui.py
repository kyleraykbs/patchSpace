"""The Clip node's timeline: picking the sides, and what dragging does.

The timeline is the node's whole point - a waveform you select a range on -
so the parts that can silently go wrong are the time/pixel mapping (the
selection must line up with the waveform it is drawn over), the side
hit-tests, and the two gestures: dragging a side narrows the range, dragging
the waveform pans the view, and the wheel zooms about the pointer.

Needs GTK; skipped when unavailable."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"
))


class _Client:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)

    def is_connected(self):
        return True


def _widget(duration=10.0, start=2.0, end=6.0):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.patchspace_widget import PatchSpaceGraphWidget

    client = _Client()
    w = PatchSpaceGraphWidget(client)
    w.physics_active = False
    w.layout_awake = False
    w.update_from_daemon({
        "nodes": {"clip1": {
            "id": "clip1", "type": "clip", "label": "Clip", "x": 0.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Clip", "description": "",
            "start": start, "end": end, "duration": duration,
            "source_start": 0.0, "source_path": "/sounds/demo.wav",
        }},
        "edges": {}, "panels": [], "groups": [],
    })
    return w, client


def test_times_and_pixels_agree_across_the_timeline():
    w, _ = _widget()
    rx, _ry, rw, _rh = w._clip_rect("clip1")
    for seconds in (0.0, 2.5, 7.0, 10.0):
        x = w._clip_x_at("clip1", seconds)
        assert w._clip_time_at("clip1", x) == pytest.approx(seconds)


def test_the_selection_sides_are_grabbable_and_the_waveform_is_not():
    w, _ = _widget()
    _rx, _ry, _rw, rh = w._clip_rect("clip1")
    _x, y, _w, _h = w._clip_rect("clip1")
    mid_y = y + rh / 2.0
    assert w.find_clip_handle_at(w._clip_x_at("clip1", 2.0), mid_y) == (
        "clip1", "start"
    )
    assert w.find_clip_handle_at(w._clip_x_at("clip1", 6.0), mid_y) == (
        "clip1", "end"
    )
    assert w.find_clip_handle_at(w._clip_x_at("clip1", 4.0), mid_y) is None
    assert w.find_clip_body_at(w._clip_x_at("clip1", 4.0), mid_y) == "clip1"


def test_a_knob_drag_moves_only_that_side():
    """A handle resizes *its* side and nothing else.  It used to start a slide,
    which dragged the other handle along with it."""
    w, client = _widget(duration=10.0, start=2.0, end=6.0)
    _x, y, _w, rh = w._clip_rect("clip1")
    mid_y = y + rh / 2.0
    kx, ky, kw, kh = w._clip_knob_rect("clip1", "start")

    w.on_drag_begin(None, kx + kw / 2.0, ky + kh / 2.0)
    assert w.clip_dragging == ("start", "clip1")
    w._drag_clip(w._clip_x_at("clip1", 3.5), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(3.5)
    # The other handle stayed exactly where it was.
    assert w.nodes["clip1"]["end"] == pytest.approx(6.0)
    assert client.sent[-1] == {
        "command": "set_node_property", "node_id": "clip1",
        "property": "start", "value": pytest.approx(3.5),
    }
    # It cannot cross the other side.
    w._drag_clip(w._clip_x_at("clip1", 9.0), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(6.0)
    assert w.nodes["clip1"]["end"] == pytest.approx(6.0)
def test_the_wheel_zooms_the_timeline_under_it_and_nothing_else():
    w, _ = _widget()
    _rx, _ry, _rw, rh = w._clip_rect("clip1")
    _x, y, rw, _h = w._clip_rect("clip1")
    mid_y = y + rh / 2.0
    cx = w._clip_x_at("clip1", 5.0)

    assert w.zoom_clip_at(cx, mid_y, 0.5) is True
    start, span = w._clip_span("clip1")
    assert span == pytest.approx(5.0)
    # The time under the pointer stayed put.
    assert w._clip_time_at("clip1", cx) == pytest.approx(5.0, abs=0.05)
    # Somewhere else the canvas keeps the wheel.
    assert w.zoom_clip_at(99999.0, 99999.0, 0.5) is False


def test_a_typed_time_understands_stamps_and_seconds():
    w, _ = _widget()
    assert w._parse_clip_time("1.5") == pytest.approx(1.5)
    assert w._parse_clip_time("0:01.5") == pytest.approx(1.5)
    assert w._parse_clip_time("1:02") == pytest.approx(62.0)
    assert w._parse_clip_time("1:02:03.5") == pytest.approx(3723.5)
    assert w._parse_clip_time("-4") == 0.0
    for junk in ("", "   ", "abc", "1:2:3:4"):
        assert w._parse_clip_time(junk) is None


def test_the_boxes_sit_side_by_side_above_the_timeline():
    w, _ = _widget()
    rx, ry, rw, _rh = w._clip_rect("clip1")
    sx, sy, sw, sh = w._clip_box_rect("clip1", "start")
    ex, ey, ew, eh = w._clip_box_rect("clip1", "end")
    # Next to each other, inside the row, above the timeline.
    assert sx + sw < ex
    assert ey == pytest.approx(sy)
    assert sy + sh <= ry
    assert ex + ew <= rx + rw
    assert w.find_clip_box_at(sx + 2, sy + 2) == ("clip1", "start")
    assert w.find_clip_box_at(ex + 2, ey + 2) == ("clip1", "end")


def test_the_knobs_sit_on_top_of_the_selection_lines():
    w, _ = _widget()
    rx, ry, rw, _rh = w._clip_rect("clip1")
    for which, seconds in (("start", 2.0), ("end", 6.0)):
        kx, ky, kw, kh = w._clip_knob_rect("clip1", which)
        # Horizontally on the line, vertically at the timeline's top edge.
        assert kx + kw / 2.0 == pytest.approx(w._clip_x_at("clip1", seconds))
        assert ky <= ry <= ky + kh
        assert rx <= kx <= rx + rw
        assert w.find_clip_knob_at(kx + kw / 2.0, ky + kh / 2.0) == (
            "clip1", which
        )


def test_dragging_a_knob_slides_the_selection():
    w, client = _widget(duration=10.0, start=2.0, end=6.0)
    _rx, _ry, rw, _rh = w._clip_rect("clip1")
    _x, y, _w, _h = w._clip_rect("clip1")
    mid_y = y + _rh / 2.0

    w.clip_dragging = ("slide", "clip1")
    w._clip_drag_origin = (w._clip_x_at("clip1", 2.0), (2.0, 6.0))
    # One second to the right: both times move, the length stays 4s.
    w._drag_clip(w._clip_x_at("clip1", 3.0), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(3.0)
    assert w.nodes["clip1"]["end"] == pytest.approx(7.0)
    assert [c["property"] for c in client.sent[-2:]] == ["start", "end"]
    # And it cannot slide off either end of the file.
    w._drag_clip(w._clip_x_at("clip1", 100.0), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(6.0)
    assert w.nodes["clip1"]["end"] == pytest.approx(10.0)


def test_dragging_the_filled_middle_moves_the_selection():
    """The selection's filled middle is the *move*: both times shift together,
    and the length is kept."""
    w, client = _widget(duration=10.0, start=2.0, end=6.0)
    _x, y, _w, rh = w._clip_rect("clip1")
    mid_y = y + rh / 2.0
    # Inside the selection, away from either side's grab band.
    w.on_drag_begin(None, w._clip_x_at("clip1", 4.0), mid_y)
    assert w.clip_dragging == ("slide", "clip1")

    w._drag_clip(w._clip_x_at("clip1", 5.0), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(3.0)
    assert w.nodes["clip1"]["end"] == pytest.approx(7.0)
    assert [c["property"] for c in client.sent[-2:]] == ["start", "end"]

    # It cannot slide off either end of the file.
    w._drag_clip(w._clip_x_at("clip1", 100.0), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(6.0)
    assert w.nodes["clip1"]["end"] == pytest.approx(10.0)
def test_a_handle_drag_is_scaled_by_the_canvas_zoom():
    """The drag offset arrives in screen pixels and the times are in world
    units, so it has to be divided by the zoom.  Testing only at zoom 1 hid
    this: the transform cancelled and the bug read as "the handle won't move"."""
    w, _ = _widget(duration=10.0, start=2.0, end=6.0)
    w.zoom = 2.5
    w.pan_x, w.pan_y = -120.0, 40.0

    # Press the *start* handle at that zoom (screen coords are world*zoom+pan).
    kx, ky, kw, kh = w._clip_knob_rect("clip1", "start")
    sx = kx * w.zoom + w.pan_x
    sy = ky * w.zoom + w.pan_y
    w.on_drag_begin(None, sx + kw * w.zoom / 2, sy + kh * w.zoom / 2)
    assert w.clip_dragging == ("start", "clip1")

    # 50 screen pixels at zoom 2.5 is 20 world units: that edge moves by exactly
    # that much time, and the other edge not at all.
    _rx, _ry, rw, _rh = w._clip_rect("clip1")
    _start, span = w._clip_span("clip1")
    w.on_drag_update(None, 50.0, 0.0)
    assert w.nodes["clip1"]["start"] - 2.0 == pytest.approx(
        20.0 / rw * span, rel=0.02
    )
    assert w.nodes["clip1"]["end"] == pytest.approx(6.0)
def test_the_wheel_zooms_in_scrolling_up_and_out_scrolling_down():
    w, _ = _widget(duration=10.0, start=2.0, end=6.0)
    _rx, _ry, _rw, rh = w._clip_rect("clip1")
    _x, y, _w, _h = w._clip_rect("clip1")
    mid_y = y + rh / 2.0
    w.track_pointer(*_to_screen(w, w._clip_x_at("clip1", 5.0), mid_y))

    wheel_up, wheel_down = -1.0, 1.0          # GTK: down is positive dy
    assert w.on_clip_scroll(None, 0.0, wheel_up) is True
    assert w._clip_span("clip1")[1] == pytest.approx(8.5)      # zoomed in
    w.on_clip_scroll(None, 0.0, wheel_down)
    w.on_clip_scroll(None, 0.0, wheel_down)
    assert w._clip_span("clip1")[1] > 8.5                      # zoomed back out
    # ...and nowhere else the wheel is left to the canvas.
    assert w.on_clip_scroll(None, 0.0, wheel_up) is True


def _to_screen(w, wx, wy):
    return (wx * w.zoom + w.pan_x, wy * w.zoom + w.pan_y)
