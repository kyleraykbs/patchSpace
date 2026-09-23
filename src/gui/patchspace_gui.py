#!/usr/bin/env python3
"""
patchspace_gui.py

Entrypoint for the Patch Space GTK4 client. Run this against a running
patchspace daemon (main.py) - it talks to it over the Unix socket at
constants.SOCKET_PATH (default $XDG_RUNTIME_DIR/patchspace.sock, or
$PATCHSPACE_SOCKET).
"""
import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib

import constants
from main_window import MainWindow


class PatchSpaceApp(Gtk.Application):
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


#: Set (e.g. by the NixOS/home-manager module, from stylix's application
#: opacity) to give the canvas a default background opacity.  The command
#: line still wins; the point of the variable is that a launcher-declared
#: window doesn't need its own desktop-entry override.
CANVAS_OPACITY_ENV = "PATCHSPACE_CANVAS_OPACITY"


def _resolve_canvas_opacity(argv, environ=None):
    """The canvas background opacity to apply: ``--canvas-opacity`` if given,
    else ``$PATCHSPACE_CANVAS_OPACITY``, else None (the built-in default).

    A bad value from the environment is ignored with a warning - it comes
    from configuration, and refusing to start over it would be worse than
    drawing an opaque canvas.  A bad *command line* value is still fatal."""
    environ = os.environ if environ is None else environ
    cleaned, value = _extract_canvas_opacity(argv)
    if value is not None:
        return cleaned, value
    raw = environ.get(CANVAS_OPACITY_ENV)
    if raw:
        try:
            float(raw)
        except ValueError:
            print(
                f"ignoring {CANVAS_OPACITY_ENV}={raw!r}: expected a number 0..1",
                file=sys.stderr,
            )
            return cleaned, None
        return cleaned, raw
    return cleaned, None


def main():
    argv, opacity = _resolve_canvas_opacity(sys.argv)
    if opacity is not None:
        try:
            constants.CANVAS_BG_ALPHA = max(0.0, min(1.0, float(opacity)))
            constants.CANVAS_BG_ALPHA_EXPLICIT = True
        except ValueError:
            print(
                f"invalid --canvas-opacity {opacity!r}: expected a number 0..1",
                file=sys.stderr,
            )
            raise SystemExit(2)
    app = PatchSpaceApp()
    app.run(argv)


if __name__ == "__main__":
    main()
