"""
portal_file_dialog.py

Save/open file dialogs backed directly by the XDG Desktop Portal's
org.freedesktop.portal.FileChooser D-Bus interface, instead of
Gtk.FileChooserNative.

Why: even GtkFileChooserNative's *fallback* (non-portal) GTK
implementation touches a GSettings schema internally (for its
recent-files list). On a system where no GSettings schemas are
compiled at all - not even GTK's own - that lookup isn't a soft
failure, it's a hard g_error() inside GLib that aborts the whole
process:

    g_settings_schema_source_lookup: assertion 'source != NULL' failed
    No GSettings schemas are installed on the system
    Aborted (core dumped)

Calling the portal directly sidesteps that code path entirely. This
is exactly what a sandboxed (Flatpak) app does to show a file picker
regardless of toolkit, xdg-desktop-portal ships on effectively every
modern desktop (GNOME, KDE, and the common WM+portal-backend combos),
and it never touches GSettings. The one system this *can't* paper
over is one with no portal implementation running at all - see
_manual_path_dialog() below for what happens then.
"""

from __future__ import annotations

import logging
from typing import Callable, List, Optional
from urllib.parse import unquote, urlparse

from gi.repository import Gio, GLib, Gtk

logger = logging.getLogger(__name__)

_BUS_NAME = "org.freedesktop.portal.Desktop"
_OBJECT_PATH = "/org/freedesktop/portal/desktop"
_FILECHOOSER_IFACE = "org.freedesktop.portal.FileChooser"
_REQUEST_IFACE = "org.freedesktop.portal.Request"


def _uri_to_path(uri: str) -> Optional[str]:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        return None
    return unquote(parsed.path)


def _call_portal(
    method: str,
    title: str,
    options: dict,
    on_paths: Callable[[List[str]], None],
    on_unavailable: Callable[[str], None],
) -> None:
    """Fire OpenFile/SaveFile over the session bus. `on_paths` is
    called with the chosen path(s) - an empty list if the user
    cancelled. `on_unavailable` is called instead if the portal
    couldn't be reached at all (no xdg-desktop-portal running, no
    FileChooser backend registered, ...), so the caller can fall back
    to something else instead of the request silently vanishing.

    We pass "" for parent_window (the spec explicitly allows this -
    "may be left empty") rather than trying to export an X11/Wayland
    window handle: the export mechanism differs by display server and
    getting it wrong is a worse compatibility bet than a picker that
    just isn't transient-for its parent.
    """
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    except GLib.Error as e:
        on_unavailable(str(e))
        return

    # Mutable cell so on_signal (defined before we have a subscription
    # id) can unsubscribe itself once it fires.
    subscription = [None]

    def on_signal(connection, sender, path, iface, signal, params):
        if subscription[0] is not None:
            bus.signal_unsubscribe(subscription[0])
            subscription[0] = None
        response_code, results = params.unpack()
        if response_code != 0:
            on_paths([])  # user cancelled, or the portal request failed
            return
        uris = results.get("uris", [])
        paths = [p for p in (_uri_to_path(u) for u in uris) if p]
        on_paths(paths)

    def on_call_ready(_conn, result, _user_data=None):
        try:
            reply = bus.call_finish(result)
        except GLib.Error as e:
            on_unavailable(str(e))
            return
        (request_path,) = reply.unpack()
        subscription[0] = bus.signal_subscribe(
            _BUS_NAME,
            _REQUEST_IFACE,
            "Response",
            request_path,
            None,
            Gio.DBusSignalFlags.NONE,
            on_signal,
        )

    bus.call(
        _BUS_NAME,
        _OBJECT_PATH,
        _FILECHOOSER_IFACE,
        method,
        GLib.Variant("(ssa{sv})", ("", title, options)),
        GLib.VariantType.new("(o)"),
        Gio.DBusCallFlags.NONE,
        -1,
        None,
        on_call_ready,
    )


def _manual_path_dialog(
    parent: Optional[Gtk.Window],
    title: str,
    accept_label: str,
    suggested_name: str,
    on_path: Callable[[Optional[str]], None],
) -> None:
    """Last-resort fallback for the rare system with no portal at all:
    a plain text-entry dialog for a path. No GtkFileChooser widget of
    any kind is involved here, so this can't hit the GSettings crash
    this module exists to avoid - it degrades to "less convenient"
    instead of "process aborts"."""
    dialog = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    dialog.add_button("_Cancel", Gtk.ResponseType.CANCEL)
    dialog.add_button(accept_label, Gtk.ResponseType.ACCEPT)
    dialog.set_default_response(Gtk.ResponseType.ACCEPT)

    box = dialog.get_content_area()
    box.set_margin_top(10)
    box.set_margin_bottom(10)
    box.set_margin_start(10)
    box.set_margin_end(10)
    box.set_spacing(6)
    box.append(Gtk.Label(label="No file picker portal is available - enter a path:"))

    entry = Gtk.Entry()
    entry.set_text(suggested_name)
    entry.set_activates_default(True)
    entry.set_hexpand(True)
    box.append(entry)

    def on_response(dlg, response):
        path = entry.get_text().strip() if response == Gtk.ResponseType.ACCEPT else None
        dlg.destroy()
        on_path(path or None)

    dialog.connect("response", on_response)
    dialog.show()


def save_file(
    parent: Optional[Gtk.Window],
    title: str,
    suggested_name: str,
    on_path: Callable[[Optional[str]], None],
) -> None:
    """Ask the user where to save a file. Calls on_path(path) with the
    chosen absolute path, or on_path(None) if they cancelled (or
    submitted nothing, in the no-portal fallback)."""

    def handle_paths(paths):
        on_path(paths[0] if paths else None)

    def handle_unavailable(message):
        logger.warning("File chooser portal unavailable, falling back: %s", message)
        _manual_path_dialog(parent, title, "_Save", suggested_name, on_path)

    _call_portal(
        "SaveFile",
        title,
        {"current_name": GLib.Variant("s", suggested_name)},
        handle_paths,
        handle_unavailable,
    )


def open_file(
    parent: Optional[Gtk.Window],
    title: str,
    on_path: Callable[[Optional[str]], None],
    folder: str = "",
    filters: Optional[List[tuple]] = None,
) -> None:
    """Ask the user which existing file to open. Same on_path contract
    as save_file().

    ``folder`` (an absolute directory) opens the picker there, so a node
    with a path already set starts browsing where that file lives.
    ``filters`` is a list of ``(label, [(glob, ...)])`` - the desktop's
    own "these file types" dropdown, e.g. ``[("Audio", ["*.wav",
    "*.flac"])]``; omit it for an unfiltered picker."""

    def handle_paths(paths):
        on_path(paths[0] if paths else None)

    def handle_unavailable(message):
        logger.warning("File chooser portal unavailable, falling back: %s", message)
        _manual_path_dialog(parent, title, "_Open", "", on_path)

    options: dict = {}
    if folder:
        # The portal takes a path as a byte array with a trailing NUL.
        options["current_folder"] = GLib.Variant("ay", folder.encode() + b"\x00")
    if filters:
        # a(sa(us)): (label, [(type, pattern)]); type 0 = glob pattern.
        options["filters"] = GLib.Variant(
            "a(sa(us))",
            [
                (label, [(0, pattern) for pattern in patterns])
                for label, patterns in filters
            ],
        )
    _call_portal("OpenFile", title, options, handle_paths, handle_unavailable)
