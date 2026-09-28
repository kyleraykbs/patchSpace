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
        # A closed window is gone.  Clearing it means a later activation
        # builds a *fresh* one instead of calling present() on a destroyed
        # window, which is what made the UI look like it reopened itself.
        self.window = None
        # And closing the window closes the app: the daemon has its own
        # service (services.patchspace), so there is nothing here worth
        # staying alive for.
        self.quit()
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


#: Where the NVIDIA kernel module announces itself.  Its presence is the
#: only reliable "this machine runs the NVIDIA driver" signal that needs
#: no dependencies (no lspci, no nvidia-smi).
NVIDIA_VERSION_PATH = "/proc/driver/nvidia/version"

#: Where glvnd's EGL vendor JSONs live.  glvnd - the dispatch layer libEGL
#: resolves to on NixOS and every modern distro - picks the EGL vendor
#: *implementation* independently of anything Mesa's own loader does, and
#: it happily loads every vendor it finds.  Pinning the vendor to Mesa's
#: JSON is the only thing that keeps the NVIDIA EGL implementation out of
#: the process at all.
EGL_VENDOR_DIRS = (
    "/run/opengl-driver/share/glvnd/egl_vendor.d",  # NixOS driver link farm
    "/usr/share/glvnd/egl_vendor.d",  # FHS distros
)


def _mesa_egl_vendor_json():
    """The Mesa EGL vendor JSON on this machine, or None if there isn't
    one (a distro glvnd is happy to enumerate for itself)."""
    for directory in EGL_VENDOR_DIRS:
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if "mesa" in name and name.endswith(".json"):
                return os.path.join(directory, name)
    return None


def _apply_nvidia_render_fallback(environ=None, proc_path=NVIDIA_VERSION_PATH):
    """Force software rendering when the NVIDIA driver is loaded; leave
    every other GPU alone.

    Why: the NVIDIA driver keeps freed video buffers in its own heap
    instead of returning them (the ``GLVidHeapReuseRatio`` quirk the niri
    wiki works around with an application profile).  Under capture load -
    a screenshare running - that heap inflates until EGL allocations fail
    outright (``EGL_BAD_ALLOC``; watched it take a Qt shell down in the
    journal while this GUI's surface wedged: Python healthy, main loop
    pumping, window dead even to fullscreen, because a CSD window cannot
    ack a configure without fresh buffers).  The canvas is a cairo
    DrawingArea that repaints at 2 fps when idle - the GPU was never doing
    meaningful work here - so software rendering costs little and behaves
    the same on every card.

    Two layers are needed, because two layers pick the renderer:

    * ``LIBGL_ALWAYS_SOFTWARE=1`` makes *Mesa* use llvmpipe, and
      ``GSK_RENDERER=cairo`` keeps GSK's compositing on the CPU;
    * ``__EGL_VENDOR_LIBRARY_FILENAMES`` pins glvnd's EGL to Mesa's vendor
      JSON.  Without it glvnd still loads libEGL_nvidia for the Wayland
      surface - measured: the NVIDIA EGL stack in /proc/<pid>/maps even
      with both flags above set - and every buffer allocation stays on the
      driver we are trying to starve-proof.  With it the surface is
      presented through Mesa (wl_shm), off the NVIDIA heap entirely.

    AMD/Intel (Mesa) are untouched: Mesa manages VRAM properly and the
    bug does not exist there, so those keep their real GPU path.  An
    explicit environment always wins - this only fills in what is unset -
    so ``GSK_RENDERER=gl`` from the session and any user override stand."""
    environ = os.environ if environ is None else environ
    if not os.path.exists(proc_path):
        return False
    for key, value in (("LIBGL_ALWAYS_SOFTWARE", "1"), ("GSK_RENDERER", "cairo")):
        if not environ.get(key):
            environ[key] = value
    if not environ.get("__EGL_VENDOR_LIBRARY_FILENAMES"):
        json_path = _mesa_egl_vendor_json()
        if json_path:
            environ["__EGL_VENDOR_LIBRARY_FILENAMES"] = json_path
    return True


def main():
    _apply_nvidia_render_fallback()
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
