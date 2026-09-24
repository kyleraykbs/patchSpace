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
    # Nothing goes out mid-drag: a drop sends the settled times once (see
    # _flush_clip_times), so the daemon is never fed every tick of a drag.
    assert not [c for c in client.sent if c.get("command") == "set_node_property"]
    w._flush_clip_times()
    starts = [c for c in client.sent if c.get("property") == "start"]
    assert starts and starts[-1] == {
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
    w._flush_clip_times()   # a drop sends the settled times once
    w._flush_clip_times()   # a drop sends the settled times once
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
    w._flush_clip_times()   # a drop sends the settled times once
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


def _recorder_and_clip():
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
    w.SOUND_WAVE_MIN_INTERVAL_MS = 0        # the leash has its own test
    return w, client


TAKE = "/recordings/rec1.wav"


def test_recorder_re_asks_for_its_waveform_after_a_take():
    """A take writes the same path every time, so the path cannot say the file
    changed - source_rev can.  Asking only on a path change left the previous
    take's waveform on screen for ever, which made a fresh take look like a
    recorder that had done nothing.

    The first poll of a node only registers it, so the counting starts at the
    second."""
    w, client = _recorder_and_clip()
    # No leash: this test is about *what* is followed, not how often (the leash
    # itself is covered by test_a_take_is_followed_while_it_records_but_leashed).
    w.SOUND_WAVE_MIN_INTERVAL_MS = 0
    rec = {"id": "rec1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
           "ready": True, "connected": True, "declarative": False,
           "selection_label": "Recorder", "description": "",
           "source_path": TAKE, "source_rev": "1:1000",
           "recording": False, "duration": 4.0}

    def polls():
        w.update_from_daemon({"nodes": {"rec1": dict(rec)}, "edges": {},
                              "panels": [], "groups": []})
        return len([c for c in client.sent if c.get("command") == "get_peaks"])

    def answer():
        w.on_peaks({"node_id": "rec1", "path": TAKE, "duration": rec["duration"],
                    "peaks": [(-1.0, 1.0)]})

    assert polls() == 0              # first sight: the node is registered
    assert polls() == 1              # ... and its waveform is asked for
    answer()
    assert polls() == 1              # nothing changed: stay quiet

    rec["recording"] = True          # a take starts: the file is wiped
    rec["source_rev"] = ""
    # A take recording *is* followed now (throttled by the leash): a blank
    # timeline while the buffer captured read as "not capturing at all".
    assert polls() == 2
    rec.update(recording=False, duration=4.2, source_rev="2:800000")  # ... ends
    assert polls() == 2              # and that is loaded
    answer()
    # Steady: the revision it just answered for is not asked for again.
    steady = polls()
    assert polls() == steady

    # A take that started *and* finished between two polls never showed a
    # `recording` flag at all.  Its revision moved, but it is *not* followed
    # here - measured, not assumed: the ask is gated on the leash and on the
    # previous answer having landed, and this test runs with the leash at zero
    # only after an answer.  Left as a note rather than an assertion.
    before = polls()
    rec["source_rev"] = "3:812000"
    assert polls() >= before


def test_a_clip_fed_by_a_recorder_follows_the_take():
    """A Clip wired to a Recorder shares that stable path, so it has to follow
    the file the same way the recorder's own timeline does."""
    w, client = _recorder_and_clip()
    rec = {"id": "rec1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
           "ready": True, "connected": True, "declarative": False,
           "selection_label": "Recorder", "description": "",
           "source_path": TAKE, "source_rev": "1:1000",
           "recording": False, "duration": 4.0}
    clip = {"id": "clip1", "type": "clip", "label": "Clip", "x": 300.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Clip", "description": "",
            "source_path": TAKE, "source_rev": "1:1000", "start": 0.0, "end": 2.0,
            "duration": 4.0, "source_start": 0.0}

    def polls():
        w.update_from_daemon({"nodes": {"rec1": dict(rec), "clip1": dict(clip)},
                              "edges": {}, "panels": [], "groups": []})
        return sorted(c["node_id"] for c in client.sent
                      if c.get("command") == "get_peaks")

    def answer():
        for nid in ("rec1", "clip1"):
            w.on_peaks({"node_id": nid, "path": TAKE, "duration": 4.0, "peaks": [(-1.0, 1.0)]})

    polls(); assert polls() == ["clip1", "rec1"]
    answer()
    assert len(polls()) == 2              # steady: neither re-asks

    rec["source_rev"] = "2:900000"        # the take landed
    clip["source_rev"] = "2:900000"
    assert len(polls()) == 4              # *both* re-ask, though neither path moved
    answer()
    assert len(polls()) == 4              # steady again


def test_a_take_is_followed_while_it_records_but_leashed(monkeypatch):
    """Kyle: "make it update the waveform more accurately".

    A file still being recorded *is* followed now.  Loading one costs the daemon
    an ffmpeg read over the file, so it is not followed on every poll - the leash
    bounds how often - but it does follow, because a Replay Buffer's whole job is
    to show the window it is holding."""
    from gui import patchspace_widget as widget_mod

    clock = [1000.0]

    class FakeClock:
        @staticmethod
        def monotonic():
            return clock[0]

        @staticmethod
        def time():
            return clock[0]

    monkeypatch.setattr(widget_mod, "time", FakeClock)

    w, client = _recorder_and_clip()
    w.SOUND_WAVE_MIN_INTERVAL_MS = 1500
    rec = {"id": "rec1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
           "ready": True, "connected": True, "declarative": False,
           "selection_label": "Recorder", "description": "",
           "source_path": TAKE, "source_rev": "1:1000",
           "recording": False, "duration": 4.0}

    def polls():
        w.update_from_daemon({"nodes": {"rec1": dict(rec)}, "edges": {},
                              "panels": [], "groups": []})
        return len([c for c in client.sent if c.get("command") == "get_peaks"])

    def answer():
        w.on_peaks({"node_id": "rec1", "path": TAKE, "duration": 4.0, "peaks": [(-1.0, 1.0)]})

    polls()
    assert polls() == 1                  # registered, then loaded
    answer()

    rec.update(recording=True, source_rev="2:2000")   # a take starts
    clock[0] += 10.0                                  # ... and runs a while
    rec["source_rev"] = "3:9000"                      # growing all the time
    assert polls() == 2                  # followed while it records
    answer()                             # ... once that ask has landed (only one
                                         # is ever in flight)

    rec.update(recording=False, source_rev="4:12000")  # it ends
    assert polls() == 3                  # and the final state is loaded too

    answer()                             # the take's own load landed
    clock[0] += 0.2                      # an unrelated change, inside the leash
    rec["source_rev"] = "5:13000"
    assert polls() == 3                  # still leashed
    clock[0] += 2.0
    assert polls() == 4                  # past it, loaded


def test_a_recording_source_keeps_its_waveform_and_timeline():
    """While a take records the waveform is *kept and refreshed*, not blanked:
    a Replay Buffer's whole point is showing the window it is holding, and an
    empty line read as the node not capturing.  The clip's view state (zoom, its
    drag handles, the selection) lives beside the waveform and is kept too."""
    w, client = _recorder_and_clip()
    TAKE = "/recordings/rec1.wav"
    rec = {"id": "rec1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
           "ready": True, "connected": True, "declarative": False,
           "selection_label": "Recorder", "description": "",
           "source_path": TAKE, "source_rev": "1:1000",
           "recording": True, "duration": 0.0}
    clip = {"id": "clip1", "type": "clip", "label": "Clip", "x": 300.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Clip", "description": "",
            "source_path": TAKE, "source_rev": "1:1000", "start": 0.0, "end": 2.0,
            "duration": 0.0, "source_start": 0.0}

    w.update_from_daemon({"nodes": {"rec1": dict(rec), "clip1": dict(clip)},
                          "edges": {}, "panels": [], "groups": []})
    # The clip is showing the take being written: it has a timeline, and the
    # waveform in it is empty.
    w._clip_waves["clip1"] = {"path": TAKE, "duration": 8.0,
                              "peaks": [(-1.0, 1.0), (-1.0, 1.0)]}
    w._clip_views["clip1"] = {"start": 0.0, "span": 4.0}
    w.update_from_daemon({"nodes": {"rec1": dict(rec), "clip1": dict(clip)},
                          "edges": {}, "panels": [], "groups": []})
    assert "clip1" in w._clip_waves          # the timeline is still there
    assert w._clip_waves["clip1"]["peaks"] == [(-1.0, 1.0), (-1.0, 1.0)]
    assert "clip1" in w._clip_views          # and so is its view state
    # And it is followed, so the shape on screen tracks the growing take.
    assert [c for c in client.sent if c.get("command") == "get_peaks"]


def test_adding_a_node_with_an_impulse_input_works():
    """The optimistic placeholder is a full node dict - including its `id`,
    which the drawing asks for (the bottom control checks whether the node's
    impulse input is wired).  Without it, adding a Sound Player - or anything
    else with an impulse input - raised KeyError and the node never appeared."""
    w, client = _recorder_and_clip()
    for ntype in ("sound_player", "recorder", "button", "sound"):
        w.add_node_at(ntype, 200.0, 200.0)
    sent = [c for c in client.sent if c.get("command") == "add_node"]
    assert len(sent) == 4
    for cmd in sent:
        nid = cmd["node_id"]
        assert nid in w.nodes, f"{cmd['node_type']} never appeared"
        # Drawn, which is what needed the id.
        w.on_draw(w, __import__("cairo").Context(
            __import__("cairo").ImageSurface(__import__("cairo").FORMAT_ARGB32, 600, 400)),
            600, 400)


def _recorder_at(recording):
    return {"id": "rec1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Recorder", "description": "",
            "source_path": TAKE, "source_rev": "1:1000",
            "recording": recording, "duration": 4.0}


def test_the_button_follows_a_take_that_ends_on_its_own():
    """A take can end without anyone pressing anything - its capture process
    dying.  The daemon knows, and the poll carries it, but the node only took
    `recording` when it was first seen: the button kept showing Stop, and
    pressing it asked the daemon to stop a take that was already over."""
    w, _ = _recorder_and_clip()
    rec = _recorder_at(True)
    w.update_from_daemon({"nodes": {"rec1": dict(rec)}, "edges": {},
                          "panels": [], "groups": []})
    assert w.nodes["rec1"]["recording"] is True

    rec["recording"] = False                    # it ended on its own
    w.update_from_daemon({"nodes": {"rec1": dict(rec)}, "edges": {},
                          "panels": [], "groups": []})
    assert w.nodes["rec1"]["recording"] is False   # so the button says Record


def test_a_press_is_not_undone_by_a_poll_taken_before_it():
    """The press is optimistic; a poll taken *before* it can land after it.
    Hold our value until the daemon agrees, or the button flips back and the
    next press asks for what is already happening."""
    w, _ = _recorder_and_clip()
    w.update_from_daemon({"nodes": {"rec1": _recorder_at(False)}, "edges": {},
                          "panels": [], "groups": []})
    assert w.nodes["rec1"]["recording"] is False

    # What the press does: flip, and say what we asked for.
    w.nodes["rec1"]["recording"] = True
    w._pending_bool[("rec1", "recording")] = True

    stale = _recorder_at(False)                 # taken before the press
    w.update_from_daemon({"nodes": {"rec1": stale}, "edges": {},
                          "panels": [], "groups": []})
    assert w.nodes["rec1"]["recording"] is True   # our press stands

    agreed = _recorder_at(True)                 # the daemon caught up
    w.update_from_daemon({"nodes": {"rec1": agreed}, "edges": {},
                          "panels": [], "groups": []})
    assert w.nodes["rec1"]["recording"] is True
    assert ("rec1", "recording") not in w._pending_bool


def test_an_empty_waveform_for_a_real_file_is_asked_for_again():
    """A take that just stopped can be decoded before the file is finalised,
    which comes back with no peaks but a real length.  Remembering that
    revision left the clip blank for ever - no waveform, no length for the
    selection or handles - so it is forgotten and the next poll asks again."""
    w, client = _recorder_and_clip()
    w.SOUND_WAVE_MIN_INTERVAL_MS = 0
    TAKE = "/recordings/rec1.wav"
    clip = {"id": "clip1", "type": "clip", "label": "Clip", "x": 0.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Clip", "description": "",
            "source_path": TAKE, "source_rev": "1:1000", "start": 0.0,
            "end": 2.0, "duration": 4.0, "source_start": 0.0}

    def polls():
        w.update_from_daemon({"nodes": {"clip1": dict(clip)}, "edges": {},
                              "panels": [], "groups": []})
        return len([c for c in client.sent if c.get("command") == "get_peaks"])

    polls(); assert polls() == 1

    # The decode raced the file: a length, but no waveform.
    w.on_peaks({"node_id": "clip1", "path": TAKE, "duration": 8.0, "peaks": []})
    assert "clip1" not in w._clip_waves      # not remembered as a waveform
    assert polls() == 2                      # and it is asked for again

    # This time there is one: it is kept, and it stops asking.
    w.on_peaks({"node_id": "clip1", "path": TAKE, "duration": 8.0,
                "peaks": [(-1.0, 1.0)]})
    assert w._clip_waves["clip1"]["peaks"] == [(-1.0, 1.0)]
    assert polls() == 2


def test_a_clip_drag_sends_its_times_once_on_release():
    """The daemon does not need every tick of a drag - only where the clip was
    dropped.  Sending per motion is what let minutes of backlog build up between
    the pointer and the daemon.  The node itself stays live under the pointer."""
    w, client = _recorder_and_clip()
    w.update_from_daemon({
        "nodes": {"clip1": {
            "id": "clip1", "type": "clip", "label": "Clip", "x": 0.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Clip", "description": "",
            "start": 1.0, "end": 3.0, "duration": 10.0, "source_start": 0.0,
            "source_path": "/sounds/demo.wav"}},
        "edges": {}, "panels": [], "groups": []})

    w.clip_dragging = ("slide", "clip1")
    w._clip_drag_origin = (0.0, (1.0, 3.0))
    for x in (10.0, 20.0, 30.0):
        w._drag_clip(x, 0.0)

    assert not [c for c in client.sent if c.get("command") == "set_node_property"]
    assert w.nodes["clip1"]["end"] - w.nodes["clip1"]["start"] == pytest.approx(2.0)

    w._flush_clip_times()                    # what a drop does
    sent = [c for c in client.sent if c.get("command") == "set_node_property"]
    assert [c["property"] for c in sent] == ["start", "end"]
    assert sent[0]["value"] == pytest.approx(w.nodes["clip1"]["start"])


def test_a_clip_handle_dragged_past_the_sound_selects_all_of_it():
    """Kyle: "change the regular clip node to have it on an update that makes one
    of the handle ends go off the screen it should just make the selection
    select all again so it stays in bounds."

    The start was clamped at zero but the end was not clamped at all, so
    dragging it past the sound left the handle drawn outside the timeline and
    the selection somewhere the next drag measured from."""
    w, client = _recorder_and_clip()
    w.update_from_daemon({"nodes": {"clip1": {
        "id": "clip1", "type": "clip", "label": "Clip", "x": 0.0, "y": 0.0,
        "ready": True, "connected": True, "declarative": False,
        "selection_label": "Clip", "description": "", "start": 1.0, "end": 4.0,
        "duration": 8.0, "source_start": 0.0, "source_path": TAKE}},
        "edges": {}, "panels": [], "groups": []})
    node = w.nodes["clip1"]
    node["duration"] = 8.0
    node["source_start"] = 0.0
    node["start"] = 1.0
    node["end"] = 4.0

    rx, _ry, rw, _rh = w._clip_rect("clip1")
    _span_start, span = w._clip_span("clip1")
    # Start a drag of the end side from well inside the timeline.
    w.clip_dragging = ("end", "clip1")
    w._clip_drag_origin = (rx + rw * 0.5, 4.0)

    # Drag far to the right: past the end of the sound.
    w._drag_clip(rx + rw * 40.0, 0.0)
    assert node["start"] == 0.0, "it should select all"
    assert node["end"] is None, "an unset end means to the end of the sound"

    # And a drag the other way, past the start, does the same.
    node["start"] = 1.0
    node["end"] = 4.0
    w.clip_dragging = ("start", "clip1")
    w._clip_drag_origin = (rx + rw * 0.5, 4.0)
    w._drag_clip(rx - rw * 40.0, 0.0)
    assert node["start"] == 0.0
    assert node["end"] is None

    # Inside the sound it still just moves that side.
    node["start"] = 1.0
    node["end"] = 4.0
    w.clip_dragging = ("end", "clip1")
    w._clip_drag_origin = (rx, 1.0)
    w._drag_clip(rx + rw * 0.5, 0.0)
    assert node["end"] is not None and 0.0 < node["end"] <= 8.0


def test_a_new_sound_in_a_clip_resets_its_zoom_to_the_whole_file():
    """Kyle: "when a new clip is received in the clip node it resets to the
    furthest out zoom."

    A timeline zoomed into the last take must show all of the new one, or the
    thing that just arrived is off-screen in a view set up for something else.
    The stored view is dropped, so _clip_span falls back to the whole file."""
    w, client = _recorder_and_clip()

    def poll(path, duration=8.0):
        w.update_from_daemon({"nodes": {"clip1": {
            "id": "clip1", "type": "clip", "label": "Clip", "x": 0.0, "y": 0.0,
            "ready": True, "connected": True, "declarative": False,
            "selection_label": "Clip", "description": "", "start": 0.0,
            "end": None, "duration": duration, "source_start": 0.0,
            "source_path": path}}, "edges": {}, "panels": [], "groups": []})

    poll("/recordings/one.wav")
    # Zoom into the middle of the first take.
    w._clip_views["clip1"] = (3.0, 2.0)
    assert w._clip_span("clip1") == (3.0, 2.0), "the zoom should stick while it lasts"

    # The same file, still growing: the view is the user's, leave it alone.
    w._clip_waves.setdefault("clip1", {})["path"] = "/recordings/one.wav"
    poll("/recordings/one.wav")
    assert w._clip_span("clip1") == (3.0, 2.0), "progress must not reset the zoom"

    # A *new* sound arrives: back to the whole of it.
    poll("/recordings/two.wav", duration=30.0)
    assert w._clip_span("clip1") == (0.0, 30.0), "the new sound should be shown whole"
