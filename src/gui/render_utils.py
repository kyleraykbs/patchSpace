"""
render_utils.py

Theme-color lookup and small Cairo/Pango drawing helpers shared by
both graph widgets. Nothing here holds any state - every function
takes what it needs as arguments.
"""

from __future__ import annotations

import math
import zlib

from gi.repository import Pango, PangoCairo

_FALLBACK_BG = (0.137, 0.137, 0.145)
_FALLBACK_NODE_BG = (0.196, 0.196, 0.208)
_FALLBACK_NODE_BORDER = (0.36, 0.36, 0.39)
_FALLBACK_TEXT = (0.93, 0.93, 0.94)
_FALLBACK_SUBTEXT = (0.63, 0.63, 0.66)
_FALLBACK_INPUT_PORT = (0.35, 0.78, 0.51)
_FALLBACK_OUTPUT_PORT = (0.94, 0.47, 0.42)
_FALLBACK_BOOLEAN_PORT = (0.55, 0.55, 0.58)
_FALLBACK_LINK = (0.42, 0.65, 0.98)
_FALLBACK_SELECT = (0.98, 0.76, 0.24)
_FALLBACK_PENDING_LINK = (0.98, 0.76, 0.24)
_FALLBACK_WARNING = (0.94, 0.65, 0.22)
_FALLBACK_ERROR = (0.88, 0.20, 0.20)
_FALLBACK_SUCCESS = (0.30, 0.72, 0.42)

_THEME_COLOR_NAMES = [
    "accent_color",
    "success_color",
    "warning_color",
    "error_color",
    "destructive_color",
]


def _lookup(widget, name, fallback):
    ctx = widget.get_style_context()
    ok, rgba = ctx.lookup_color(name)
    if ok:
        return (rgba.red, rgba.green, rgba.blue)
    return fallback


def theme_color(widget, name, fallback):
    """Public named-colour lookup from the running GTK theme (e.g.
    "accent_color", "success_color"), so callers can assign stable,
    theme-consistent colours to specific things instead of hashing an
    arbitrary string.  Falls back to `fallback` when the theme doesn't
    define `name`."""
    return _lookup(widget, name, fallback)


def _stable_index(text: str, modulo: int) -> int:
    """A process-stable hash of `text` (Python's built-in hash() is
    salted per process, which made the same string pick a different
    colour every time the app restarted)."""
    return zlib.crc32(text.encode("utf-8")) % modulo


def theme_palette(widget) -> dict:
    return {
        "bg": _lookup(widget, "window_bg_color", _FALLBACK_BG),
        "node_bg": _lookup(widget, "card_bg_color", _FALLBACK_NODE_BG),
        "node_border": _lookup(widget, "borders", _FALLBACK_NODE_BORDER),
        "text": _lookup(widget, "window_fg_color", _FALLBACK_TEXT),
        "subtext": _lookup(widget, "dim_label_color", _FALLBACK_SUBTEXT),
        "input_port": _FALLBACK_INPUT_PORT,
        "output_port": _FALLBACK_OUTPUT_PORT,
        "boolean_port": _FALLBACK_BOOLEAN_PORT,
        "link": _lookup(widget, "accent_color", _FALLBACK_LINK),
        "select": _FALLBACK_SELECT,
        "pending_link": _FALLBACK_PENDING_LINK,
        "warning": _lookup(widget, "warning_color", _FALLBACK_WARNING),
        "error": _lookup(widget, "error_color", _FALLBACK_ERROR),
        "success": _lookup(widget, "success_color", _FALLBACK_SUCCESS),
    }


def theme_class_color(widget, class_str, fallback=None):
    """Deterministically map an arbitrary string (media class, node
    type, ...) to one of the theme's semantic accent colors, so node
    borders get some visual variety without a hand-maintained color
    table."""
    if fallback is None:
        fallback = _lookup(widget, "borders", _FALLBACK_NODE_BORDER)
    if not class_str:
        return fallback
    idx = _stable_index(class_str, len(_THEME_COLOR_NAMES))
    return _lookup(widget, _THEME_COLOR_NAMES[idx], fallback)


def draw_rounded_rect(cr, x, y, w, h, r=10):
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
    cr.close_path()


def draw_text(cr, x, y, text, font_size=12, color=(1, 1, 1)):
    cr.set_font_size(font_size)
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    cr.show_text(text)


def draw_text_ellipsized(cr, x, y, text, max_width, font_size, color):
    layout = PangoCairo.create_layout(cr)
    layout.set_text(text, -1)
    layout.set_font_description(Pango.FontDescription.from_string(f"sans {font_size}"))
    layout.set_width(int(max_width * Pango.SCALE))
    layout.set_ellipsize(Pango.EllipsizeMode.END)
    PangoCairo.update_layout(cr, layout)
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)


def draw_text_unbounded(cr, x, y, text, font_size, color):
    """Draw one line of text with no width/ellipsis constraint.

    Used for a group's title: it sits above its box and the box never
    constrains it, so it must always read in full.  The ellipsized
    variant measured the text in unscaled world units but let
    PangoCairo apply the view's zoom to the font, so as soon as you
    zoomed in the fixed width clipped the title to an ellipsis."""
    layout = PangoCairo.create_layout(cr)
    layout.set_text(text or "", -1)
    layout.set_font_description(Pango.FontDescription.from_string(f"sans {font_size}"))
    PangoCairo.update_layout(cr, layout)
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)


def draw_text_wrapped(cr, x, y, text, max_width, font_size, color):
    """Like draw_text_ellipsized, but wraps onto as many lines as it
    needs instead of truncating one line with an ellipsis - used for
    a node's label/id text, which should always be fully readable
    rather than cut off (see PatchSpaceGraphWidget._draw_header). Word-
    char wrapping (not plain word wrapping) so a single long unbroken
    token - a node id like "node_1738699999999" - still wraps instead
    of overflowing the node's width. Returns the pixel height the
    drawn text actually occupied, so the caller can stack further
    lines below it.

    Callers that need this height *before* drawing (to size the node
    itself - see node_height()) should use wrapped_text_height()
    below instead; it computes the identical layout without needing a
    Cairo context, since node sizing runs outside on_draw."""
    layout = PangoCairo.create_layout(cr)
    layout.set_text(text, -1)
    layout.set_font_description(Pango.FontDescription.from_string(f"sans {font_size}"))
    layout.set_width(int(max_width * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    PangoCairo.update_layout(cr, layout)
    cr.set_source_rgb(*color)
    cr.move_to(x, y)
    PangoCairo.show_layout(cr, layout)
    return layout.get_pixel_size()[1]


# Node sizing calls wrapped_text_height hundreds of times per redraw
# (node_height -> header/extra height, the group bounds walk, the force
# layout), always for the same handful of (label, width, font) tuples.
# Each call built a throwaway Pango layout; profiling a ~35-node graph
# showed this was ~44% of on_draw's cost.  The height is a pure function
# of the text and the widget's font setup, so memoise it.  Keyed by
# id(widget) so the two graph widgets can't bleed into each other; the
# entry count is bounded so a session that renames nodes constantly can't
# grow it without limit.
_WRAPPED_HEIGHT_CACHE: dict = {}
_WRAPPED_HEIGHT_CACHE_MAX = 8192


def wrapped_text_height(widget, text, max_width, font_size):
    """Pixel height `text` would occupy if drawn with
    draw_text_wrapped() at the same max_width/font_size - without
    needing a Cairo context, so this can be called from sizing/layout
    code (node_height(), the force-layout sizes dict) that runs
    outside on_draw, where no `cr` exists yet. Uses `widget`'s own
    Pango context (Gtk.Widget.create_pango_layout()) rather than
    PangoCairo.create_layout(), which is the only reason this needs a
    widget and draw_text_wrapped() above doesn't - text metrics come
    from the same font/fontconfig setup either way, so the two stay
    in agreement.

    Memoised - see _WRAPPED_HEIGHT_CACHE above."""
    if not text:
        return 0
    key = (id(widget), text, max_width, font_size)
    cached = _WRAPPED_HEIGHT_CACHE.get(key)
    if cached is not None:
        return cached
    layout = widget.create_pango_layout(text)
    layout.set_font_description(Pango.FontDescription.from_string(f"sans {font_size}"))
    layout.set_width(int(max_width * Pango.SCALE))
    layout.set_wrap(Pango.WrapMode.WORD_CHAR)
    height = layout.get_pixel_size()[1]
    if len(_WRAPPED_HEIGHT_CACHE) >= _WRAPPED_HEIGHT_CACHE_MAX:
        _WRAPPED_HEIGHT_CACHE.clear()
    _WRAPPED_HEIGHT_CACHE[key] = height
    return height


def draw_bezier_link(cr, x1, y1, x2, y2):
    dx = max(40, abs(x2 - x1) * 0.5)
    cr.move_to(x1, y1)
    cr.curve_to(x1 + dx, y1, x2 - dx, y2, x2, y2)
    cr.stroke()


def draw_smooth_path(cr, points, iterations=3):
    """Stroke a polyline as a smooth curve by Chaikin corner-cutting.

    The routed wire detours are orthogonal, which reads as rigid; a few
    rounds of Chaikin replace each corner with a gentle arc.  The result
    stays inside the polyline's convex hull, so it can't bulge back into
    a node the route was avoiding, and it keeps the endpoints exact."""
    if not points:
        return
    pts = list(points)
    if len(pts) > 2:
        for _ in range(max(1, iterations)):
            smoothed = [pts[0]]
            for i in range(len(pts) - 1):
                x0, y0 = pts[i]
                x1, y1 = pts[i + 1]
                smoothed.append((0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1))
                smoothed.append((0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1))
            smoothed.append(pts[-1])
            pts = smoothed
    cr.move_to(pts[0][0], pts[0][1])
    for x, y in pts[1:]:
        cr.line_to(x, y)
    cr.stroke()


def draw_grid_background(cr, pal, pan_x, pan_y, zoom, width, height, spacing=40):
    """Subtle line grid drawn in world space, so it pans and zooms
    with the graph like a node editor's canvas instead of staying
    fixed to the screen. Call this right after apply_view_transform()
    and before any nodes/edges are drawn.

    The canvas transform makes "the whole graph" conceptually
    unbounded, so this works backward from the current viewport: given
    pan/zoom, it derives the visible world-space rectangle and only
    draws the grid lines that fall inside it, rather than drawing a
    fixed-size grid that would either not cover the view or draw
    thousands of pointless offscreen lines.
    """
    if zoom <= 0 or width <= 0 or height <= 0:
        return

    left = -pan_x / zoom
    top = -pan_y / zoom
    right = left + width / zoom
    bottom = top + height / zoom

    r, g, b = pal.get("node_border", _FALLBACK_NODE_BORDER)
    cr.set_source_rgba(r, g, b, 0.15)
    # A hairline at every zoom level, not a line that gets thicker as
    # you zoom in - screen-space width divided out by the same zoom
    # the transform will multiply it back by.
    cr.set_line_width(1.0 / zoom)

    x = math.floor(left / spacing) * spacing
    while x <= right:
        cr.move_to(x, top)
        cr.line_to(x, bottom)
        x += spacing

    y = math.floor(top / spacing) * spacing
    while y <= bottom:
        cr.move_to(left, y)
        cr.line_to(right, y)
        y += spacing

    cr.stroke()
