"""
constants.py
Shared timing / limit constants for the PatchBay GTK client.
"""

import os

SOCKET_PATH = "/tmp/patchbay.sock"

# Where the daemon auto-saves the current PatchSpace after every
# structural/config change (see main.py's _auto_export_session). Never
# loaded automatically on startup - only loaded on an explicit "Import
# Last Session" from the hamburger menu (see
# patchspace_widget.show_import_last_session), which just tells the
# daemon to load its own cache (a bare {"command": "load_session"}) -
# the GUI no longer reads this file itself. Kept here only in case a
# future GUI feature needs to *display* the path; if that need never
# materializes, this constant can go away entirely.
SESSION_CACHE_PATH = os.path.expanduser("~/.cache/patchbay/last_session.json")

REFRESH_INTERVAL_MS = 400
POLL_RESPONSES_MS = 50
POST_MUTATION_REFRESH_MS = 150

# How often the log console (main_window.LogConsole) asks the daemon for
# new log lines while it is open. Slower than REFRESH_INTERVAL_MS - log
# output is bursty and there's no point polling it as hard as the graph.
LOG_POLL_MS = 800

# Session loads (main.py's _cmd_load_session runs in a background thread
# and stages nodes one at a time) drive a transparent loading-wheel
# overlay + auto-opened log console in the GUI. The overlay is kept up
# for at least this long even if every node reports ready on the very
# first poll, so a short import doesn't flash a spinner for one frame;
# the timeout is a safety net so a node stuck "starting" can't pin the
# overlay/console on forever.
SESSION_LOAD_OVERLAY_MIN_MS = 800
SESSION_LOAD_OVERLAY_TIMEOUT_MS = 90000

LAYOUT_TICK_MS = 33
LAYOUT_SETTLE_TICKS = 20
LAYOUT_SETTLE_EPSILON = 0.05

# How long to wait after the node layout changes (drag end, anchor
# toggle, the physics settling) before pushing the canvas positions +
# anchored flags to the daemon to be persisted. Long enough to coalesce
# a burst of changes into one set_node_layout, short enough that a
# normal edit is saved well before the next get_nodes poll.
LAYOUT_SAVE_DEBOUNCE_MS = 600

# Zoom bounds. There is deliberately no practical lower bound: the wheel
# can keep zooming the graph out until it's a speck, so a huge layout
# can always be framed. ZOOM_MIN is only a tiny positive floor to keep
# to_world()'s division (and the pan math) away from zero.
ZOOM_MIN = 1e-6
ZOOM_MAX = 3.0
ZOOM_STEP = 1.1
UNDO_LIMIT = 100

# Width of the PatchSpace tab's "Add Node" side panel
# (patchspace_widget.build_add_node_panel). The user can drag the
# Gtk.Paned handle in main_window._build_patchspace_page to resize it
# between ADD_NODE_PANEL_MIN_WIDTH and ADD_NODE_PANEL_MAX_WIDTH -
# ADD_NODE_PANEL_WIDTH is just the width it starts at. The min bound
# is also set as the panel's own set_size_request(), which is what
# stops it (via the Paned's shrink-start-child=False) from being
# dragged/squeezed away entirely on a narrow window.
ADD_NODE_PANEL_WIDTH = 260
ADD_NODE_PANEL_MIN_WIDTH = 180
ADD_NODE_PANEL_MAX_WIDTH = 420

# Minimum size for the two graph canvases (PipeWireGraphWidget,
# PatchSpaceGraphWidget). This is a floor so a canvas never collapses
# to nothing, NOT a target size to design a window around - it used to
# be 800x600, which combined with a fixed-width sidebar meant the
# window's real minimum size was silently ~1000x600+, so shrinking a
# window smaller than that made GTK give up on honoring every widget's
# minimum at once (the sidebar, the canvas, and the popover box in
# main_window.py all competing for space), which is what "wonky" /
# underflowing layout on resize actually was.
GRAPH_CANVAS_MIN_SIZE = (200, 150)

# Minimum change in a dragged volume slider (0..1 fraction) before we
# bother sending another set_volume command to the daemon. Keeps a
# slider drag from putting a message on the socket for every single
# motion event. The *final* value on release is always sent
# regardless of this threshold (see PatchSpaceGraphWidget.on_drag_end)
# so a drag can never end on a value the daemon never heard about.
VOLUME_SEND_EPSILON = 0.01
