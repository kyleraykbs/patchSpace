"""
main_window.py

The application window: a two-tab notebook (raw PipeWire graph +
PatchSpace editor) sharing one PatchBayClient connection, plus the
GLib timeout that drains daemon responses and routes each one to
whichever tab it belongs to.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from constants import (
    POLL_RESPONSES_MS,
    ADD_NODE_PANEL_WIDTH,
    ADD_NODE_PANEL_MIN_WIDTH,
    ADD_NODE_PANEL_MAX_WIDTH,
)
from socket_client import PatchBayClient
from pipewire_widget import PipeWireGraphWidget
from patchspace_widget import PatchSpaceGraphWidget

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


class MainWindow(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app)
        self.set_title("PatchBay")
        self.set_default_size(1200, 800)

        self.client = PatchBayClient()

        self.notebook = Gtk.Notebook()
        self.pw_widget = PipeWireGraphWidget(self.client)
        self.ps_widget = PatchSpaceGraphWidget(self.client)

        self.notebook.append_page(self.pw_widget, Gtk.Label(label="PipeWire Graph"))
        self.notebook.append_page(
            self._build_patchspace_page(), Gtk.Label(label="PatchSpace Graph")
        )

        self.set_child(self.notebook)

        self.pw_widget.refresh()
        self.ps_widget.refresh()

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
        menu (Export/Import) pinned to its top-right corner. ps_widget
        itself is a plain Gtk.DrawingArea with no room for child
        widgets, so the menu button lives in a Gtk.Overlay wrapped
        around it instead of inside it."""
        overlay = Gtk.Overlay()
        overlay.set_child(self.ps_widget)
        overlay.set_hexpand(True)
        overlay.set_vexpand(True)

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
        # 180 rather than 140 now that "Import Last Session" is the
        # longest label in here.
        box.set_size_request(180, -1)

        export_btn = Gtk.Button(label="Export\u2026")
        export_btn.get_child().set_wrap(False)
        export_btn.connect(
            "clicked",
            lambda _b: (popover.popdown(), self.ps_widget.show_export_dialog()),
        )
        box.append(export_btn)

        import_btn = Gtk.Button(label="Import\u2026")
        import_btn.get_child().set_wrap(False)
        import_btn.connect(
            "clicked",
            lambda _b: (popover.popdown(), self.ps_widget.show_import_dialog()),
        )
        box.append(import_btn)

        # Pulls from the daemon's auto-saved cache (see main.py's
        # _auto_export_session) rather than a user-chosen file - never
        # loaded automatically, only on this explicit click. See
        # PatchSpaceGraphWidget.show_import_last_session.
        import_last_btn = Gtk.Button(label="Import Last Session")
        import_last_btn.get_child().set_wrap(False)
        import_last_btn.connect(
            "clicked",
            lambda _b: (popover.popdown(), self.ps_widget.show_import_last_session()),
        )
        box.append(import_last_btn)

        popover.set_child(box)
        menu_button.set_popover(popover)

        overlay.add_overlay(menu_button)

        add_node_panel = self.ps_widget.build_add_node_panel()

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
        page.set_end_child(overlay)
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

    def process_responses(self):
        for resp in self.client.get_responses():
            if resp.get("status") != "ok":
                if resp.get("status") == "error":
                    print(
                        f"[patchbay] daemon error: {resp.get('message')}",
                        file=sys.stderr,
                    )
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
                elif "nodes" in resp:
                    self.ps_widget.update_from_daemon(resp)
            except Exception:
                import traceback

                traceback.print_exc()
        return True

    def on_close(self, *args):
        self.client.stop()
