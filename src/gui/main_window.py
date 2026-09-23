"""
main_window.py

The application window: a two-tab notebook (raw PipeWire graph +
PatchSpace editor) sharing one PatchSpaceClient connection, plus the
GLib timeout that drains daemon responses and routes each one to
whichever tab it belongs to.
"""

from __future__ import annotations

import constants
import logging
import math
import os
import signal
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib, GObject, Pango, Adw

from constants import (
    POLL_RESPONSES_MS,
    LOG_POLL_MS,
    DAEMON_POLL_MS,
    PANELS_VIEW_REFRESH_MS,
    ADD_NODE_PANEL_WIDTH,
    ADD_NODE_PANEL_MIN_WIDTH,
    ADD_NODE_PANEL_MAX_WIDTH,
)
from socket_client import PatchSpaceClient
from daemon_control import DaemonManager
from pipewire_widget import PipeWireGraphWidget
from patchspace_widget import PatchSpaceGraphWidget

logger = logging.getLogger(__name__)

# Wall-clock heartbeat of the GTK main thread, updated by _heartbeat() on
# a GLib timeout. Only used by the PATCHSPACE_TRACE_HANG watchdog below.
_heartbeat_time = [0.0]


def _heartbeat() -> bool:
    _heartbeat_time[0] = time.monotonic()
    return True


def _arm_hang_watchdog() -> None:
    """Diagnostic for the "UI randomly freezes" report (no exception, no
    traceback): a hard hang here is a Python busy-loop on the main
    thread, which never returns control to the GLib loop, so GTK can't
    tell us anything. Enable with PATCHSPACE_TRACE_HANG=1 when launching
    the GUI; a watchdog thread then sends SIGUSR1 (registered via
    faulthandler to dump every thread's stack to stderr) the moment the
    main thread's heartbeat goes stale for more than 4 seconds. The
    dumped stack shows exactly which callback is spinning."""
    import faulthandler

    faulthandler.register(signal.SIGUSR1, all_threads=True)

    def _watcher() -> None:
        while True:
            time.sleep(2)
            if time.monotonic() - _heartbeat_time[0] > 4.0:
                try:
                    os.kill(os.getpid(), signal.SIGUSR1)
                except OSError:
                    pass

    threading.Thread(target=_watcher, daemon=True).start()


class LogConsole(Gtk.Box):
    """A read-only, monospace log view fed by the daemon's get_logs
    command (see main.py's _cmd_get_logs / in-memory ring handler).

    It owns a *dedicated* daemon connection and drains its own responses
    rather than sharing the main window's request/response routing, so
    it can never have its replies consumed by (or miss replies during) a
    burst of graph refreshes.  Hidden until the toolbar's console button
    toggles it on, then polled while visible."""

    MAX_LINES = 2000

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.add_css_class("opaque-chrome")
        self.client = PatchSpaceClient()
        self._active = False
        self._since = 0
        self._line_count = 0
        self._poll_id = 0
        self._outstanding = False

        self._buffer = Gtk.TextBuffer()
        view = Gtk.TextView()
        view.set_buffer(self._buffer)
        view.set_editable(False)
        view.set_cursor_visible(False)
        view.set_monospace(True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self._view = view

        # Auto-follow: stay pinned to the newest line while the user is
        # at the bottom, but stop following the moment they scroll up.
        # `value-changed` only fires on an actual value change (user
        # scroll or our own programmatic scroll), not on the upper bound
        # growing as text is appended, so growing the log never flips
        # following off by itself.
        self._follow = True
        vadj = self._view.get_vadjustment()
        if vadj is not None:
            vadj.connect("value-changed", self._on_scrolled)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_child(view)
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_size_request(-1, 200)
        self.append(scrolled)
        self.set_visible(False)

    @property
    def active(self) -> bool:
        return self._active

    def _at_bottom(self) -> bool:
        adj = self._view.get_vadjustment()
        if adj is None:
            return True
        return (adj.get_value() + adj.get_page_size()) >= (adj.get_upper() - 2.0)

    def _on_scrolled(self, adj) -> None:
        # A user scroll (or our own follow scroll) moved the viewport:
        # follow only while it's still at the bottom.
        self._follow = self._at_bottom()

    def _scroll_to_bottom(self):
        if self._active and self._follow:
            self._view.scroll_to_iter(self._buffer.get_end_iter(), 0.0, True, 0.0, 1.0)
        return False

    def set_active(self, active: bool) -> None:
        self._active = bool(active)
        self.set_visible(self._active)
        if self._active:
            # Pull everything the daemon still has buffered and jump to
            # the newest line.
            self._since = 0
            self._outstanding = False
            self._follow = True
            if self._poll_id == 0:
                self._poll_id = GLib.timeout_add(LOG_POLL_MS, self._poll)
            self._poll()
            GLib.idle_add(self._scroll_to_bottom)
        elif self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0

    def _poll(self):
        if not self._active:
            self._poll_id = 0
            return False
        # Drain any reply from the previous request first, then ask for
        # the next batch - one request outstanding at a time.
        self._drain()
        if not self._outstanding:
            self._outstanding = True
            self.client.send({"command": "get_logs", "since": self._since})
        return True

    def _drain(self) -> None:
        for resp in self.client.get_responses():
            self._outstanding = False
            if resp.get("status") == "ok" and "lines" in resp:
                self.append_lines(resp.get("lines", []), resp.get("last_seq", 0))
            elif resp.get("status") == "error":
                self._append_text(f"[log console] daemon error: {resp.get('message')}")

    def _append_text(self, text: str) -> None:
        self._buffer.insert(self._buffer.get_end_iter(), text + "\n")
        self._line_count += 1

    def append_lines(self, lines, last_seq) -> None:
        # A last_seq below what we've seen means the daemon restarted
        # (its sequence resets); start over and pull from the top.
        if last_seq and last_seq < self._since:
            self._buffer.set_text("")
            self._line_count = 0
            self._since = 0
            self._outstanding = False
            return

        for item in lines:
            self._since = max(self._since, int(item.get("seq", 0)))
            self._append_text(str(item.get("text", "")))
        if not lines:
            return
        self._trim()
        # Follow the tail only if we were already at the bottom; after
        # layout, so the new upper bound is known.
        if self._follow:
            GLib.idle_add(self._scroll_to_bottom)

    def _trim(self) -> None:
        if self._line_count > self.MAX_LINES:
            self._buffer.delete(
                self._buffer.get_start_iter(),
                self._buffer.get_iter_at_line(self._line_count - self.MAX_LINES),
            )
            self._line_count = self.MAX_LINES

    def stop(self) -> None:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        self.client.stop()


class MainWindow(Gtk.ApplicationWindow):
    # Side-view column widths, shared by the header row and every panel row
    # so the labels line up over the controls they describe.
    _PANEL_DOT_W = 16
    _PANEL_BTN_W = 36

    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Patch Space")
        self.set_default_size(1200, 800)
        self._install_translucency_css()
        self._set_canvas_translucency(constants.CANVAS_BG_ALPHA < 1.0)

        # Explicit titlebar with the program name (the default CSD title
        # also shows it, but this makes the name unambiguous and matches
        # the libadwaita look).
        header = Adw.HeaderBar()
        header.add_css_class("opaque-chrome")
        title_label = Gtk.Label(label="Patch Space")
        title_label.add_css_class("title")
        header.set_title_widget(title_label)
        self.set_titlebar(header)

        # Adopt a daemon that is already running, or start one in the
        # background.  Only a daemon we started is ours to shut down (on
        # close / via the menu) - see daemon_control.DaemonManager.
        self.daemon = DaemonManager()
        self.daemon.ensure_started()
        self._daemon_busy = False
        self.client = PatchSpaceClient()

        self.notebook = Gtk.Notebook()
        self.pw_widget = PipeWireGraphWidget(self.client)
        self.ps_widget = PatchSpaceGraphWidget(self.client)

        self.notebook.append_page(self.pw_widget, Gtk.Label(label="PipeWire Graph"))
        self.notebook.append_page(
            self._build_patchspace_page(), Gtk.Label(label="PatchSpace Graph")
        )
        # Open on the editable PatchSpace canvas; the raw PipeWire graph is
        # there for inspection but isn't where you start working.
        self.notebook.set_current_page(1)

        # A small bottom-right "not connected" badge while the socket isn't
        # answering, so a GUI launched before its daemon (or after a stop)
        # reads as "not ready yet" rather than as a broken, empty app.  A
        # periodic reachability poll toggles it.
        self._root_overlay = Gtk.Overlay()
        self._root_overlay.set_child(self.notebook)
        self._disconnected_badge = self._build_disconnected_badge()
        self._root_overlay.add_overlay(self._disconnected_badge)
        self.set_child(self._root_overlay)

        self._daemon_connected = self.client.is_connected()
        self._disconnected_badge.set_visible(not self._daemon_connected)

        # Update the badge the instant the client's connection flips
        # (the client redials on its own), with the periodic poll below
        # as a fallback.  The callback fires on the client's worker
        # thread, so marshal it onto the GTK main loop.
        self.client.on_connection_changed.append(self._on_client_connection_changed)

        self.pw_widget.refresh()
        self.ps_widget.refresh()

        GLib.timeout_add(DAEMON_POLL_MS, self._poll_daemon_connection)
        GLib.timeout_add(POLL_RESPONSES_MS, self.process_responses)
        GLib.timeout_add(PANELS_VIEW_REFRESH_MS, self._poll_panels_view)
        # Heartbeat + optional hang watchdog - see _arm_hang_watchdog.
        _heartbeat_time[0] = time.monotonic()
        GLib.timeout_add(200, _heartbeat)
        if os.environ.get("PATCHSPACE_TRACE_HANG"):
            _arm_hang_watchdog()

    def _build_patchspace_page(self):
        """The PatchSpace tab is a side panel (drag-and-drop "add
        node" source, see PatchSpaceGraphWidget.build_add_node_panel)
        next to the ps_widget canvas, which itself carries a hamburger
        menu (Export/Import/panel/daemon actions) pinned to its
        top-right corner. ps_widget itself is a plain Gtk.DrawingArea with no room for child
        widgets, so the menu button lives in a Gtk.Overlay wrapped
        around it instead of inside it."""
        # Filled in below; the loading overlay is created after the
        # toolbar so _on_console_toggled/_console_button exist.
        self._loading_overlay = None
        self._loading_spinner = None
        self._console_auto_opened = False

        overlay = Gtk.Overlay()
        overlay.set_child(self.ps_widget)
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)
        # Kept so _on_loading_changed can add/remove the loading overlay
        # on top of the canvas.
        self.ps_overlay = overlay

        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        menu_button.set_halign(Gtk.Align.END)
        menu_button.set_valign(Gtk.Align.START)
        menu_button.set_margin_top(8)
        menu_button.set_margin_end(8)

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        # The popover otherwise sizes itself to the button labels'
        # natural width, which some themes/fonts squeeze just enough
        # to force a wrap ("Export…" wrapping mid-word). Forcing a
        # minimum width on the box - and turning off label wrapping as
        # a belt-and-suspenders fix - keeps both entries on one line.
        box.set_size_request(200, -1)

        export_btn = Gtk.Button(label="Export\u2026")
        export_btn.get_child().set_wrap(False)
        export_btn.set_tooltip_text(
            "Save the current graph to a file (or the clipboard)."
        )
        export_btn.connect(
            "clicked",
            lambda _b: (popover.popdown(), self.ps_widget.show_export_dialog()),
        )
        box.append(export_btn)

        import_btn = Gtk.Button(label="Import\u2026")
        import_btn.get_child().set_wrap(False)
        import_btn.set_tooltip_text("Load a graph from a file.")
        import_btn.connect(
            "clicked",
            lambda _b: (popover.popdown(), self.ps_widget.show_import_dialog()),
        )
        box.append(import_btn)

        reload_panels_btn = Gtk.Button(label="Reload Panels")
        reload_panels_btn.get_child().set_wrap(False)
        reload_panels_btn.set_tooltip_text(
            "Re-read every panel file now (daemon-side edits will be lost)"
        )
        reload_panels_btn.connect(
            "clicked",
            lambda _b: (
                popover.popdown(),
                self.ps_widget.reload_panels(),
            ),
        )
        box.append(reload_panels_btn)

        # All physics (node layout and panel repulsion) runs by default;
        # this pauses it.  Mirrors the floating top-right pause/resume
        # button (see _set_physics).  New panels are pinned, so turning
        # physics on doesn't shove a freshly created panel around.
        self._physics_check = Gtk.CheckButton(label="Physics")
        self._physics_check.set_active(True)
        self._physics_check.set_tooltip_text(
            "Run node/panel physics (off: the graph stays put)"
        )
        self._physics_check.connect(
            "toggled", lambda b: self._set_physics(b.get_active())
        )
        box.append(self._physics_check)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # "Rebuild" lives in this menu - it's a rarely-needed recovery
        # action, not an everyday view control.  Tears the live PatchSpace
        # down and rebuilds it from a snapshot of itself (main.py's
        # rebuild command): the user's "turn it off and on again" when a
        # node's routing has wedged.  Nothing is lost; it's the same
        # graph, recreated.
        rebuild_btn = Gtk.Button(label="Rebuild Graph")
        rebuild_btn.get_child().set_wrap(False)
        rebuild_btn.set_tooltip_text(
            "Tear down and rebuild the graph in place (restart the live nodes)"
        )
        rebuild_btn.connect(
            "clicked",
            lambda _b: (popover.popdown(), self.ps_widget.rebuild_graph()),
        )
        box.append(rebuild_btn)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        # Daemon lifecycle.  The GUI starts a background daemon on launch
        # if none is running; these let the user start / stop / restart it
        # explicitly (the actions run off the main thread so the wait for
        # the socket never freezes the window - see _daemon_action).
        for label, action, tip in (
            ("Start Daemon", "start", "Start the background Patch Space daemon"),
            ("Stop Daemon", "stop", "Stop the background Patch Space daemon"),
            ("Restart Daemon", "restart", "Restart the background Patch Space daemon"),
        ):
            btn = Gtk.Button(label=label)
            btn.get_child().set_wrap(False)
            btn.set_tooltip_text(tip)
            btn.connect(
                "clicked",
                lambda _b, a=action: (popover.popdown(), self._daemon_action(a)),
            )
            box.append(btn)

        popover.set_child(box)
        menu_button.set_popover(popover)

        overlay.add_overlay(menu_button)

        # Panels side-view toggle, directly under the hamburger, top-right.
        self._panels_toggle = Gtk.ToggleButton()
        self._panels_toggle.set_icon_name("sidebar-show-symbolic")
        self._panels_toggle.set_tooltip_text("Show/hide the panels list")
        self._panels_toggle.set_halign(Gtk.Align.END)
        self._panels_toggle.set_valign(Gtk.Align.START)
        self._panels_toggle.set_margin_top(48)
        self._panels_toggle.set_margin_end(8)
        self._panels_toggle.connect("toggled", self._on_panels_toggled)
        overlay.add_overlay(self._panels_toggle)

        # Physics pause/resume, directly under the panels toggle.  Active =
        # panel physics running; shows a pause icon while running and a play
        # icon while paused.  Physics is on by default now (new panels are
        # still pinned, so they don't move on their own).
        self._physics_toggle = Gtk.ToggleButton()
        self._physics_toggle.set_icon_name("media-playback-pause-symbolic")
        self._physics_toggle.set_active(True)
        self._physics_toggle.set_tooltip_text(
            "Pause/resume all physics (off: the graph stays put)"
        )
        self._physics_toggle.set_halign(Gtk.Align.END)
        self._physics_toggle.set_valign(Gtk.Align.START)
        self._physics_toggle.set_margin_top(90)
        self._physics_toggle.set_margin_end(8)
        self._physics_toggle.connect(
            "toggled", lambda b: self._set_physics(b.get_active())
        )
        overlay.add_overlay(self._physics_toggle)

        # Bottom-left mouse-controls cheat sheet.
        overlay.add_overlay(self._build_mouse_help())

        # Floating delete button for the current selection, pinned to the
        # canvas's bottom-right and shown only while something is selected.
        # Asks for confirmation before removing anything.
        self._ps_delete_button = Gtk.Button()
        self._ps_delete_button.add_css_class("selection-delete")
        self._ps_delete_button.set_child(
            Gtk.Image.new_from_icon_name("user-trash-symbolic")
        )
        self._ps_delete_button.set_size_request(40, 40)
        self._ps_delete_button.set_tooltip_text("Delete the selected nodes")
        self._ps_delete_button.set_halign(Gtk.Align.END)
        self._ps_delete_button.set_valign(Gtk.Align.END)
        self._ps_delete_button.set_margin_end(16)
        self._ps_delete_button.set_margin_bottom(16)
        self._ps_delete_button.set_visible(False)
        self._ps_delete_button.connect(
            "clicked", lambda _b: self.ps_widget.confirm_delete_selection()
        )
        overlay.add_overlay(self._ps_delete_button)

        # Transparent loading wheel + faint dim over the canvas, shown
        # while a session import stages its nodes (see
        # PatchSpaceGraphWidget.on_loading_changed).  Created here so it
        # sits above the menu button; _on_loading_changed toggles it.
        self._loading_overlay = self._build_loading_overlay()
        overlay.add_overlay(self._loading_overlay)

        add_node_panel = self.ps_widget.build_add_node_panel()

        # Canvas + a thin tool strip pinned under it, plus an optional
        # log console that slides in beneath the strip (toggled by the
        # console button at the strip's left).  The strip and the canvas
        # share the Paned's end child so the strip stays the same width
        # as the canvas (not the whole window).
        self.log_console = LogConsole()
        canvas_area = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        canvas_area.append(overlay)
        # Wrap the toolbar in a full-width opaque strip: the toolbar itself
        # carries margins, and with the window surface transparent those
        # margins would otherwise be 100% see-through (the "padding area").
        toolbar_strip = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        toolbar_strip.add_css_class("opaque-chrome")
        toolbar = self._build_patchspace_toolbar()
        toolbar.set_hexpand(True)
        toolbar_strip.append(toolbar)
        canvas_area.append(toolbar_strip)
        canvas_area.append(self.log_console)
        # Load progress (start/stop) drives the overlay + console
        # auto-open/collapse; see _on_loading_changed.
        self.ps_widget.on_loading_changed.append(self._on_loading_changed)
        self.ps_widget.on_canvas_opacity.append(self._apply_canvas_opacity)

        # A Gtk.Paned instead of a plain Box+Separator: it draws its
        # own draggable handle, so the user can grab the edge between
        # the panel and the canvas to resize it, instead of the panel
        # being a fixed width forever. resize_start_child(False) keeps
        # the panel's width stable when the *window* is resized (only
        # dragging the handle itself changes it); shrink_start_child
        # (False) stops it from being dragged/squeezed narrower than
        # add_node_panel's own minimum (see build_add_node_panel).
        # shrink_end_child(True) lets the canvas give up space down to
        # its own minimum (GRAPH_CANVAS_MIN_SIZE) instead of jamming.
        page = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        page.set_start_child(add_node_panel)
        page.set_resize_start_child(False)
        page.set_shrink_start_child(False)
        page.set_end_child(canvas_area)
        page.set_resize_end_child(True)
        page.set_shrink_end_child(True)
        page.set_position(ADD_NODE_PANEL_WIDTH)

        def _clamp_panel_width(paned, _pspec):
            pos = paned.get_position()
            clamped = max(ADD_NODE_PANEL_MIN_WIDTH, min(ADD_NODE_PANEL_MAX_WIDTH, pos))
            if clamped != pos:
                paned.set_position(clamped)

        page.connect("notify::position", _clamp_panel_width)

        # The panels side view (list of panel files, hidden by default) as
        # the end child of an outer paned, so opening it shrinks the canvas
        # rather than covering it.
        self.panels_view = self._build_panels_view()
        # Open by default: the panel list is how you find your panels, and
        # the toggle (and its close button) hide it again.
        self._panels_toggle.set_active(True)
        outer = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        outer.set_start_child(page)
        outer.set_resize_start_child(True)
        outer.set_shrink_start_child(True)
        outer.set_end_child(self.panels_view)
        outer.set_resize_end_child(False)
        outer.set_shrink_end_child(False)
        self._panels_outer = outer
        return outer

    def _build_panels_view(self):
        """A docked, scrollable list of the panel files on the right."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("side-panel-fill")
        # Wide enough for a panel name beside the color dot and the
        # auto-load/add/delete buttons (~36px each): at 240 the names were
        # cut off.  The divider is draggable, so this is only the default.
        box.set_size_request(300, -1)
        # Padding comes from CSS (.side-panel-fill) so it is *inside* the
        # opaque background - GTK margins would sit outside it and leave a
        # transparent gap at the window edge.

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        title = Gtk.Label(label="Panels")
        title.set_xalign(0)
        title.set_hexpand(True)
        title.add_css_class("heading")
        header.append(title)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic")
        close.add_css_class("flat")
        close.set_tooltip_text("Hide the panels list")
        close.connect("clicked", lambda _b: self._panels_toggle.set_active(False))
        header.append(close)
        box.append(header)

        # Column headers, aligned with the per-row controls below: the
        # color dot, the panel name, then the auto-load checkbox and the
        # add/delete buttons.  Each carries a tooltip explaining its column.
        cols = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        cols.set_margin_top(2)
        cols.set_margin_bottom(2)
        spacer = Gtk.Box()
        spacer.set_size_request(self._PANEL_DOT_W, -1)
        cols.append(spacer)
        name_hdr = Gtk.Label(label="Panel")
        name_hdr.set_xalign(0)
        name_hdr.set_hexpand(True)
        name_hdr.add_css_class("dim-label")
        cols.append(name_hdr)
        for text, tip in (
            ("Auto", "Whether this panel file loads automatically at startup"),
            ("Add", "Add a placement of this panel to the canvas"),
            ("Del", "Delete this panel file and all its placements"),
        ):
            lab = Gtk.Label(label=text)
            lab.set_size_request(self._PANEL_BTN_W, -1)
            lab.add_css_class("dim-label")
            lab.set_tooltip_text(tip)
            cols.append(lab)
        box.append(cols)

        self._panels_list_box = Gtk.ListBox()
        # Transparent inner list; the rounded darker card lives on the
        # ScrolledWindow (below) so its corners stay put while the list
        # scrolls.
        self._panels_list_box.add_css_class("panels-list-inner")
        self._panels_list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled = Gtk.ScrolledWindow()
        scrolled.add_css_class("panels-list")
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        scrolled.set_child(self._panels_list_box)
        box.append(scrolled)
        return box

    def _on_panels_toggled(self, button):
        self.panels_view.set_visible(button.get_active())
        if button.get_active():
            self._refresh_panels_view()

    def _refresh_panels_view(self):
        """Ask the daemon for a fresh panel-file listing (side view only)."""
        self.client.send({"command": "list_panel_files"})

    def _poll_panels_view(self):
        """Periodically re-read the panel-file list while the side view is
        open, so files that change outside the current GUI action (or an
        action whose reply we don't otherwise see) show up on their own.
        Returns True to keep the GLib timeout alive."""
        if hasattr(self, "panels_view") and self.panels_view.get_visible():
            self._refresh_panels_view()
        return True

    def _set_physics(self, active):
        """Single entry point for the physics pause/resume state, kept in
        sync between the hamburger checkbox and the floating button."""
        if getattr(self, "_syncing_physics", False):
            return
        self._syncing_physics = True
        try:
            active = bool(active)
            self.ps_widget.set_physics_active(active)
            if self._physics_check.get_active() != active:
                self._physics_check.set_active(active)
            if self._physics_toggle.get_active() != active:
                self._physics_toggle.set_active(active)
            self._physics_toggle.set_icon_name(
                "media-playback-pause-symbolic"
                if active
                else "media-playback-start-symbolic"
            )
        finally:
            self._syncing_physics = False

    def _update_panels_view(self, resp):
        """Rebuild the side-view rows from a list_panel_files reply: one row
        per panel *file*, grouped under its folder, with an auto-load
        checkbox, a Place button and a Delete button.

        Skipped when the listing is unchanged: the periodic refresh calls
        this every PANELS_VIEW_REFRESH_MS, and rebuilding the rows each time
        destroys the button under the pointer mid-click - which is what made
        Delete look like it did nothing."""
        if not hasattr(self, "_panels_list_box"):
            return
        files = resp.get("files", [])
        sig = tuple(
            (
                e.get("stem"),
                e.get("folder", ""),
                e.get("label"),
                e.get("color"),
                bool(e.get("auto_load")),
                bool(e.get("writable")),
                int(e.get("node_count", 0) or 0),
                tuple(e.get("children") or []),
            )
            for e in files
        )
        if getattr(self, "_panels_view_sig", None) == sig:
            return
        self._panels_view_sig = sig
        while True:
            child = self._panels_list_box.get_first_child()
            if child is None:
                break
            self._panels_list_box.remove(child)
        if not files:
            placeholder = Gtk.Label(label="No panel files")
            placeholder.set_margin_top(12)
            placeholder.add_css_class("dim-label")
            self._panels_list_box.append(placeholder)
            return
        groups: dict = {}
        for entry in files:
            groups.setdefault(str(entry.get("folder") or ""), []).append(entry)
        show_folders = any(folder for folder in groups)
        for folder in sorted(groups):
            if show_folders:
                head = Gtk.Label(label=folder or "Panels")
                head.set_xalign(0)
                head.add_css_class("dim-label")
                head.set_margin_top(6)
                head.set_margin_start(2)
                head.set_tooltip_text(
                    f"Folder: {folder}" if folder else "Top-level panel files"
                )
                self._panels_list_box.append(head)
            for entry in groups[folder]:
                self._panels_list_box.append(self._build_panel_row(entry))

    def _build_panel_row(self, entry):
        writable = bool(entry.get("writable"))
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.set_margin_top(3)
        row.set_margin_bottom(3)
        dot = Gtk.Label()
        dot.set_size_request(self._PANEL_DOT_W, -1)
        dot.set_halign(Gtk.Align.CENTER)
        dot.set_valign(Gtk.Align.START)
        # Resolve the panel's color the same way the canvas does, so a
        # panel colored by a theme slot (@blue, ...) shows the color that
        # is actually drawn - and follows a theme change.
        r, g, b = self.ps_widget.resolve_color(
            entry.get("color"), entry.get("stem") or entry.get("name") or ""
        )
        dot.set_markup(
            "<span foreground='{}'>\u25cf</span>".format(
                GLib.markup_escape_text("#%02x%02x%02x" % (
                    max(0, min(255, round(r * 255))),
                    max(0, min(255, round(g * 255))),
                    max(0, min(255, round(b * 255))),
                ))
            )
        )
        row.append(dot)
        # Name over the dim counts (nodes above panels).  The name wraps
        # (WORD_CHAR) instead of ellipsizing so long names stay readable.
        text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        text.set_hexpand(True)
        name = Gtk.Label(label=str(entry.get("label") or entry.get("stem", "?")))
        name.set_xalign(0)
        name.set_hexpand(True)
        name.set_wrap(True)
        name.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
        text.append(name)
        # Counts stacked, nodes above panels (not side by side).
        nodes = int(entry.get("node_count", 0) or 0)
        children = [str(c) for c in (entry.get("children") or [])]
        for line, tip in (
            (
                f"{nodes} {'node' if nodes == 1 else 'nodes'}",
                "Nodes defined by this panel file",
            ),
            (
                f"{len(children)} " f"{'panel' if len(children) == 1 else 'panels'}",
                "Sub-panels: " + ", ".join(children) if children else "No sub-panels",
            ),
        ):
            lab = Gtk.Label(label=line)
            lab.set_xalign(0)
            lab.set_ellipsize(Pango.EllipsizeMode.END)
            lab.add_css_class("dim-label")
            lab.set_tooltip_text(tip)
            text.append(lab)
        row.append(text)
        auto = Gtk.CheckButton()
        auto.set_size_request(self._PANEL_BTN_W, -1)
        auto.set_valign(Gtk.Align.START)
        auto.set_active(bool(entry.get("auto_load")))
        auto.set_sensitive(writable)
        auto.set_tooltip_text(
            "Load this panel at startup" if writable else "This panel file is read-only"
        )
        auto.connect(
            "toggled",
            lambda b, e=entry: self.client.send(
                {
                    "command": "set_panel_file_autoload",
                    "stem": e.get("stem"),
                    "enabled": b.get_active(),
                }
            ),
        )
        row.append(auto)
        place = Gtk.Button.new_from_icon_name("list-add-symbolic")
        place.add_css_class("flat")
        place.set_size_request(self._PANEL_BTN_W, -1)
        place.set_valign(Gtk.Align.START)
        place.set_tooltip_text("Add a placement of this panel to the canvas")
        place.connect(
            "clicked",
            lambda _b, e=entry: self.ps_widget.place_panel_at_view_center(
                e.get("stem")
            ),
        )
        row.append(place)
        delete = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete.add_css_class("flat")
        delete.set_size_request(self._PANEL_BTN_W, -1)
        delete.set_valign(Gtk.Align.START)
        delete.set_sensitive(writable)
        delete.set_tooltip_text(
            "Delete this panel file and all its placements"
            if writable
            else "This panel file is read-only"
        )
        delete.connect(
            "clicked",
            lambda _b, e=entry: self._confirm_delete_panel_file(e),
        )
        row.append(delete)
        # Drag the row onto the canvas to place it where dropped.
        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.COPY)
        drag_source.connect(
            "prepare",
            lambda _s, _x, _y, e=entry: Gdk.ContentProvider.new_for_value(
                GObject.Value(GObject.TYPE_STRING, "panel:" + str(e.get("stem")))
            ),
        )
        row.add_controller(drag_source)
        return row

    def _confirm_delete_panel_file(self, entry):
        """Confirm, then delete a panel *file* (and every placement of it)."""
        label = entry.get("label") or entry.get("stem")
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(f"Delete panel file {label}?")
        dialog.set_detail(
            "This deletes the file itself and removes every placement of it "
            "from the canvas, along with the nodes they contain."
        )
        dialog.set_buttons(["Cancel", "Delete"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)
        dialog.choose(
            self,
            None,
            lambda d, result, e=entry: self._on_delete_panel_file(d, result, e),
        )

    def _on_delete_panel_file(self, dialog, result, entry):
        try:
            index = dialog.choose_finish(result)
        except GLib.Error:
            return
        if index != 1:
            return
        self.client.send({"command": "delete_panel_file", "stem": entry.get("stem")})
        # The listing is refreshed when the daemon's reply lands (see
        # process_responses) - and by the periodic side-view poll - so the
        # row disappears even if this reply is delayed.

    def _build_mouse_help(self):
        """A small bottom-left legend of the canvas mouse controls: left =
        pick/pan, middle = pan, right = select.  Purely informational (not a
        hit target)."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("mouse-help")
        box.set_halign(Gtk.Align.START)
        box.set_valign(Gtk.Align.END)
        box.set_margin_start(10)
        box.set_margin_bottom(10)
        box.set_can_target(False)
        for button, text in (
            ("left", "Pick / Move / Pan"),
            ("middle", "Pan"),
            ("right", "Select"),
        ):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            row.append(self._mouse_icon(button))
            label = Gtk.Label(label=text)
            label.set_xalign(0)
            row.append(label)
            box.append(row)
        return box

    def _mouse_icon(self, button):
        """A tiny mouse with `button` ('left'|'middle'|'right') highlighted."""
        area = Gtk.DrawingArea()
        area.set_content_width(18)
        area.set_content_height(26)
        area.set_valign(Gtk.Align.CENTER)
        area.set_draw_func(lambda _a, cr, w, h: self._draw_mouse_icon(cr, w, h, button))
        return area

    @staticmethod
    def _draw_mouse_icon(cr, w, h, button):
        x, y = 2.0, 1.5
        bw, bh = w - 4.0, h - 3.0
        r = bw / 2.0
        cx = x + bw / 2.0
        btn_h = bh * 0.42

        def body_path():
            cr.move_to(x, y + r)
            cr.arc(cx, y + r, r, math.pi, 2.0 * math.pi)
            cr.line_to(x + bw, y + bh - r)
            cr.arc(cx, y + bh - r, r, 0.0, math.pi)
            cr.close_path()

        # Button fill, clipped to the body.
        cr.save()
        body_path()
        cr.clip()
        cr.set_source_rgba(0.30, 0.72, 1.0, 0.85)
        if button == "left":
            cr.rectangle(x, y, bw / 2.0, btn_h)
        elif button == "right":
            cr.rectangle(cx, y, bw / 2.0, btn_h)
        else:
            cr.rectangle(cx - 2.5, y, 5.0, btn_h * 0.8)
        cr.fill()
        cr.restore()

        # Outline + button divider lines.
        cr.set_source_rgba(1.0, 1.0, 1.0, 0.85)
        cr.set_line_width(1.3)
        body_path()
        cr.stroke()
        cr.move_to(x, y + btn_h)
        cr.line_to(x + bw, y + btn_h)
        cr.move_to(cx, y)
        cr.line_to(cx, y + btn_h)
        cr.stroke()

    def _build_loading_overlay(self):
        """A translucent full-canvas sheet with a centered Gtk.Spinner,
        hidden until a session load starts.  Gtk.Overlay overlay children
        are only as big as their natural size unless they expand, so the
        sheet sets hexpand/vexpand + FILL alignment to cover the whole
        canvas."""
        self._install_loading_css()

        sheet = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sheet.add_css_class("loading-overlay")
        sheet.set_halign(Gtk.Align.FILL)
        sheet.set_valign(Gtk.Align.FILL)
        sheet.set_hexpand(True)
        sheet.set_vexpand(True)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        card.set_halign(Gtk.Align.CENTER)
        card.set_valign(Gtk.Align.CENTER)

        self._loading_spinner = Gtk.Spinner()
        self._loading_spinner.set_size_request(48, 48)
        card.append(self._loading_spinner)

        label = Gtk.Label(label="Loading nodes\u2026")
        card.append(label)

        sheet.append(card)
        sheet.set_visible(False)
        return sheet

    def _build_disconnected_badge(self):
        """A small "not connected" chip pinned to the window's bottom-right
        while the daemon's socket isn't answering.  Deliberately not a
        cover: it is click-through (can_target False) and tiny, so it just
        clearly flags the state without getting in the way."""
        self._install_loading_css()

        badge = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        badge.add_css_class("disconnected-badge")
        badge.set_halign(Gtk.Align.END)
        badge.set_valign(Gtk.Align.END)
        badge.set_margin_end(14)
        badge.set_margin_bottom(14)
        # Clicks pass straight through to the UI underneath.
        badge.set_can_target(False)

        badge.append(Gtk.Label(label="\u25cf"))  # filled dot
        badge.append(Gtk.Label(label="Not connected to daemon"))

        badge.set_visible(False)
        return badge

    def _on_client_connection_changed(self, connected):
        """Client worker-thread callback: marshal the state change onto
        the GTK main loop."""
        GLib.idle_add(self._apply_connection_state, connected)
        return False

    def _apply_connection_state(self, connected):
        if connected == self._daemon_connected:
            return False
        self._daemon_connected = connected
        if self._disconnected_badge is not None:
            self._disconnected_badge.set_visible(not connected)
        if connected:
            # Fresh connection (or a reconnected daemon): pull a snapshot
            # now instead of waiting for the next poll.
            self.pw_widget.refresh()
            self.ps_widget.refresh()
        return False

    def _set_canvas_translucency(self, on):
        """The canvas is see-through only when the alpha says so; the class is
        what makes the window surface and the containers around the grids
        transparent (see `_install_translucency_css`)."""
        if on:
            self.add_css_class("translucent-canvas")
        else:
            self.remove_css_class("translucent-canvas")

    def _apply_canvas_opacity(self, value):
        """The canvas opacity the daemon reports - the deployment's preference
        (the module derives it from stylix).  `--canvas-opacity` and
        `$PATCHSPACE_CANVAS_OPACITY` are the user's own choice and win."""
        if value is None or constants.CANVAS_BG_ALPHA_EXPLICIT:
            return
        value = max(0.0, min(1.0, float(value)))
        if abs(value - constants.CANVAS_BG_ALPHA) < 1e-6:
            return
        constants.CANVAS_BG_ALPHA = value
        self._set_canvas_translucency(value < 1.0)
        self.pw_widget.queue_draw()
        self.ps_widget.queue_draw()

    def _poll_daemon_connection(self):
        """Fallback to the client's live state in case a state change was
        missed; the client itself is the thing actively reconnecting."""
        self._apply_connection_state(self.client.is_connected())
        return True

    def _install_translucency_css(self):
        """Grid-only transparency + the bottom-left mouse-controls legend.

        Only the canvases are see-through: their backgrounds are painted at
        partial alpha and the window surface plus the immediate canvas
        containers are transparent.  Everything around the grid - the
        headerbar, tab bar, toolbars, side panels, console and the window's
        CSD edges (shadow/rounded corners removed) - is forced opaque via
        ``.opaque-chrome`` so the rest of the window stays filled.  Needs the
        compositor not to fill a border background behind the window (niri:
        ``draw-border-with-background false`` for this app-id).

        The opaque color is written literally (not ``@window_bg_color``):
        this is a plain ``Gtk.Application`` window, so libadwaita's named
        colors aren't loaded and the named-color rule would be dropped,
        leaving the chrome transparent."""
        from render_utils import theme_palette, theme_color

        r, g, b = theme_palette(self).get("bg", (0.1, 0.1, 0.1))
        bg_hex = "#%02x%02x%02x" % (
            int(round(r * 255)),
            int(round(g * 255)),
            int(round(b * 255)),
        )
        # Side-panel "alt" background: the GTK headerbar/sidebar color (the
        # brighter one Firefox uses for its titlebar/active tab), falling
        # back to the window bg lightened a little if the theme lacks it.
        alt = theme_color(self, "headerbar_bg_color", None)
        if alt is None:
            alt = theme_color(self, "sidebar_bg_color", None)
        if alt is None:
            alt = tuple(min(1.0, c + 0.05) for c in (r, g, b))
        alt_hex = "#%02x%02x%02x" % (
            int(round(alt[0] * 255)),
            int(round(alt[1] * 255)),
            int(round(alt[2] * 255)),
        )
        css = Gtk.CssProvider()
        css.load_from_data(
            (
                "window.translucent-canvas {"
                "  background-color: transparent;"
                "  background-image: none;"
                "  box-shadow: none;"
                "  border-width: 0;"
                "  border-radius: 0; }"
                "window.translucent-canvas .csd,"
                "window.translucent-canvas headerbar,"
                "window.translucent-canvas notebook > header {"
                f"  background-color: {bg_hex};"
                "  box-shadow: none;"
                "  border-width: 0;"
                "  border-radius: 0; }"
                # GtkPaned's 1px handle would otherwise be a transparent line
                # between the panels/canvas.
                "window.translucent-canvas paned > separator {"
                f"  background-color: {bg_hex}; }}"
                "window.translucent-canvas notebook > stack,"
                "window.translucent-canvas paned,"
                "window.translucent-canvas overlay {"
                "  background-color: transparent; }"
                # No notebook padding/border: it would be a transparent gap
                # around the page (the "padding area") at the window edges.
                "window.translucent-canvas notebook {"
                "  padding: 0; border-width: 0; }"
                "window.translucent-canvas .opaque-chrome {"
                f"  background-color: {bg_hex}; }}"
                # The side panels: the background must cover their padding,
                # so the padding is CSS (inside the background) rather than a
                # GTK margin (which would sit outside and show as a
                # transparent strip at the window edge).
                ".side-panel-fill {"
                f"  background-color: {alt_hex};"
                "  padding: 8px; }"
                # The panel-file list is a darker "card" inside the lighter
                # side panel; round its corners.
                ".panels-list {"
                f"  background-color: {bg_hex};"
                "  border-radius: 10px;"
                "  padding: 4px; }"
                ".panels-list-inner { background: transparent; }"
                ".panels-list-inner > row { background: transparent; }"
                ".mouse-help {"
                "  background-color: rgba(0, 0, 0, 0.38);"
                "  border-radius: 8px; padding: 6px 9px; }"
                ".mouse-help label { color: #ffffff; font-size: 11px; }"
            ).encode()
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, css, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

    def _install_loading_css(self):
        # GTK4 has no per-widget background-color setter; a display-wide
        # provider with one class is the sanctioned way to get the faint
        # dim behind the spinner.  Added once (re-adding would stack
        # providers on every window).
        if getattr(self, "_loading_css_installed", False):
            return
        self._loading_css_installed = True
        css = Gtk.CssProvider()
        css.load_from_data(
            b".loading-overlay { background-color: rgba(0, 0, 0, 0.28); }"
            b".disconnected-badge {"
            b"  background-color: rgba(150, 44, 44, 0.92);"
            b"  border-radius: 10px; padding: 5px 10px; }"
            b".disconnected-badge label { color: #ffffff; font-size: 12px; }"
            b".selection-delete {"
            b"  background-color: rgba(180, 50, 50, 0.92);"
            b"  border-radius: 999px; padding: 8px;"
            b"  box-shadow: 0 2px 6px rgba(0, 0, 0, 0.35); }"
            b".selection-delete image { color: #ffffff; }"
        )
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                css,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

    def _on_loading_changed(self, loading):
        """Show/hide the loading wheel and keep the log console visible
        for the duration of a session import.  On finish, only collapse
        the console if WE opened it - a console the user opened stays
        open."""
        if self._loading_overlay is not None:
            self._loading_overlay.set_visible(loading)
        if self._loading_spinner is not None:
            if loading:
                self._loading_spinner.start()
            else:
                self._loading_spinner.stop()

        button = getattr(self, "_console_button", None)
        if button is None:
            return
        if loading:
            if not self.log_console.active:
                self._console_auto_opened = True
                button.set_active(True)
        elif self._console_auto_opened:
            self._console_auto_opened = False
            button.set_active(False)

    def _build_patchspace_toolbar(self):
        """Strip under the PatchSpace canvas: the log-console toggle, the
        Recenter view control, the selection readout, and the selection
        actions (Group / Anchor).  Every button carries a hover tooltip
        explaining what it does.  (Rebuild lives in the top-right
        hamburger menu now - see _build_patchspace_page.)"""
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        bar.add_css_class("opaque-chrome")
        bar.set_margin_top(4)
        bar.set_margin_bottom(4)
        bar.set_margin_start(8)
        bar.set_margin_end(8)

        # Console toggle on the left of the strip - opens/closes the log
        # console beneath it (see LogConsole).  Icon + text so it stays
        # visible even on a theme missing the symbolic icon.
        self._console_button = Gtk.ToggleButton()
        self._console_button.set_tooltip_text("Show/hide the daemon log console")
        console_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        console_box.append(Gtk.Image.new_from_icon_name("utilities-terminal-symbolic"))
        console_box.append(Gtk.Label(label="Logs"))
        self._console_button.set_child(console_box)
        self._console_button.connect("toggled", self._on_console_toggled)
        bar.append(self._console_button)

        # "Recenter" sits immediately right of Logs: re-frames the whole
        # canvas (PatchSpaceGraphWidget.zoom_to_fit) - horizontally AND
        # vertically centred - so a graph that has drifted off-screen (or
        # been zoomed way out) comes back into view.
        self._recenter_button = Gtk.Button()
        self._recenter_button.set_tooltip_text("Recenter and fit all nodes")
        recenter_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        recenter_box.append(Gtk.Image.new_from_icon_name("zoom-fit-best-symbolic"))
        recenter_box.append(Gtk.Label(label="Recenter"))
        self._recenter_button.set_child(recenter_box)
        self._recenter_button.connect(
            "clicked", lambda _b: self.ps_widget.zoom_to_fit()
        )
        bar.append(self._recenter_button)

        self._ps_selection_label = Gtk.Label(label="No selection")
        self._ps_selection_label.set_halign(Gtk.Align.START)
        self._ps_selection_label.set_hexpand(True)
        bar.append(self._ps_selection_label)

        self._ps_group_button = Gtk.Button(label="Group")
        self._ps_group_button.set_sensitive(False)
        self._ps_group_button.set_tooltip_text(
            "Wrap the selected nodes in a labelled, colored group"
        )
        self._ps_group_button.connect(
            "clicked", lambda _b: self.ps_widget.create_group_from_selection()
        )
        bar.append(self._ps_group_button)

        self._ps_anchor_button = Gtk.Button(label="Anchor")
        self._ps_anchor_button.set_sensitive(False)
        self._ps_anchor_button.set_tooltip_text(
            "Pin the selected nodes in place (they can still be dragged)"
        )
        self._ps_anchor_button.connect(
            "clicked", lambda _b: self.ps_widget.toggle_anchor_selected()
        )
        bar.append(self._ps_anchor_button)

        # Create a new panel, optionally moving the current selection into
        # it (so it stays enabled with nothing selected).
        self._ps_declare_button = Gtk.Button(label="Create Panel\u2026")
        self._ps_declare_button.set_tooltip_text(
            "Create a new panel file (moves the selected nodes into it, if any)"
        )
        self._ps_declare_button.connect(
            "clicked",
            lambda _b: self.ps_widget.show_create_panel_dialog(),
        )
        bar.append(self._ps_declare_button)

        # Vertically centre every strip control; a horizontal Gtk.Box
        # otherwise FILLs each child to the bar's full height, which left
        # the short icon+label buttons sitting taller than they needed to.
        for widget in (
            self._console_button,
            self._recenter_button,
            self._ps_selection_label,
            self._ps_group_button,
            self._ps_anchor_button,
            self._ps_declare_button,
        ):
            widget.set_valign(Gtk.Align.CENTER)

        self.ps_widget.on_selection_changed.append(self._update_patchspace_toolbar)
        self._update_patchspace_toolbar()
        return bar

    def _update_patchspace_toolbar(self):
        widget = self.ps_widget
        count = len(widget.selected_nodes)
        if hasattr(self, "_ps_delete_button"):
            self._ps_delete_button.set_visible(count > 0)
        if count == 0:
            self._ps_selection_label.set_label("No selection")
            self._ps_group_button.set_sensitive(False)
            self._ps_anchor_button.set_sensitive(False)
            self._ps_anchor_button.set_label("Anchor")
            return
        all_anchored = all(
            nid in widget.anchored_nodes for nid in widget.selected_nodes
        )
        plural = "s" if count != 1 else ""
        self._ps_selection_label.set_label(f"{count} node{plural} selected")
        self._ps_group_button.set_sensitive(True)
        self._ps_declare_button.set_sensitive(True)
        self._ps_anchor_button.set_sensitive(True)
        self._ps_anchor_button.set_label("Unanchor" if all_anchored else "Anchor")

    def _on_console_toggled(self, button):
        if self.log_console is not None:
            self.log_console.set_active(button.get_active())

    def process_responses(self):
        for resp in self.client.get_responses():
            if resp.get("status") != "ok":
                if resp.get("status") == "error":
                    logger.warning("daemon error: %s", resp.get("message"))
                continue
            try:
                if "graph" in resp:
                    self.pw_widget.update_graph(resp["graph"])
                elif "devices" in resp:
                    self.ps_widget.on_hardware_devices(resp["devices"])
                elif "titles" in resp:
                    self.ps_widget.on_titles(resp["titles"])
                elif "applications" in resp:
                    self.ps_widget.on_applications(resp["applications"])
                elif "profiles" in resp:
                    self.ps_widget.on_device_profiles(resp)
                elif "config" in resp:
                    self.ps_widget.on_export_config(resp["config"])
                elif resp.get("panel_files"):
                    self._update_panels_view(resp)
                elif "payload" in resp and "panel_id" in resp:
                    self.ps_widget.on_panel_export(resp)
                elif "stem" in resp and ("removed" in resp or "auto_load" in resp):
                    # A panel-*file* action completed (delete, auto-load):
                    # re-read the listing so the side view updates now
                    # rather than at the next periodic refresh.
                    self._refresh_panels_view()
                elif "panel_id" in resp:
                    # create_panel / clone_panel landed - a new file exists.
                    self._refresh_panels_view()
                elif "nodes" in resp:
                    self.ps_widget.update_from_daemon(resp)
            except Exception:
                import traceback

                traceback.print_exc()
        return True

    def _daemon_action(self, action):
        """Run a daemon start/stop/restart off the GTK main thread - each
        waits on the Unix socket to appear/disappear - then refresh once
        it's done so the canvases repopulate (or clear)."""
        if getattr(self, "_daemon_busy", False):
            return
        self._daemon_busy = True

        def _run():
            try:
                if action == "start":
                    ok = self.daemon.start()
                elif action == "stop":
                    ok = self.daemon.stop()
                else:
                    ok = self.daemon.restart()
                logger.info("Daemon %s %s", action, "ok" if ok else "failed")
            except Exception:
                logger.exception("Daemon %s failed", action)
            finally:
                GLib.idle_add(self._daemon_action_done)

        threading.Thread(target=_run, daemon=True).start()

    def _daemon_action_done(self):
        self._daemon_busy = False
        # The old connection (if any) is to a daemon that may be gone or
        # replaced; drop it so the client redials on the next command.
        try:
            self.client._drop_connection()
        except Exception:
            pass
        self.pw_widget.refresh()
        self.ps_widget.refresh()
        return False

    def on_close(self, *args):
        if self.log_console is not None:
            self.log_console.stop()
        # Tear down the daemon only if this GUI started it; an adopted
        # daemon is deliberately left running.
        try:
            self.daemon.shutdown_owned()
        except Exception:
            logger.exception("Daemon shutdown on close failed")
        self.client.stop()
