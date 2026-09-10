#!/usr/bin/env python3
"""
patchbay_gui.py

Entrypoint for the PatchBay GTK4 client. Run this against a running
patchbay daemon (main.py) - it talks to it over the Unix socket at
constants.SOCKET_PATH (default /tmp/patchbay.sock).
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

from main_window import MainWindow


class PatchBayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.example.patchbay")
        # User-facing program name (window titlebar / task switcher).
        GLib.set_application_name("Patch Space")
        self.window = None

    def do_activate(self):
        Adw.StyleManager.get_default().set_color_scheme(Adw.ColorScheme.PREFER_DARK)
        if not self.window:
            self.window = MainWindow(self)
            self.window.connect("close-request", self._on_close_request)
        self.window.present()

    def _on_close_request(self, window):
        window.on_close()
        return False

    def do_shutdown(self):
        if self.window:
            self.window.client.stop()
        Gtk.Application.do_shutdown(self)


def main():
    app = PatchBayApp()
    app.run(sys.argv)


if __name__ == "__main__":
    main()
