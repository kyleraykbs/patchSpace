"""
constants.py
Shared timing / limit constants for the PatchBay GTK client.
"""

import os

SOCKET_PATH = "/tmp/patchbay.sock"

# Where the daemon auto-saves the current PatchSpace after every
# structural/config change (see main.py's _auto_export_session). Never
# loaded automatically on startup - only read back on an explicit
# "Import Last Session" from the hamburger menu (see
# patchspace_widget.show_import_last_session). Duplicated here rather
# than imported from main.py for the same reason SOCKET_PATH already
# is (see patchbay_cli.py's comment on that) - keep the two paths in
# sync if this ever changes.
SESSION_CACHE_PATH = os.path.expanduser("~/.cache/patchbay/last_session.json")

REFRESH_INTERVAL_MS = 400
POLL_RESPONSES_MS = 50
POST_MUTATION_REFRESH_MS = 150

LAYOUT_TICK_MS = 33
LAYOUT_SETTLE_TICKS = 20
LAYOUT_SETTLE_EPSILON = 0.05

ZOOM_MIN = 0.2
ZOOM_MAX = 3.0
ZOOM_STEP = 1.1
UNDO_LIMIT = 100

# Minimum change in a dragged volume slider (0..1 fraction) before we
# bother sending another set_volume command to the daemon. Keeps a
# slider drag from putting a message on the socket for every single
# motion event. The *final* value on release is always sent
# regardless of this threshold (see PatchSpaceGraphWidget.on_drag_end)
# so a drag can never end on a value the daemon never heard about.
VOLUME_SEND_EPSILON = 0.01
