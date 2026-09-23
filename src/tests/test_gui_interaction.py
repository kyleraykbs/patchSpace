"""The window's own behaviours, distinct from any one node's face.

Three things were each verified once by hand and never pinned: a drag owning
the graph (the physics used to spring nodes away from the pointer), closing the
window actually closing the app, and a Record press surviving the poll that was
already in flight when it was made.  Needs GTK; skipped when unavailable."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"
))


class _Client:
    def __init__(self, queued=None):
        self.sent = []
        self.queued = list(queued or [])

    def send(self, cmd):
        self.sent.append(cmd)

    def is_connected(self):
        return True

    def get_responses(self):
        queued, self.queued = self.queued, []
        return queued


def _recorder(recording=False, **over):
    node = {
        "id": "rec1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
        "ready": True, "connected": True, "declarative": False,
        "selection_label": "Recorder", "description": "",
        "source_path": "/recordings/rec1.wav", "source_rev": "1:1000",
        "recording": recording, "duration": 4.0,
    }
    node.update(over)
    return node


def _widget(queued=None):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.patchspace_widget import PatchSpaceGraphWidget

    client = _Client(queued)
    w = PatchSpaceGraphWidget(client)
    w.physics_active = False
    w.SOUND_WAVE_MIN_INTERVAL_MS = 0
    return w, client


def _poll(w, node):
    w.update_from_daemon({"nodes": {"rec1": dict(node)}, "edges": {},
                          "panels": [], "groups": []})


def test_a_drag_owns_the_graph():
    """While a node is being dragged the pointer is the only thing placing it.
    The force layout kept stepping underneath and sprung the node back to where
    it was picked up - "when I move around the handles it doesn't move"."""
    w, _ = _widget()
    _poll(w, _recorder())
    w.layout_awake = True

    w.dragging_node = "rec1"
    w.drag_node_starts = {"rec1": (0.0, 0.0)}
    w.nodes["rec1"]["x"], w.nodes["rec1"]["y"] = 400.0, 300.0

    for _ in range(5):
        w.on_layout_tick()

    assert (w.nodes["rec1"]["x"], w.nodes["rec1"]["y"]) == (400.0, 300.0)
    assert w.layout_awake is True          # and it did not go to sleep either


def test_the_layout_resumes_after_the_drop():
    w, _ = _widget()
    _poll(w, _recorder())
    w.layout_awake = True
    w.dragging_node = None                 # dropped
    w.drag_node_starts = {}
    w.on_layout_tick()                     # must be free to run
    assert w.layout_awake is True


def test_closing_the_window_closes_the_app():
    """Closing destroyed the window but left the app pointing at it and running,
    so a later activation presented a dead window instead of building a fresh
    one: it looked like the UI reopened itself."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    import patchspace_gui

    app = patchspace_gui.PatchSpaceApp()
    quits = []
    app.quit = lambda: quits.append(True)
    closed = []

    class Window:
        def on_close(self):
            closed.append(True)

    app.window = Window()
    app._on_close_request(app.window)

    assert closed == [True]
    assert app.window is None
    assert quits == [True]


def test_a_record_press_survives_a_poll_taken_before_it():
    """The press is optimistic and the poll in flight was taken *before* it, so
    it still says "not recording".  Accepting that flipped the button straight
    back to Record: reported as "I click record and it instantly stops"."""
    w, client = _widget()
    _poll(w, _recorder(recording=False))
    assert w.nodes["rec1"]["recording"] is False

    bx, by, bw, bh = w._record_button_rect("rec1")
    w.on_click(None, 1, bx + bw / 2.0, by + bh / 2.0)      # the real press path

    assert [c for c in client.sent if c.get("command") == "record"][-1]["recording"] is True
    assert w.nodes["rec1"]["recording"] is True

    _poll(w, _recorder(recording=False))          # taken before the press
    assert w.nodes["rec1"]["recording"] is True   # the press stands

    _poll(w, _recorder(recording=True))           # the daemon agrees
    assert w.nodes["rec1"]["recording"] is True


def test_a_take_ending_on_its_own_reaches_the_button():
    """"The stop button stops working and appears detached from the running
    recording": `recording` was only taken from the daemon when a node was first
    seen, so a take that ended on its own left the button on Stop for ever."""
    w, _ = _widget()
    _poll(w, _recorder(recording=True))
    assert w.nodes["rec1"]["recording"] is True

    _poll(w, _recorder(recording=False))          # ended on its own
    assert w.nodes["rec1"]["recording"] is False


def test_responses_coalesce_to_the_newest_state():
    """A backlog of polls is the same state repeated; applying each one costs a
    full UI update and they can only end up showing the newest.  Waveforms are
    per node (an older one is of an older file) and everything else is an event
    that keeps its order."""
    import main_window

    queued = [
        {"status": "ok", "nodes": {"a": 1}},
        {"status": "ok", "peaks": [1], "node_id": "n1"},
        {"status": "ok", "devices": ["d"]},
        {"status": "ok", "nodes": {"a": 2}},               # newer poll
        {"status": "ok", "peaks": [2], "node_id": "n1"},   # newer waveform
        {"status": "error", "message": "boom"},
        {"status": "ok", "apps": ["firefox"]},
    ]
    latest_nodes, latest_peaks, events = main_window._coalesce_responses(queued)

    assert latest_nodes == {"status": "ok", "nodes": {"a": 2}}
    assert latest_peaks == {"n1": {"status": "ok", "peaks": [2], "node_id": "n1"}}
    assert [e.get("devices") or e.get("apps") for e in events] == [["d"], ["firefox"]]


def _walk(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from _walk(child)
        child = child.get_next_sibling()


def test_panel_settings_apply_leaves_the_colour_applied():
    """"The color panel when clicking apply doesn't do anything and just
    closes."  The colour (and the auto-load flag) were applied only when the
    *name* field was non-empty, so Apply could silently discard both - the
    dialog closed and nothing had changed."""
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.patchspace_widget import PatchSpaceGraphWidget
    from color_picker import ColorPicker

    client = _Client()
    win = Gtk.Window()
    w = PatchSpaceGraphWidget(client)
    win.set_child(w)
    win.present()
    w.update_from_daemon({"nodes": {}, "edges": {}, "groups": [], "panels": [{
        "id": "P1", "label": "P1", "color": "#ff0000", "x": 0, "y": 0,
        "w": 200, "h": 150, "writable": True, "readonly": False,
        "stem": "P1", "auto_load": False}]})

    def pump(ms=120):
        loop = GLib.MainLoop()
        GLib.timeout_add(ms, lambda: (loop.quit(), False)[1])
        loop.run()

    pump()
    w.show_panel_settings_dialog("P1")
    pump()
    dialogs = [t for t in Gtk.Window.get_toplevels() if isinstance(t, Gtk.Dialog)]
    assert dialogs, "the settings dialog did not open"
    dialog = dialogs[0]
    pickers = [x for x in _walk(dialog) if isinstance(x, ColorPicker)]
    entries = [x for x in _walk(dialog) if isinstance(x, Gtk.Entry)]
    assert pickers and entries
    pickers[0].set_hex("#00ff00")
    entries[0].set_text("")                 # no name given
    dialog.response(Gtk.ResponseType.APPLY)
    pump()

    assert w.panels["P1"]["color"] == "#00ff00"
    assert w.panels["P1"]["label"] == "P1"      # keeps the name it had
    sent = [c for c in client.sent if c.get("command") == "edit_panel"]
    assert sent and sent[-1]["color"] == "#00ff00"
    # Tear the window down and let any deferred work run while the objects are
    # gone: a popup scheduled against a torn-down tree used to segfault the
    # whole process (see popup_context_menu's _show).
    win.destroy()
    pump(200)
