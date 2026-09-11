"""
main_window.py

The application window: a two-tab notebook (raw PipeWire graph +
PatchSpace editor) sharing one PatchBayClient connection, plus the
GLib timeout that drains daemon responses and routes each one to
whichever tab it belongs to.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Gdk, GLib, Adw

from constants import (
    POLL_RESPONSES_MS,
    LOG_POLL_MS,
    DAEMON_POLL_MS,
    ADD_NODE_PANEL_WIDTH,
    ADD_NODE_PANEL_MIN_WIDTH,
    ADD_NODE_PANEL_MAX_WIDTH,
)
from socket_client import PatchBayClient
from daemon_control import DaemonManager
from pipewire_widget import PipeWireGraphWidget
from patchspace_widget import PatchSpaceGraphWidget

logger = logging.getLogger(__name__)

# Wall-clock heartbeat of the GTK main thread, updated by _heartbeat() on
# a GLib timeout. Only used by the PATCHBAY_TRACE_HANG watchdog below.
_heartbeat_time = [0.0]


def _heartbeat() -> bool:
    _heartbeat_time[0] = time.monotonic()
    return True


def _arm_hang_watchdog() -> None:
    """Diagnostic for the "UI randomly freezes" report (no exception, no
    traceback): a hard hang here is a Python busy-loop on the main
    thread, which never returns control to the GLib loop, so GTK can't
    tell us anything. Enable with PATCHBAY_TRACE_HANG=1 when launching
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
        self.client = PatchBayClient()
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
            self._view.scroll_to_iter(
                self._buffer.get_end_iter(), 0.0, True, 0.0, 1.0
            )
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
                self.append_lines(
                    resp.get("lines", []), resp.get("last_seq", 0)
                )
            elif resp.get("status") == "error":
                self._append_text(
                    f"[log console] daemon error: {resp.get('message')}"
                )

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
                self._buffer.get_iter_at_line(
                    self._line_count - self.MAX_LINES
                ),
            )
            self._line_count = self.MAX_LINES

    def stop(self) -> None:
        if self._poll_id:
            GLib.source_remove(self._poll_id)
            self._poll_id = 0
        self.client.stop()



class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("Patch Space")
        self.set_default_size(1200, 800)

        # Explicit titlebar with the program name (the default CSD title
        # also shows it, but this makes the name unambiguous and matches
        # the libadwaita look).
        header = Adw.HeaderBar()
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
        self.client = PatchBayClient()

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
        # Heartbeat + optional hang watchdog - see _arm_hang_watchdog.
        _heartbeat_time[0] = time.monotonic()
        GLib.timeout_add(200, _heartbeat)
        if os.environ.get("PATCHBAY_TRACE_HANG"):
            _arm_hang_watchdog()

    def _build_patchspace_page(self):
        """The PatchSpace tab is a side panel (drag-and-drop "add
        node" source, see PatchSpaceGraphWidget.build_add_node_panel)
        next to the ps_widget canvas, which itself carries a hamburger
        menu (Export/Import/declarative/daemon actions) pinned to its
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

        # Declarative node files are loaded automatically at daemon
        # start-up now (main.py's _load_startup_sessions), so the old
        # "Import Last Session" button is gone.  This dialog is the
        # management surface: list files, rename/delete the writable ones,
        # and see which nodes each one defines.
        declarative_btn = Gtk.Button(label="Declarative Nodes\u2026")
        declarative_btn.get_child().set_wrap(False)
        declarative_btn.set_tooltip_text(
            "Manage file-backed nodes loaded from the declarative directories"
        )
        declarative_btn.connect(
            "clicked",
            lambda _b: (
                popover.popdown(),
                self.ps_widget.show_declarative_dialog(),
            ),
        )
        box.append(declarative_btn)

        reload_declarative_btn = Gtk.Button(label="Reload Declarative Files")
        reload_declarative_btn.get_child().set_wrap(False)
        reload_declarative_btn.set_tooltip_text(
            "Re-read every declarative file now (daemon-side edits will be lost)"
        )
        reload_declarative_btn.connect(
            "clicked",
            lambda _b: (
                popover.popdown(),
                self.ps_widget.reload_declarative(),
            ),
        )
        box.append(reload_declarative_btn)

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
            ("Start Daemon", "start", "Start the background PatchBay daemon"),
            ("Stop Daemon", "stop", "Stop the background PatchBay daemon"),
            ("Restart Daemon", "restart", "Restart the background PatchBay daemon"),
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
        canvas_area.append(self._build_patchspace_toolbar())
        canvas_area.append(self.log_console)
        # Load progress (start/stop) drives the overlay + console
        # auto-open/collapse; see _on_loading_changed.
        self.ps_widget.on_loading_changed.append(self._on_loading_changed)

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

        return page

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

    def _poll_daemon_connection(self):
        """Fallback to the client's live state in case a state change was
        missed; the client itself is the thing actively reconnecting."""
        self._apply_connection_state(self.client.is_connected())
        return True

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
        console_box.append(
            Gtk.Image.new_from_icon_name("utilities-terminal-symbolic")
        )
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
        recenter_box.append(
            Gtk.Image.new_from_icon_name("zoom-fit-best-symbolic")
        )
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
            "Wrap the selected nodes in a labelled, coloured group"
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

        # Write the selection into a declarative file, so a Nix config
        # (or anything else) can own and re-derive it.
        self._ps_declare_button = Gtk.Button(label="Declare\u2026")
        self._ps_declare_button.set_sensitive(False)
        self._ps_declare_button.set_tooltip_text(
            "Export the selected nodes to a declarative file in the "
            "read-write declarative directory"
        )
        self._ps_declare_button.connect(
            "clicked",
            lambda _b: self.ps_widget.show_export_declarative_dialog(),
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
        if count == 0:
            self._ps_selection_label.set_label("No selection")
            self._ps_group_button.set_sensitive(False)
            self._ps_anchor_button.set_sensitive(False)
            self._ps_declare_button.set_sensitive(False)
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
                elif "applications" in resp:
                    self.ps_widget.on_applications(resp["applications"])
                elif "profiles" in resp:
                    self.ps_widget.on_device_profiles(resp)
                elif "config" in resp:
                    self.ps_widget.on_export_config(resp["config"])
                elif "files" in resp and "directories" in resp:
                    self.ps_widget.on_declarative_files(resp)
                elif resp.get("declarative_action"):
                    self.ps_widget.on_declarative_action(resp)
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
