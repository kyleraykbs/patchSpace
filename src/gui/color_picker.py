"""
color_picker.py

A small, self-contained HSV colour picker used by the group settings
dialog.

Deliberately NOT a Gtk.ColorButton / Gtk.ColorChooserDialog: those pull
in GtkColorDialog, which reads GSettings for its "recent colours" list
and hard-aborts the whole process (``No GSettings schemas are installed
on the system``) on a machine without compiled schemas - a bare Nix
shell, for instance.  This draws its own saturation/value square and hue
strip with Cairo, so it needs nothing beyond GTK and PyCairo.
"""

from __future__ import annotations

import math

import cairo
from gi.repository import Gdk, Gtk


def _hsv_to_rgb(h: float, s: float, v: float):
    """(h, s, v) each 0..1 -> (r, g, b) each 0..1."""
    h = h % 1.0
    i = int(h * 6.0)
    f = h * 6.0 - i
    p = v * (1.0 - s)
    q = v * (1.0 - f * s)
    t = v * (1.0 - (1.0 - f) * s)
    i %= 6
    if i == 0:
        return (v, t, p)
    if i == 1:
        return (q, v, p)
    if i == 2:
        return (p, v, t)
    if i == 3:
        return (p, q, v)
    if i == 4:
        return (t, p, v)
    return (v, p, q)


def _rgb_to_hsv(r: float, g: float, b: float):
    """(r, g, b) each 0..1 -> (h, s, v) each 0..1."""
    mx = max(r, g, b)
    mn = min(r, g, b)
    d = mx - mn
    if d == 0:
        h = 0.0
    elif mx == r:
        h = ((g - b) / d) % 6.0
    elif mx == g:
        h = (b - r) / d + 2.0
    else:
        h = (r - g) / d + 4.0
    h /= 6.0
    s = 0.0 if mx == 0 else d / mx
    return (h % 1.0, s, mx)


def hex_to_rgb(value: str):
    text = (value or "#3584e4").strip().lstrip("#")
    if len(text) != 6:
        text = "3584e4"
    try:
        return tuple(int(text[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
    except ValueError:
        return (0.2, 0.5, 0.9)


def rgb_to_hex(rgb) -> str:
    return "#%02x%02x%02x" % tuple(
        max(0, min(255, int(round(c * 255)))) for c in rgb
    )


class ColorPicker(Gtk.Box):
    """Saturation/value square + hue strip + preview + hex entry."""

    SV_WIDTH = 200
    SV_HEIGHT = 150
    HUE_WIDTH = 18
    MARKER_RADIUS = 6

    def __init__(self, initial: str = "#3584e4", presets=(), resolve=None):
        """`initial` may be a hex string or one of the theme-slot values the
        presets use (see `resolve`); `resolve(value)` maps a stored value to
        RGB, so a caller can keep a panel or group coloured by *slot* - the
        picker holds that value, not the hex it happens to resolve to, and
        `get_value()` hands it back unchanged until the user edits the colour
        by hand."""
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._resolve = resolve or hex_to_rgb
        self._value = initial
        self._h, self._s, self._v = _rgb_to_hsv(*self._resolve(initial))
        self._updating_entry = False

        if presets:
            self.append(self._build_presets(presets))

        swatches = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self._sv = Gtk.DrawingArea()
        self._sv.set_content_width(self.SV_WIDTH)
        self._sv.set_content_height(self.SV_HEIGHT)
        self._sv.set_draw_func(self._draw_sv)
        sv_drag = Gtk.GestureDrag()
        sv_drag.set_button(Gdk.BUTTON_PRIMARY)
        sv_drag.connect("drag-begin", lambda g, *_: self._apply_sv(g))
        sv_drag.connect("drag-update", lambda g, *_: self._apply_sv(g))
        self._sv.add_controller(sv_drag)
        swatches.append(self._sv)

        self._hue = Gtk.DrawingArea()
        self._hue.set_content_width(self.HUE_WIDTH)
        self._hue.set_content_height(self.SV_HEIGHT)
        self._hue.set_draw_func(self._draw_hue)
        hue_drag = Gtk.GestureDrag()
        hue_drag.set_button(Gdk.BUTTON_PRIMARY)
        hue_drag.connect("drag-begin", lambda g, *_: self._apply_hue(g))
        hue_drag.connect("drag-update", lambda g, *_: self._apply_hue(g))
        self._hue.add_controller(hue_drag)
        swatches.append(self._hue)

        self.append(swatches)

        bottom = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._preview = Gtk.DrawingArea()
        self._preview.set_content_width(26)
        self._preview.set_content_height(26)
        self._preview.set_draw_func(self._draw_preview)
        bottom.append(self._preview)

        self._hex_entry = Gtk.Entry()
        self._hex_entry.set_width_chars(9)
        self._hex_entry.set_max_width_chars(9)
        self._hex_entry.connect("changed", self._on_hex_changed)
        bottom.append(self._hex_entry)
        self.append(bottom)

        self._sync_from_state(update_entry=True)

    # -- presets ---------------------------------------------------------

    def _build_presets(self, presets):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for item in presets:
            # A preset is either a stored value (a slot like "@blue", or a
            # hex) or a (value, rgb) pair - the pair form lets the caller say
            # what the swatch looks like *now* while the button still stores
            # the slot, so the colour keeps following the theme.
            if isinstance(item, tuple):
                value, color = item
            else:
                value, color = item, self._resolve(item)
            button = Gtk.Button()
            button.set_has_frame(False)
            button.set_tooltip_text(str(value))
            area = Gtk.DrawingArea()
            area.set_content_width(18)
            area.set_content_height(18)

            def _draw(_area, cr, _w, _h, color=color):
                cr.set_source_rgb(*color)
                cr.paint()

            area.set_draw_func(_draw)
            button.set_child(area)
            button.connect("clicked", lambda _b, v=value: self.set_value(v))
            row.append(button)
        return row

    # -- public API ------------------------------------------------------

    def get_hex(self) -> str:
        """The concrete colour currently shown, as hex."""
        return rgb_to_hex(_hsv_to_rgb(self._h, self._s, self._v))

    def get_value(self) -> str:
        """What to store: the value this picker was opened with (a slot like
        `@blue` keeps following the theme), or a hex once the user changed the
        colour by hand."""
        return self._value

    def set_value(self, value: str) -> None:
        self._value = value
        self._h, self._s, self._v = _rgb_to_hsv(*self._resolve(value))
        self._sync_from_state(update_entry=True)

    def set_hex(self, value: str) -> None:
        self._value = value
        self._h, self._s, self._v = _rgb_to_hsv(*hex_to_rgb(value))
        self._sync_from_state(update_entry=True)

    # -- interaction -----------------------------------------------------

    def _apply_sv(self, gesture):
        ok, sx, sy = gesture.get_start_point()
        if not ok:
            return
        ok, ox, oy = gesture.get_offset()
        x = sx + (ox if ok else 0.0)
        y = sy + (oy if ok else 0.0)
        self._s = max(0.0, min(1.0, x / self.SV_WIDTH))
        self._v = max(0.0, min(1.0, 1.0 - y / self.SV_HEIGHT))
        self._value = self.get_hex()          # edited by hand: no longer a slot
        self._sync_from_state(update_entry=True)

    def _apply_hue(self, gesture):
        ok, _sx, sy = gesture.get_start_point()
        if not ok:
            return
        ok, _ox, oy = gesture.get_offset()
        y = sy + (oy if ok else 0.0)
        self._h = max(0.0, min(1.0, y / self.SV_HEIGHT)) % 1.0
        self._value = self.get_hex()
        self._sync_from_state(update_entry=True)

    def _on_hex_changed(self, entry):
        if self._updating_entry:
            return
        text = entry.get_text().strip().lstrip("#")
        if len(text) != 6:
            return
        try:
            int(text, 16)
        except ValueError:
            return
        self._h, self._s, self._v = _rgb_to_hsv(*hex_to_rgb(text))
        self._value = "#" + text.lower()
        self._sv.queue_draw()
        self._hue.queue_draw()
        self._preview.queue_draw()

    def _sync_from_state(self, update_entry=False):
        if update_entry:
            self._updating_entry = True
            self._hex_entry.set_text(self.get_hex())
            self._updating_entry = False
        self._sv.queue_draw()
        self._hue.queue_draw()
        self._preview.queue_draw()

    # -- drawing ---------------------------------------------------------

    def _draw_sv(self, _area, cr, w, h):
        hue = _hsv_to_rgb(self._h, 1.0, 1.0)
        horizontal = cairo.LinearGradient(0, 0, w, 0)
        horizontal.add_color_stop_rgb(0, 1, 1, 1)
        horizontal.add_color_stop_rgb(1, *hue)
        cr.set_source(horizontal)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        vertical = cairo.LinearGradient(0, 0, 0, h)
        vertical.add_color_stop_rgba(0, 0, 0, 0, 0)
        vertical.add_color_stop_rgba(1, 0, 0, 0, 1)
        cr.set_source(vertical)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        cx = self._s * w
        cy = (1.0 - self._v) * h
        r = self.MARKER_RADIUS
        cr.set_line_width(2)
        cr.set_source_rgb(0, 0, 0)
        cr.arc(cx, cy, r, 0, 2 * math.pi)
        cr.stroke()
        cr.set_source_rgb(1, 1, 1)
        cr.arc(cx, cy, r - 1.5, 0, 2 * math.pi)
        cr.stroke()

    def _draw_hue(self, _area, cr, w, h):
        gradient = cairo.LinearGradient(0, 0, 0, h)
        for i in range(7):
            r, g, b = _hsv_to_rgb(i / 6.0, 1.0, 1.0)
            gradient.add_color_stop_rgb(i / 6.0, r, g, b)
        cr.set_source(gradient)
        cr.rectangle(0, 0, w, h)
        cr.fill()

        cy = self._h * h
        cr.set_line_width(2)
        cr.set_source_rgb(0, 0, 0)
        cr.move_to(0, cy)
        cr.line_to(w, cy)
        cr.stroke()
        cr.set_line_width(1)
        cr.set_source_rgb(1, 1, 1)
        cr.move_to(0, cy)
        cr.line_to(w, cy)
        cr.stroke()

    def _draw_preview(self, _area, cr, w, h):
        cr.set_source_rgb(*_hsv_to_rgb(self._h, self._s, self._v))
        cr.rectangle(0, 0, w, h)
        cr.fill()
        cr.set_source_rgba(0, 0, 0, 0.4)
        cr.set_line_width(1)
        cr.rectangle(0.5, 0.5, w - 1, h - 1)
        cr.stroke()
