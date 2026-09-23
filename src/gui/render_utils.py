"""
render_utils.py

Theme-color lookup and small Cairo/Pango drawing helpers shared by
both graph widgets. Nothing here holds any state - every function
takes what it needs as arguments.
"""

from __future__ import annotations

import math
import zlib

from gi.repository import Gtk, Pango, PangoCairo

_FALLBACK_BG = (0.137, 0.137, 0.145)
_FALLBACK_NODE_BG = (0.196, 0.196, 0.208)
_FALLBACK_NODE_BORDER = (0.36, 0.36, 0.39)
_FALLBACK_TEXT = (0.93, 0.93, 0.94)
# Entry-like chrome (a node's text field, a device row's select box).  The
# theme's view colours are what a real GtkEntry uses, so a themed desktop
# (stylix recolours these) gets entries that match the rest of the window
# instead of a fixed near-black box.
_FALLBACK_VIEW_BG = (0.14, 0.14, 0.15)
_FALLBACK_VIEW_FG = (0.85, 0.85, 0.86)
# The theme's blue, used for anything that should read as "the active value":
# the impulse port/button and the sliders.  Adwaita's blue_3 is #3584e4; under
# stylix it is the palette's blue.  The fallback is the blue the sliders always
# used, for a theme that defines no named colours at all.
_FALLBACK_ACCENT = (0.4, 0.7, 0.9)
# How far a field's background is lifted off the surface behind it (the node
# card).  Every named "surface" colour a theme offers is the *same* tone as the
# card here - window_bg/view_bg are darker still - so an entry that should read
# as a lighter inset is derived from the card rather than looked up.
FIELD_BG_LIFT = 0.16
_FALLBACK_SUBTEXT = (0.63, 0.63, 0.66)
_FALLBACK_INPUT_PORT = (0.35, 0.78, 0.51)
_FALLBACK_OUTPUT_PORT = (0.94, 0.47, 0.42)
_FALLBACK_BOOLEAN_PORT = (0.55, 0.55, 0.58)
# Bundle wires carry a *set* of streams, filter wires a classifier
# predicate - both are control-plane-ish, so they get distinct cool
# colours rather than the audio green/red.
_FALLBACK_BUNDLE_PORT = (0.55, 0.80, 0.95)
_FALLBACK_FILTER_PORT = (0.78, 0.62, 0.95)
# An impulse is a momentary event, not a stream: a cyan that no other
# socket kind uses, so a Button's output is unmistakable next to the
# audio green/red and the boolean grey.
_FALLBACK_IMPULSE_PORT = (0.30, 0.86, 0.86)
_FALLBACK_LINK = (0.42, 0.65, 0.98)
_FALLBACK_SELECT = (0.98, 0.76, 0.24)
_FALLBACK_PENDING_LINK = (0.98, 0.76, 0.24)
_FALLBACK_WARNING = (0.94, 0.65, 0.22)
_FALLBACK_ERROR = (0.88, 0.20, 0.20)

# Grid safety caps (see draw_grid_background): below this on-screen line
# spacing the grid is a flat wash, and this absolute ceiling guards against
# a pathological zoom making the line loops run away.
_MIN_GRID_SCREEN_SPACING = 6.0
_MAX_GRID_LINES = 2000
_FALLBACK_SUCCESS = (0.30, 0.72, 0.42)

_THEME_COLOR_NAMES = [
    "accent_color",
    "success_color",
    "warning_color",
    "error_color",
    "destructive_color",
]


def _theme_key():
    """A cheap key that changes when the theme / colour scheme changes, so
    named-colour lookups can be cached across frames (they were re-queried
    from GTK dozens of times per frame)."""
    try:
        settings = Gtk.Settings.get_default()
        return (
            settings.get_property("gtk-theme-name"),
            settings.get_property("gtk-application-prefer-dark-theme"),
        )
    except Exception:
        return None


_COLOR_CACHE = {}


def _lookup(widget, name, fallback):
    key = _theme_key()
    cache_key = (name, key)
    if key is not None and cache_key in _COLOR_CACHE:
        cached = _COLOR_CACHE[cache_key]
        return cached if cached is not None else fallback
    ctx = widget.get_style_context()
    ok, rgba = ctx.lookup_color(name)
    if ok:
        value = (rgba.red, rgba.green, rgba.blue)
        if key is not None:
            _COLOR_CACHE[cache_key] = value
        return value
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


def _lighten(color, amount, toward=(1.0, 1.0, 1.0)):
    """Blend `color` `amount` of the way toward `toward` (white by default),
    so a derived tone follows the theme instead of being a literal."""
    return tuple(
        c + (t - c) * max(0.0, min(1.0, amount))
        for c, t in zip(color, toward)
    )


def theme_palette(widget) -> dict:
    return {
        "bg": _lookup(widget, "window_bg_color", _FALLBACK_BG),
        "node_bg": _lookup(widget, "card_bg_color", _FALLBACK_NODE_BG),
        "node_border": _lookup(widget, "borders", _FALLBACK_NODE_BORDER),
        "text": _lookup(widget, "window_fg_color", _FALLBACK_TEXT),
        "subtext": _lookup(widget, "dim_label_color", _FALLBACK_SUBTEXT),
        "field_bg": _lighten(
            _lookup(widget, "card_bg_color", _FALLBACK_NODE_BG), FIELD_BG_LIFT
        ),
        "field_fg": _lookup(widget, "view_fg_color", _FALLBACK_VIEW_FG),
        "input_port": _FALLBACK_INPUT_PORT,
        "output_port": _FALLBACK_OUTPUT_PORT,
        "boolean_port": _FALLBACK_BOOLEAN_PORT,
        "bundle_port": _FALLBACK_BUNDLE_PORT,
        "filter_port": _FALLBACK_FILTER_PORT,
        # An impulse is a momentary event, not a stream: the theme's own
        # blue (Adwaita's #3584e4 by default; stylix maps blue_3 to its
        # palette) so it reads as a distinct, vibrant signal and still
        # follows the desktop's colours.  Falls back to the old cyan.
        "impulse_port": _lookup(widget, "blue_3", _FALLBACK_IMPULSE_PORT),
        # The active-value colour: sliders, and anything else that is "set".
        "accent": _lookup(widget, "blue_3", _FALLBACK_ACCENT),
        # A slider's unfilled groove: the card tone lifted the same amount as
        # an entry field, so it reads as an inset rather than a grey bar.
        "slider_track": _lighten(
            _lookup(widget, "card_bg_color", _FALLBACK_NODE_BG), FIELD_BG_LIFT
        ),
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


def draw_text_unbounded(cr, x, y, text, font_size, color, bold=False):
    """Draw one line of text with no width/ellipsis constraint.

    Used for a group's title: it sits above its box and the box never
    constrains it, so it must always read in full.  The ellipsized
    variant measured the text in unscaled world units but let
    PangoCairo apply the view's zoom to the font, so as soon as you
    zoomed in the fixed width clipped the title to an ellipsis.

    ``bold`` is used for panel titles, which read as headings over their
    box rather than annotations beside it."""
    layout = PangoCairo.create_layout(cr)
    layout.set_text(text or "", -1)
    weight = "bold " if bold else ""
    layout.set_font_description(
        Pango.FontDescription.from_string(f"sans {weight}{font_size}")
    )
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


# One corner radius for every wire bend.  Kept constant (not scaled by the
# adjoining segment) so a wire's curves don't visibly vary; the router keeps
# its segments at least 2*radius long (stub >= MIN_STUB) so the clamp below
# rarely kicks in.
CORNER_RADIUS = 14.0

#: Straight run left between the two bends that share a segment.  Without it a
#: one-cell jog (a wire stepping a few pixels between two nearly level sockets)
#: was blended end to end and read as a smooth sigmoid rather than a step.
MIN_STRAIGHT = 6.0


def draw_square_path(cr, points, radius=CORNER_RADIUS):
    """Stroke an axis-aligned polyline as a flowing "squared" wire.

    The router (gui/wire_router.py) returns right-angled detours around
    nodes/panels, already reduced to few turns (see
    PatchSpaceGraphWidget._simplify_orthogonal).  Each bend is blended over
    the constant ``radius`` as a cubic whose control points both sit at the
    vertex; the radius is only shortened when a segment is too short to fit
    it, so consecutive bends join into smooth curves rather than hard right
    angles (cairo has no arc-to, which is why the corner is a Bezier)."""
    pts = list(points)
    if len(pts) < 2:
        return
    cr.move_to(*pts[0])
    for i in range(1, len(pts) - 1):
        ax, ay = pts[i - 1]
        vx, vy = pts[i]
        bx, by = pts[i + 1]
        inx, iny = vx - ax, vy - ay
        outx, outy = bx - vx, by - vy
        lin = math.hypot(inx, iny)
        lout = math.hypot(outx, outy)
        if lin < 1e-6 or lout < 1e-6:
            cr.line_to(vx, vy)
            continue
        # Generous corner: on the ~one-cell segments the router emits this
        # consumes the whole segment, so consecutive bends join into a
        # smooth sigmoid instead of leaving a stub of straight line.
        r = min(radius, lin / 2.0, lout / 2.0)
        if lin > MIN_STRAIGHT:
            r = min(r, (lin - MIN_STRAIGHT) / 2.0)
        if lout > MIN_STRAIGHT:
            r = min(r, (lout - MIN_STRAIGHT) / 2.0)
        cr.line_to(vx - inx / lin * r, vy - iny / lin * r)
        cr.curve_to(vx, vy, vx, vy, vx + outx / lout * r, vy + outy / lout * r)
    cr.line_to(*pts[-1])
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

    Zoomed far out the visible world rect is enormous, so the line count
    explodes (a near-freeze at ZOOM_MIN).  Skip the grid once the lines
    would be closer than MIN_GRID_SCREEN_SPACING on screen - at that point
    it is a flat wash anyway."""
    if zoom <= 0 or width <= 0 or height <= 0:
        return

    left = -pan_x / zoom
    top = -pan_y / zoom
    right = left + width / zoom
    bottom = top + height / zoom

    # Cap the line count two ways: screen spacing (density) and an absolute
    # ceiling, so a pathological zoom can never make this loop run away.
    line_px = spacing * zoom
    if line_px < _MIN_GRID_SCREEN_SPACING:
        return
    cols = (right - left) / spacing
    rows = (bottom - top) / spacing
    if cols > _MAX_GRID_LINES or rows > _MAX_GRID_LINES:
        return

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
