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


def test_dragging_a_side_writes_that_time_and_tells_the_daemon():
    w, client = _widget()
    _rx, _ry, _rw, rh = w._clip_rect("clip1")
    _x, y, _w, _h = w._clip_rect("clip1")
    mid_y = y + rh / 2.0

    w.clip_dragging = ("start", "clip1")
    w._drag_clip(w._clip_x_at("clip1", 3.5), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(3.5)
    assert client.sent[-1] == {
        "command": "set_node_property", "node_id": "clip1",
        "property": "start", "value": pytest.approx(3.5),
    }
    # It cannot cross the other side.
    w._drag_clip(w._clip_x_at("clip1", 9.0), mid_y)
    assert w.nodes["clip1"]["start"] == pytest.approx(6.0)


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
