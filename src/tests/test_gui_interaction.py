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


def test_a_sound_dump_shows_its_two_fields_on_its_body():
    """Kyle: "Name and folder for sound dump should appear on the node itself
    as two textboxes... and just add the directory selector".  The body owns
    both rows, and the folder row carries the button."""
    from gui import node_specs

    assert node_specs.spec_for("sound_dump").control == "dump"

    w, client = _widget()
    w.update_from_daemon({
        "nodes": {"d1": {
            "id": "d1", "type": "sound_dump", "label": "Sound Dump",
            "x": 0.0, "y": 0.0, "ready": True, "connected": True,
            "declarative": False, "selection_label": "Sound Dump",
            "description": "", "folder": "~/Dumps", "name": "take 1",
            "dump_path": ""}},
        "edges": {}, "panels": [], "groups": []})

    # Two rows on the body, the folder above the name, and the button beside
    # the folder row - everything derived from one place.
    fx, fy, fw, fh = w._dump_row_rect("d1", "folder")
    nx, ny, nw, nh = w._dump_row_rect("d1", "name")
    assert fy < ny
    px, py, pw, ph = w._dump_picker_rect("d1")
    assert px > fx + fw
    assert (px, py) == (fx + fw + w.PICKER_GAP,
                        fy + (fh - w.PICKER_SIZE) / 2.0)

    assert w.find_dump_field_at(fx + 2, fy + fh / 2.0) == ("d1", "folder")
    assert w.find_dump_field_at(nx + 2, ny + nh / 2.0) == ("d1", "name")
    assert w.find_dump_picker_at(px + pw / 2.0, py + ph / 2.0) == "d1"
    assert w.find_dump_field_at(px + pw / 2.0, py + ph / 2.0) is None
    assert w.find_dump_picker_at(fx + 2, fy + fh / 2.0) is None

    # The status light sits beside the folder button, inside the node - the
    # row gives up the room for it, so it can't fall off the edge.
    w.nodes["d1"]["dump_state"] = "saving"
    light_x = px + pw + w.DUMP_LIGHT_GAP + w.DUMP_LIGHT_R
    assert light_x + w.DUMP_LIGHT_R <= w.nodes["d1"]["x"] + w.NODE_WIDTH + 0.01
    assert px >= fx + fw - 0.01                      # and it misses the row

    # It draws without error, which is what puts them on screen.
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 700, 500)
    w.on_draw(w, cairo.Context(surf), 700, 500)


def test_a_sound_dumps_face_and_rows_do_not_overlap():
    """The impulse face and the two field rows are stacked on the node's body:
    the face first, then folder, then name.  Leaving the face out of the node's
    height drew the rows on top of it (and each other)."""
    w, client = _widget()
    w.update_from_daemon({
        "nodes": {"d1": {
            "id": "d1", "type": "sound_dump", "label": "Sound Dump",
            "x": 0.0, "y": 0.0, "ready": True, "connected": True,
            "declarative": False, "selection_label": "Sound Dump",
            "description": "", "folder": "~/Dumps", "name": "take 1",
            "dump_path": ""}},
        "edges": {}, "panels": [], "groups": []})

    fx, fy, fw, fh = w._impulse_face_rect("d1")
    dx, dy, dw, dh = w._dump_row_rect("d1", "folder")
    nx, ny, nw, nh = w._dump_row_rect("d1", "name")

    assert fy + fh <= dy + 0.01, "the Save face overlaps the folder row"
    assert dy + dh <= ny + 0.01, "the folder row overlaps the name row"
    assert ny + nh <= w.nodes["d1"]["y"] + w.node_height("d1") + 0.01


def test_clicking_a_dumps_save_face_lights_it_up():
    """Kyle: "make it responsive immediately as in make the button light up when
    clicked."  The press pulses the face itself, the way a Button node does -
    it must not wait for the command's round trip (or for the encode)."""
    w, client = _widget()
    w.update_from_daemon({
        "nodes": {"d1": {
            "id": "d1", "type": "sound_dump", "label": "Sound Dump",
            "x": 0.0, "y": 0.0, "ready": True, "connected": True,
            "declarative": False, "selection_label": "Sound Dump",
            "description": "", "folder": "~/Dumps", "name": "take 1",
            "dump_path": "", "dump_state": "idle"}},
        "edges": {}, "panels": [], "groups": []})

    assert w._impulse_flash_progress("d1") is None      # not pressed yet

    fx, fy, fw, fh = w._impulse_face_rect("d1")
    w.on_click(None, 1, fx + fw / 2.0, fy + fh / 2.0)

    # Lit at once, and the command went out.
    assert "d1" in w._impulse_flash
    assert w._impulse_flash_progress("d1") is not None
    assert [c for c in client.sent if c.get("command") == "impulse"]

    # It ages out on its own (the tick does that - see _anim_tick).
    import time
    from gui import constants
    w._impulse_flash["d1"] = time.monotonic() - (
        constants.IMPULSE_FLASH_MS / 1000.0
    )
    assert w._impulse_flash_progress("d1") is None


def _clip_node(**extra):
    node = {
        "id": "c1", "type": "replay_buffer", "label": "Replay Buffer",
        "x": 0.0, "y": 0.0, "ready": True, "connected": True,
        "declarative": False, "selection_label": "Replay Buffer",
        "description": "", "window": 60.0, "clip_path": "",
        "clip_state": "idle",
    }
    node.update(extra)
    return node


def test_a_replay_buffer_shows_its_window_on_its_body():
    """Kyle: "add a clip previous node that allows me to quick clip the last N
    seconds, it should have a number box with the number of seconds it holds,
    there should be a clip button that when pressed locks in the last 60 seconds
    into its output sound"."""
    from gui import node_specs

    assert node_specs.spec_for("replay_buffer").control == "replay_buffer"

    w, client = _widget()
    w.update_from_daemon({"nodes": {"c1": _clip_node()},
                          "edges": {}, "panels": [], "groups": []})

    # One row on the body, holding the seconds, and the status light inside the
    # node beside it - the row gives up the room, so it can't fall off the edge.
    x, y, width, height = w._clip_prev_row_rect("c1")
    assert w.find_clip_field_at(x + 2, y + height / 2.0) == "c1"
    light_x = x + width + w.DUMP_LIGHT_GAP + w.DUMP_LIGHT_R
    assert light_x + w.DUMP_LIGHT_R <= w.nodes["c1"]["x"] + w.NODE_WIDTH + 0.01

    # The Clip face is *reachable*: pressing it is the only way to fire a clip,
    # since the node has no impulse output to pulse and no impulse input either.
    fx, fy, fw, fh = w._impulse_face_rect("c1")
    assert w.find_impulse_fallback_at(fx + fw / 2, fy + fh / 2) == "c1"

    # It draws without error, which is what puts the row and the Clip face on
    # screen.
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 700, 600)
    w.on_draw(w, cairo.Context(surf), 700, 600)


def test_a_replay_buffers_face_and_row_do_not_overlap():
    """The Clip face and the window row are stacked on the body: the face
    first, then the row.  The same mistake a dump made - leaving the face out of
    the node's height - drew them on top of each other."""
    w, client = _widget()
    w.update_from_daemon({"nodes": {"c1": _clip_node()},
                          "edges": {}, "panels": [], "groups": []})

    fx, fy, fw, fh = w._impulse_face_rect("c1")
    rx, ry, rw, rh = w._clip_prev_row_rect("c1")

    assert fy + fh <= ry + 0.01, "the Clip face overlaps the window row"
    assert ry + rh <= w.nodes["c1"]["y"] + w.node_height("c1") + 0.01


def _split_widget(members):
    """A Split Bundle reporting `members` as its live outputs."""
    w, client = _widget()
    ports = [f"m{i}" for i in range(len(members))]
    w.update_from_daemon({
        "nodes": {"s1": {
            "id": "s1", "type": "bundle_split", "label": "Split Bundle",
            "x": 0.0, "y": 0.0, "ready": True, "connected": True,
            "declarative": False, "selection_label": "Split Bundle",
            "description": "", "inputs": ["in"], "outputs": ports,
            "bundle_members": [
                {"port": p, "label": m} for p, m in zip(ports, members)
            ]}},
        "edges": {}, "panels": [], "groups": []})
    return w


def test_a_split_bundle_names_its_line_with_a_single_member():
    """Kyle: "make split bundles always show the name and be positioned properly
    even when there is only one node."

    A Split Bundle reports one output per live member, and each line's name is
    its member - the only thing that says which member it carries.  With a lone
    member the label was skipped (labels were only drawn for 2+ sockets), so the
    line was anonymous."""
    w = _split_widget(["Firefox"])

    assert w.nodes["s1"]["outputs"] == ["m0"]
    assert w.nodes["s1"]["output_labels"] == {"m0": "Firefox"}, "the name to show"
    from gui import node_specs

    assert node_specs.spec_for("bundle_split").label_lone_output

    # And it is the *drawn* rule, not just the data: with one socket the label
    # is still wanted.
    spec = node_specs.spec_for("bundle_split")
    assert spec.socket_labels and (len(w.nodes["s1"]["outputs"]) > 1
                                   or spec.label_lone_output)


def test_a_lone_socket_sits_below_a_wrapped_header():
    """The socket insets for a single socket are symmetric, which cancels out of
    the centre - so the socket landed on the node's middle whatever they were.
    A node whose header wraps to several lines (a Split Bundle's type name, its
    label, its id) therefore centred the socket *inside its own title*."""
    w = _split_widget(["Firefox"])
    node = w.nodes["s1"]

    sx, sy = w._socket_position("s1", "out", 0)
    header = w._header_stack_height("s1", node)
    assert sy - node["y"] > header, "the lone socket sits inside the header"
    # The socket's own label hangs 5px above its centre, so the clearance has to
    # cover the label too - not just the circle.
    assert sy - node["y"] - 5 >= header
    assert sy < node["y"] + w.node_height("s1"), "and stays on the node"
    # Inside its own insets, not squeezed past them.
    top, _bottom = w._socket_margins("s1", node)
    assert top <= sy - node["y"]

    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 700, 500)
    w.on_draw(w, cairo.Context(surf), 700, 500)


def test_a_replay_buffer_keeps_the_window_the_daemon_reports():
    """Kyle: "when I click the textbox says 60s but the field shows 0s and on
    launch it didn't follow the value I set."

    The GUI keeps only the daemon fields it knows about for each node, and
    `window` was not among them: the body's box fell back to 0 however many
    seconds the node was keeping, while the editor (which falls back to 60) said
    something else.  The node's own read-out has to survive the merge."""
    w, client = _widget()
    w.update_from_daemon({
        "nodes": {"c1": dict(_clip_node(), window=12.5, clip_path="/tmp/x.wav",
                             clip_state="saved")},
        "edges": {}, "panels": [], "groups": []})

    assert w.nodes["c1"]["window"] == 12.5
    assert w.nodes["c1"]["clip_state"] == "saved"
    assert w.nodes["c1"]["clip_path"] == "/tmp/x.wav"


def test_the_old_clip_previous_type_key_still_loads():
    """It was renamed to Replay Buffer, so a session saved under the old key has
    to keep working."""
    import main

    assert "replay_buffer" in main.NODE_TYPE_REGISTRY
    assert main.NODE_TYPE_REGISTRY.get("clip_previous") is \
        main.NODE_TYPE_REGISTRY["replay_buffer"]


def test_a_node_with_a_bottom_control_is_not_inflated_by_it():
    """Kyle: "there is wayy too much dead space in all these nodes make them a
    lil less tall."

    A lone socket's insets were symmetric - the same value top and bottom -
    which cancels out of the centre so the socket lands on the node's middle
    whatever they are.  The value counted the node's bottom control, so the node
    was inflated by its own control: that is the gap between the header and the
    body on a Recorder or a Replay Buffer."""
    w, client = _widget()
    w.update_from_daemon({"nodes": {"r1": {
        "id": "r1", "type": "recorder", "label": "Recorder", "x": 0.0, "y": 0.0,
        "ready": True, "connected": True, "declarative": False,
        "selection_label": "Recorder", "description": ""}},
        "edges": {}, "panels": [], "groups": []})

    top, bottom = w._socket_margins("r1", w.nodes["r1"])
    # The bottom inset is the control plus its pad - not the control twice.
    # The bottom inset is the control plus its 8px pad - not the control twice.
    assert bottom == 8 + w._bottom_control_height(w.nodes["r1"])
    assert w.node_height("r1") <= w._base_node_height("r1") + 2
    # And the socket still clears the header.
    _x, sy = w._socket_position("r1", "out", 0)
    assert sy >= w._header_stack_height("r1", w.nodes["r1"])


def test_an_idle_poll_does_not_repaint_the_canvas():
    """Kyle: "the UI is VERY prone to locking up entirely now when left alone in
    the bg for awhile or during load."

    The poll repainted unconditionally, and a frame costs tens of milliseconds
    on a large graph (47ms measured at 105 nodes: every node's text and every
    socket is redrawn).  At 2.5 polls/s idle - far more during a load - that
    pinned the main thread."""
    w, client = _widget()
    payload = {"nodes": {"r1": {
        "id": "r1", "type": "sound", "label": "Sound", "x": 0.0, "y": 0.0,
        "ready": True, "connected": True, "declarative": False,
        "selection_label": "Sound", "description": "", "path": "/tmp/x.wav"}},
        "edges": {}, "panels": [], "groups": []}
    w.update_from_daemon(payload)

    drawn = []
    w.queue_draw = lambda *a, **k: drawn.append(1)
    w.update_from_daemon(payload)            # the same state again
    assert drawn == [], "an unchanged poll repainted the canvas"

    payload["nodes"]["r1"]["label"] = "Renamed"   # something visible changed
    w.update_from_daemon(payload)
    assert drawn, "a real change did not repaint"


def test_a_replay_buffers_light_follows_what_it_is_doing():
    """Green while it is recording, red when a clip failed, grey when nothing is
    plugged in."""
    w, client = _widget()

    from render_utils import theme_palette

    pal = theme_palette(w)
    amber = w._replay_light_colour(dict(_clip_node(), recording=True), pal)
    green = w._replay_light_colour(
        dict(_clip_node(), recording=True, clip_state="saved"), pal)
    red = w._replay_light_colour(dict(_clip_node(), recording=False,
                                      clip_state="failed"), pal)
    grey = w._replay_light_colour(dict(_clip_node(), recording=False), pal)

    # Amber while it is capturing the window, green once a clip has landed,
    # red when capturing or writing failed, grey when it is capturing nothing.
    assert amber == (0.95, 0.76, 0.20), "capturing should read amber"
    assert green == pal["success"], "a landed clip should read green"
    assert red == pal["error"], "a failure should read red"
    assert len({amber, green, red, grey}) == 4, "the four states must differ"


def test_the_layout_tick_does_not_repaint_a_settled_graph():
    """Kyle: "If I resize it a whole bunch then scroll out the ui freezes."

    The layout tick repainted unconditionally, and it runs at 30Hz whether or not
    the graph has settled - so a still canvas was redrawn about twice a second
    for ever, and those redraws queued up behind each other while a resize was in
    flight."""
    w, client = _widget()
    w.update_from_daemon({"nodes": {"r1": {
        "id": "r1", "type": "sound", "label": "Sound", "x": 0.0, "y": 0.0,
        "ready": True, "connected": True, "declarative": False,
        "selection_label": "Sound", "description": "", "path": "/tmp/x.wav"}},
        "edges": {}, "panels": [], "groups": []})

    w.layout_awake = True
    drawn = []
    w.queue_draw = lambda *a, **k: drawn.append(1)

    # A step that moves nothing must not repaint...
    w._hierarchical_step = lambda: 0.0
    for _ in range(5):
        w.on_layout_tick()
    assert drawn == [], "a settled layout repainted the canvas"

    # ...and one that moves something must.
    w._hierarchical_step = lambda: 3.0
    w.on_layout_tick()
    assert drawn, "a moving layout did not repaint"


def test_a_burst_of_mutations_leaves_no_polling_behind():
    """Kyle: "ensure there is nothing else thats gonna freeze my ui".

    The post-mutation refresh sites handed `refresh` straight to timeout_add -
    but `refresh` returns True ("keep calling me"), because it is also the
    periodic poll's callback.  So every action the user took spawned one more
    *permanent* get_nodes poll timer, and they accumulated with use: more
    requests in flight and more full updates per reply, the longer it ran."""
    w, client = _widget()
    w.update_from_daemon({"nodes": {"r1": {
        "id": "r1", "type": "sound", "label": "Sound", "x": 0.0, "y": 0.0,
        "ready": True, "connected": True, "declarative": False,
        "selection_label": "Sound", "description": "", "path": "/tmp/x.wav"}},
        "edges": {}, "panels": [], "groups": []})
    sent = []
    client.send = lambda cmd: sent.append(cmd)

    # Ten actions leave exactly one pending refresh, not ten timers.
    ids = set()
    for _ in range(10):
        w.schedule_refresh()
        ids.add(w._refresh_source)
    assert len(ids) == 1 and 0 not in ids, "ten mutations scheduled %d timers" % len(ids)

    # That refresh runs once and leaves nothing behind.
    src = w._refresh_source
    assert w._refresh_once() is False, "a scheduled refresh must not repeat"
    assert w._refresh_source == 0
    assert len([c for c in sent if c.get("command") == "get_nodes"]) == 1

    # A later mutation schedules again on its own.
    w.schedule_refresh()
    assert w._refresh_source not in (0, src)
