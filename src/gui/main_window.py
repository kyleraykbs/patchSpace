"""
main_window.py

The application window: a two-tab notebook (raw PipeWire graph +
PatchSpace editor) sharing one PatchBayClient connection, plus the
GLib timeout that drains daemon responses and routes each one to
whichever tab it belongs to.
"""

from __future__ import annotations

import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from constants import POLL_RESPONSES_MS
from socket_client import PatchBayClient
from pipewire_widget import PipeWireGraphWidget
from patchspace_widget import PatchSpaceGraphWidget


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

        page = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        page.append(add_node_panel)
        page.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))
        page.append(overlay)
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
