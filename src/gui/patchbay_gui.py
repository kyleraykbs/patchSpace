#!/usr/bin/env python3
"""
patchbay_gui.py

Entrypoint for the PatchBay GTK4 client. Run this against a running
patchbay daemon (main.py) - it talks to it over the Unix socket at
constants.SOCKET_PATH (default /tmp/patchbay.sock, or $PATCHBAY_SOCKET).
"""
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

import constants
from main_window import MainWindow


class PatchBayApp(Gtk.Application):
    def __init__(self):
        super().__init__(application_id="org.patchspace")
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


def _extract_canvas_opacity(argv):
    """Pull ``--canvas-opacity VALUE`` / ``--canvas-opacity=VALUE`` out of
    ``argv`` (GTK's GApplication would reject the unknown option otherwise).
    Returns (cleaned_argv, value_or_None)."""
    cleaned = [argv[0]]
    value = None
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--canvas-opacity" and i + 1 < len(argv):
            value = argv[i + 1]
            i += 2
            continue
        if arg.startswith("--canvas-opacity="):
            value = arg.split("=", 1)[1]
            i += 1
            continue
        cleaned.append(arg)
        i += 1
    return cleaned, value


def main():
    argv, opacity = _extract_canvas_opacity(sys.argv)
    if opacity is not None:
        try:
            constants.CANVAS_BG_ALPHA = max(0.0, min(1.0, float(opacity)))
        except ValueError:
            print(
                f"invalid --canvas-opacity {opacity!r}: expected a number 0..1",
                file=sys.stderr,
            )
            raise SystemExit(2)
    app = PatchBayApp()
    app.run(argv)


if __name__ == "__main__":
    main()
