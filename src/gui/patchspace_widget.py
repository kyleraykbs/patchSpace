"""
patchspace_widget.py

The "PatchSpace Graph" tab: the editable signal-chain node editor,
with inline volume sliders, mute-switch checkboxes, a big gate toggle,
and inline text fields (regex/media-class/description). New nodes can
be added via the right-click canvas menu or dragged in from the
add-node side panel (see build_add_node_panel()).

Every inline control funnels through exactly two methods -
_send_set_volume() and _send_set_gate() - which are the only places
in this file that build a "set_volume"/"set_gate" command dict. That
means there's one place to look to confirm a control actually talks
to the daemon, instead of the same command being built ad-hoc in three
different event handlers with no guarantee they stayed in sync.

Node-type-specific behavior (what sockets a type has, what inline
control/field it draws, its display label) all comes from
node_specs.NODE_TYPE_SPECS instead of being duplicated across
if/elif chains here.
"""

from __future__ import annotations

import cairo
import json
import os
import constants
import logging
import math
import random
import time

from gi.repository import Gtk, Gdk, GdkPixbuf, GLib, GObject, Gsk, Pango

from constants import (
    REFRESH_INTERVAL_MS,
    LAYOUT_TICK_MS,
    LAYOUT_SETTLE_TICKS,
    LAYOUT_SETTLE_EPSILON,
    LAYOUT_SAVE_DEBOUNCE_MS,
    POST_MUTATION_REFRESH_MS,
    VOLUME_SEND_EPSILON,
    ADD_NODE_PANEL_MIN_WIDTH,
    GRAPH_CANVAS_MIN_SIZE,
    SESSION_LOAD_OVERLAY_MIN_MS,
    SESSION_LOAD_OVERLAY_TIMEOUT_MS,
    NODE_MATERIALIZE_MS,
    NODE_FADE_MS,
    NODE_LOADING_ALPHA,
    NODE_DELETE_MS,
    ANIM_TICK_MS,
    IMPULSE_FLASH_MS,
    EDGE_DRAW_MS,
    ZOOM_MIN,
    ZOOM_MAX,
)
from render_utils import (
    theme_palette,
    theme_color,
    theme_class_color,
    draw_rounded_rect,
    draw_text_ellipsized,
    draw_text_unbounded,
    draw_text_wrapped,
    wrapped_text_height,
    draw_bezier_link,
    draw_square_path,
    draw_grid_background,
)
from force_layout import ForceLayout
from wire_router import (
    BUNDLE_SPACING as WIRE_BUNDLE_SPACING,
    CELL as WIRE_CELL,
    MARGIN as WIRE_MARGIN,
    MIN_STUB as WIRE_MIN_STUB,
    PAD as WIRE_PAD,
    SPACING as WIRE_SPACING,
    STUB as WIRE_STUB,
    orthogonalize as wire_orthogonalize,
    polyline_rects as wire_polyline_rects,
    route as route_wire,
    segment_blocked,
    simplify as wire_simplify,
)
from view_mixin import GraphViewMixin
from node_specs import (
    ADD_NODE_MENU_ITEMS,
    ADD_NODE_CATEGORIES,
    FIELD_LABELS,
    media_class_choices_for,
    media_class_label,
    normalize_node_type,
    spec_for,
    port_kind,
    is_mute_node,
    type_label,
    description_for,
    setting_tooltip,
    icon_for_add_node_type,
    color_name_for_node_type,
)
from portal_file_dialog import open_file, save_file
from color_picker import ColorPicker
from bool_state import resolve_bool_state_from_poll

logger = logging.getLogger(__name__)

# The world grid drawn by draw_grid_background is 40px; wire waypoints snap
# to its half-step so runs sit on grid lines "as best as possible".
WIRE_GRID_STEP = 20.0

# A socket stub shorter than this is treated as "no usable sideways exit":
# it reads as no exit at all, and the cleanup pass (_drop_short_straights,
# keyed on WIRE_GRID_STEP) merges it away - leaving the wire to hug its own
# node's border.  Below this the stub is attached as a short leg *after*
# cleanup instead (or replaced by a perpendicular jog).
MIN_USABLE_STUB = WIRE_GRID_STEP

# Length of the transparent fade at the leading tip while a freshly-made
# connection draws itself in (see _draw_growing_wire).
WIRE_FADE = 34.0

# Dash pattern for a bundle or filter wire - a bundle stands for a whole
# set of streams and a filter wire carries a predicate, so neither reads
# as a single solid audio link.
BUNDLE_DASH = (5.0, 4.0)

# Dash pattern for an impulse wire - shorter and tighter than BUNDLE_DASH,
# so a momentary event (a Button firing a Sound Effect) never reads as a
# dashed bundle of streams.
IMPULSE_DASH = (2.5, 3.0)


def choice_row_visibility(labels, text):
    """Which rows a searchable choice list shows for `text`, and whether the
    typed text needs a row of its own.

    Matching is a case-insensitive substring; the raw text gets its own row
    whenever it is set and isn't already one of the labels, because the fields
    these lists feed (the Title classifier's) match substrings - "YouTube" is a
    valid value even when no live title is exactly that.  Split out from the
    popover so the rule is testable without GTK."""
    low = (text or "").strip().lower()
    if not low:
        return list(labels), False
    matching = [label for label in labels if low in label.lower()]
    typed = not any(label.lower() == low for label in labels)
    return matching, typed


class PatchSpaceGraphWidget(Gtk.DrawingArea, GraphViewMixin):
    NODE_WIDTH = 180
    # Extra world padding around the viewport when culling offscreen draws,
    # so a panel's floating title / a node's shadow just outside still draw.
    _CULL_MARGIN = 260.0
    NODE_HEIGHT = 80
    # Top padding before the first header line, and the gap between
    # each wrapped header line thereafter (type label, then
    # description/label/id - see _draw_header/_header_blocks).
    HEADER_TOP_PAD = 14
    #: Left padding every header line is drawn at (the `x + 10` in
    #: `_draw_header`); one place so the measurement and the drawing agree.
    HEADER_SIDE_PAD = 10
    HEADER_BLOCK_GAP = 4
    SLIDER_HEIGHT = 16
    SLIDER_MARGIN = 10
    FIELD_HEIGHT = 22
    FIELD_MARGIN = 10
    #: Space kept below a node's field row (and below the device rows), so the
    #: field's own border isn't sitting on the node's.  One constant for the
    #: drawing (`_field_rect`) and the height reservation
    #: (`_bottom_control_height`) - they used to disagree by two pixels.
    FIELD_BOTTOM_PAD = 10
    # Extra height a node-body switch row needs (the Sound Effect's
    # Stack switch).  The switch is drawn above the inline field/control
    # row; see _bottom_control_height and _toggle_switch_rect, which
    # derive their geometry from this one number.
    TOGGLE_ROW_HEIGHT = 20
    # Gap between that switch row and the inline field/control below it.
    # Without it the switch's bottom edge rides the field box's top edge;
    # the node grows by this much (see _bottom_control_height) so the
    # switch moves up without eating into the field.
    TOGGLE_ROW_GAP = 6
    # The inline on/off switch a `toggle` row draws: the gate toggle's
    # two-segment shape shrunk to sit in that row beside its caption
    # (see _toggle_switch_rect / _draw_toggle_row).
    TOGGLE_SWITCH_WIDTH = 62
    TOGGLE_SWITCH_HEIGHT = 18
    # Room reserved on the right of a node's inline field for its live
    # status read-out (a dot + count) - see _play_indicator_rect.
    INDICATOR_WIDTH = 30
    # The folder button a `picker` spec draws in the field row, between the
    # field and the status read-out (see _path_picker_rect), and the file
    # types it offers.  The globs are the ones the sound-effect player can
    # actually open (pw-cat reads through libsndfile + MP3 - see
    # SoundEffectNode), so an m4a/aac file libsndfile would refuse isn't
    # the first thing on offer.
    PICKER_SIZE = 18
    PICKER_GAP = 5
    PICKER_FILE_GLOBS = [
        "*.wav", "*.flac", "*.ogg", "*.oga", "*.opus", "*.mp3", "*.aiff",
        "*.aif", "*.au", "*.caf", "*.w64", "*.rf64",
    ]
    # The gate control is a big centered toggle rather than a small
    # checkbox (see _draw_gate_toggle/_gate_rect), so it claims more
    # of the node body than the generic "has_extra_row" bump other
    # single-row controls use.
    GATE_HEIGHT = 32
    GATE_MARGIN = 14
    GATE_BOTTOM_MARGIN = 10
    GATE_AREA_HEIGHT = GATE_HEIGHT + GATE_BOTTOM_MARGIN + 8
    # THE standard vertical spacing between neighbouring socket circles
    # on a node, in pixels, whenever a node has more than one socket on a
    # side: Echo Cancel's two inputs ("mic"/"probe") and the Switcher's
    # two outputs ("a"/"b") both lay out at this gap.  node_height()
    # reserves enough room for it and _socket_position() distributes the
    # circles evenly across that room, so the same rule governs where
    # sockets are drawn, where they are hit-tested and where edges land.
    # Big enough that the label text beside one socket never runs into
    # its neighbour.
    SOCKET_MIN_STEP = 22
    # Radius of a drawn socket circle. Kept as a constant so the layout
    # maths (how much room a stack of sockets needs) and the drawing can
    # never drift apart.
    SOCKET_RADIUS = 6
    # How close the pointer has to get to a socket to start/land a
    # connection. Much larger than the drawn circle: nodes drift under
    # the force layout, so a tight target made wiring frustrating.
    # find_socket_at returns the NEAREST socket within this radius, so
    # neighbouring sockets (e.g. the Switcher's a/b, 22px apart) still
    # resolve to the one you actually meant.
    SOCKET_HIT_RADIUS = 16
    # Horizontal room reserved in the node header for the anchor icon and
    # three-dot menu, so a long type label wraps before running under them.
    # Kept just wide enough for the widest type label ("Inv. Switcher")
    # to stay on one line.
    HEADER_ICON_RESERVE = 44
    # The node-type glyph drawn in the header's top-left corner, and the
    # room its row reserves to the left of the first header line so the
    # type label wraps/ellipsizes before running under it.
    NODE_ICON_SIZE = 16
    NODE_ICON_LEFT_RESERVE = 22
    # A *compact* node (see _COMPACT_NODE_TYPES: splitter and the boolean
    # logic gates) with no label renders as a plain square this many
    # pixels on a side (just the three-dot menu and, dimmed, the anchor
    # badge), so it stays out of the way instead of occupying a full node.
    # Give it a label and it grows back out to NODE_WIDTH to wrap the text.
    SPLITTER_MIN_SIZE = 64
    # Padding a group's dotted box leaves around its member nodes, and
    # the default color palette new groups cycle through.
    GROUP_PADDING = 26
    # Extra inset per group a group encloses, so a group that surrounds
    # other groups leaves a visible gap instead of drawing its dotted box
    # right on top of theirs.
    GROUP_SPACING = 18
    # Extra clearance a container leaves above an enclosed group's name
    # block (label/id/color chip), on top of that block's own height, so
    # a nested group's name never touches or pokes past its container's
    # top edge.  A small deliberate bump, not a full GROUP_SPACING.
    GROUP_NAME_BUMP = 6
    # How far a group's member bounds may fall *outside* another's and
    # still count as enclosed.  Node positions are continuous floats, so
    # two groups that are conceptually nested ("Config" vs. a narrower
    # "Noise Cancel Config" sharing most of its nodes) routinely miss
    # strict containment by a fraction of a pixel.  Without slack the
    # container wouldn't grow around the inner group and their name blocks
    # would be drawn in the same spot.
    GROUP_ENCLOSE_TOLERANCE = 2.0
    GROUP_COLORS = (
        "#3584e4",  # blue
        "#33d17a",  # green
        "#f5c211",  # amber
        "#e01b24",  # red
        "#9141ac",  # purple
        "#2ec27e",  # teal
    )

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.nodes = {}
        self.edges = {}
        self.nodes_pending_wiring = set()
        # Cached node pixel dimensions, keyed by node id.  A single redraw
        # asks for them thousands of times (edge endpoints, socket
        # positions, group bounds, hit tests); they only change when the
        # daemon poll updates a node's label/spec/state, so they are
        # cleared at the start of update_from_daemon rather than
        # recomputed per call.  See node_height/node_width.
        self._node_h_cache = {}
        self._node_w_cache = {}
        # Node-type glyphs, rendered once from the GTK icon theme to a
        # pixbuf and cached: {(icon_name, size, rgb): GdkPixbuf.Pixbuf}.
        # Rendering goes through a Gsk.CairoRenderer (GTK4 no longer
        # paints symbolic icons straight onto a foreign cairo context).
        self._node_icon_cache: dict = {}
        # Per-node appearance animation: {nid: monotonic birth time} for the
        # materialize scale-up, and {nid: alpha} faded toward 1 as a node
        # finishes loading (see _anim_tick / _draw_node).
        self._node_born: dict = {}
        self._node_alpha: dict = {}
        self._node_fade: dict = {}
        # Nodes the ticker has already started animating, so a node's pop
        # begins exactly once - when it first becomes visible.
        self._anim_seen: set = set()
        # Deleted nodes linger as a fading outline: [{x,y,w,h,t0}] plus the
        # ids already ghosted so a delete isn't double-started (user action
        # then the poll that drops the node).
        self._ghosts: list = []
        self._ghosted: set = set()
        # Optimistically-added nodes waiting for the daemon to report them
        # (heavy nodes take seconds to spawn).  A poll that arrives before
        # the daemon has created the node must NOT cull the placeholder, or
        # the node later reappears at the daemon's default position - see
        # the deletion loop in update_from_daemon.
        self._placeholder_since: dict = {}
        # Connections the user just made: their wire draws itself in from
        # source to target.  _pending_edge_draw holds edge ids we've asked
        # the daemon to create (before the poll echoes them); once the edge
        # appears the ticker stamps its birth in _edge_born and it animates
        # for EDGE_DRAW_MS.  Keyed by the deterministic edge id so a poll
        # that lags the command doesn't matter.
        self._pending_edge_draw: dict = {}
        self._edge_born: dict = {}
        # A removed connection retracts: its last drawn path is snapshotted
        # (eid -> {points, is_bool, t0}) and drawn shrinking from the target
        # back to the source, the reverse of the draw-in animation.
        self._edge_ghosts: dict = {}
        self._edge_ghosted: set = set()
        # Buttons the user just pressed: {nid: press time}, driving the
        # grey -> green -> grey pulse on the button face (see
        # _draw_impulse_button).  Purely local - the button carries no
        # state the daemon could echo back, so this is the press's only
        # feedback; the play count on whatever it fired comes from the
        # poll.
        self._impulse_flash: dict = {}
        # Per-edge timestamp of the last time wire-spacing invalidated its
        # cached route (see _route_still_valid), so two wires that keep
        # ending up within spacing of each other can't be re-routed on every
        # single frame forever.
        self._route_spacing_evicted: dict = {}
        GLib.timeout_add(ANIM_TICK_MS, self._anim_tick)
        # Per-frame geometry cache for group nesting (the enclosed-group
        # walk is O(groups^2) and was recomputed many times inside one
        # redraw).  Cleared at the start of each on_draw - see
        # _raw_group_bounds / _enclosed_group_ids / _group_bounds.
        self._group_geo_cache = {}

        self.pan_x = 0.0
        self.pan_y = 0.0
        self.panning = False
        self._dragging_volume = False
        self.pan_drag_start = (0.0, 0.0)

        # Monotonic counter so two nodes added in the same millisecond
        # (e.g. several Speaker/Mic Lines in a row) can't collide on id.
        self._node_seq = 0

        self.dragging_node = None
        # A panel port being dragged to reorder (vertical-only).
        self.dragging_port = None
        self._port_drag_start_y = 0.0
        self.drag_node_start = (0, 0)
        # When a drag moves a multi-node selection, every dragged node's
        # start position is remembered here (id -> (x, y)) so each can be
        # offset by the same delta.
        self.drag_node_starts: dict = {}

        self.connecting_from = None
        self.detaching_edge = None
        self.drag_start_xy = (0, 0)
        self.drag_current_xy = (0, 0)
        self.hover_target_node = None
        # The Button node whose face the pointer is over (see on_motion and
        # _draw_impulse_button): its face lifts a step to read as pressable.
        self.hover_impulse = None

        # Slider-drag state.
        self.slider_dragging = (
            None  # ("process"|"device", node_id) currently being dragged
        )
        self.slider_drag_start_x = 0
        self.slider_initial_volume = 0
        self._slider_last_sent = None  # last volume value we sent (throttling)

        # Effect sliders (Reverb wet/dry, Sensitivity threshold) whose
        # set_node_property triggers a slow backing rebuild on the
        # daemon. node_id -> (kind, value) for the most recent value we
        # sent; until a get_nodes poll reports that same value back,
        # any stale in-flight poll (sent while we were still dragging)
        # is ignored instead of yanking the handle backwards.
        self._pending_effect_slider = {}

        # Boolean on/off toggles (gate `enabled`, switcher / On-Off source
        # `output`) we just flipped optimistically.  (node_id, field) ->
        # value.  A get_nodes poll racing our in-flight set command would
        # otherwise report the pre-click value and the switch would flash
        # back to the opposite for a poll or two; keep our value until the
        # daemon echoes it (see _accept_bool_echo).
        self._pending_bool = {}

        self.pinned_nodes = set()

        # -- anchoring / selection ------------------------------------
        # Anchored nodes are held fixed by the force layout (they still
        # push/pull their neighbours, they just don't move themselves).
        # Nodes the user adds are anchored by default; nodes that arrive
        # from the daemon (a loaded session, the builtins, ...) are not.
        self.anchored_nodes = set()
        self._user_created_nodes = set()
        # Nodes currently marquee-selected (right-drag). Drives the
        # bottom tool panel's Anchor button.
        self.selected_nodes = set()
        # Marquee state.  select_rect is shared by the right-drag marquee
        # and the modifier (shift/ctrl) left-drag marquee.
        self.select_rect = None
        # "add" (shift-drag) / "remove" (ctrl-drag) while a modifier
        # marquee is in progress, else None.  _marquee_base is the
        # selection it started from, so each update re-derives from a
        # stable base instead of accumulating drift.
        self._marquee_mode = None
        self._marquee_base = set()
        self._marquee_start_world = (0.0, 0.0)
        self._right_drag_moved = False
        self._right_drag_mods = Gdk.ModifierType(0)
        self._right_drag_cancelled = False
        self._right_marquee_base = set()
        # (panel_id, "copy"|"save") while waiting for an export_panel reply.
        self._pending_panel_export = None
        # Panels whose local placement is ahead of the daemon (physics /
        # drag): pid -> (x, y, w, h, anchored).  See
        # _update_panels_from_daemon.
        self._pending_panels = {}
        self._right_drag_start_widget = (0.0, 0.0)
        self._right_drag_start_world = (0.0, 0.0)
        # Callbacks fired whenever the selection or anchor set changes,
        # so the tool panel can keep its button in sync.
        self.on_selection_changed: list = []
        # Callbacks fired when a session load starts/finishes, so
        # main_window can show/hide the loading-wheel overlay and
        # auto-open/collapse the log console.  `loading` is True only for
        # a bulk load_session import (see _begin_load), never for the
        # brief not-ready window of a single added node.
        self.on_loading_changed: list = []
        # The canvas opacity the daemon reports (the deployment's UI
        # preference - see main.CANVAS_OPACITY).  The window applies it, so
        # the widget only carries the value and calls back when it changes.
        self.canvas_opacity_from_daemon = None
        self.on_canvas_opacity: list = []
        self.loading = False
        self._load_started_at = 0.0
        self._load_min_visible_until = 0.0
        self._load_timeout_at = 0.0
        # The daemon reports via get_nodes whether it is running its own
        # start-up session load (which the GUI never issued, so it has no
        # command-side signal for).  Tracked so a False->True transition
        # raises the overlay exactly once.
        self._daemon_loading = False
        # GLib source for the deferred post-load zoom_to_fit (see
        # _set_loading/_fit_after_load).
        self._fit_source = 0
        # One-shot: frame the whole graph the first time it's drawn with
        # nodes and a real allocation, so a GUI launched against an
        # already-running daemon opens centred on the session instead of
        # at the (0,0) world origin.  Cleared once the initial fit runs.
        self._needs_initial_fit = True
        # GLib source id for the debounced "push layout to daemon" timer.
        self._layout_save_source = 0

        # Node groups: id -> {"label", "color" (#rrggbb), "nodes": set()}.
        # Purely a canvas annotation, persisted through the daemon like
        # positions/anchoring.  _pending_groups are ids the GUI just
        # created and hasn't seen echoed back yet, so a get_nodes poll
        # arriving first doesn't wipe them.
        self.groups: dict = {}
        self._pending_groups: set = set()
        # Armed by a group's +/- header button: (group_id, "add"|"remove"),
        # then the next node click adjusts that group's membership.
        self._group_pick_mode = None

        # Panels: first-class container boxes (see main.py's panels).
        # panel_id -> {id,parent,label,color,mode,readonly,writable,x,y,w,h,
        # anchored,path,children}.  Placement is parent-relative; the
        # canvas draws each box at the folded absolute origin.  The root
        # panel ("") is the canvas itself and is not drawn.
        self.panels: dict = {}
        self.dragging_panel = None
        self.drag_panel_start = (0.0, 0.0)
        self.drag_panel_origin = (0.0, 0.0)
        self._drag_panel_applied = (0.0, 0.0)
        self._panel_geo_cache: dict = {}
        # Wire routing, rebuilt once per frame in on_draw: the routed
        # polyline per edge, the keep-out bounds its wire adds to the
        # owning panel (so panels grow to enclose their wires), and the
        # expanded panel rects derived from those bounds.
        self._wire_routes: dict = {}
        self._wire_bounds: dict = {}
        self._wire_panel_cache: dict = {}
        # Signature of the last routed frame's inputs (node boxes, edges,
        # panels, groups, reveal state).  Routing is expensive, so if nothing
        # that affects it changed since the last frame we reuse the routes
        # wholesale instead of re-validating (and re-routing) every wire.
        self._wire_routing_sig = None
        # Edges whose last route was the obstacle-unaware _stubbed_fallback
        # (no path exists).  Those would fail the static re-validation every
        # frame and be re-routed forever, so they're reused until their
        # endpoints move.
        self._route_fallback: set = set()
        # Edges whose route actually changed on the last frame.  Cached-route
        # spacing is only re-checked against these (not against every wire
        # every frame), which stops the spacing check from churning.
        self._route_changed = None
        # Per-node/panel boxes from the last routed frame, so a cached route
        # only needs re-checking against obstacles that actually moved.
        self._wire_last_boxes = {}
        self._wire_last_panels = {}
        # Transient: set by _wire_points when it gives up to the fallback.
        self._wire_fell_back = False
        # eid -> last committed route, reused while it stays clear (see
        # _route_still_valid) so interacting wires don't flip between
        # near-equal detours every frame.
        self._route_cache: dict = {}
        # panel_id -> rect frozen when a node drag began (see _panel_rect);
        # cleared on drag end.
        self._panel_drag_baseline: dict = {}
        # Armed by a panel header's +/- button to add/remove the next
        # clicked node from that panel.
        self._panel_pick_mode = None

        # font_size -> single line's pixel height (measured once via
        # wrapped_text_height(), see _single_line_height()). Used to
        # tell whether a header block actually needed to wrap onto
        # more than one line, so node_height() only grows a node past
        # its normal size when the text genuinely doesn't fit on one
        # line at that font size.
        self._line_height_cache = {}

        # node_id -> (world_x, world_y) for a node that was just
        # created via drag-and-drop from the add-node side panel (see
        # add_node_at()/build_add_node_panel()), so update_from_daemon()
        # can place it where it was dropped instead of the default
        # grid slot. Popped the first time that node id shows up.
        self._pending_positions = {}

        self.force_layout = ForceLayout(
            spring_length=280, repulsion=70000, flow_gap=500, flow_k=0.035
        )
        # Panels are large boxes.  Their edges use size-aware springs, so
        # the rest length grows with each box's bounding box and connected
        # panels settle edge-to-edge instead of overlapping; spring_length
        # is just the gap between them.  There is deliberately *no* global
        # centre pull (it beat 1/d^2 repulsion past ~600px, so distant
        # panels crept toward each other).  Repulsion is short-range
        # (cutoff ~ one box) so nearby panels push apart - visible physics -
        # but stop instead of flinging apart without bound, and the
        # rectangle-aware overlap pass handles the rest.  The flow bias is
        # for node chains, so it's off here too.
        self.panel_force_layout = ForceLayout(
            repulsion=80000,
            spring_length=120,
            center_k=0.0,
            flow_gap=0,
            flow_k=0.0,
            repulsion_cutoff=650,
            size_aware_springs=True,
        )
        self.layout_awake = True
        self._settle_ticks = 0
        # All physics (node physics *and* panel-vs-panel) is paused by
        # default: the graph stays exactly where it is until the user
        # resumes it with the pause/resume button (or the menu check).
        # Physics runs by default; the top-right pause button (and the
        # hamburger "Physics" check) turns it off.  New panels start
        # anchored/paused regardless (see _panel_is_paused).
        self.physics_active = True
        # Consecutive awake layout ticks since the last settle/sleep -
        # capped in on_layout_tick so a non-converging layout can't
        # spin the CPU forever (see that method).
        self._awake_ticks = 0
        self._prev_node_ids = set()
        self._prev_edge_set = set()
        self._prev_panel_ids = set()
        # While a bulk load is running (`loading`), only nodes that have
        # come up are drawn, so the graph assembles live; None means "show
        # everything".
        self._revealed = None

        self.set_draw_func(self.on_draw)
        self.set_size_request(*GRAPH_CANVAS_MIN_SIZE)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_focus(True)
        # Hovering a node long enough (GTK's own tooltip delay) shows the
        # node type + its description - see _on_query_tooltip.
        self.set_has_tooltip(True)
        self.connect("query-tooltip", self._on_query_tooltip)

        drag = Gtk.GestureDrag()
        # Left button only.  A GtkGestureSingle with no button set tracks
        # EVERY button, so without this a right/middle press would also
        # run on_drag_begin() - starting a node-drag/pan/connection right
        # as the right-click popover opens - and the two grabs fought,
        # leaving the canvas unclickable after "drag, then right-click".
        # Middle-button panning has its own gesture (see
        # GraphViewMixin._init_view_controls), and right-click its own
        # GestureClick, so neither is lost by narrowing this one.
        drag.set_button(Gdk.BUTTON_PRIMARY)
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        # A drag whose sequence is claimed by a parent controller (or
        # otherwise cancelled, e.g. the pointer released over another
        # window) gets ::cancel rather than a reliable drag-end; without
        # this the canvas could stay stuck in "connecting"/"panning"
        # mode - and the pending-link grab with it - until restart.
        drag.connect("cancel", self.on_drag_cancel)
        self.add_controller(drag)
        # Kept so the right-button marquee/context handler can force a
        # still-held left drag to release before it takes the pointer.
        self._drag_gesture = drag

        # Right button: a plain click opens the context menu, a drag
        # draws a marquee and selects the nodes inside it.  One gesture
        # handles both so the two can't grab the pointer at once (the
        # old separate GestureClick(button=3) opened the menu on press,
        # before we could tell a click from a drag).
        right_drag = Gtk.GestureDrag()
        right_drag.set_button(Gdk.BUTTON_SECONDARY)
        right_drag.connect("drag-begin", self.on_right_drag_begin)
        right_drag.connect("drag-update", self.on_right_drag_update)
        right_drag.connect("drag-end", self.on_right_drag_end)
        right_drag.connect("cancel", self.on_right_drag_cancel)
        self.add_controller(right_drag)
        self._right_drag_gesture = right_drag

        # Accepts a node-type string dropped from the add-node side
        # panel (build_add_node_panel()) - the other half of that
        # panel's Gtk.DragSource.
        drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop_target.connect("drop", self._on_node_type_dropped)
        self.add_controller(drop_target)

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        motion.connect("leave", self.on_leave)
        self.add_controller(motion)

        click = Gtk.GestureClick(button=1)
        click.connect("pressed", self.on_click)
        self.add_controller(click)

        self._init_view_controls()

        GLib.timeout_add(REFRESH_INTERVAL_MS, self.refresh)
        GLib.timeout_add(LAYOUT_TICK_MS, self.on_layout_tick)

    def refresh(self):
        self.client.send({"command": "get_nodes"})
        return True

    # ---------- session-load progress ----------

    def _set_loading(self, loading: bool) -> None:
        loading = bool(loading)
        if loading == self.loading:
            return
        self.loading = loading
        if loading:
            self._revealed = self._revealed if self._revealed is not None else set()
        for cb in self.on_loading_changed:
            try:
                cb(loading)
            except Exception:
                logger.exception("loading-changed callback failed")
        if not loading:
            # A completed load just dropped the new graph on the canvas at
            # whatever scattered positions it had - frame all of it so the
            # user sees the whole session at once instead of a corner.
            # This supersedes the startup auto-fit.
            self._revealed = None
            self._needs_initial_fit = False
            # Deferred: the console collapse above changes the canvas
            # height, and fitting before GTK re-lays-out would centre
            # against the old (shorter) canvas and leave the graph sitting
            # high once the console actually disappears.
            self._schedule_fit(80)

    def _node_revealed(self, nid) -> bool:
        return self._revealed is None or nid in self._revealed

    def _schedule_fit(self, delay_ms: int) -> None:
        """(Re)arm the one-shot deferred zoom_to_fit.  Deferring matters:
        the canvas height changes as the page/toolbar/console lay out, and
        fitting against a transient size leaves the graph vertically off."""
        if self._fit_source:
            GLib.source_remove(self._fit_source)
        self._fit_source = GLib.timeout_add(delay_ms, self._fit_after_load)

    def _fit_after_load(self):
        self._fit_source = 0
        if not self.loading:
            self.zoom_to_fit()
        return False

    def _begin_load(self) -> None:
        """Mark a bulk session load as in progress.  Called the moment a
        load_session command is sent; _update_loading_state clears it once
        the daemon reports every node ready (or the safety timeout
        fires).  Individual add_node commands never call this - only a
        whole-config import goes through _apply_config/_load_session."""
        now = time.monotonic()
        # A bulk load/reload makes the daemon's placement authoritative
        # again; drop any local panel edits we were holding ahead of it.
        self._pending_panels.clear()
        self._load_started_at = now
        self._load_min_visible_until = now + SESSION_LOAD_OVERLAY_MIN_MS / 1000.0
        self._load_timeout_at = now + SESSION_LOAD_OVERLAY_TIMEOUT_MS / 1000.0
        self._revealed = set()
        self._set_loading(True)

    def _update_loading_state(self, daemon_nodes) -> None:
        if not self.loading:
            return
        now = time.monotonic()
        # Keep the overlay up for a minimum beat even if a short session's
        # nodes are all ready by the first poll, so it doesn't flicker.
        if now < self._load_min_visible_until:
            return
        if now >= self._load_timeout_at:
            self._set_loading(False)
            return
        if self._daemon_loading:
            # The daemon is still working (its own startup load, or a
            # second cross-reference pass).  Node readiness alone would
            # clear the overlay between passes; wait for its "loading"
            # flag to drop (bounded by the timeout above).
            return
        if not daemon_nodes:
            # A rebuild/import has torn the old graph down (or a rebuild's
            # deletion phase has emptied it) but hasn't staged the new
            # nodes yet.  An empty space is NOT "finished" - keep the
            # overlay until nodes actually land; the timeout above bounds
            # a load that never produces any.
            return
        pending = any(
            ndata.get("health") == "starting"
            or (not ndata.get("ready", True) and ndata.get("health") != "dead")
            for ndata in daemon_nodes.values()
        )
        if not pending:
            self._set_loading(False)

    def zoom_to_fit(self, margin: float = 40.0) -> None:
        """Point the camera at every node at once: pick the zoom that
        fits the nodes' bounding box (with `margin` px of canvas padding)
        inside the current allocation, then pan so that box's centre sits
        in the middle of the viewport.  No-op on an empty canvas or
        before the widget has a real size."""
        if not self.nodes:
            return
        # During a bulk load, frame only the nodes that have come up so far.
        visible = [
            (nid, n) for nid, n in self.nodes.items()
            if self._node_revealed(nid)
        ]
        if not visible:
            return
        view_w = self.get_width()
        view_h = self.get_height()
        if view_w <= 1 or view_h <= 1:
            # Called from inside on_draw, before GTK exposes the
            # allocation via get_width()/get_height() - use the size we
            # were just handed.
            view_w, view_h = getattr(self, "_last_view_size", (0, 0))
        if view_w <= 1 or view_h <= 1:
            return

        min_x = min(n["x"] for _nid, n in visible)
        min_y = min(n["y"] for _nid, n in visible)
        max_x = max(n["x"] + self.node_width(nid) for nid, n in visible)
        max_y = max(n["y"] + self.node_height(nid) for nid, n in visible)

        world_w = max(1.0, max_x - min_x)
        world_h = max(1.0, max_y - min_y)
        usable_w = max(1.0, view_w - 2.0 * margin)
        usable_h = max(1.0, view_h - 2.0 * margin)
        zoom = min(usable_w / world_w, usable_h / world_h)
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))

        self.pan_x = view_w / 2.0 - ((min_x + max_x) / 2.0) * self.zoom
        self.pan_y = view_h / 2.0 - ((min_y + max_y) / 2.0) * self.zoom
        self.queue_draw()

    def set_physics_active(self, active):
        """Resume/pause *all* physics (node layout and panel repulsion).

        On by default; this is the hamburger menu's / floating button's
        global switch.  A *panel* that is pinned (`anchored`) is a separate,
        per-panel thing: its box is held still, but node physics still runs
        inside it (see `_panel_is_paused`)."""
        self.physics_active = bool(active)
        if self.physics_active:
            self.layout_awake = True
            self._settle_ticks = 0
        self.queue_draw()

    # ---------- commands to the daemon ----------
    # These two methods are the ONLY places that build set_volume /
    # set_gate command dicts. Every control below (slider, mute
    # checkbox, gate checkbox, the settings dialog) calls one of these
    # rather than constructing the command itself.

    def _send_set_volume(self, node_id, volume):
        """Push a volume value for `node_id` to the daemon. Handled on
        the daemon side by main.py's _cmd_set_volume(), which calls
        VolumeProcessNode.set_volume() and re-syncs the patch space -
        this is what both the slider and the mute-switch checkbox rely
        on to actually take effect."""
        self.client.send(
            {"command": "set_volume", "node_id": node_id, "volume": volume}
        )

    def _send_volume_range(self, node_id, min_val, max_val):
        self.client.send(
            {
                "command": "set_volume_range",
                "node_id": node_id,
                "min": min_val,
                "max": max_val,
            }
        )

    def _send_set_gate(self, node_id, enabled):
        """Push a gate's enabled state to the daemon (main.py's
        _cmd_set_gate())."""
        self.client.send(
            {"command": "set_gate", "node_id": node_id, "enabled": enabled}
        )

    def _send_switcher_output(self, node_id, output):
        """Push a Switcher's selected output (0="a", 1="b") to the
        daemon (main.py handles it as set_node_property "output")."""
        self._send_property(node_id, "output", output)

    def _toggle_gate_state(self, nid):
        """Flip a gate and remember the new value until the daemon echoes
        it (see _accept_bool_echo)."""
        node = self.nodes[nid]
        node["enabled"] = not node.get("enabled", True)
        self._pending_bool[(nid, "enabled")] = node["enabled"]
        self._send_set_gate(nid, node["enabled"])

    def _toggle_filter_mode(self, nid):
        """Flip a Filter node's Include/Exclude switch and remember the new
        value until the daemon echoes it (see _accept_bool_echo)."""
        node = self.nodes[nid]
        node["exclude"] = not node.get("exclude", False)
        self._pending_bool[(nid, "exclude")] = node["exclude"]
        self._send_property(nid, "exclude", node["exclude"])

    def _toggle_switch_state(self, nid):
        """Flip a switcher / On-Off source and remember the new value until
        the daemon echoes it (see _accept_bool_echo)."""
        node = self.nodes[nid]
        node["output"] = 0 if node.get("output") else 1
        self._pending_bool[(nid, "output")] = node["output"]
        self._send_switcher_output(nid, node["output"])

    # ---------- Sensitivity Gate slider ----------
    # The Sensitivity Gate's own live LADSPA threshold isn't reliable on
    # every build (see node_specs.py's sensitivity_gate spec comment), so
    # the slider does NOT touch the gate's threshold directly. Instead the
    # daemon invisibly brackets every Sensitivity node with two hidden
    # VolumeProcessNodes - a pre-boost and a reciprocal post-cut (see
    # main.py's _ensure_sensitivity_internals) - and this slider is just a
    # single 0..1 value handed to the daemon, which drives both. Nothing
    # here knows about those Volume nodes, so there is nothing for the user
    # to create or wire, and the control works the instant the node exists.
    def _apply_sensitivity_slider(self, gate_id, fraction):
        """Update the local slider position and push the 0..1 value to
        the daemon, which fans it out to the hidden gain-staging volume
        nodes bracketing this gate (main.py's _apply_sensitivity)."""
        node = self.nodes.get(gate_id)
        if node is None:
            return
        fraction = max(0.0, min(1.0, fraction))
        node["sensitivity"] = fraction
        self.client.send(
            {
                "command": "set_node_property",
                "node_id": gate_id,
                "property": "sensitivity",
                "value": fraction,
            }
        )

    def _apply_gain_slider(self, node_id, fraction):
        """Normalize's inline boost slider: store the 0..1 fraction
        locally and send it as `boost_db` (0..30 dB) to the daemon."""
        node = self.nodes.get(node_id)
        if node is None:
            return
        fraction = max(0.0, min(1.0, fraction))
        node["gain"] = fraction
        self.client.send(
            {
                "command": "set_node_property",
                "node_id": node_id,
                "property": "boost_db",
                "value": fraction * 30.0,
            }
        )

    def _home_relative_path(self, path):
        """`path` with a leading home directory rewritten to ``~``, so a
        picked file is stored portably (the daemon expands ``~`` at play
        time).  Anything outside home is left absolute."""
        home = os.path.expanduser("~")
        if home and (path == home or path.startswith(home + os.sep)):
            return "~" + path[len(home):]
        return path

    def _open_path_picker(self, nid):
        """Open the desktop's file chooser for a `picker` field (the Sound
        Effect's path) and store what comes back.

        Uses the same portal helper the Import/Save actions do: on a
        desktop that is the file manager's own "open file" dialog, which
        is the only standard way to ask a file manager for a selection.
        The picker starts in the directory the node already points at
        (``~`` expanded), and offers the formats the player can decode."""
        node = self.nodes.get(nid)
        if node is None:
            return
        current = os.path.expanduser(
            (node.get("meta", {}).get("path") or "").strip()
        )
        folder = os.path.dirname(current) if current else ""
        if folder and not os.path.isdir(folder):
            folder = ""
        if not folder:
            folder = os.path.expanduser("~")

        def on_chosen(path):
            if not path:
                return
            value = self._home_relative_path(path)
            node["meta"]["path"] = value
            self._send_property(nid, "path", value)
            self.queue_draw()
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        open_file(
            self.get_root(),
            "Choose a Sound File",
            on_chosen,
            folder=folder,
            filters=[("Audio", self.PICKER_FILE_GLOBS)],
        )

    def _send_impulse(self, node_id):
        """Fire one impulse out of a Button node (main.py's _cmd_impulse).

        The button holds no state, so there is nothing optimistic to
        remember: the pulse happens daemon-side, and the nodes it reaches
        report their own play count on the next poll.  The local flash is
        the press's only immediate feedback (see _draw_impulse_button)."""
        self.client.send({"command": "impulse", "node_id": node_id})

    def _send_property(self, node_id, prop, value):
        self.client.send(
            {
                "command": "set_node_property",
                "node_id": node_id,
                "property": prop,
                "value": value,
            }
        )

    def _send_effect_slider(self, node_id, kind, prop, value):
        """Send an effect-slider value (Reverb wet_dry / Sensitivity
        level) and remember it as the value we expect to see echoed
        back. Both sliders trigger a slow backing rebuild server-side,
        so a get_nodes poll that was already in flight while we were
        dragging can come back stale *after* release and yank the
        handle back; update_from_daemon() ignores such echoes until
        they report this value (see _pending_effect_slider)."""
        self._pending_effect_slider[node_id] = (kind, value)
        self.client.send(
            {
                "command": "set_node_property",
                "node_id": node_id,
                "property": prop,
                "value": value,
            }
        )

    def _accept_effect_slider_echo(self, node_id, kind, reported, tolerance):
        """Decide whether a get_nodes value for an effect slider should
        be applied. While we have a pending value of our own (one we
        sent that the daemon is still rebuilding toward), only accept
        the poll once it actually reports that value back - anything
        earlier is a stale in-flight response and must not yank the
        handle backwards."""
        pending = self._pending_effect_slider.get(node_id)
        if pending and pending[0] == kind:
            if abs(reported - pending[1]) <= tolerance:
                del self._pending_effect_slider[node_id]
                return reported
            return None
        return reported

    def _accept_bool_echo(self, node_id, field, reported):
        """Value to store for a boolean toggle field from a poll, or None
        to keep our locally-flipped value.

        Clicking a gate/switcher flips the node optimistically and sends
        a command; the daemon's reply lands a poll or two later.  Keep the
        optimistic value until the daemon reports it back, so the switch
        can't flash to the opposite and back while the command is in
        flight."""
        pending = self._pending_bool.get((node_id, field))
        if pending is not None:
            if bool(reported) == bool(pending):
                del self._pending_bool[(node_id, field)]
                return reported
            return None
        return reported

    # ---------- daemon state -> local model ----------

    def _update_panels_from_daemon(self, daemon_panels):
        """Refresh the local panel model from a get_nodes poll.

        Placement is *locally* authoritative while the physics or a drag
        has moved a panel (`_pending_panels`): a poll carries the last
        placement we flushed, so accepting it would snap the panel (and,
        because moving a panel moves its nodes, drag every node back)
        every refresh.  We hand placement back to the daemon once it
        echoes exactly what we sent.  Label/color/mode/membership always
        come from the daemon."""
        live = set()
        for p in daemon_panels:
            pid = p.get("id", "")
            live.add(pid)
            if pid == self.dragging_panel:
                continue
            reported = (
                float(p.get("x", 0.0) or 0.0),
                float(p.get("y", 0.0) or 0.0),
                float(p.get("w", 420.0) or 420.0),
                float(p.get("h", 260.0) or 260.0),
                bool(p.get("anchored", False)),
            )
            pending = self._pending_panels.get(pid)
            local = self.panels.get(pid)
            if pending is not None and reported == pending:
                del self._pending_panels[pid]
                pending = None
            if pending is not None and local is not None:
                x, y, w, h, anchored = (
                    local["x"], local["y"], local["w"], local["h"],
                    local.get("anchored", False),
                )
            else:
                x, y, w, h, anchored = reported
            self.panels[pid] = {
                "id": pid,
                "parent": p.get("parent", ""),
                "label": p.get("label", ""),
                "color": p.get("color", "#3584e4"),
                "mode": p.get("mode", "read-write"),
                "readonly": bool(p.get("readonly", False)),
                "writable": bool(p.get("writable", True)),
                "x": x,
                "y": y,
                "w": w,
                "h": h,
                "anchored": anchored,
                "auto_load": bool(p.get("auto_load", False)),
                "edit_mode": bool(p.get("edit_mode", False)),
                "path": p.get("path"),
                "children": list(p.get("children") or []),
            }
        for pid in list(self.panels):
            if pid not in live:
                del self.panels[pid]
        for pid in list(self._pending_panels):
            if pid not in live:
                del self._pending_panels[pid]
        self._panel_geo_cache.clear()

    def _mark_panel_moved(self, pid):
        """Remember that a panel's local placement is ahead of the daemon
        (see _update_panels_from_daemon)."""
        panel = self.panels.get(pid)
        if panel is None:
            return
        self._pending_panels[pid] = (
            float(panel["x"]),
            float(panel["y"]),
            float(panel["w"]),
            float(panel["h"]),
            bool(panel.get("anchored", False)),
        )

    def _panel_absolute(self, panel_id):
        """Absolute (x, y) top-left of a panel, folding ancestor offsets."""
        x = y = 0.0
        pid = panel_id
        guard = 0
        while pid and pid in self.panels and guard < 64:
            panel = self.panels[pid]
            x += panel["x"]
            y += panel["y"]
            pid = panel.get("parent", "")
            guard += 1
        return x, y

    def _panel_is_paused(self, panel_id):
        """Whether `panel_id` is pinned/paused: True if it, or any ancestor,
        carries the panel header's physics-stop flag (`anchored`).

        This pins the panel's **box**, not its nodes: `_hierarchical_step`
        still runs node physics inside a paused panel's local frame (so the
        contents settle, which is the whole point of a declarative panel
        whose nodes arrive unanchored), while the panel-motion pass holds
        the box where it is.  Pinning individual *nodes* is `anchored_nodes`
        - which is what a node the user placed by hand gets, so hand
        placement is never disturbed by either."""
        pid = panel_id
        guard = 0
        while pid and pid in self.panels and guard < 64:
            if self.panels[pid].get("anchored"):
                return True
            pid = self.panels[pid].get("parent", "")
            guard += 1
        return False

    def _panel_member_nodes(self, panel_id):
        prefix = panel_id + "::" if panel_id else ""
        out = []
        for nid in self.nodes:
            if panel_id:
                if nid.startswith(prefix):
                    out.append(nid)
            elif "::" not in nid:
                out.append(nid)
        return out

    def _wall_nodes_into_panels(self, rooms):
        """Pull each panel's members back inside its box (the capped
        `_panel_rect_base`, inset by `PANEL_PADDING`).

        This is the other half of "physics runs inside panels": node
        physics is free to push a member outward, and the box is what
        defines the room (see `_panel_growth_limits`, which is what stops a
        spreading cloud from simply growing the box without bound).

        The correction is a *fraction of the overshoot per step* rather than
        a hard clamp, so a spring that genuinely wants to pull outward
        doesn't leave a node glued to the border with the solver fighting
        the wall, and a loaded file whose coordinates sit far outside its
        panel walks back in over a few frames - with a clamp to the padded
        bound as a backstop, so a member is never drawn outside its box.

        ``rooms`` is the box snapshot taken *before* this step's node
        physics (see `_hierarchical_step`): walling against a box computed
        from the new positions would chase its own tail.

        The room is the panel's allowed box (its capped size) *minus*
        neighbouring panels' boxes, so a member is never settled into a
        neighbour - which is what stops two pinned neighbours (neither of
        which the panel pass may move) from growing into each other.

        Skipped for nodes/ports that are pinned at the node level, and for
        the root panel (it has no box).  Returns the largest distance it
        moved a node, for the caller's settle detection."""
        moved = 0.0
        ports = self._PORT_IN_TYPES | self._PORT_OUT_TYPES
        pad = self.PANEL_PADDING
        for pid, rect in rooms.items():
            if rect is None:
                continue
            rx, ry, rw, rh = rect
            # The room is the *allowed* box (placement plus
            # PANEL_PHYSICS_GROW per side), centred on the fitted box the
            # user is looking at - not the fitted box itself: a box that
            # only ever hugs its current contents would refuse to let those
            # contents spread at all (the wall would hold them inside the
            # size they already had), which is the opposite of settling
            # them.
            max_w, max_h = self._panel_growth_limits(pid)
            if max_w is not None:
                cx = rx + rw / 2.0
                rx, rw = cx - max_w / 2.0, max_w
            if max_h is not None:
                cy = ry + rh / 2.0
                ry, rh = cy - max_h / 2.0, max_h
            left, top = rx + pad, ry + pad
            right, bottom = rx + rw - pad, ry + rh - pad
            # Neighbouring panels own their space (see _panel_blockers): cut
            # the room back so a member can't be settled into one.  Since a
            # panel's fitted box is its content plus PANEL_PADDING, stopping
            # the content 2*PANEL_PADDING short of the neighbour's box leaves
            # one full padding of air between the two *boxes* - which is what
            # keeps two pinned neighbours (neither of which the panel pass
            # may move) from ever crossing, instead of merely growing into
            # each other.
            for bx, by, bw, bh in self._panel_blockers(pid, rooms):
                bl, bt = bx - 2.0 * pad, by - 2.0 * pad
                br, bb = bx + bw + 2.0 * pad, by + bh + 2.0 * pad
                if right <= bl or left >= br or bottom <= bt or top >= bb:
                    continue
                # Cut along the axis that loses the least room.
                keep_left, keep_right = bl - left, right - br
                keep_top, keep_bottom = bt - top, bottom - bb
                if max(keep_left, keep_right) >= max(keep_top, keep_bottom):
                    if keep_left >= keep_right:
                        right = bl
                    else:
                        left = br
                elif keep_top >= keep_bottom:
                    bottom = bt
                else:
                    top = bb
            if right - left < 1.0 or bottom - top < 1.0:
                # Squeezed into nothing by neighbours: leave the members
                # where physics put them rather than clamping to a sliver.
                continue
            for nid in self._panel_direct_nodes(pid):
                if nid in self.anchored_nodes or nid in self.pinned_nodes:
                    continue
                node = self.nodes[nid]
                if node.get("type") in ports:
                    continue
                w, h = self.node_width(nid), self.node_height(nid)
                ox, oy = node["x"], node["y"]
                dx = dy = 0.0
                if node["x"] < left:
                    dx = (left - node["x"]) * self.PANEL_WALL_PULL
                elif node["x"] + w > right:
                    dx = (right - (node["x"] + w)) * self.PANEL_WALL_PULL
                if node["y"] < top:
                    dy = (top - node["y"]) * self.PANEL_WALL_PULL
                elif node["y"] + h > bottom:
                    dy = (bottom - (node["y"] + h)) * self.PANEL_WALL_PULL
                node["x"] += dx
                node["y"] += dy
                # Backstop: never leave a member outside the padded box (a
                # node too big for the room is centred rather than pinned to
                # one edge).
                if w >= right - left:
                    node["x"] = left - (w - (right - left)) / 2.0
                else:
                    node["x"] = min(max(node["x"], left), right - w)
                if h >= bottom - top:
                    node["y"] = top - (h - (bottom - top)) / 2.0
                else:
                    node["y"] = min(max(node["y"], top), bottom - h)
                moved = max(moved, abs(node["x"] - ox) + abs(node["y"] - oy))
        return moved

    def _panel_blockers(self, pid, rooms):
        """The boxes of the *other* panels a member of `pid` must stay out
        of: every panel except itself and its ancestors (an ancestor
        *contains* this panel, so treating it as an obstacle would erase the
        room entirely).

        Child panels are blockers on purpose - a parent's nodes should not be
        settled on top of a sub-panel - and siblings are what keeps two
        pinned neighbours from growing into each other (neither can be moved
        by the panel pass, so the room itself has to respect the boundary).

        ``rooms`` is the pre-step box snapshot, so this never recurses into
        computing another panel's box from positions that are mid-step."""
        out = []
        for qid, rect in rooms.items():
            if rect is None or qid == pid or not qid:
                continue
            if pid and pid.startswith(qid + "::"):
                continue  # qid is an ancestor of pid
            out.append(rect)
        return out

    def _panel_direct_nodes(self, panel_id):
        """A panel's *own* nodes only - descendants belong to their own
        panel.  The subtree variant (`_panel_member_nodes`) double-counts
        once a panel has sub-panels, which drifts nodes (two physics
        integrations in two origin frames) and grows the box without
        bound.  Use this for both node physics and auto-fit bounds."""
        prefix = panel_id + "::" if panel_id else ""
        out = []
        for nid in self.nodes:
            if panel_id:
                if not nid.startswith(prefix):
                    continue
            elif "::" in nid:
                continue
            if self._panel_of_node(nid) == panel_id:
                out.append(nid)
        return out

    def _translate_panel_local(self, panel_id, dx, dy):
        """Shift a panel's subtree's *nodes* in the local model.

        Nodes store absolute positions, so moving a panel must move them.
        Child panels store parent-relative placement and derive their
        absolute position by folding ancestors (`_panel_absolute`), so they
        already move with their parent and must **not** be shifted here -
        doing so moved them by 2*dx and made the boxes fight their nodes."""
        prefix = panel_id + "::"
        for nid, node in self.nodes.items():
            if nid.startswith(prefix):
                node["x"] += dx
                node["y"] += dy

    def update_from_daemon(self, data):
        reported_opacity = data.get("canvas_opacity")
        if reported_opacity != self.canvas_opacity_from_daemon:
            self.canvas_opacity_from_daemon = reported_opacity
            for cb in self.on_canvas_opacity:
                try:
                    cb(reported_opacity)
                except Exception:
                    logger.exception("canvas opacity callback failed")
        daemon_nodes = data.get("nodes", {})
        daemon_edges = data.get("edges", {})
        # Which gates/switchers have a boolean control signal wired into
        # their "ctrl" input.  Computed up front (not after the node loop)
        # so the per-node update below can tell "wired but the daemon
        # hasn't resolved it yet" from "not wired at all".
        ctrl_connected = {
            edata.get("to_node")
            for edata in daemon_edges.values()
            if edata.get("to_port") == "ctrl"
        }
        self._update_panels_from_daemon(data.get("panels", []))
        # The daemon auto-loads the saved session on start-up; show the
        # same loading overlay the GUI would for an import it triggered,
        # raising it only on the False->True edge.
        loading_now = bool(data.get("loading"))
        if loading_now and not self._daemon_loading:
            self._begin_load()
        self._daemon_loading = loading_now
        selection_before = set(self.selected_nodes)
        anchored_before = set(self.anchored_nodes)
        # A poll can change a node's label/description/control state (and
        # therefore its height/width), so drop the dimension cache and let
        # the next redraw repopulate it.
        self._node_h_cache.clear()
        self._node_w_cache.clear()

        for nid in list(self.nodes.keys()):
            if nid not in daemon_nodes:
                # An optimistically-added placeholder the daemon hasn't
                # reported yet (heavy nodes take seconds to spawn): keep it
                # (with its position/anchoring) until the daemon confirms
                # or a timeout gives up.
                since = self._placeholder_since.get(nid)
                if since is not None:
                    if time.monotonic() - since < 30.0:
                        continue
                    del self._placeholder_since[nid]
                # Leave a fading outline behind (the node is really gone
                # from the model immediately, so nothing hit-tests it).
                self._start_node_ghost(nid)
                del self.nodes[nid]
                self._pending_effect_slider.pop(nid, None)
                for key in [k for k in self._pending_bool if k[0] == nid]:
                    del self._pending_bool[key]
                self.anchored_nodes.discard(nid)
                self.selected_nodes.discard(nid)
                self._user_created_nodes.discard(nid)
                self._node_born.pop(nid, None)
                self._node_alpha.pop(nid, None)
                self._node_fade.pop(nid, None)
                self._anim_seen.discard(nid)

        for nid, ndata in daemon_nodes.items():
            ntype = normalize_node_type(ndata.get("type"))
            spec = spec_for(ntype)
            if nid not in self.nodes:
                is_user_created = nid in self._user_created_nodes
                if nid in self._pending_positions:
                    px, py = self._pending_positions.pop(nid)
                elif ndata.get("x") is not None and ndata.get("y") is not None:
                    # A layout the daemon already had (this GUI restarted
                    # against a live daemon, or a session was imported):
                    # restore exactly where it was.
                    px, py = float(ndata["x"]), float(ndata["y"])
                else:
                    slot = len(self.nodes)
                    px = 100 + (slot % 4) * 300 + random.uniform(-15, 15)
                    py = 100 + (slot // 4) * 140 + random.uniform(-15, 15)
                self.nodes[nid] = {
                    "type": ntype,
                    "x": px,
                    "y": py,
                    "inputs": spec.inputs,
                    "outputs": spec.outputs,
                    "meta": ndata,
                    "label": ndata.get("label", ""),
                    "enabled": ndata.get("enabled", True),
                    "output": ndata.get("output", 0),
                    # Boolean-controlled nodes: whether a ctrl signal is
                    # wired and, if so, the value it drives - so the
                    # on/off switch can be drawn read-only and white
                    # showing the state actually in effect.
                    "bool_driven": ndata.get("bool_driven", False),
                    "bool_state": ndata.get("bool_state"),
                    "volume": ndata.get("volume", 1.0),
                    "wet_dry": ndata.get("wet_dry", 0.3),
                    "level": ndata.get("level", 25.0),
                    # Sensitivity Gate's 0..1 slider value. Persisted
                    # daemon-side (the daemon fans it out to the hidden
                    # pre/post volume nodes it owns - see main.py's
                    # _apply_sensitivity), so it survives reloads and is
                    # refreshed here like any other daemon field.
                    "sensitivity": ndata.get("sensitivity", 0.0),
                    # Normalize's inline boost slider, as a 0..1 fraction
                    # of its 0..30 dB range (the daemon stores boost_db).
                    "gain": max(
                        0.0,
                        min(
                            1.0,
                            float(ndata.get("boost_db", 15.0) or 0.0) / 30.0,
                        ),
                    ),
                    "device_volume": 1.0,
                    "device_name": ndata.get("device_name", ""),
                    "app_name": ndata.get("app_name", ""),
                    # Sound Effect: live playback count + the body
                    # checkbox's value (absent on every other type).
                    "playing": ndata.get("playing", 0),
                    "overlap": ndata.get("overlap", False),
                    # The Filter node's Include/Exclude switch (absent on
                    # every other type).
                    "exclude": ndata.get("exclude", False),
                    "connected": ndata.get("connected", False),
                    "is_bluetooth": ndata.get("is_bluetooth", False),
                    "selection_label": ndata.get("selection_label", ""),
                    # Absent for node types the daemon never tracks
                    # readiness for (plain filters, gates, ...) - treat
                    # those as always-ready rather than flashing an
                    # "offline" badge nothing will ever clear.
                    "ready": ndata.get("ready", True),
                    # "ok" / "starting" / "dead" - see main.py's
                    # _node_health. Only backed nodes carry it; anything
                    # else is treated as healthy.
                    "health": ndata.get("health", "ok"),
                    "device_volume": ndata.get("device_volume", 1.0),
                    "profile_index": ndata.get("profile_index"),
                    "codec_label": ndata.get("profile_description", ""),
                    "volume_locked": ndata.get("volume_locked", True),
                    "force_default": ndata.get("force_default", True),
                }
                self._apply_dynamic_ports(self.nodes[nid], ndata)
                # A node the GUI itself asked the daemon to create is a
                # user-spawned node -> anchor it by default.  Anything
                # else that appears is a loaded/imported node, so use
                # whatever anchored state the daemon saved for it (absent
                # -> free to drift).
                if is_user_created:
                    anchored = True
                else:
                    anchored = bool(ndata.get("anchored", False))
                if anchored:
                    self.anchored_nodes.add(nid)
            else:
                node = self.nodes[nid]
                # Daemon confirmed the node: it is no longer a waiting
                # placeholder (so it culls normally from here on).
                self._placeholder_since.pop(nid, None)
                # Update all fields except volume if this node is being dragged
                node["type"] = ntype
                node["inputs"] = spec.inputs
                node["outputs"] = spec.outputs
                self._apply_dynamic_ports(node, ndata)
                node["meta"] = ndata
                new_label = ndata.get("label", "")
                if node.get("label", "") != new_label and self._is_compact_node(node):
                    # A splitter grows to full width / shrinks to a square
                    # as its label comes and goes, so re-run the layout.
                    self.layout_awake = True
                    self._settle_ticks = 0
                node["label"] = new_label
                # Guard a just-clicked toggle against a poll that raced
                # our in-flight set command (see _accept_bool_echo).
                enabled = self._accept_bool_echo(
                    nid, "enabled", ndata.get("enabled", True)
                )
                if enabled is not None:
                    node["enabled"] = enabled
                output = self._accept_bool_echo(
                    nid, "output", ndata.get("output", 0)
                )
                if output is not None:
                    node["output"] = output
                node["bool_driven"] = ndata.get("bool_driven", False)
                # See resolve_bool_state_from_poll: hold the last resolved
                # value through a transient unresolved poll.
                node["bool_state"] = resolve_bool_state_from_poll(
                    ndata.get("bool_state"),
                    nid in ctrl_connected,
                    node.get("bool_state"),
                )
                node["device_name"] = ndata.get("device_name", "")
                node["app_name"] = ndata.get("app_name", "")
                # Sound Effect: the live play count (drives the node's
                # dot/count read-out) and its retrigger checkbox, which
                # takes the same optimistic-echo guard as the other
                # boolean body controls.  Both keys are only sent for
                # nodes that have them, so nothing else gains dead state.
                if "playing" in ndata:
                    node["playing"] = ndata.get("playing", 0)
                if "overlap" in ndata:
                    overlap = self._accept_bool_echo(
                        nid, "overlap", ndata.get("overlap", False)
                    )
                    if overlap is not None:
                        node["overlap"] = bool(overlap)
                # Filter: its Include/Exclude switch, same echo guard as the
                # other boolean body controls (only Filter nodes send it).
                if "exclude" in ndata:
                    exclude = self._accept_bool_echo(
                        nid, "exclude", ndata.get("exclude", False)
                    )
                    if exclude is not None:
                        node["exclude"] = bool(exclude)
                node["connected"] = ndata.get("connected", False)
                node["is_bluetooth"] = ndata.get("is_bluetooth", False)
                node["selection_label"] = ndata.get("selection_label", "")
                node["ready"] = ndata.get("ready", True)
                node["health"] = ndata.get("health", "ok")
                # Only update volume if not dragging this node
                # Only update volume if not dragging this node
                if self.slider_dragging != ("process", nid):
                    node["volume"] = ndata.get("volume", 1.0)
                else:
                    logger.debug("Skipping volume update for dragged node %s", nid)
                # Same drag guard for the Reverb dry/wet mix slider -
                # a poll landing mid-drag would otherwise yank it back.
                if self.slider_dragging != ("wetdry", nid):
                    reported = ndata.get("wet_dry", node.get("wet_dry", 0.3))
                    value = self._accept_effect_slider_echo(
                        nid, "wetdry", reported, 0.05
                    )
                    if value is not None:
                        node["wet_dry"] = value
                # Sensitivity Gate's `level` is now a Settings-dialog-
                # only static value (see node_specs.py) - no longer
                # touched by dragging the node's inline slider (that
                # slider drives _apply_sensitivity_slider instead), so
                # there's no "pending" value of our own to guard
                # against here; just take whatever the daemon reports.
                reported = ndata.get("level", node.get("level", 25.0))
                value = self._accept_effect_slider_echo(
                    nid, "threshold", reported, 0.5
                )
                if value is not None:
                    node["level"] = value
                # Sensitivity Gate's 0..1 inline slider - just mirror the
                # daemon's value, except mid-drag where our own handle is
                # authoritative until release.
                if self.slider_dragging != ("sensitivity", nid):
                    node["sensitivity"] = ndata.get(
                        "sensitivity", node.get("sensitivity", 0.0)
                    )
                # Normalize's inline boost slider - same drag guard.
                if self.slider_dragging != ("gain", nid):
                    node["gain"] = max(
                        0.0,
                        min(
                            1.0,
                            float(ndata.get("boost_db", 15.0) or 0.0) / 30.0,
                        ),
                    )
                # Always update min/max (they don't change during drag)
                node["volume_min"] = ndata.get("volume_min", 0.0)
                node["volume_max"] = ndata.get("volume_max", 1.0)
                # Same drag guard as the process-node volume above -
                # device_volume is now server-side config (see
                # patchSpace.DeviceControlMixin), so a poll landing
                # mid-drag would otherwise yank the slider back to
                # whatever was last confirmed by the daemon.
                if self.slider_dragging != ("device", nid):
                    node["device_volume"] = ndata.get("device_volume", 1.0)
                    node["volume_locked"] = ndata.get("volume_locked", True)
                node["force_default"] = ndata.get("force_default", True)
                node["profile_index"] = ndata.get("profile_index")
                node["codec_label"] = ndata.get("profile_description", "")

        new_edges = {
            eid: {
                "from_node": edata["from_node"],
                "to_node": edata["to_node"],
                "to_port": edata.get("to_port", "in"),
                "from_port": edata.get("from_port", "out"),
                # See main.py's _serialize_edges / PatchSpace.edge_wired.
                # Absent (an older daemon) is treated as wired so a
                # stale badge can never get stuck on.
                "wired": edata.get("wired", True),
            }
            for eid, edata in daemon_edges.items()
        }
        # Edges that vanished get a reverse (retract) animation: snapshot
        # their last drawn path now and draw it shrinking to nothing.
        for eid, old_edge in self.edges.items():
            if eid not in new_edges:
                self._start_edge_ghost(eid, old_edge)
        # An edge that came back (a fresh add) cancels any in-flight retract;
        # a poll that merely still reports an optimistically-removed edge does
        # not, so the retract can't be interrupted by poll lag.
        for eid in new_edges:
            if eid in self._edge_ghosts and (
                eid in self._pending_edge_draw or eid in self._edge_born
            ):
                del self._edge_ghosts[eid]
                self._edge_ghosted.discard(eid)
        self.edges = new_edges

        # A connection the user just made is echoed here; stamp its birth
        # now so the very first frame it is drawn already animates (the
        # ticker would otherwise promote it one frame later).
        if self._pending_edge_draw:
            for eid in [e for e in self._pending_edge_draw if e in self.edges]:
                del self._pending_edge_draw[eid]
                self._edge_born[eid] = time.monotonic()

        # Nodes touched by at least one not-yet-wired edge - drawn with
        # a "still wiring" badge (see _draw_node / _header_blocks)
        # instead of looking indistinguishable from a fully-connected
        # node. Recomputed fresh every poll since wiring status changes
        # as sync_locked() catches up in the background.
        self.nodes_pending_wiring = {
            nid
            for e in self.edges.values()
            if not e.get("wired", True)
            for nid in (e["from_node"], e["to_node"])
        }

        # Which bool-controlled nodes currently have a boolean control
        # edge wired into their "ctrl" input.  Drives the fallback
        # on/off button, which is hidden the moment ctrl is connected.
        for nid, node in self.nodes.items():
            node["ctrl_connected"] = nid in ctrl_connected

        # Groups: the daemon is authoritative, but a group we just created
        # or edited must not be clobbered by a poll that raced our
        # in-flight add_group/set_group command.  That race showed the old
        # label/members for a few polls - the "settings dialog flickers and
        # changes don't stick" report.  While a gid is pending, keep our
        # optimistic copy; hand it back to the daemon once it echoes the
        # exact same group.
        seen_groups = set()
        for g in data.get("groups", []):
            gid = g.get("id")
            if not gid:
                continue
            seen_groups.add(gid)
            incoming = {
                "label": g.get("label", "Group"),
                "color": g.get("color", self.GROUP_COLORS[0]),
                "nodes": set(g.get("nodes", [])),
            }
            if gid in self._pending_groups:
                if self.groups.get(gid) == incoming:
                    self._pending_groups.discard(gid)
                else:
                    # The daemon's copy still lags our edit (or is the
                    # pre-rename id we've already dropped); keep ours.
                    continue
            self.groups[gid] = incoming
        for gid in list(self.groups.keys()):
            if gid not in seen_groups and gid not in self._pending_groups:
                del self.groups[gid]

        new_node_ids = set(self.nodes.keys())
        new_edge_set = {
            (e["from_node"], e["to_node"], e["to_port"], e["from_port"])
            for e in self.edges.values()
        }
        nodes_added = new_node_ids != self._prev_node_ids
        panels_changed = set(self.panels) != self._prev_panel_ids
        if nodes_added or panels_changed or new_edge_set != self._prev_edge_set:
            # A new/changed panel needs the layout to run so the boxes are
            # arranged; otherwise the physics stays asleep until a node is
            # moved by hand.
            self.layout_awake = True
            self._settle_ticks = 0
        if nodes_added:
            # Persist the position the GUI just chose for the new node(s).
            self._mark_layout_dirty()
        self._prev_node_ids = new_node_ids
        self._prev_edge_set = new_edge_set
        self._prev_panel_ids = set(self.panels)
        self.force_layout.prune(new_node_ids)
        self.panel_force_layout.prune(set(self.panels))

        # A poll can add/remove nodes (and thus anchored/selected ids);
        # keep the tool panel in sync when it did.
        if (
            self.selected_nodes != selection_before
            or self.anchored_nodes != anchored_before
        ):
            self._notify_selection_changed()

        # Incremental reveal while a bulk load runs: pop each node onto the
        # canvas as it comes up, and reframe the growing graph.
        if self.loading or self._daemon_loading:
            if self._revealed is None:
                self._revealed = set()
            newly = False
            for nid, ndata in daemon_nodes.items():
                if nid in self._revealed:
                    continue
                if ndata.get("ready", True) and ndata.get("health") != "starting":
                    self._revealed.add(nid)
                    newly = True
            if newly:
                self.zoom_to_fit()
        else:
            self._revealed = None

        # A bulk import has no explicit "done" reply (see main.py's
        # _cmd_load_session); infer completion from the node health the
        # daemon reports each poll.
        self._update_loading_state(daemon_nodes)

        # Keep panel port squares centered on their edges as boxes change.
        self._layout_panel_ports()

        self.queue_draw()

    def on_layout_tick(self):
        if not self.layout_awake:
            return True

        max_delta = self._hierarchical_step()

        if max_delta < LAYOUT_SETTLE_EPSILON and self.dragging_node is None \
                and self.dragging_panel is None:
            self._settle_ticks += 1
            if self._settle_ticks > LAYOUT_SETTLE_TICKS:
                self.layout_awake = False
                self._awake_ticks = 0
                # The physics just stopped moving things - persist the
                # settled positions so a reload restores this layout.
                self._mark_layout_dirty()
        else:
            self._settle_ticks = 0

        # Safety valve: if the forces never converge (overlapping
        # pinned nodes, pathological repulsion, ...), stop burning CPU
        # on a layout that isn't settling after a generous number of
        # ticks (~40s) rather than spinning forever and freezing the
        # UI. Any structural change re-arms layout via update_from_daemon.
        self._awake_ticks += 1
        if self._awake_ticks > 1200:
            self.layout_awake = False
            self._awake_ticks = 0
            self._settle_ticks = 0
            self._mark_layout_dirty()

        self.queue_draw()
        return True

    def _hierarchical_step(self):
        """One physics step: node physics runs *inside* each panel (in the
        panel's local frame, internal edges only) and then the panels
        themselves repel each other in the parent frame.  Nodes never exert
        forces across a panel boundary; cross-panel edges gently spring the
        two panels together instead.

        A **pinned/paused panel holds its box still, not its nodes** (see
        `_panel_is_paused`).  That distinction is what makes a declarative
        panel usable: nodes an external config created (unanchored - only
        nodes the *user* placed are node-anchored) settle themselves inside
        their panel while the box stays where it was put.  Pinning the
        nodes too is what `anchored_nodes` is for.
        """
        if not self.physics_active:
            # All physics paused: leave layout exactly as-is.
            self._panel_geo_cache.clear()
            return 0.0
        max_delta = 0.0
        # The wall's authority is the box as the user is looking at it now -
        # captured *before* members move.  Deriving it from the positions
        # physics has just produced would let the box chase its own tail, and
        # the wall would only ever bite a fraction of the overshoot.
        rooms = {
            pid: self._panel_rect_base(pid) for pid in self.panels if pid
        }
        for pid in [""] + [p for p in self.panels if p]:
            members = self._panel_direct_nodes(pid)
            if len(members) < 2:
                continue
            ox, oy = self._panel_absolute(pid) if pid else (0.0, 0.0)
            mset = set(members)
            positions = {
                nid: (self.nodes[nid]["x"] - ox, self.nodes[nid]["y"] - oy)
                for nid in members
            }
            sizes = {
                nid: (self.node_width(nid), self.node_height(nid))
                for nid in members
            }
            edges = [
                (e["from_node"], e["to_node"])
                for e in self.edges.values()
                if e["from_node"] in mset and e["to_node"] in mset
            ]
            pinned = (set(self.pinned_nodes) | self.anchored_nodes) & mset
            if self.dragging_node in mset:
                pinned.add(self.dragging_node)
            pinned |= {n for n in self.drag_node_starts if n in mset}
            # Panel ports are locked to their bar - never apply physics.
            pinned |= {
                n for n in mset
                if self.nodes[n].get("type")
                in (self._PORT_IN_TYPES | self._PORT_OUT_TYPES)
            }
            delta = self.force_layout.step(members, positions, sizes, edges, pinned)
            max_delta = max(max_delta, delta)
            for nid in members:
                self.nodes[nid]["x"] = positions[nid][0] + ox
                self.nodes[nid]["y"] = positions[nid][1] + oy

        # Nodes have moved: pull any member that physics pushed outside its
        # (size-capped) box back in, against the boxes captured above, before
        # the panel pass reads them.
        if self.dragging_node is None and self.dragging_panel is None:
            max_delta = max(max_delta, self._wall_nodes_into_panels(rooms))
            self._panel_geo_cache.clear()

        # Panels repel/spring only against their *siblings*, each group in
        # its parent's local frame.  Running every panel through one flat
        # pass made a child panel fight its own parent (and its ancestors'
        # offsets), which is what pushed nested panels apart endlessly.
        def panel_of(nid):
            return nid.rsplit("::", 1)[0] if "::" in nid else ""

        by_parent = {}
        for pid in self.panels:
            if pid == "":
                continue
            parent = self.panels[pid].get("parent", "")
            by_parent.setdefault(parent, []).append(pid)

        for parent, pids in by_parent.items():
            if len(pids) < 2:
                continue
            # The box auto-fits its contents, so its top-left can be left/
            # above the declared panel origin; physics must use the box
            # corner (in the parent's local frame), not panel["x"/"y"].
            pax, pay = self._panel_absolute(parent) if parent else (0.0, 0.0)
            positions = {}
            sizes = {}
            for pid in pids:
                # Physics uses the content-only box: if it reacted to the
                # wire-expanded _panel_rect, panel motion -> node motion ->
                # new wire bounds -> bigger panel -> motion would never
                # converge.
                r = self._panel_rect_base(pid)
                if r is None:
                    positions[pid] = (0.0, 0.0)
                    sizes[pid] = (420.0, 260.0)
                else:
                    positions[pid] = (r[0] - pax, r[1] - pay)
                    sizes[pid] = (r[2], r[3])
            kid_set = set(pids)
            edges = []
            for e in self.edges.values():
                a = panel_of(e["from_node"])
                b = panel_of(e["to_node"])
                if a != b and a in kid_set and b in kid_set:
                    edges.append((a, b))
            pinned = {pid for pid in pids if self._panel_is_paused(pid)}
            if self.dragging_panel in kid_set:
                pinned.add(self.dragging_panel)
            delta = self.panel_force_layout.step(pids, positions, sizes, edges, pinned)
            max_delta = max(max_delta, delta)
            for pid in pids:
                if self.dragging_panel == pid:
                    continue
                r_old = self._panel_rect_base(pid)
                ox = (r_old[0] - pax) if r_old else 0.0
                oy = (r_old[1] - pay) if r_old else 0.0
                nx, ny = positions[pid]
                dx = nx - ox
                dy = ny - oy
                if dx or dy:
                    self.panels[pid]["x"] += dx
                    self.panels[pid]["y"] += dy
                    self._translate_panel_local(pid, dx, dy)
                    self._mark_panel_moved(pid)
        self._panel_geo_cache.clear()
        return max_delta

    # ---------- geometry ----------

    def _single_line_height(self, font_size):
        """Pixel height of one line at `font_size`, measured once and
        cached. This is the baseline node_height()'s fixed 80/100px
        budget already assumes for each header line - so comparing a
        block's actual wrapped height against this (see
        _header_extra_height) tells us whether it wrapped onto extra
        lines, not just how tall a single line happens to be."""
        if font_size not in self._line_height_cache:
            self._line_height_cache[font_size] = wrapped_text_height(
                self, "Ag", 1000, font_size
            )
        return self._line_height_cache[font_size]

    def _header_blocks(self, nid, node):
        """(text, font_size, color_key) for every line drawn in the
        node's header, top to bottom - exactly the same text and
        font-size choices the old fixed-line drawing code used
        (type label, then description/label/id per the same
        priority), just pulled out so _draw_header, node_height(), and
        _header_stack_height() all agree on what's being drawn instead
        of the drawing code and the sizing code each encoding the rules
        separately."""
        label = node.get("label", "")
        desc = node.get("meta", {}).get("description", "")
        if node["type"] in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
            # A panel port shows its label, then its optional description.
            blocks = []
            if label:
                blocks.append((label, 12, "text"))
            if desc:
                blocks.append((desc, 9, "subtext"))
            return blocks
        if self._is_compact_node(node):
            if node["type"] == "splitter":
                # A splitter shows nothing but its (optional) label - no
                # type name, no node id - so an unlabelled one is just a
                # small blank square.
                return [(label, 12, "text")] if label else []
            # The boolean logic gates keep their type name at the top -
            # an unlabelled AND/OR/Invert would otherwise be an
            # indistinguishable square - then an optional user label.  No
            # node id.
            blocks = [(type_label(node["type"], nid), 10, "subtext")]
            if label:
                blocks.append((label, 12, "text"))
            return blocks
        blocks = [(type_label(node["type"], nid), 10, "subtext")]
        if node["type"] in ("device_input", "device_output", "app_input", "app_output"):
            # Hardware / app nodes name an external device, so their
            # header is deliberately ordered: user label first (its
            # normal spot), node id under it, then the device name it's
            # currently bound to - never the device name masquerading as
            # the title. When there's no user label, the id takes the
            # label slot and the device name still sits beneath.
            device = (
                node.get("meta", {}).get("description")
                or node.get("device_name")
                or node.get("app_name")
                or ""
            )
            if label:
                blocks.append((label, 12, "text"))
                blocks.append((str(nid), 9, "subtext"))
                if device:
                    blocks.append((device, 10, "subtext"))
            else:
                blocks.append((str(nid), 12, "text"))
                if device:
                    blocks.append((device, 10, "subtext"))
            return blocks
        if desc:
            blocks.append((desc, 12, "text"))
            blocks.append((label or str(nid), 10 if label else 9, "subtext"))
        elif label:
            blocks.append((label, 12, "text"))
            blocks.append((str(nid), 9, "subtext"))
        else:
            blocks.append((str(nid), 12, "text"))
        if node.get("health") == "dead":
            # The node exists but its module died or its interior never
            # connected, so no audio can pass - flag it distinctly from
            # a node that's merely still coming up. See main.py's
            # _node_health.
            blocks.append(("\u2716 dead", 9, "error"))
        elif not node.get("ready", True):
            # Backed node (echo cancel, noise cancel, volume, ...) whose
            # real PipeWire objects haven't all been confirmed present
            # yet - see main.py's _node_is_ready. Appended last so it
            # never displaces the identifying blocks above it.
            blocks.append(("\u25cf not connected yet", 9, "warning"))
        elif nid in self.nodes_pending_wiring:
            # Structurally fine, but at least one edge touching this
            # node hasn't landed as a live PipeWire link yet - see
            # main.py's _serialize_edges / PatchSpace.edge_wired. Only
            # shown once "ready" is true so a node doesn't carry two
            # overlapping badges while it's still coming up.
            blocks.append(("\u25d0 wiring\u2026", 9, "warning"))
        return blocks

    def _device_header_bonus(self, node):
        """Extra header pixels a hardware/app node needs when it shows a
        user label AND a bound device name (a fourth header line on top
        of the type/label/id stack the base budget already fits)."""
        if node["type"] not in (
            "device_input",
            "device_output",
            "app_input",
            "app_output",
        ):
            return 0
        device = (
            node.get("meta", {}).get("description")
            or node.get("device_name")
            or node.get("app_name")
        )
        return 18 if (node.get("label") and device) else 0

    @staticmethod
    def _header_block_is_id(nid, text) -> bool:
        """Whether a header block is the node's raw id.  The id is a long
        opaque token (``node_1738...``), so it is drawn ellipsized on a
        single line rather than wrapped onto several - wrapping it just
        made the node tall for no readable gain."""
        return text == str(nid)

    def _header_extra_height(self, node_id):
        """Extra vertical room node_id's header needs beyond what
        node_height()'s fixed base already budgets for single-line
        text - 0 unless a label or description is long enough to wrap
        onto more than one line at the node's current width, in which
        case this is exactly enough to fit every wrapped line without
        clipping (see _draw_header, which stacks blocks using these
        same measurements).  The node id never contributes: it is
        ellipsized, not wrapped."""
        node = self.nodes[node_id]
        extra = 0.0
        for i, (text, font_size, _color) in enumerate(
            self._header_blocks(node_id, node)
        ):
            if self._header_block_is_id(node_id, text):
                continue
            # Must match _draw_header's per-line max_width exactly -
            # the type label (i == 0) shares its row with the node-type
            # glyph and the three-dot menu icon, so it wraps at a
            # narrower width than the lines below it.
            _left, max_width = self._header_line_layout(node_id, node, i)
            wrapped_h = wrapped_text_height(self, text, max_width, font_size)
            extra += max(0.0, wrapped_h - self._single_line_height(font_size))
        return extra

    # Node types that render as a compact square when unlabelled (see
    # _node_width_for / _compute_node_height / _header_blocks): a bare
    # splitter is pure plumbing, and the boolean logic gates (AND/OR/
    # Invert) are tiny control operators rather than signal processors.
    # None needs the full type-label + node-id header, so with a label
    # they show only the label and with none they collapse to a square.
    _COMPACT_NODE_TYPES = frozenset(
        {"splitter", "boolean_and", "boolean_or", "boolean_xor",
         "boolean_invert",
         "panel_in", "panel_out", "bool_panel_in", "bool_panel_out"}
    )

    @classmethod
    def _is_compact_node(cls, node) -> bool:
        return node.get("type") in cls._COMPACT_NODE_TYPES

    def _node_width_for(self, nid, node):
        """Width a node renders at: a compact node (splitter / boolean
        logic gate) with no label collapses to a square
        (SPLITTER_MIN_SIZE); every other node - and a labelled compact
        one, which wraps its text - uses the normal node width."""
        if node["type"] in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
            # Ports size to their label/description, capped at the normal
            # node width.
            label = node.get("label") or ""
            desc = (node.get("meta") or {}).get("description") or ""
            if not label and not desc:
                return self.SPLITTER_MIN_SIZE
            lw, _lh = self._text_size(label, 12)
            dw, _dh = self._text_size(desc, 9)
            return max(
                self.SPLITTER_MIN_SIZE,
                min(self.NODE_WIDTH, int(max(lw, dw)) + 34),
            )
        if self._is_compact_node(node) and not node.get("label"):
            if node["type"] == "splitter":
                return self.SPLITTER_MIN_SIZE
            # A gate still shows its type name at the top beside the
            # three-dot menu, so it must be wide enough for that text -
            # otherwise the name would wrap into a sliver.
            return max(
                self.SPLITTER_MIN_SIZE, self._header_needed_width(nid, node)
            )
        return max(self.NODE_WIDTH, self._header_needed_width(nid, node))

    def node_width(self, node_id):
        cached = self._node_w_cache.get(node_id)
        if cached is None:
            cached = self._node_width_for(node_id, self.nodes[node_id])
            self._node_w_cache[node_id] = cached
        return cached

    def node_height(self, node_id):
        cached = self._node_h_cache.get(node_id)
        if cached is None:
            cached = self._compute_node_height(node_id)
            self._node_h_cache[node_id] = cached
        return cached

    def _compute_node_height(self, node_id):
        node = self.nodes[node_id]
        if self._is_compact_node(node):
            header = self._header_stack_height(node_id, node)
            if not header:
                # A truly bare compact node (an unlabelled splitter) is a
                # plain square with its sockets centred.
                return self.SPLITTER_MIN_SIZE
            # Gates carry a type-name header (and maybe a user label, both
            # wrapped); reserve it and pack the sockets below at the
            # standard step.
            top, bottom = self._socket_margins(node_id, node)
            count = max(len(node.get("inputs", [])), len(node.get("outputs", [])))
            span = (count - 1) * self.SOCKET_MIN_STEP if count > 1 else 0
            need = top + bottom + span + 2 * self.SOCKET_RADIUS
            return max(self.SPLITTER_MIN_SIZE, int(need))
        base = self._base_node_height(node_id)
        # A node with more than one socket on EITHER side labels its
        # sockets and needs enough vertical room to lay them out at the
        # standard SOCKET_MIN_STEP gap - multi-input types (Echo Cancel's
        # "mic"/"probe") spread below the header, multi-output types (the
        # Switcher's "a"/"b") spread above the bottom control. Both use
        # the same per-socket step so the circles never overlap; see
        # _socket_margins/_socket_position, which this mirrors so the
        # drawn sockets, hit-testing and edge endpoints all agree.
        socket_count = max(len(node.get("inputs", [])), len(node.get("outputs", [])))
        if socket_count > 1:
            top, bottom = self._socket_margins(node_id, node)
            if self._uses_compact_sockets(node):
                # Just the circles plus the gap between them - the
                # sockets are stacked directly under the header rather
                # than centred in a band with a whole extra step of
                # padding above and below (which made the Switcher /
                # Inverse Switcher far taller than they needed to be).
                span = (socket_count - 1) * self.SOCKET_MIN_STEP
                need = top + bottom + span + 2 * self.SOCKET_RADIUS
            else:
                need = top + bottom + self.SOCKET_MIN_STEP * (socket_count + 1)
            base = max(base, int(need))
        return base

    def _uses_compact_sockets(self, node):
        """A multi-socket node that also carries a bottom control (the
        Switcher and Inverse Switcher) packs its sockets tightly under
        the header instead of reserving a full step of padding around
        them - the control already anchors the bottom, so centring the
        sockets in the whole body just makes the node needlessly tall."""
        socket_count = max(len(node.get("inputs", [])), len(node.get("outputs", [])))
        return socket_count > 1 and self._bottom_control_height(node) > 0

    def _base_node_height(self, node_id):
        """node_height() without the extra room a labelled multi-input
        node needs below its header - the historical height every
        existing single-socket node type used, kept intact."""
        node = self.nodes[node_id]
        base = self.NODE_HEIGHT
        if node.get("meta", {}).get("description") or node.get("label"):
            base += 20
        base += self._device_header_bonus(node)
        base += self._header_extra_height(node_id)
        base += self._bottom_control_height(node)
        return base

    def _bottom_control_height(self, node):
        """Pixels a node's inline bottom control needs - the generic
        "has an extra row" bump, the bigger gate/switcher toggle area, or
        the device/app rows currently on show.  Shared by
        _base_node_height() and _socket_margins() so labelled sockets are
        laid out above exactly the same block the control is drawn in."""
        rows = self._device_rows(node)
        if rows:
            return (
                len(rows) * (self.FIELD_HEIGHT + 4) + self.FIELD_BOTTOM_PAD
            )
        spec = spec_for(node["type"])
        if spec.control == "fallback_onoff":
            # The on/off button is always shown: interactive while
            # nothing is wired into the ctrl input, and a read-only white
            # state indicator once a boolean signal drives the node.
            return self.GATE_AREA_HEIGHT
        if spec.control in ("gate", "switcher", "boolean", "impulse",
                            "filter_mode"):
            # The impulse Button's face and the Filter node's Include/Exclude
            # switch are the same big rounded rect the gate toggle draws (see
            # _gate_rect / _draw_impulse_button / _draw_filter_mode_button),
            # so they claim the same height.
            return self.GATE_AREA_HEIGHT
        if spec.has_extra_row:
            # The generic bottom block: the inline field/control row, plus
            # (for a node with one) the gap and the switch row above it -
            # the node grows by the gap so the switch clears the field.
            extra = self.TOGGLE_ROW_GAP + self.TOGGLE_ROW_HEIGHT
            return (
                self.FIELD_HEIGHT + self.FIELD_BOTTOM_PAD
                + (extra if spec.toggle else 0)
            )
        return 0

    def _toggle_switch_rect(self, nid):
        """Geometry of a `toggle` row's inline on/off switch - single
        source of truth shared by drawing (_draw_toggle_row) and
        hit-testing (find_toggle_switch_at), like _gate_rect.

        The switch's top edge is derived from _bottom_control_height (the
        reserved block, which already contains TOGGLE_ROW_GAP above the
        field row, so the switch can't ride the field's top edge), so the
        reserved height, the drawn switch and its hit-test can't drift."""
        node = self.nodes[nid]
        x = (
            node["x"] + self.NODE_WIDTH - self.FIELD_MARGIN
            - self.TOGGLE_SWITCH_WIDTH
        )
        y = (
            node["y"] + self.node_height(nid)
            - (self._bottom_control_height(node) + 2)
        )
        return (x, y, self.TOGGLE_SWITCH_WIDTH, self.TOGGLE_SWITCH_HEIGHT)

    def _header_stack_height(self, node_id, node):
        """Pixel height of everything drawn in the node's header block,
        measured the same way _draw_header stacks it (identical per-line
        widths and gaps) - used to keep labelled sockets clear of it."""
        blocks = self._header_blocks(node_id, node)
        total = self.HEADER_TOP_PAD
        for i, (text, font_size, _color) in enumerate(blocks):
            _left, max_width = self._header_line_layout(node_id, node, i)
            if self._header_block_is_id(node_id, text):
                # Ellipsized to a single line - see _draw_header.
                total += self._single_line_height(font_size)
            else:
                total += wrapped_text_height(self, text, max_width, font_size)
            if i + 1 < len(blocks):
                total += self.HEADER_BLOCK_GAP
        return total

    def _socket_margins(self, node_id, node):
        """(top, bottom) insets, in node-local pixels, inside which this
        node's socket centres live.

        Ordinary nodes return the legacy single-value offset for both,
        which leaves a lone socket centred on the node body (the two
        insets cancel out of the centre calculation), so nothing moves
        for them. A node that labels its sockets reserves the wrapped
        header text on top instead, so "mic"/"probe" and the Switcher's
        "a"/"b" sit below the title/label rather than on top of it.
        Multi-socket nodes that also have a bottom control (the two
        switchers) reserve just the control below, not a small pad, so
        the A/B toggle stays close to the sockets above it."""
        if self._is_compact_node(node):
            header = self._header_stack_height(node_id, node)
            if not header:
                # Bare splitter: a square with its sockets centred.
                return 0, 0
            # A gate reserves its type/label header on top, and a user
            # label gets a little pad below the sockets too.
            return header + 4, (8 if node.get("label") else 4)

        if self._uses_compact_sockets(node):
            # Multi-socket nodes with a bottom control: sockets sit
            # right under the wrapped header, control reserved below.
            top = self._header_stack_height(node_id, node) + 4
            bottom = self._bottom_control_height(node)
            return top, bottom

        # A single socket on *both* sides is the legacy case: centred in
        # the body with the header's height added symmetrically.  A node
        # with several sockets on either side (Echo Cancel's inputs, a
        # Split Bundle's many outputs, ...) labels them and must clear the
        # header below it.
        if max(len(node.get("inputs", [])), len(node.get("outputs", []))) <= 1:
            offset = 0
            if node.get("meta", {}).get("description") or node.get("label"):
                offset += 20
            offset += self._device_header_bonus(node)
            offset += self._header_extra_height(node_id)
            offset += self._bottom_control_height(node)
            return offset, offset

        # Labelled sockets (more than one on a side, e.g. Echo Cancel or
        # a Split Bundle):
        # sit below the wrapped header on top and clear of the bottom
        # control too, so an input switch's "a"/"b" circles never land
        # on the A/B button.
        top = self._header_stack_height(node_id, node) + 4
        bottom = 8 + self._bottom_control_height(node)
        return top, bottom

    def _socket_position(self, node_id, direction, index):
        """The single source of truth for where a socket circle is (and
        therefore where an edge endpoint, hover highlight, and click hit-
        test must point too), so drawing and hit-testing can never drift
        apart. Distributed across the vertical band between
        _socket_margins()'s top and bottom insets, which node_height()
        has already sized to hold this side's sockets at the standard
        SOCKET_MIN_STEP gap (so Echo Cancel's inputs and the Switcher's
        outputs are spaced identically and never overlap)."""
        node = self.nodes[node_id]
        ports = node["inputs"] if direction == "in" else node["outputs"]
        x = node["x"] if direction == "in" else node["x"] + self.node_width(node_id)
        total = len(ports)
        if total == 0:
            return (x, node["y"] + self.node_height(node_id) / 2)
        top, bottom = self._socket_margins(node_id, node)
        band = self.node_height(node_id) - top - bottom
        if (self._uses_compact_sockets(node) or self._is_compact_node(node)) and total > 1:
            # Packed at the standard step and centred in the (now tight)
            # band, instead of the proportional spread used elsewhere.
            span = (total - 1) * self.SOCKET_MIN_STEP
            y = node["y"] + top + (band - span) / 2 + index * self.SOCKET_MIN_STEP
        else:
            y = node["y"] + top + band * (index + 1) / (total + 1)
        return (x, y)

    @staticmethod
    def _apply_dynamic_ports(node, ndata):
        """Apply the daemon-reported dynamic ports.

        A Bundle merge node reports ``bundle_inputs`` (one socket per
        plugged line plus a spare) so it grows an input each time one is
        used; a Bundle Split node reports ``bundle_members`` (one output
        socket per live member, with a readable label each).  Any other
        node keeps its static spec ports."""
        merge_inputs = ndata.get("bundle_inputs")
        filter_inputs = ndata.get("filter_inputs")
        if merge_inputs:
            node["inputs"] = list(merge_inputs)
        elif filter_inputs:
            # Bundle in first, then the classifier sockets.
            node["inputs"] = ["in"] + list(filter_inputs)
        members = ndata.get("bundle_members")
        if not members:
            node.pop("output_labels", None)
            return
        ports = []
        labels = {}
        for member in members:
            port = member.get("port")
            if not port or port in labels:
                continue
            ports.append(port)
            labels[port] = member.get("label") or port
        node["outputs"] = ports
        node["output_labels"] = labels

    @staticmethod
    def _socket_color(pal, kind, is_output):
        """The palette color for a socket of `kind`.  Bundle, filter and
        impulse sockets are not audio, so they get their own cool colors
        instead of the input/output green/red."""
        if kind == "boolean":
            return pal["boolean_port"]
        if kind == "bundle":
            return pal["bundle_port"]
        if kind == "filter":
            return pal["filter_port"]
        if kind == "impulse":
            return pal["impulse_port"]
        return pal["output_port"] if is_output else pal["input_port"]

    @staticmethod
    def _trace_socket(cr, sx, sy, kind):
        """Trace one socket's path (not fill/stroke it).  Audio, boolean
        and impulse sockets are circles; a *bundle* (a set of streams on
        one wire) or *filter* (a classifier predicate) socket is a
        diamond, so "this port is not one signal" reads before its wire
        is drawn.  An impulse is a filled dot of its own color instead -
        it pairs only with another impulse socket, and its wire's short
        dash (see IMPULSE_DASH) is what marks it as an event."""
        r = PatchSpaceGraphWidget.SOCKET_RADIUS
        if kind in ("bundle", "filter"):
            d = r * 1.4
            cr.move_to(sx, sy - d)
            cr.line_to(sx + d, sy)
            cr.line_to(sx, sy + d)
            cr.line_to(sx - d, sy)
            cr.close_path()
        else:
            cr.arc(sx, sy, r, 0, 2 * math.pi)

    def _edge_wire_kind(self, edge):
        """The kind of signal an edge carries, from its two endpoints:
        "filter" / "bundle" / "boolean" / "impulse" / "audio".  A bundle
        wire is dotted and cannot pair with a boolean/filter/impulse
        wire, so either endpoint's kind is authoritative for its group."""
        src = self.nodes.get(edge.get("from_node"))
        dst = self.nodes.get(edge.get("to_node"))
        kinds = []
        if src is not None:
            kinds.append(port_kind(src["type"], edge.get("from_port", "out"), "out"))
        if dst is not None:
            kinds.append(port_kind(dst["type"], edge.get("to_port", "in"), "in"))
        for special in ("filter", "bundle", "boolean", "impulse"):
            if special in kinds:
                return special
        return "audio"

    @staticmethod
    def _wire_color(pal, kind):
        if kind == "boolean":
            return pal["boolean_port"]
        if kind == "bundle":
            return pal["bundle_port"]
        if kind == "filter":
            return pal["filter_port"]
        if kind == "impulse":
            return pal["impulse_port"]
        return pal["link"]

    @staticmethod
    def _wire_dash(kind):
        """The dash pattern for one wire kind, or None for a solid line.
        Bundle and filter wires are dashed alike; an impulse gets a
        shorter, tighter mark so a momentary event never reads as a
        (dashed) bundle of streams."""
        if kind == "impulse":
            return IMPULSE_DASH
        if kind in ("bundle", "filter"):
            return BUNDLE_DASH
        return None

    def _draw_field_caret(self, cr, pal, field_x, field_y, field_w, field_h):
        """The little triangle at a choice field's right edge."""
        cx = field_x + field_w - 11
        cy = field_y + field_h / 2.0
        cr.set_source_rgb(*pal["subtext"])
        cr.move_to(cx - 4, cy - 1.5)
        cr.line_to(cx + 4, cy - 1.5)
        cr.line_to(cx, cy + 3.5)
        cr.close_path()
        cr.fill()

    def _field_rect(self, nid):
        """Geometry of a node's inline text field - single source of truth
        shared by drawing (_draw_text_field), hit-testing (find_field_at)
        and the two things that share the field's row on the right: the
        folder button a `picker` spec draws (_path_picker_rect) and the
        live status read-out (_play_indicator_rect).  The field gives up
        their widths instead of sliding under them."""
        node = self.nodes[nid]
        spec = spec_for(node["type"])
        x = node["x"] + self.FIELD_MARGIN
        w = self.NODE_WIDTH - 2 * self.FIELD_MARGIN
        if spec.picker:
            w -= self.PICKER_SIZE + self.PICKER_GAP
        if spec.indicator:
            w -= self.INDICATOR_WIDTH
        y = (
            node["y"] + self.node_height(nid)
            - self.FIELD_HEIGHT - self.FIELD_BOTTOM_PAD
        )
        return (x, y, w, self.FIELD_HEIGHT)

    def _path_picker_rect(self, nid):
        """Geometry of the folder button in a `picker` spec's field row:
        right of the field, left of the status read-out, vertically
        centred in the row - derived from _field_rect so it can't drift
        from the field it belongs to."""
        fx, fy, fw, fh = self._field_rect(nid)
        x = fx + fw + self.PICKER_GAP
        y = fy + (fh - self.PICKER_SIZE) / 2.0
        return (x, y, float(self.PICKER_SIZE), float(self.PICKER_SIZE))

    def _play_indicator_rect(self, nid):
        """(dot_x, dot_y, dot_r, text_right) for a node's live status
        read-out: a status dot hard against the node's bottom-right
        corner, with its count right-aligned just to the left of it.
        Only nodes whose spec declares ``indicator`` have one."""
        node = self.nodes[nid]
        fx, fy, _fw, fh = self._field_rect(nid)
        dot_r = 5.0
        dot_x = node["x"] + self.NODE_WIDTH - self.FIELD_MARGIN - dot_r
        dot_y = fy + fh / 2.0
        return (dot_x, dot_y, dot_r, dot_x - dot_r - 5.0)

    def _gate_rect(self, nid):
        """Geometry of the big gate toggle - single source of truth
        shared by drawing (_draw_gate_toggle) and hit-testing
        (find_gate_toggle_at), same pattern as _device_row_rect."""
        node = self.nodes[nid]
        node_h = self.node_height(nid)
        w = self.NODE_WIDTH - 2 * self.GATE_MARGIN
        h = self.GATE_HEIGHT
        x = node["x"] + self.GATE_MARGIN
        y = node["y"] + node_h - h - self.GATE_BOTTOM_MARGIN
        return (x, y, w, h)

    def _edge_endpoints(self, edge):
        """The exact (out_x, out_y, in_x, in_y) positions an edge is
        drawn at, taken from _socket_position so an edge always lands on
        the drawn socket circles - the old code used the no-offset
        centre guess, which drifts away from a labelled multi-input
        socket (e.g. Echo Cancel's "probe")."""
        out_x, out_y = self._socket_position(
            edge["from_node"], "out", self._edge_from_port_index(edge)
        )
        in_x, in_y = self._socket_position(
            edge["to_node"], "in", self._edge_to_port_index(edge)
        )
        return out_x, out_y, in_x, in_y

    @staticmethod
    def _contains_point(rect, x, y):
        x1, y1, x2, y2 = rect
        return x1 <= x <= x2 and y1 <= y <= y2

    def _simplify_orthogonal(self, path, obstacles):
        """Collapse a staircase route into as few straight runs/L-elbows as
        possible without cutting through anything.

        A* emits a point per grid cell, so even a clean detour arrives as a
        long staircase; this greedily skips to the farthest later point that
        a clear two-segment L can reach (longest skip first) instead of
        leaving all the redundant corners for the renderer to curve."""
        if len(path) < 3:
            return list(path)
        n = len(path)
        out = [path[0]]
        i = 0

        def _clear(a, b):
            return not segment_blocked(
                a[0], a[1], b[0], b[1], obstacles, pad=WIRE_PAD
            )

        while i < n - 1:
            placed = False
            # Continue the direction we arrived in (or the socket's outgoing
            # stub) so the merged run stays as straight as possible.
            if i > 0:
                dxin = path[i][0] - path[i - 1][0]
            else:
                dxin = 1.0
            horizontal_first = abs(dxin) > 1e-6
            first_h = abs(path[0][1] - path[1][1]) < 1e-6
            last_h = abs(path[-2][1] - path[-1][1]) < 1e-6
            # The exit/entry segments must keep their *direction* as well as
            # their orientation: a merge that flips the first segment sends
            # the wire backwards out of the socket, so it runs past the port
            # and hooks back (the "stubs overshoot each other" case).
            first_dx = path[1][0] - path[0][0]
            first_dy = path[1][1] - path[0][1]
            last_dx = path[-1][0] - path[-2][0]
            last_dy = path[-1][1] - path[-2][1]
            for j in range(n - 1, i + 1, -1):
                a, b = path[i], path[j]
                if abs(a[1] - b[1]) < 1e-6 or abs(a[0] - b[0]) < 1e-6:
                    candidates = [[a, b]]
                else:
                    h = [a, (b[0], a[1]), b]
                    v = [a, (a[0], b[1]), b]
                    candidates = [h, v] if horizontal_first else [v, h]
                for cand in candidates:
                    # Never change how the wire leaves/enters a socket: keep
                    # the first/last segment's orientation (the horizontal
                    # stubs), or a merge could dive straight into the node.
                    if i == 0:
                        a0, a1 = cand[0], cand[1]
                        if (abs(a0[1] - a1[1]) < 1e-6) != first_h:
                            continue
                        if first_h:
                            if (a1[0] - a0[0]) * first_dx <= 0:
                                continue
                        elif (a1[1] - a0[1]) * first_dy <= 0:
                            continue
                    if j == n - 1:
                        b0, b1 = cand[-2], cand[-1]
                        if (abs(b0[1] - b1[1]) < 1e-6) != last_h:
                            continue
                        if last_h:
                            if (b1[0] - b0[0]) * last_dx <= 0:
                                continue
                        elif (b1[1] - b0[1]) * last_dy <= 0:
                            continue
                    if all(_clear(cand[k], cand[k + 1])
                           for k in range(len(cand) - 1)):
                        out.extend(cand[1:])
                        i = j
                        placed = True
                        break
                if placed:
                    break
            if not placed:
                out.append(path[i + 1])
                i += 1
        return wire_simplify(wire_orthogonalize(out))

    def _snap_to_grid(self, path, obstacles, step=WIRE_GRID_STEP):
        """Nudge each segment onto the canvas half-grid (best effort).

        Snaps every non-socket segment's perpendicular coordinate to the
        nearest multiple of ``step`` while rebuilding corners as the
        intersection of the snapped segments, so runs line up with the drawn
        grid.  Segments touching a socket keep the socket's exact
        coordinate, and the whole snap is rejected (original returned) if it
        would put any segment into an obstacle."""
        if step <= 0 or len(path) < 3:
            return path
        n = len(path)
        horiz = [abs(path[k][1] - path[k + 1][1]) < 1e-6 for k in range(n - 1)]
        seg = []
        for k in range(n - 1):
            if k in (0, n - 2):
                # Segment on a socket: keep its exact perpendicular coord.
                coord = path[k][1] if horiz[k] else path[k][0]
            else:
                coord = round(
                    (path[k][1] if horiz[k] else path[k][0]) / step
                ) * step
            seg.append((horiz[k], coord))
        pts = [path[0]]
        for i in range(1, n - 1):
            if seg[i - 1][0]:
                pts.append((seg[i][1], seg[i - 1][1]))
            else:
                pts.append((seg[i - 1][1], seg[i][1]))
        pts.append(path[-1])
        pts = wire_simplify(wire_orthogonalize(pts))
        m = len(pts)
        for k in range(m - 1):
            # Skip the segments touching a socket: they legitimately graze
            # the endpoint node (which is in `obstacles`), and the snap
            # never changes their perpendicular coordinate anyway.
            if k == 0 or k == m - 2:
                continue
            ax, ay = pts[k]
            bx, by = pts[k + 1]
            if abs(ax - bx) > 0.5 and abs(ay - by) > 0.5:
                return path
            if segment_blocked(ax, ay, bx, by, obstacles, pad=WIRE_PAD):
                return path
        return pts


    @staticmethod
    def _stubbed_fallback(x1, y1, x2, y2, slen=None):
        """A last-resort orthogonal path that still leaves/enters each socket
        horizontally by at least WIRE_MIN_STUB.

        Used when the router can't find a path, and for an edge whose node is
        mid-animation/detach.  A bare two-segment L degenerates to a straight
        vertical line when the ports are stacked (x1 ~= x2), which reads as a
        wire "going straight up/down" - this Z always shows a sideways stub.
        Obstacle-unaware on purpose (there is no route to be aware of)."""
        if slen is None:
            slen = WIRE_MIN_STUB
        if abs(y1 - y2) < 1e-6:
            if x2 >= x1:
                # Same row and the target is ahead: a straight run is right.
                return [(x1, y1), (x2, y2)]
            # Same row but the target is *behind* the source: step around so
            # the wire never runs back through its own source node.
            d = max(slen, abs(x1 - x2) * 0.5)
            return [
                (x1, y1), (x1 + d, y1), (x1 + d, y1 - d),
                (x2 - d, y1 - d), (x2 - d, y1), (x2, y1),
            ]
        # Always exit the source to the *right* (x1 + slen) and enter the
        # target from the *left* (x2 - slen), even when that makes the middle
        # run double back.  Clamping the stubs to the midpoint instead made
        # the source exit leftwards whenever the target sat to its left -
        # straight through the node body and out the far side.
        mid_y = (y1 + y2) / 2.0
        return [
            (x1, y1), (x1 + slen, y1), (x1 + slen, mid_y),
            (x2 - slen, mid_y), (x2 - slen, y2), (x2, y2),
        ]

    @staticmethod
    def _close_pair_z(x1, y1, x2, y2, core):
        """A socket-to-socket Z for a close pair whose L hides a leg under an
        endpoint node, or None when the route is fine as it stands.

        Only the perpendicular run's *position* is at issue: the two sockets
        are close enough that the jog has to happen inside the gap, and if it
        lands on the source's (x1) or target's (x2) border the node covers it
        (nodes are painted after wires).  Putting it in the middle of the gap
        keeps every segment visible.  A pair level in y needs no jog at all,
        and a route whose jog is already between the two borders is left
        alone."""
        if core is None or len(core) < 3:
            return None
        if abs(x2 - x1) < 1.0 or abs(y2 - y1) < 1.0:
            return None
        # Where the *perpendicular runs* are (a run = a vertical segment): a
        # route with none is a level pair's straight line, and a route whose
        # runs are all strictly between the two borders is already fine.
        runs = {
            round(ax, 3)
            for (ax, ay), (bx, by) in zip(core, core[1:])
            if abs(ax - bx) < 0.5 and abs(ay - by) >= 0.5
        }
        if not runs or all(
            abs(x - x1) > 0.75 and abs(x - x2) > 0.75 for x in runs
        ):
            return None
        mid = (x1 + x2) / 2.0
        return [(x1, y1), (mid, y1), (mid, y2), (x2, y2)]

    def _wire_points(self, edge, x1, y1, x2, y2, wire_rects, wire_panels,
                     extra_obstacles=(), panel_titles=None):
        """A square route (rounded at draw time) from socket to socket that
        steers clear of other nodes, panels, other wires, *and its own
        endpoints' bodies*.  Always returns an orthogonal path (a plain L on
        the rare fallback when every route is blocked).

        ``wire_rects`` is {nid: (x1,y1,x2,y2)} and ``wire_panels`` a list of
        (pid, (x1,y1,x2,y2)); both are built once per frame (see on_draw).
        ``extra_obstacles`` are keep-out rects from already-routed wires.
        ``panel_titles`` maps a panel id to its floating title row; wires
        prefer to route around those but are allowed to cross them when no
        title-clear route exists.  Panels holding either endpoint are ignored
        (a wire has to be able to leave/enter its own panel)."""
        panel_titles = panel_titles or {}
        skip = {edge["from_node"], edge["to_node"]}
        own = [r for nid, r in wire_rects.items() if nid in skip]
        other_nodes = [r for nid, r in wire_rects.items() if nid not in skip]
        panels = []
        titles = []
        for pid, rect in wire_panels:
            if self._contains_point(rect, x1, y1) or self._contains_point(
                rect, x2, y2
            ):
                continue
            panels.append(rect)
            header = panel_titles.get(pid)
            if header is not None:
                titles.append(header)
        extra = list(extra_obstacles)
        # The router only searches `endpoint span + MARGIN`, so drop any
        # wire keep-out rect entirely outside that area: the router would
        # clip it away anyway, but rasterizing hundreds of far strips per
        # route is what made routing slow on a busy graph.
        _lo_x = min(x1, x2) - WIRE_MARGIN
        _hi_x = max(x1, x2) + WIRE_MARGIN
        _lo_y = min(y1, y2) - WIRE_MARGIN
        _hi_y = max(y1, y2) + WIRE_MARGIN
        extra = [
            r for r in extra
            if not (r[2] < _lo_x or r[0] > _hi_x
                    or r[3] < _lo_y or r[1] > _hi_y)
        ]

        # Route around everything, with a corridor out of each socket so the
        # wire can leave/enter its own (now-blocking) node.  The corridor
        # hole must only punch through the *endpoint* nodes; every other
        # node/panel is passed as keep_blocked (applied after the hole) so a
        # socket corridor can't cut through a node that happens to sit next
        # to the socket.  Other wires are kept blocked too.
        static = other_nodes + own + panels
        keep_nodes = other_nodes + panels
        # Post-processing clearance checks include the titles too, so a
        # cleanup pass can't introduce a fresh crossing.
        obstacles = static + titles + extra
        # Endpoint node boxes excluded: the socket sits on their border, so a
        # socket-adjacent smoothing check must not treat its own node as a
        # blocker (it would reject every short first/last blend).
        smooth_obstacles = other_nodes + panels + titles + extra
        corr = WIRE_CELL * 1.5
        clear = [
            # Outward-only: the socket sits on the node border, so the wire
            # leaves that way (or runs along the border).  Punching *inward*
            # too let a route step a few px inside its own node.
            (x1, y1 - corr, x1 + WIRE_CELL, y1 + corr),
            (x2 - WIRE_CELL, y2 - corr, x2, y2 + corr),
        ]

        def _route(a, b, clear_rects, with_titles, behind=()):
            """Route a->b avoiding nodes/panels and, when ``with_titles``,
            the panel title rows.  Wire keep-out strips are honoured only
            while they don't force an absurd detour: if clearing them costs
            far more than going direct, fall back to the node/panel-clear
            route (it may pass closer to a wire, but not across nodes).
            ``behind`` rects stay blocked after the corridors so a stubbed
            endpoint can only be approached from *outside* its stub (used for
            the hook-retry below, not for the normal route)."""
            obs = static + (titles if with_titles else [])
            keep = keep_nodes + (titles if with_titles else []) + list(behind)
            r = route_wire(a[0], a[1], b[0], b[1], obs,
                           clear_rects=clear_rects, keep_blocked=keep + extra)
            if r and extra:
                direct = abs(a[0] - b[0]) + abs(a[1] - b[1])
                rlen = sum(
                    math.hypot(q[0] - p[0], q[1] - p[1])
                    for p, q in zip(r, r[1:])
                )
                if rlen > 1.5 * direct + WIRE_CELL:
                    r2 = route_wire(a[0], a[1], b[0], b[1], obs,
                                    clear_rects=clear_rects, keep_blocked=keep)
                    if r2:
                        r2len = sum(
                            math.hypot(q[0] - p[0], q[1] - p[1])
                            for p, q in zip(r2, r2[1:])
                        )
                        if r2len < rlen:
                            r = r2
            if not r:
                r = route_wire(a[0], a[1], b[0], b[1], obs,
                               clear_rects=clear_rects, keep_blocked=keep)
            return r

        core = _route((x1, y1), (x2, y2), clear, True)
        if not core:
            # No title-clear route: allow crossing the panel titles rather
            # than give up (only other nodes/panels/wires stay hard).
            core = _route((x1, y1), (x2, y2), clear, False)
        # The socket-to-socket route, kept as a node/panel-clear fallback if
        # the stubbed re-route below fails (it is far better than the
        # obstacle-unaware _stubbed_fallback).
        core_socket = core

        def _outward(points, idx, sx, sy, direction):
            # Only counts as an outward exit if it actually runs a
            # reasonable distance sideways; a few-px sideways hop (or a
            # vertical start hugging the node edge) must still get a stub,
            # so a wire never dives straight up/down into a port.  The bar
            # is WIRE_MIN_STUB, not the (smaller) grid step: a one-cell
            # sidestep is exactly the "leaves almost no room then goes
            # straight down" case.
            px, py = points[idx]
            return (
                abs(py - sy) < 0.5
                and (px - sx) * direction >= WIRE_MIN_STUB
            )

        if core:
            src_outward = len(core) >= 2 and _outward(core, 1, x1, y1, 1.0)
            dst_outward = len(core) >= 2 and _outward(
                core, len(core) - 2, x2, y2, -1.0
            )
            src_stub = not src_outward
            dst_stub = not dst_outward
        else:
            src_stub = dst_stub = True
            src_outward = dst_outward = False
        # Stick out only where the route doesn't already head outward.  The
        # wanted stub is capped at half the horizontal span: if the two
        # sockets are closer than two stubs, a full WIRE_MIN_STUB each would
        # overshoot each other (a hook/curve), so half the span lets the two
        # stubs *meet* in the middle instead.  It is also capped by the
        # nearest other node/panel along that direction: a fixed-length stub
        # could end inside a neighbouring node, the router couldn't reach that
        # endpoint, and the fallback drew a straight line through the node.
        span = abs(x2 - x1)
        want = min(WIRE_STUB, span * 0.5)
        # The two nodes this wire connects are never "crowding" its sockets:
        # the source sits behind its output, and the destination is where the
        # wire is going.  Only a *third* node/panel should force an escape.
        skip_ends = {edge["from_node"], edge["to_node"]}

        def _stub_len(sx, sy, direction, own):
            """How far the stub may run outward from (sx, sy) before it
            reaches another node/panel (leaving WIRE_PAD plus a 2px margin).
            ``own`` (the endpoint being stubbed) is skipped, so the *other*
            endpoint still clamps it - the stub must not run past the node
            it is connecting to."""
            limit = want
            rects = [
                r for nid, r in wire_rects.items() if nid != own
            ] + panels
            for rx1, ry1, rx2, ry2 in rects:
                if not (ry1 - WIRE_PAD <= sy <= ry2 + WIRE_PAD):
                    continue
                if direction > 0 and rx1 >= sx:
                    limit = min(limit, rx1 - WIRE_PAD - 2.0 - sx)
                elif direction < 0 and rx2 <= sx:
                    limit = min(limit, sx - rx2 - WIRE_PAD - 2.0)
            return max(0.0, limit)

        def _third_node_escape(sx, sy, direction, dodge_down):
            """When a *third* node/panel (not the other endpoint) sits right
            in front of the socket, build an L-shaped escape: run
            horizontally out as far as the gap allows (pad-clear if
            possible, else just clear of the crowding body), then step
            perpendicular past every crowding rect on ``dodge_down``'s side.
            Returns (points_from_socket, tip) or None when nothing crowds or
            the dodge can't clear it."""
            # The escape's horizontal leg is bounded by the same span-aware
            # budget as a normal stub (never longer than half the span).
            step = min(WIRE_MIN_STUB, want)
            rects = [
                r for nid, r in wire_rects.items() if nid not in skip_ends
            ] + panels
            # Only rects whose near edge sits within the stub's reach count
            # as crowding the socket - a node far off to the side (but on the
            # same row) must not make the escape dodge past it.
            reach = want + WIRE_PAD + 2.0
            crowd = []
            for rx1, ry1, rx2, ry2 in rects:
                if not (ry1 - WIRE_PAD <= sy <= ry2 + WIRE_PAD):
                    continue
                if direction > 0 and sx <= rx1 <= sx + reach:
                    crowd.append((rx1, ry1, rx2, ry2))
                elif direction < 0 and sx - reach <= rx2 <= sx:
                    crowd.append((rx1, ry1, rx2, ry2))
            if not crowd:
                return None
            near = min(abs(r[0] - sx) if direction > 0 else abs(sx - r[2])
                       for r in crowd)
            h = near - WIRE_PAD - 2.0
            if h <= 0.0:
                # No pad-clear room: settle for staying out of the body.
                h = max(0.0, near - 2.0)
            h = min(h, step)
            tip_x = sx + direction * h
            downs = [r[3] + WIRE_PAD + WIRE_GRID_STEP for r in crowd]
            ups = [r[1] - WIRE_PAD - WIRE_GRID_STEP for r in crowd]
            dodge_y = max(downs) if dodge_down else min(ups)
            tip = (tip_x, dodge_y)
            for rx1, ry1, rx2, ry2 in rects:
                if (rx1 - 1.0 <= tip[0] <= rx2 + 1.0
                        and ry1 - 1.0 <= tip[1] <= ry2 + 1.0):
                    return None
            return [(sx, sy), (tip_x, sy), tip], tip

        # Both ends dodge to the same side (the target's side of the source)
        # so the two escapes head the same way instead of crossing.
        dodge_down = y2 >= y1
        src_len = _stub_len(x1, y1, 1.0, edge["from_node"]) if src_stub else 0.0
        dst_len = _stub_len(x2, y2, -1.0, edge["to_node"]) if dst_stub else 0.0
        # Share the room equally: when only one side's clamp leaves space, the
        # other would otherwise stick fully out while this one dives straight
        # out of its socket.  Both get the tighter of the two usable lengths,
        # so the two legs stay symmetric and meet in the middle.  (Extending
        # the cramped side *past* its clamp was tried and pushed the stub into
        # the crowding node, so this only ever shortens the freer side.)
        if src_stub and dst_stub and src_len > 1.0 and dst_len > 1.0:
            shared = min(src_len, dst_len)
            src_len = shared
            dst_len = shared
        src_esc = (
            _third_node_escape(x1, y1, 1.0, dodge_down)
            if src_stub and src_len < MIN_USABLE_STUB else None
        )
        dst_esc = (
            _third_node_escape(x2, y2, -1.0, dodge_down)
            if dst_stub and dst_len < MIN_USABLE_STUB else None
        )
        # A stub only clamped by the far endpoint (no third-node crowd) means
        # there is genuinely no room for a sideways exit *between the two
        # nodes* - forcing one collapses it to a near-zero segment that the
        # cleanup passes then erase, and the wire hugs its own border.  Route
        # socket-to-socket instead (the first core already does).
        #
        # Gate this on the two sockets actually being close: when the clamp
        # came from a *third* node and the escape couldn't clear it, don't
        # drop the stub - keep the (short) sideways leg, or the wire dives
        # straight up/down out of a socket that still has room to the side.
        close_span = span < 2.0 * MIN_USABLE_STUB
        if (core is not None and src_stub and close_span
                and src_len < MIN_USABLE_STUB and src_esc is None):
            src_stub = False
        if (core is not None and dst_stub and close_span
                and dst_len < MIN_USABLE_STUB and dst_esc is None):
            dst_stub = False

        if not (src_stub or dst_stub):
            # The route leaves/enters sideways already (or the far endpoint
            # is too close to stub): use it as-is - EXCEPT for the one shape
            # that *looks* broken.  With the two sockets close, the A* grid
            # collapses to a single cell and there are exactly two
            # equal-cost socket-to-socket Ls; one of them puts its corner on
            # the target's border (the perpendicular run at x == x2), and
            # since nodes are painted *after* wires that leg is covered by
            # the node - the wire reads as "stops at the box" instead of
            # entering the socket.  Which L the heap tie-break returns is not
            # stable, so any re-route (a poll, a node growing, physics) can
            # flip the same pair between the two, i.e. "sometimes it does
            # this".  Rebuild the route as the Z the stub logic would have
            # produced had there been room: sideways out of the source,
            # across the middle of the gap, sideways into the socket, with
            # every segment in open air.
            core = self._close_pair_z(x1, y1, x2, y2, core) or core
            # Orthogonal throughout: merge redundant corners, align to the
            # grid, merge surviving short straights, then drop any retrace.
            # The sigmoid blending that used to sit between those passes made
            # a wire read as "weird curvature" (and, where it folded back,
            # as the wire clipping into itself).
            return self._dehairpin(self._drop_short_straights(
                self._sigmoid_short_segments(
                    self._snap_to_grid(
                        self._simplify_orthogonal(list(core), obstacles),
                        obstacles,
                    ),
                    smooth_obstacles,
                ),
                smooth_obstacles,
            ))

        # Build each exit leg.  A third-node escape (or a stub shorter than
        # MIN_USABLE_STUB) is attached *after* the cleanup passes, since they
        # would otherwise merge its short first segment away.
        src_leg = None
        dst_leg = None
        if src_esc is not None:
            src_leg, start = src_esc
        elif src_stub:
            start = (x1 + src_len, y1)
            if src_len < MIN_USABLE_STUB:
                src_leg = [(x1, y1), start]
        elif src_outward:
            # The direct route already left sideways, but we're re-routing
            # (the far end still needs a stub).  Start the new route a stub
            # out and prepend the exit, or the fresh A* may leave the socket
            # vertically instead.  Capped by `want` like every other stub so
            # it can't overshoot a tight near-side span (`WIRE_MIN_STUB` is
            # only the ceiling, not a fixed length).
            src_leg_len = min(WIRE_MIN_STUB, want)
            start = (x1 + src_leg_len, y1)
            src_leg = [(x1, y1), start]
        else:
            start = (x1, y1)
        if dst_esc is not None:
            dst_leg, end = dst_esc
        elif dst_stub:
            end = (x2 - dst_len, y2)
            if dst_len < MIN_USABLE_STUB:
                dst_leg = [(x2, y2), end]
        elif dst_outward:
            dst_leg_len = min(WIRE_MIN_STUB, want)
            end = (x2 - dst_leg_len, y2)
            dst_leg = [(x2, y2), end]
        else:
            end = (x2, y2)

        # Clear the route's start/end; a side with an exit leg gets an
        # outward-only corridor, an un-stubbed side the wide socket corridor
        # so the wire can leave in any direction.
        clear2 = []
        if src_stub or src_leg is not None:
            clear2.append(
                (start[0], start[1] - corr,
                 start[0] + WIRE_CELL, start[1] + corr)
            )
        else:
            clear2.append(
                (x1 - WIRE_CELL, y1 - corr, x1 + WIRE_CELL, y1 + corr)
            )
        if dst_stub or dst_leg is not None:
            clear2.append(
                (end[0] - WIRE_CELL, end[1] - corr,
                 end[0], end[1] + corr)
            )
        else:
            clear2.append(
                (x2 - WIRE_CELL, y2 - corr, x2 + WIRE_CELL, y2 + corr)
            )
        core = _route(start, end, clear2, core_socket is not None)
        if not core:
            # Crossing the panel titles is preferred over giving up.
            core = _route(start, end, clear2, False)
        # A hook: the route leaves/enters a stubbed endpoint horizontally from
        # the *socket side*, so the appended exit leg doubles back over it.
        # Retry with a thin "behind" strip that blocks that approach - this
        # makes the A* come around and enter from outside (a proper corner)
        # instead of leaving a loop for the renderer to round.
        if core is not None:
            hook = False
            if (src_stub and start[0] > x1 + 1.0 and len(core) >= 2
                    and abs(core[1][1] - start[1]) < 0.5
                    and core[1][0] < start[0]):
                hook = True
            if (dst_stub and end[0] < x2 - 1.0 and len(core) >= 2
                    and abs(core[-2][1] - end[1]) < 0.5
                    and core[-2][0] > end[0]):
                hook = True
            if hook:
                bm = WIRE_PAD + 3.0
                behind = []
                if src_stub and start[0] > x1 + 1.0:
                    behind.append((x1, y1 - bm, start[0], y1 + bm))
                if dst_stub and end[0] < x2 - 1.0:
                    behind.append((end[0], y2 - bm, x2, y2 + bm))
                core2 = _route(
                    start, end, clear2, core_socket is not None, behind
                )
                if not core2:
                    core2 = _route(start, end, clear2, False, behind)
                if core2:
                    core = core2
        direct = False
        if not core and core_socket is not None:
            # The stubbed re-route failed (usually because the stub endpoint
            # ended up boxed in).  Before giving up on the sideways exit, try
            # once with the *wide* socket corridors as well as the stub ones:
            # the narrow clear2 boxes the route in at a crowded socket, and we
            # don't want to fall back to a socket-to-socket route that dives
            # straight up/down out of the port.
            wide = clear2 + clear
            core3 = _route(start, end, wide, core_socket is not None)
            if not core3:
                core3 = _route(start, end, wide, False)
            if core3:
                core = core3
            else:
                core = core_socket
                direct = True
        if not core:
            # Last resort (everything blocked): an obstacle-unaware Z that
            # still exits each socket horizontally by at least WIRE_MIN_STUB,
            # so even stacked/vertical ports don't get a bare vertical line.
            self._wire_fell_back = True
            return self._dehairpin(self._stubbed_fallback(x1, y1, x2, y2))
        path = list(core)
        if not direct:
            if src_stub and src_leg is None:
                path.insert(0, (x1, y1))
            if dst_stub and dst_leg is None:
                path.append((x2, y2))
        cleaned = self._drop_short_straights(
            self._sigmoid_short_segments(
                self._snap_to_grid(
                    self._simplify_orthogonal(path, obstacles), obstacles
                ),
                smooth_obstacles,
            ),
            smooth_obstacles,
        )
        # Attach the exit legs *after* cleanup: their first (sideways)
        # segment can be shorter than a grid step, and the cleanup passes
        # would otherwise merge it away and the wire would hug the port - the
        # exact failure the stub exists to prevent.
        if src_leg is not None and not direct:
            cleaned = src_leg[:-1] + cleaned
        if dst_leg is not None and not direct:
            cleaned = cleaned + list(reversed(dst_leg[:-1]))
        return self._dehairpin(cleaned)

    @staticmethod
    def _sample_cubic(p0, p1, p2, p3, steps=12):
        pts = []
        for i in range(steps + 1):
            t = i / steps
            mt = 1.0 - t
            w0 = mt * mt * mt
            w1 = 3 * mt * mt * t
            w2 = 3 * mt * t * t
            w3 = t * t * t
            pts.append((
                w0 * p0[0] + w1 * p1[0] + w2 * p2[0] + w3 * p3[0],
                w0 * p0[1] + w1 * p1[1] + w2 * p2[1] + w3 * p3[1],
            ))
        return pts


    def _sigmoid_short_segments(self, path, obstacles,
                                min_len=WIRE_GRID_STEP):
        """Replace a too-short perpendicular jog (a vertical step between two
        horizontal runs, or rarely the reverse) with a smooth sigmoid.

        Orthogonal routing sometimes has to step a few px up/down when two
        runs are nearly collinear; a stubby straight segment there reads as a
        hard little kink, so the step is blended into an S-curve as long as
        the curve clears the obstacles.  The result is a dense polyline
        (sampled curve) drawn like any other route."""
        if len(path) < 4:
            return path
        pts = list(path)
        out = [pts[0]]

        def _clear(a, b):
            return not segment_blocked(
                a[0], a[1], b[0], b[1], obstacles, pad=WIRE_PAD
            )

        i = 1
        while i < len(pts) - 1:
            prev = out[-1]
            a, b = pts[i], pts[i + 1]
            nxt = pts[i + 2] if i + 2 < len(pts) else None
            vertical = abs(a[0] - b[0]) < 1e-6 and abs(a[1] - b[1]) > 1e-6
            horizontal = abs(a[1] - b[1]) < 1e-6 and abs(a[0] - b[0]) > 1e-6
            short = math.hypot(a[0] - b[0], a[1] - b[1]) < min_len
            jog = (
                short and nxt is not None
                and (
                    vertical
                    and abs(prev[1] - a[1]) < 1e-6
                    and abs(nxt[1] - b[1]) < 1e-6
                )
            ) or (
                short and nxt is not None
                and horizontal
                and abs(prev[0] - a[0]) < 1e-6
                and abs(nxt[0] - b[0]) < 1e-6
            )
            if not jog:
                out.append(a)
                i += 1
                continue
            if vertical:
                s = min(min_len, abs(prev[0] - a[0]), abs(nxt[0] - b[0]))
                if s <= 1e-6:
                    out.append(a)
                    i += 1
                    continue
                dir_a = 1.0 if prev[0] > a[0] else -1.0
                dir_b = 1.0 if nxt[0] > b[0] else -1.0
                start = (a[0] + dir_a * s, a[1])
                end = (b[0] + dir_b * s, b[1])
            else:
                s = min(min_len, abs(prev[1] - a[1]), abs(nxt[1] - b[1]))
                if s <= 1e-6:
                    out.append(a)
                    i += 1
                    continue
                dir_a = 1.0 if prev[1] > a[1] else -1.0
                dir_b = 1.0 if nxt[1] > b[1] else -1.0
                start = (a[0], a[1] + dir_a * s)
                end = (b[0], b[1] + dir_b * s)
            # Controls at the original corners give a smooth S with
            # horizontal/vertical tangents at start/end.
            p1, p2 = a, b
            curve = []
            steps = max(8, int(math.hypot(start[0] - end[0],
                                          start[1] - end[1]) / 14))
            for k in range(steps + 1):
                t = k / steps
                mt = 1.0 - t
                w0 = mt * mt * mt
                w1 = 3 * mt * mt * t
                w2 = 3 * mt * t * t
                w3 = t * t * t
                curve.append((
                    w0 * start[0] + w1 * p1[0] + w2 * p2[0] + w3 * end[0],
                    w0 * start[1] + w1 * p1[1] + w2 * p2[1] + w3 * end[1],
                ))
            if all(_clear(curve[k], curve[k + 1])
                   for k in range(len(curve) - 1)):
                out.append(start)
                out.extend(curve[1:])
                i += 2
            else:
                out.append(a)
                i += 1
        while i < len(pts):
            out.append(pts[i])
            i += 1
        return out

    @staticmethod
    def _drop_short_straights(path, obstacles, min_len=WIRE_GRID_STEP):
        """Last-resort cleanup: delete any surviving short axis-aligned
        straight by merging it into its neighbours, kept only where the joined
        run stays clear *and* axis-aligned - a diagonal, however short, reads
        as a weirdly angled straight line rather than a wire."""
        pts = list(path)
        if len(pts) < 3:
            return pts

        def _clear(a, b):
            return not segment_blocked(
                a[0], a[1], b[0], b[1], obstacles, pad=WIRE_PAD
            )

        def _merge_ok(a, c):
            # Merging deletes the middle point and joins the neighbours.  The
            # joined run must be clear *and* axis-aligned: a merge that leaves
            # a diagonal (however short) reads as a weirdly angled straight
            # line rather than a wire.
            if not _clear(a, c):
                return False
            return (
                abs(a[0] - c[0]) < 1e-6 or abs(a[1] - c[1]) < 1e-6
            )

        i = 1
        guard = 0
        while i < len(pts) - 1 and guard < 200:
            guard += 1
            a, b = pts[i], pts[i + 1]
            length = math.hypot(b[0] - a[0], b[1] - a[1])
            axis = abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6
            if not (axis and length < min_len):
                i += 1
                continue
            c = pts[i + 2] if i + 2 < len(pts) else None
            if c is not None and _merge_ok(a, c):
                del pts[i + 1]
                continue
            if _merge_ok(pts[i - 1], b):
                del pts[i]
                i = max(1, i - 1)
                continue
            i += 1
        return pts

    @staticmethod
    def _dehairpin(path):
        """Remove points where the polyline doubles back on itself.

        A wire can reach a stub tip from the socket side and then run back
        out along the appended exit leg; that over-and-back reads as a
        self-crossing loop once `draw_square_path` rounds the corners.
        Collinear reversals are pure retraces (dropped outright).  A short
        non-collinear reversal (a smoothed curve bending past the tip) is
        replaced by an orthogonal elbow at the corner, so the path stays
        square instead of becoming a weird diagonal.  Long non-collinear
        reversals are part of the route and left alone."""
        if len(path) < 3:
            return path
        cur = list(path)
        for _ in range(8):
            out = [cur[0]]
            changed = False
            for p in cur[1:]:
                if len(out) >= 2:
                    a, b = out[-2], out[-1]
                    d1x, d1y = b[0] - a[0], b[1] - a[1]
                    d2x, d2y = p[0] - b[0], p[1] - b[1]
                    l1 = math.hypot(d1x, d1y)
                    l2 = math.hypot(d2x, d2y)
                    if (d1x * d2x + d1y * d2y < 0.0
                            and min(l1, l2) > 1e-9):
                        if abs(d1x * d2y - d1y * d2x) < 1e-6:
                            out.pop()
                            changed = True
                        elif min(l1, l2) < 2.0 * WIRE_GRID_STEP:
                            out.pop()
                            # Elbow square with the longer neighbour.
                            if abs(d2x) >= abs(d2y):
                                out.append((a[0], p[1]))
                            else:
                                out.append((p[0], a[1]))
                            changed = True
                out.append(p)
            cur = out
            if not changed:
                break
        return cur

    def _route_still_valid(self, points, edge, x1, y1, x2, y2,
                           wire_rects, base_panels, spacing_rects=(),
                           moved_nodes=None, moved_panels=None):
        """Whether a cached route can be reused: it still starts/ends on the
        current sockets, is square, clears the *static* obstacles (nodes and
        panels), and (``spacing_rects``) is still a sane distance from the
        other wires.

        Wire-vs-wire spacing used to be applied only when a wire was first
        routed, so a cached wire could be overlapped by a neighbour that
        moved afterwards and never be pushed off it.  Checking the cached
        path against the other wires' keep-out strips catches that.  The
        caller throttles how often this can fire per edge (a pair that keeps
        ending up close must not re-route every frame), and the strips are
        thin enough that a merely-parallel neighbour doesn't jitter."""
        if not points or len(points) < 2:
            return False
        if (abs(points[0][0] - x1) > 0.5 or abs(points[0][1] - y1) > 0.5
                or abs(points[-1][0] - x2) > 0.5
                or abs(points[-1][1] - y2) > 0.5):
            return False
        skip = {edge["from_node"], edge["to_node"]}
        # Inflate by a hair *less* than the routing pad: a route that runs
        # exactly along the pad boundary (which the router legitimately
        # produces) must not be treated as a violation, or every route
        # invalidates itself every frame.
        pad = WIRE_PAD - 3.0
        if moved_nodes is None:
            obs_src = [(nid, r) for nid, r in wire_rects.items()
                       if nid not in skip]
        else:
            obs_src = [(nid, r) for nid, r in moved_nodes
                       if nid not in skip]
        obs = [
            (r[0] - pad, r[1] - pad, r[2] + pad, r[3] + pad)
            for _nid, r in obs_src
        ]
        panels = base_panels if moved_panels is None else moved_panels
        for _pid, rect in panels:
            if self._contains_point(rect, x1, y1) or self._contains_point(
                rect, x2, y2
            ):
                continue
            obs.append(
                (rect[0] - pad, rect[1] - pad, rect[2] + pad, rect[3] + pad)
            )
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            # Sigmoid-smoothed jogs are diagonal on purpose, so there is no
            # squareness requirement - only clearance.
            if segment_blocked(ax, ay, bx, by, obs, pad=0.0):
                return False
            if spacing_rects and segment_blocked(
                ax, ay, bx, by, spacing_rects, pad=-1.0
            ):
                return False
        return True

    def _wire_spacing_rects(self, eid, fn, tn, routes, only=None):
        """Keep-out strips for the *other* wires in ``routes`` (a routes dict
        keyed by edge id), for checking a cached route's clearance.
        Shared-endpoint wires use the smaller bundle half-width so a fan-out
        only re-routes a stale cached sibling when they are genuinely close,
        not merely within full spacing.

        ``only``, when given, restricts the check to those edge ids - used to
        test a cached route only against wires that actually changed (rather
        than every wire every frame, which churned and was slow)."""
        rects = []
        for oeid, pts in routes.items():
            if oeid == eid or not pts:
                continue
            if only is not None and oeid not in only:
                continue
            oe = self.edges.get(oeid)
            if oe is None:
                continue
            shared = oe["from_node"] == fn or oe["to_node"] == tn
            rects.extend(wire_polyline_rects(
                pts, half=WIRE_BUNDLE_SPACING if shared else WIRE_SPACING
            ))
        return rects

    def _wire_routing_signature(self):
        """A cheap fingerprint of everything routing reads: revealed node
        boxes, the edge set, panel content boxes (+ labels), group membership
        and the reveal/detach/drag state.  Compared each frame so an
        unchanged graph can skip routing entirely."""
        nodes = tuple(
            (nid, round(node["x"], 3), round(node["y"], 3),
             self.node_width(nid), self.node_height(nid))
            for nid, node in self.nodes.items()
            if self._node_revealed(nid)
        )
        edges = tuple(
            (eid, e["from_node"], e["to_node"],
             e.get("to_port", "in"), e.get("from_port", "out"))
            for eid, e in self.edges.items()
        )
        panels = []
        for pid, panel in self.panels.items():
            if not pid:
                continue
            rect = self._panel_rect_base(pid)
            if rect is None:
                continue
            panels.append((
                pid, panel.get("label", ""),
                round(rect[0], 3), round(rect[1], 3),
                round(rect[2], 3), round(rect[3], 3),
            ))
        groups = tuple(
            (gid, g.get("label", ""), g.get("color", ""),
             tuple(sorted(g.get("nodes") or ())))
            for gid, g in self.groups.items()
        )
        return (
            nodes, edges, tuple(panels), groups,
            None if self._revealed is None else frozenset(self._revealed),
            None if self.detaching_edge is None else self.detaching_edge[0],
            self.dragging_node,
        )

    def _route_all_wires(self):
        """Route every edge once per frame, in edge order.

        Obstacles are node/panel boxes (see _wire_points) *plus the wires
        already routed*, each laid down as a thin keep-out strip so the next
        wire keeps its distance.  Panel obstacles use the content-only
        ``_panel_rect_base`` so growing a panel to enclose its wires can't
        feed back into the routing (which would make panels creep outward
        every frame).  ``_wire_bounds`` records where each panel's own wires
        go so ``_panel_rect`` can grow to contain them."""
        sig = self._wire_routing_signature()
        if sig == self._wire_routing_sig:
            # Nothing routing reads has changed: keep last frame's routes.
            return
        self._wire_routing_sig = sig
        self._wire_routes = {}
        self._wire_bounds = {}
        self._wire_panel_cache = {}
        wire_rects = {
            nid: (
                node["x"], node["y"],
                node["x"] + self.node_width(nid),
                node["y"] + self.node_height(nid),
            )
            for nid, node in self.nodes.items()
            if self._node_revealed(nid)
        }
        base_panels = []
        panel_titles = {}
        for pid in self.panels:
            if not pid:
                continue
            rect = self._panel_rect_base(pid)
            if rect is not None:
                rx, ry, rw, rh = rect
                base_panels.append((pid, (rx, ry, rx + rw, ry + rh)))
                # The floating title row above the box is a *soft* obstacle:
                # wires prefer to go around it but may cross it when there is
                # no other route (see _wire_points).
                header = self._panel_header_rects(pid, rect).get("header")
                if header is not None:
                    panel_titles[pid] = header
        # Only obstacles that moved since the last routed frame need to be
        # re-checked on a cached route: everything else is unchanged, and the
        # cached route was already clear of it.  On the first frame every box
        # is "moved" (nothing recorded yet), so the check is exhaustive.
        moved_nodes = [
            (nid, rect) for nid, rect in wire_rects.items()
            if self._wire_last_boxes.get(nid) != rect
        ]
        self._wire_last_boxes = dict(wire_rects)
        moved_panels = [
            (pid, rect) for pid, rect in base_panels
            if self._wire_last_panels.get(pid) != rect
        ]
        self._wire_last_panels = dict(base_panels)
        strips = []
        # Same paths laid down with the smaller bundle half-width, for wires
        # that share an endpoint (see the extra= filter below).
        bundle_strips = []
        cache = {}
        prev_routes = self._route_cache
        prev_fallback = self._route_fallback
        self._route_fallback = set()
        # Wires that changed last frame; spacing is only re-checked against
        # those (None on the first frame = check against all).
        changed_before = self._route_changed
        changed_now = set()
        now = time.monotonic()
        for eid in list(self._route_spacing_evicted):
            if eid not in self.edges:
                del self._route_spacing_evicted[eid]
        # Only let wire-spacing invalidate a cached route once every this
        # long, per edge, so two wires that keep drifting within spacing of
        # each other settle instead of re-routing every frame.
        evict_cooldown = 0.5
        for eid, edge in self.edges.items():
            if self.detaching_edge and self.detaching_edge[0] == eid:
                continue
            if edge["from_node"] not in self.nodes or edge["to_node"] not in self.nodes:
                continue
            if not (
                self._node_revealed(edge["from_node"])
                and self._node_revealed(edge["to_node"])
            ):
                continue
            out_x, out_y, in_x, in_y = self._edge_endpoints(edge)
            # Wires that don't lead to the same place (share neither
            # endpoint) keep clear of each other at full spacing; wires that
            # do (same source or same destination) are allowed to *bundle*,
            # but still get a smaller keep-out so a fan-out reads as a close
            # pair rather than one thick line sitting exactly on top of
            # itself.
            fn, tn = edge["from_node"], edge["to_node"]
            extra = [r for f, t, r in strips if f != fn and t != tn]
            extra += [r for f, t, r in bundle_strips if f == fn or t == tn]
            points = self._route_cache.get(eid)
            old_points = points
            if points is not None and eid in prev_fallback:
                # Last frame this edge had no route at all (the fallback Z):
                # don't re-validate it against the obstacles it was allowed
                # to cross.  On a small drag, translate the fallback instead
                # of spending several full A* searches that will just fail
                # again; only a large jump re-tries the router.
                dx0 = out_x - points[0][0]
                dy0 = out_y - points[0][1]
                dx1 = in_x - points[-1][0]
                dy1 = in_y - points[-1][1]
                if max(abs(dx0), abs(dy0), abs(dx1), abs(dy1)) <= WIRE_CELL:
                    mx = (dx0 + dx1) / 2.0
                    my = (dy0 + dy1) / 2.0
                    shifted = [(p[0] + mx, p[1] + my) for p in points]
                    # Pin the true sockets and re-square the adjacent
                    # segments: setting the shifted first/last point straight
                    # to the new socket leaves the exit segment diagonal when
                    # the two ends moved by different amounts (the "angled
                    # line" seen while dragging a fallback wire).
                    shifted[0] = (out_x, out_y)
                    shifted[-1] = (in_x, in_y)
                    if len(shifted) >= 2:
                        if abs(points[0][1] - points[1][1]) < 1e-6:
                            shifted[1] = (shifted[1][0], out_y)
                        else:
                            shifted[1] = (out_x, shifted[1][1])
                        if abs(points[-1][1] - points[-2][1]) < 1e-6:
                            shifted[-2] = (shifted[-2][0], in_y)
                        else:
                            shifted[-2] = (in_x, shifted[-2][1])
                    points = self._dehairpin(shifted)
                    valid = True
                    self._route_fallback.add(eid)
                else:
                    valid = False
            elif points is not None:
                spacing = ()
                last = self._route_spacing_evicted.get(eid, 0.0)
                if now - last >= evict_cooldown:
                    spacing = self._wire_spacing_rects(
                        eid, fn, tn, prev_routes, only=changed_before
                    )
                valid = self._route_still_valid(
                    points, edge, out_x, out_y, in_x, in_y,
                    wire_rects, base_panels, spacing_rects=spacing,
                    moved_nodes=moved_nodes, moved_panels=moved_panels,
                )
                if not valid and spacing:
                    self._route_spacing_evicted[eid] = now
            else:
                valid = False
            if not valid:
                self._wire_fell_back = False
                points = self._wire_points(
                    edge, out_x, out_y, in_x, in_y, wire_rects, base_panels,
                    extra_obstacles=extra, panel_titles=panel_titles,
                )
                if self._wire_fell_back:
                    self._route_fallback.add(eid)
                if points != old_points:
                    changed_now.add(eid)
            cache[eid] = points
            self._wire_routes[eid] = points
            path_pts = points or [(out_x, out_y), (in_x, in_y)]
            for r in wire_polyline_rects(path_pts):
                strips.append((fn, tn, r))
            for r in wire_polyline_rects(path_pts, half=WIRE_BUNDLE_SPACING):
                bundle_strips.append((fn, tn, r))
            owner = self._panel_lca(
                self._panel_of_node(edge["from_node"]),
                self._panel_of_node(edge["to_node"]),
            )
            if not owner:
                continue
            xs = [p[0] for p in path_pts]
            ys = [p[1] for p in path_pts]
            box = self._wire_bounds.get(owner)
            new = (min(xs), min(ys), max(xs), max(ys))
            if box is None:
                self._wire_bounds[owner] = new
            else:
                self._wire_bounds[owner] = (
                    min(box[0], new[0]), min(box[1], new[1]),
                    max(box[2], new[2]), max(box[3], new[3]),
                )
        self._route_cache = cache
        self._route_changed = changed_now

    def _hit_nodes(self, x=None, y=None, require_ready=True):
        """Nodes in hit-test order: top-most (last drawn) first.

        Nodes are painted in insertion order (see on_draw), so the most
        recently added one is visually on top.  Every find_* hit test
        below iterates through this - NOT self.nodes directly - so a
        click on an overlap lands on the node the user actually sees.

        When (x, y) is supplied and falls inside some node's body, only
        that top-most node is yielded: a node on top owns every click
        within its rectangle, so the click can't fall through to a
        slider/body of a node drawn underneath.  Callers that hit-test
        things outside any body (sockets, the canvas itself) still get
        every node, top-first.

        ``require_ready=False`` lifts the not-yet-ready filter.  That
        filter belongs on live-signal controls (a volume slider or gate
        toggle on a node that isn't up yet is meaningless), but NOT on
        configuration actions (the inline field, the three-dot/Settings
        menu): those are how a node that got stuck not-ready is fixed, so
        gating them out traps the user."""
        if x is not None and y is not None:
            for nid, node in reversed(self.nodes.items()):
                if require_ready and not node.get("ready", True):
                    # Still loading: drawn translucent and not a hit target.
                    continue
                if node["x"] <= x <= node["x"] + self.node_width(nid) and node[
                    "y"
                ] <= y <= node["y"] + self.node_height(nid):
                    return iter(((nid, node),))
        return iter(
            (nid, node) for nid, node in reversed(self.nodes.items())
            if (not require_ready) or node.get("ready", True)
        )

    # ---------- panel hit tests ----------

    def _panel_paint_order(self):
        """Panels in paint order, bottom to top.

        A panel's ancestors paint before it, so a nested panel sits on
        top of its parent; among panels at the same depth the one added
        (or placed) most recently paints last.  This is the single source
        of truth for both drawing and hit testing - hit tests walk the
        *reverse* of this so a click lands on the panel the user sees."""
        order = list(self.panels)
        index = {pid: i for i, pid in enumerate(order)}
        return sorted(order, key=lambda p: (p.count("::"), index[p]))

    def _panel_order_top_first(self):
        return list(reversed(self._panel_paint_order()))

    def find_panel_at(self, x, y, exclude=None):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            if exclude and (
                pid == exclude or pid.startswith(exclude + "::")
            ):
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            px, py, pw, ph = rect
            if px <= x <= px + pw and py <= y <= py + ph:
                return pid
        return None

    def _panel_drop_target(self, cx, cy):
        """Which panel a node dropped with its centre at (cx, cy) belongs
        to, or "" for the root canvas.

        Membership is decided from each panel's *drag-start* box
        (``_panel_drag_baseline``), not the box as it grows toward the
        dragged node during the drag.  The grown box would otherwise follow
        the node out and swallow the drop, making it hard to drag a node
        out - and, worse, a source panel that grew under the pointer could
        shadow the panel the user actually dropped onto, so the node never
        nested there."""
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_drag_baseline.get(pid)
            if rect is None:
                rect = self._panel_rect_base(pid)
            if rect is None:
                continue
            px, py, pw, ph = rect
            if px <= cx <= px + pw and py <= cy <= py + ph:
                return pid
        return ""

    def find_panel_reset_at(self, x, y):
        for pid in self._panel_order_top_first():
            panel = self.panels.get(pid)
            if pid == "" or panel is None or not panel.get("readonly"):
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            r = self._panel_header_rects(pid, rect)["reset"]
            if r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return pid
        return None

    def find_panel_anchor_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x1, y1, x2, y2 = self._panel_header_rects(pid, rect)["anchor"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return pid
        return None

    def find_panel_header_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x1, y1, x2, y2 = self._panel_header_rects(pid, rect)["header"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return pid
        return None

    def find_panel_menu_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x1, y1, x2, y2 = self._panel_header_rects(pid, rect)["menu"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return pid
        return None

    def find_panel_delete_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            r = self._panel_header_rects(pid, rect).get("delete")
            if r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return pid
        return None

    def find_panel_edit_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            r = self._panel_header_rects(pid, rect).get("edit")
            if r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return pid
        return None

    def find_panel_close_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            r = self._panel_header_rects(pid, rect).get("close")
            if r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return pid
        return None

    def find_node_at(self, x, y, require_ready=True):
        for nid, node in self._hit_nodes(x, y, require_ready=require_ready):
            if node["x"] <= x <= node["x"] + self.node_width(nid) and node["y"] <= y <= node[
                "y"
            ] + self.node_height(nid):
                return nid
        return None

    def find_socket_at(self, x, y):
        """The socket nearest the pointer, within SOCKET_HIT_RADIUS.
        Nearest (not first-hit) so the wide radius can't make two
        adjacent sockets ambiguous."""
        best = None
        best_dist = self.SOCKET_HIT_RADIUS
        for nid, node in self._hit_nodes(x, y):
            for direction, ports in (("in", node["inputs"]), ("out", node["outputs"])):
                for i in range(len(ports)):
                    sx, sy = self._socket_position(nid, direction, i)
                    dist = math.hypot(x - sx, y - sy)
                    if dist < best_dist:
                        best_dist = dist
                        best = (nid, direction, i)
        return best

    def _edge_from_port_index(self, edge):
        """Which of the source node's output sockets `edge` leaves from -
        0 unless its from_port names a later socket (the Switcher's "b").
        Falls back to 0 for a from_port that isn't (or isn't yet) one of
        the source's declared outputs."""
        node = self.nodes.get(edge["from_node"])
        if node is None:
            return 0
        try:
            return node["outputs"].index(edge.get("from_port", "out"))
        except ValueError:
            return 0

    def _edge_to_port_index(self, edge):
        """Which of the target node's input sockets `edge` actually
        lands on - 0 for every single-input node type (and the
        common case even for a multi-input one), only different when
        the edge's to_port names a later socket (e.g. EchoCancelNode's
        "probe", index 1 - see node_specs.NODE_TYPE_SPECS["echo_cancel"]
        and Edge.to_port in patchSpace.py). Falls back to 0 for a
        to_port that isn't (or isn't yet) one of the target's declared
        inputs, so a stale/renamed port never crashes rendering."""
        node = self.nodes.get(edge["to_node"])
        if node is None:
            return 0
        try:
            return node["inputs"].index(edge.get("to_port", "in"))
        except ValueError:
            return 0

    def find_edge_at(self, x, y):
        threshold = 6
        for eid, edge in self.edges.items():
            if edge["from_node"] not in self.nodes or edge["to_node"] not in self.nodes:
                continue
            out_x, out_y, in_x, in_y = self._edge_endpoints(edge)
            dx, dy = in_x - out_x, in_y - out_y
            if dx == 0 and dy == 0:
                dist = math.hypot(x - out_x, y - out_y)
            else:
                t = max(
                    0,
                    min(1, ((x - out_x) * dx + (y - out_y) * dy) / (dx * dx + dy * dy)),
                )
                dist = math.hypot(x - (out_x + t * dx), y - (out_y + t * dy))
            if dist < threshold:
                return eid
        return None

    def find_slider_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "volume" or is_mute_node(nid):
                continue
            nx, ny = node["x"], node["y"]
            node_h = self.node_height(nid)
            slider_y = ny + node_h - self.SLIDER_HEIGHT - 5
            slider_x = nx + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            if (
                slider_x <= x <= slider_x + slider_width
                and slider_y - 6 <= y <= slider_y + 6
            ):
                return nid
        return None

    def find_wetdry_slider_at(self, x, y):
        """Reverb's dry/wet mix slider - same geometry as the volume
        slider (both live at the bottom of the node body) but only on
        control == "wetdry" nodes."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "wetdry":
                continue
            nx, ny = node["x"], node["y"]
            node_h = self.node_height(nid)
            slider_y = ny + node_h - self.SLIDER_HEIGHT - 5
            slider_x = nx + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            if (
                slider_x <= x <= slider_x + slider_width
                and slider_y - 6 <= y <= slider_y + 6
            ):
                return nid
        return None

    def find_gain_slider_at(self, x, y):
        """Normalize's boost slider - same bottom-of-node-body geometry
        as the volume/wetdry/sensitivity sliders, control == "gain". The
        drawn value is a 0..1 fraction of the plugin's 0..30 dB boost."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "gain":
                continue
            nx, ny = node["x"], node["y"]
            node_h = self.node_height(nid)
            slider_y = ny + node_h - self.SLIDER_HEIGHT - 5
            slider_x = nx + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            if (
                slider_x <= x <= slider_x + slider_width
                and slider_y - 6 <= y <= slider_y + 6
            ):
                return nid
        return None

    def find_sensitivity_slider_at(self, x, y):
        """Sensitivity Gate's 0..1 gain-staging slider - same bottom-of-
        node-body geometry as the volume/wetdry sliders, control ==
        "sensitivity". It sends the value to the daemon (which drives the
        hidden pre/post Volume nodes it owns) rather than to this node's
        own threshold - see _apply_sensitivity_slider and
        node_specs.py's sensitivity_gate spec comment for why."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "sensitivity":
                continue
            nx, ny = node["x"], node["y"]
            node_h = self.node_height(nid)
            slider_y = ny + node_h - self.SLIDER_HEIGHT - 5
            slider_x = nx + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            if (
                slider_x <= x <= slider_x + slider_width
                and slider_y - 6 <= y <= slider_y + 6
            ):
                return nid
        return None

    def find_settings_gear_at(self, x, y):
        """The green settings cog beside a node's anchor badge - returns
        the node whose Settings dialog should open when it's clicked.
        Only nodes with spec.settings (extra controls beyond the generic
        ID/label rows) and not panel ports draw one."""
        # Config action: reachable even while the node isn't ready (Settings
        # is how an unset required field gets fixed).
        for nid, node in self._hit_nodes(x, y, require_ready=False):
            if node.get("type") in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
                continue
            if not spec_for(node["type"]).settings:
                continue
            gx, gy = self._settings_cog_center(
                node["x"], node["y"], self.node_width(nid)
            )
            if gx - 12 <= x <= gx + 12 and gy - 12 <= y <= gy + 12:
                return nid
        return None

    def _device_rows(self, node) -> list:
        """Which extra bottom rows this specific device/app node
        currently needs - depends on live connection state, not just
        node type, so this is computed fresh rather than looked up
        from node_specs. Used identically by drawing and hit-testing
        below, so a row can never be drawn without also being
        clickable."""
        ntype = node["type"]
        if ntype in ("patchspace_device", "patchspace_mic_device"):
            # Speaker Line / Mic Line nodes always show the shared
            # built-in device's volume + lock.
            return ["volume"]
        if ntype not in ("device_input", "device_output", "app_input", "app_output"):
            return []
        rows = ["select"]
        if node.get("connected") and ntype in ("device_input", "device_output"):
            rows.append("volume")
            if node.get("is_bluetooth"):
                rows.append("codec")
        return rows

    def _device_row_rect(self, nid, row_index):
        node = self.nodes[nid]
        node_h = self.node_height(nid)
        row_h = self.FIELD_HEIGHT
        row_gap = 4
        rows = self._device_rows(node)
        total = len(rows)
        stack_h = total * row_h + max(0, total - 1) * row_gap
        start_y = node["y"] + node_h - stack_h - 5
        y = start_y + row_index * (row_h + row_gap)
        x = node["x"] + self.FIELD_MARGIN
        w = self.NODE_WIDTH - 2 * self.FIELD_MARGIN
        return (x, y, w, row_h)

    # Width of the lock button reserved at the right of a volume row.
    VOLUME_LOCK_SIZE = 18

    @staticmethod
    def _has_force_default(node):
        """Only the built-in sink/mic line nodes carry the force-default
        toggle (hardware devices don't)."""
        return node.get("type") in ("patchspace_device", "patchspace_mic_device")

    def _volume_row_rects(self, nid):
        """(slider_rect, force_rect_or_None, lock_rect) for `nid`'s volume
        row.  The slider stops short of the buttons so a press on one
        can't also start a volume drag; the lock sits at the right edge
        and the force-default button just left of it.  Shared by drawing,
        hit-testing and the drag handlers so they can't drift."""
        node = self.nodes[nid]
        rows = self._device_rows(node)
        rx, ry, rw, rh = self._device_row_rect(nid, rows.index("volume"))
        size = self.VOLUME_LOCK_SIZE
        gap = 6
        lock = (rx + rw - size, ry + (rh - size) / 2.0, size, size)
        force = None
        if self._has_force_default(node):
            force = (
                rx + rw - 2 * size - gap,
                ry + (rh - size) / 2.0,
                size,
                size,
            )
            slider_w = max(10, rw - 2 * size - 2 * gap)
        else:
            slider_w = max(10, rw - size - gap)
        slider = (rx, ry, slider_w, rh)
        return slider, force, lock

    def find_device_row_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
            rows = self._device_rows(node)
            for i, row_kind in enumerate(rows):
                rx, ry, rw, rh = self._device_row_rect(nid, i)
                if rx <= x <= rx + rw and ry <= y <= ry + rh:
                    return (nid, row_kind)
        return None

    def find_device_volume_slider_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
            if "volume" not in self._device_rows(node):
                continue
            sx, sy, sw, sh = self._volume_row_rects(nid)[0]
            if sx <= x <= sx + sw and sy <= y <= sy + sh:
                return nid
        return None

    def find_volume_lock_at(self, x, y):
        """The lock button at the right of a volume row (hardware device
        or Speaker/Mic Line) - returns the node whose lock was clicked."""
        for nid, node in self._hit_nodes(x, y):
            if "volume" not in self._device_rows(node):
                continue
            lx, ly, lw, lh = self._volume_row_rects(nid)[2]
            if lx <= x <= lx + lw and ly <= y <= ly + lh:
                return nid
        return None

    def find_force_default_at(self, x, y):
        """The force-default button, just left of the lock on a
        Speaker/Mic Line node - returns the line node clicked."""
        for nid, node in self._hit_nodes(x, y):
            if "volume" not in self._device_rows(node):
                continue
            rect = self._volume_row_rects(nid)[1]
            if rect is None:
                continue
            fx, fy, fw, fh = rect
            if fx <= x <= fx + fw and fy <= y <= fy + fh:
                return nid
        return None

    def find_gate_toggle_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "gate":
                continue
            gx, gy, gw, gh = self._gate_rect(nid)
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                return nid
        return None

    def find_filter_mode_at(self, x, y):
        """The Filter node's Include/Exclude button - same geometry as the
        gate toggle (see _gate_rect)."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "filter_mode":
                continue
            gx, gy, gw, gh = self._gate_rect(nid)
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                return nid
        return None

    def find_switcher_toggle_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "switcher":
                continue
            gx, gy, gw, gh = self._gate_rect(nid)
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                return nid
        return None

    def find_boolean_toggle_at(self, x, y):
        """The On/Off button on a boolean source node - same geometry as
        the old gate/switcher toggles (see _gate_rect)."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "boolean":
                continue
            gx, gy, gw, gh = self._gate_rect(nid)
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                return nid
        return None

    def find_fallback_toggle_at(self, x, y):
        """The interactive on/off button on a gate/switcher whose boolean
        ctrl input is unwired.  Once a ctrl signal is wired the button is
        still drawn (read-only, white - see _draw_node), but is not a hit
        target so it can't be clicked."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "fallback_onoff":
                continue
            if node.get("ctrl_connected"):
                continue
            gx, gy, gw, gh = self._gate_rect(nid)
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                return nid
        return None

    def find_impulse_button_at(self, x, y):
        """The Button node's clickable face.  Same geometry as the gate
        toggle (see _gate_rect), because it is drawn the same way."""
        for nid, node in self._hit_nodes(x, y):
            if spec_for(node["type"]).control != "impulse":
                continue
            gx, gy, gw, gh = self._gate_rect(nid)
            if gx <= x <= gx + gw and gy <= y <= gy + gh:
                return nid
        return None

    def find_toggle_switch_at(self, x, y):
        """The node-body on/off switch a spec declares with ``toggle``
        (the Sound Effect's Stack switch).  Geometry comes from
        _toggle_switch_rect, so it matches the drawn switch.

        Config action: reachable even while the node isn't ready - it is
        a setting, not a live control (same rule as find_field_at)."""
        for nid, node in self._hit_nodes(x, y, require_ready=False):
            if not spec_for(node["type"]).toggle:
                continue
            sx, sy, sw, sh = self._toggle_switch_rect(nid)
            if sx <= x <= sx + sw and sy <= y <= sy + sh:
                return nid
        return None

    def find_path_picker_at(self, x, y):
        """The folder button a `picker` spec draws beside its field
        (Sound Effect's path).  Geometry comes from _path_picker_rect, so
        it matches the drawn button."""
        for nid, node in self._hit_nodes(x, y):
            if not spec_for(node["type"]).picker:
                continue
            px, py, pw, ph = self._path_picker_rect(nid)
            if px <= x <= px + pw and py <= y <= py + ph:
                return nid
        return None

    def find_mute_checkbox_at(self, x, y):
        return self._find_bottom_checkbox_at(
            x,
            y,
            lambda node, nid: spec_for(node["type"]).control == "volume"
            and is_mute_node(nid),
        )

    def _find_bottom_checkbox_at(self, x, y, predicate):
        size = 14
        for nid, node in self._hit_nodes(x, y):
            if not predicate(node, nid):
                continue
            node_h = self.node_height(nid)
            cb_x, cb_y = node["x"] + 10, node["y"] + node_h - 22
            if cb_x <= x <= cb_x + size and cb_y <= y <= cb_y + size:
                return nid
        return None

    def find_three_dots_at(self, x, y):
        # Config action: the menu (and its Settings entry) must stay
        # reachable on a node that isn't ready, or the user is locked out
        # of the only way to fix it.
        for nid, node in self._hit_nodes(x, y, require_ready=False):
            if node.get("type") in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
                continue
            dot_x = node["x"] + self.node_width(nid) - 14
            dot_y = node["y"] + 12
            if dot_x - 10 <= x <= dot_x + 10 and dot_y - 10 <= y <= dot_y + 22:
                return nid
        return None

    def find_field_at(self, x, y):
        # Config action: reachable even while the node isn't ready, so an
        # unset field (the very thing keeping it not ready) can be edited.
        for nid, node in self._hit_nodes(x, y, require_ready=False):
            if not spec_for(node["type"]).field:
                continue
            field_x, field_y, field_w, field_h = self._field_rect(nid)
            if (
                field_x <= x <= field_x + field_w
                and field_y <= y <= field_y + field_h
            ):
                return nid
        return None

    def _field_value(self, node):
        field = spec_for(node["type"]).field
        if not field:
            return ""
        raw = node.get("meta", {}).get(field, "") or ""
        if field == "media_class":
            return media_class_label(raw)
        return raw

    def _on_query_tooltip(self, widget, x, y, keyboard_mode, tooltip):
        """GTK "query-tooltip" handler: after the normal hover delay over
        a node body, show its type name and description."""
        wx, wy = self.to_world(x, y)

        nid = self.find_force_default_at(wx, wy)
        if nid is not None:
            if bool(self.nodes[nid].get("force_default", True)):
                tooltip.set_text(
                    "Forcing system default: the daemon keeps this virtual "
                    "device as the default, re-checking every couple of "
                    "seconds. Click to stop."
                )
            else:
                tooltip.set_text(
                    "Not forcing the system default. Click to keep this "
                    "virtual device as the default output/input."
                )
            return True

        nid = self.find_volume_lock_at(wx, wy)
        if nid is not None:
            if bool(self.nodes[nid].get("volume_locked", True)):
                tooltip.set_text(
                    "Volume locked: the daemon re-asserts this device's "
                    "volume every tick. Click to let external changes through."
                )
            else:
                tooltip.set_text(
                    "Volume unlocked: external volume changes are adopted. "
                    "Click to lock it again."
                )
            return True

        nid = self.find_node_at(wx, wy)
        if nid is None:
            return False
        node = self.nodes.get(nid)
        if node is None:
            return False
        label = type_label(node["type"], nid)
        desc = description_for(node["type"])
        tooltip.set_text(f"{label}\n{desc}" if desc else label)
        return True

    # ---------- drawing ----------

    def _rect_visible(self, x1, y1, x2, y2):
        """Whether a world-space AABB intersects the current viewport (plus
        a margin so panels/shadows/headers just offscreen still draw).  Used
        to skip drawing work for offscreen items."""
        vr = getattr(self, "_view_rect", None)
        if vr is None:
            return True
        m = self._CULL_MARGIN
        return not (x2 < vr[0] - m or x1 > vr[2] + m
                    or y2 < vr[1] - m or y1 > vr[3] + m)

    def on_draw(self, area, cr, w, h):
        # Frame the graph once, the first time there are nodes and a real
        # allocation, so startup opens on the session rather than empty
        # space.  Done here (not on the first poll) because zoom_to_fit
        # needs the widget's real size.  A bulk import already fits via
        # _fit_after_load; this covers a GUI attached to a running daemon.
        self._last_view_size = (w, h)
        if self._needs_initial_fit and not self.loading and self.nodes and w > 1 and h > 1:
            self._needs_initial_fit = False
            # Deferred, exactly like the post-load fit: at first draw the
            # notebook page / toolbar / console haven't finished laying
            # out, so fitting now centres against a transient canvas
            # height and the graph ends up vertically off.  Let layout
            # settle, then frame.
            self._schedule_fit(150)
        # Group nesting geometry is stable for the duration of one frame;
        # clear the per-frame cache here (see _group_geo_cache).
        self._group_geo_cache.clear()
        # Route wires before drawing anything: panels grow to enclose the
        # wires running inside them, so their boxes must know the routes.
        self._route_all_wires()
        pal = theme_palette(self)
        # Grid background.  Fully opaque by default; a canvas opacity < 1
        # paints it at that alpha so the desktop shows through, and
        # _draw_panel_boxes paints a panel's backing at the same alpha (nodes
        # and wires stay opaque) - see constants.CANVAS_BG_ALPHA.
        cr.save()
        alpha = constants.CANVAS_BG_ALPHA
        if alpha < 1.0:
            cr.set_source_rgba(*pal["bg"], max(0.0, alpha))
        else:
            cr.set_source_rgb(*pal["bg"])
        cr.paint()
        cr.restore()

        cr.save()
        self.apply_view_transform(cr)
        # Visible world rect for cheap AABB culling of the draw loops below
        # (the model can be much larger than the viewport).
        _v1x, _v1y = self.to_world(0.0, 0.0)
        _v2x, _v2y = self.to_world(float(w), float(h))
        self._view_rect = (
            min(_v1x, _v2x), min(_v1y, _v2y),
            max(_v1x, _v2x), max(_v1y, _v2y),
        )

        draw_grid_background(cr, pal, self.pan_x, self.pan_y, self.zoom, w, h)

        # Panels are the outermost containers, then group annotations.
        self._draw_panel_boxes(cr, pal)

        # Group boxes sit behind the graph; their headers are drawn on
        # top of the nodes further down so the label stays clickable.
        self._draw_group_boxes(cr, pal)

        cr.set_source_rgb(*pal["link"])
        cr.set_line_width(2)
        # Routes were computed in _route_all_wires() before panels were
        # drawn (so their boxes could grow around them).
        _wire_now = time.monotonic()
        for eid, edge in self.edges.items():
            if eid in self._edge_ghosts:
                # Already removed locally; the retract animation draws it.
                continue
            if self.detaching_edge and self.detaching_edge[0] == eid:
                continue
            if edge["from_node"] not in self.nodes or edge["to_node"] not in self.nodes:
                continue
            if not (
                self._node_revealed(edge["from_node"])
                and self._node_revealed(edge["to_node"])
            ):
                continue
            out_x, out_y, in_x, in_y = self._edge_endpoints(edge)
            points = self._wire_routes.get(eid)
            if points:
                _xs = [p[0] for p in points]
                _ys = [p[1] for p in points]
                if not self._rect_visible(min(_xs), min(_ys), max(_xs), max(_ys)):
                    continue
            elif not self._rect_visible(
                min(out_x, in_x), min(out_y, in_y),
                max(out_x, in_x), max(out_y, in_y),
            ):
                continue
            kind = self._edge_wire_kind(edge)
            color = self._wire_color(pal, kind)
            if points:
                wire_pts = points
            else:
                # Skipped by _route_all_wires (detaching/unrevealed): a
                # stubbed Z keeps the no-bezier invariant and still exits
                # each socket sideways.
                wire_pts = self._stubbed_fallback(out_x, out_y, in_x, in_y)
            dash = self._wire_dash(kind)
            if dash is not None:
                cr.set_dash(dash)
            # A connection the user just made draws itself in from source to
            # target, with a fading leading tip (see _draw_growing_wire).
            born = self._edge_born.get(eid)
            if born is not None:
                dur = EDGE_DRAW_MS / 1000.0
                t = (_wire_now - born) / dur if dur > 0 else 1.0
                if t < 1.0:
                    self._draw_growing_wire(
                        cr, wire_pts, color, self._ease_in_out(t)
                    )
                    cr.set_dash([])
                    continue
            cr.set_source_rgb(*color)
            draw_square_path(cr, wire_pts)
            cr.set_dash([])

        # Removed connections retract (the draw-in run in reverse: the
        # visible tip walks back from the target to the source).
        if self._edge_ghosts:
            dur = EDGE_DRAW_MS / 1000.0
            for g in self._edge_ghosts.values():
                t = (_wire_now - g["t0"]) / dur if dur > 0 else 1.0
                if t >= 1.0:
                    continue
                pts = g["points"]
                _xs = [p[0] for p in pts]
                _ys = [p[1] for p in pts]
                if not self._rect_visible(min(_xs), min(_ys), max(_xs), max(_ys)):
                    continue
                gkind = g.get("kind", "boolean" if g.get("is_bool") else "audio")
                gdash = self._wire_dash(gkind)
                if gdash is not None:
                    cr.set_dash(gdash)
                self._draw_growing_wire(
                    cr, pts,
                    self._wire_color(pal, gkind),
                    self._ease_in_out(1.0 - t),
                )
                cr.set_dash([])

        for nid, node in self.nodes.items():
            if not self._node_revealed(nid):
                continue
            if not self._rect_visible(
                node["x"], node["y"],
                node["x"] + self.node_width(nid),
                node["y"] + self.node_height(nid),
            ):
                continue
            if nid not in self._anim_seen:
                # First frame this node is visible: start the pop now so it
                # scales up from nothing even if the ticker hasn't run yet.
                self._anim_seen.add(nid)
                self._node_born[nid] = time.monotonic()
                self._node_alpha.setdefault(nid, 0.0)
            scale = self._node_scale(nid)
            alpha = self._node_alpha.get(nid, 1.0)
            if scale >= 0.999 and alpha >= 0.999:
                self._draw_node(cr, pal, nid, node)
                continue
            # Materialize/loading: scale about the node centre and paint the
            # whole node through a group so the alpha applies uniformly.
            cr.save()
            if scale < 0.999:
                cx = node["x"] + self.node_width(nid) / 2.0
                cy = node["y"] + self.node_height(nid) / 2.0
                cr.translate(cx, cy)
                cr.scale(max(scale, 0.01), max(scale, 0.01))
                cr.translate(-cx, -cy)
            cr.push_group()
            self._draw_node(cr, pal, nid, node)
            cr.pop_group_to_source()
            cr.paint_with_alpha(max(alpha, 0.0))
            cr.restore()

        # Deleted nodes fade out as a translucent outline.
        if self._ghosts:
            now_g = time.monotonic()
            dur = NODE_DELETE_MS / 1000.0
            border = pal.get("node_border", (0.36, 0.36, 0.39))
            for g in self._ghosts:
                t = (now_g - g["t0"]) / dur if dur > 0 else 1.0
                a = max(0.0, 1.0 - min(1.0, t))
                if a <= 0.0:
                    continue
                cr.save()
                draw_rounded_rect(cr, g["x"], g["y"], g["w"], g["h"], 8)
                cr.set_source_rgba(*border, a * 0.5)
                cr.fill_preserve()
                cr.set_source_rgba(*border, a * 0.9)
                cr.set_line_width(2.0)
                cr.stroke()
                cr.restore()

        # A tiny panel-color chip at each node's bottom-left corner while
        # the node lives in a non-root panel (root-level nodes get none).
        for nid, node in self.nodes.items():
            if not self._node_revealed(nid):
                continue
            pid = self._panel_of_node(nid)
            if not pid:
                continue
            panel = self.panels.get(pid)
            if panel is None:
                continue
            r, g, b = self._panel_rgb(pid)
            side = 9.0
            px = node["x"]
            py = node["y"] + self.node_height(nid) + 3
            draw_rounded_rect(cr, px, py, side, side, 2.5)
            cr.set_source_rgb(r, g, b)
            cr.fill()
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.35)
            cr.set_line_width(1.0)
            draw_rounded_rect(cr, px, py, side, side, 2.5)
            cr.stroke()

        # A nested panel shows which panel it's in with the same tiny chip,
        # in the *parent* panel's color, at its bottom-left corner
        # (top-level panels get none, like root-level nodes).
        for pid in self.panels:
            if pid == "":
                continue
            parent = self.panels[pid].get("parent", "")
            if not parent:
                continue
            ppanel = self.panels.get(parent)
            rect = self._panel_rect(pid)
            if ppanel is None or rect is None:
                continue
            r, g, b = self._hex_to_rgb(ppanel.get("color", "#3584e4"))
            side = 9.0
            px = rect[0]
            py = rect[1] + rect[3] + 3
            draw_rounded_rect(cr, px, py, side, side, 2.5)
            cr.set_source_rgb(r, g, b)
            cr.fill()
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.35)
            cr.set_line_width(1.0)
            draw_rounded_rect(cr, px, py, side, side, 2.5)
            cr.stroke()

        # (A selected node's ring is drawn by _draw_node - inside its body and
        # under its sockets, so a port on the edge is never covered.)

        # Group labels/ids/color chips on top of the nodes.
        self._draw_group_headers(cr, pal)
        # Panel headers/labels/reset/resize on the very top.
        self._draw_panel_headers(cr, pal)

        if self.select_rect:
            x1, y1, x2, y2 = self.select_rect
            cr.set_source_rgba(*pal["select"], 0.18)
            cr.rectangle(x1, y1, x2 - x1, y2 - y1)
            cr.fill()
            cr.set_source_rgb(*pal["select"])
            cr.set_line_width(1)
            cr.rectangle(x1, y1, x2 - x1, y2 - y1)
            cr.stroke()

        if self.connecting_from:
            nid, idx = self.connecting_from
            node = self.nodes.get(nid)
            if node:
                sx, sy = self._socket_position(nid, "out", idx)
                cx, cy = self.drag_current_xy
                kind = port_kind(node["type"], node["outputs"][idx], "out")
                dash = self._wire_dash(kind)
                if dash is not None:
                    color = self._wire_color(pal, kind)
                    cr.set_dash(dash)
                else:
                    color = pal["pending_link"]
                cr.set_source_rgb(*color)
                cr.set_line_width(2)
                # Rubber band while dragging a new connection: a smooth
                # sigmoid curve (square routing would look terrible mid-
                # gesture, and the final wire is re-routed on release).
                draw_bezier_link(cr, sx, sy, cx, cy)
                cr.set_dash([])

        cr.restore()

    @staticmethod
    def _ease_out_back(t):
        """Pop: 0 -> 1 with a noticeable overshoot past 1 near the end."""
        t = max(0.0, min(1.0, t))
        c1 = 2.2
        c3 = c1 + 1.0
        u = t - 1.0
        return 1.0 + c3 * u * u * u + c1 * u * u

    @staticmethod
    def _ease_in_out(t):
        t = max(0.0, min(1.0, t))
        return t * t * (3.0 - 2.0 * t)

    def _start_node_ghost(self, nid):
        """Remember a just-deleted node's box so it can fade as an outline.

        The node itself is removed from the model right away (so it is not
        hit-tested, wired, laid out, ...); only this snapshot lives on for
        the delete animation."""
        if nid in self._ghosted:
            return
        node = self.nodes.get(nid)
        if node is None:
            return
        self._ghosted.add(nid)
        self._ghosts.append({
            "id": nid,
            "x": node["x"],
            "y": node["y"],
            "w": self.node_width(nid),
            "h": self.node_height(nid),
            "t0": time.monotonic(),
        })

    def _start_edge_ghost(self, eid, edge):
        """Snapshot a removed connection's last drawn path so it can retract
        (draw in reverse) instead of blinking out of existence.  Called both
        optimistically when the user removes an edge and from the poll that
        drops it; de-duplicated so only one animation runs."""
        if eid in self._edge_ghosted:
            return
        points = self._wire_routes.get(eid) or self._route_cache.get(eid)
        if not points:
            return
        kind = self._edge_wire_kind(edge)
        self._edge_ghosted.add(eid)
        self._edge_ghosts[eid] = {
            "points": list(points),
            "kind": kind,
            "is_bool": kind == "boolean",
            "t0": time.monotonic(),
        }

    def _retire_edge(self, eid):
        """Start the retract animation for an edge the GUI is removing, so
        it begins on the user's action instead of waiting for the poll."""
        edge = self.edges.get(eid)
        if edge is not None:
            self._start_edge_ghost(eid, edge)

    def _impulse_flash_progress(self, nid):
        """0..1 progress through a button's press pulse, or None when it
        isn't pulsing.  The flash is local (see _impulse_flash) and ages
        out in _anim_tick, which also keeps repainting while it runs."""
        t0 = self._impulse_flash.get(nid)
        if t0 is None:
            return None
        dur = IMPULSE_FLASH_MS / 1000.0
        if dur <= 0:
            return None
        t = (time.monotonic() - t0) / dur
        if t >= 1.0:
            return None
        return t

    def _node_scale(self, nid):
        born = self._node_born.get(nid)
        if born is None:
            return 1.0
        dur = NODE_MATERIALIZE_MS / 1000.0
        if dur <= 0:
            return 1.0
        t = (time.monotonic() - born) / dur
        if t >= 1.0:
            return 1.0
        return self._ease_out_back(t)

    def _anim_tick(self):
        """Drive the node appearance animations and repaint while any is
        running.  A node fades toward NODE_LOADING_ALPHA until the daemon
        reports it ready, then toward full; a new node also scales up (see
        _node_scale)."""
        now = time.monotonic()
        mat = NODE_MATERIALIZE_MS / 1000.0
        fade = NODE_FADE_MS / 1000.0
        active = False
        for nid, node in self.nodes.items():
            # The pop starts the first time the ticker sees the node visible,
            # rather than whenever it first appeared in a poll: a slow load
            # can add the node long before it is shown, and the animation
            # would otherwise be over before its first frame.
            if nid not in self._anim_seen:
                if not self._node_revealed(nid):
                    continue
                self._anim_seen.add(nid)
                self._node_born[nid] = now
                self._node_alpha.setdefault(nid, 0.0)
            target = 1.0 if node.get("ready", True) else NODE_LOADING_ALPHA
            st = self._node_fade.get(nid)
            if st is None or st["target"] != target:
                st = {
                    "target": target,
                    "from": self._node_alpha.get(nid, target),
                    "t0": now,
                }
                self._node_fade[nid] = st
            t = (now - st["t0"]) / fade if fade > 0 else 1.0
            if t >= 1.0:
                self._node_alpha[nid] = target
            else:
                self._node_alpha[nid] = st["from"] + (
                    target - st["from"]
                ) * self._ease_in_out(t)
                active = True
            born = self._node_born.get(nid)
            if born is not None and now - born < mat:
                active = True
        # Advance/expire delete ghosts.
        if self._ghosts:
            dur = NODE_DELETE_MS / 1000.0
            self._ghosts = [
                g for g in self._ghosts
                if dur > 0 and now - g["t0"] < dur
            ]
            self._ghosted = {g["id"] for g in self._ghosts}
            active = True
        # Draw-in animation for freshly-made connections.  Promote a pending
        # edge once the daemon has echoed it (its id exists locally), then
        # keep repainting until every one has finished.
        if self._pending_edge_draw:
            for eid, t0 in list(self._pending_edge_draw.items()):
                if eid in self.edges:
                    del self._pending_edge_draw[eid]
                    self._edge_born[eid] = now
                elif now - t0 > 3.0:
                    # The add never showed up (rejected/removed): stop
                    # waiting so this can't grow without bound.
                    del self._pending_edge_draw[eid]
        if self._edge_born:
            dur = EDGE_DRAW_MS / 1000.0
            for eid, born in list(self._edge_born.items()):
                if dur <= 0 or now - born >= dur:
                    del self._edge_born[eid]
                else:
                    active = True
        # Retracting (removed) connections.
        if self._edge_ghosts:
            dur = EDGE_DRAW_MS / 1000.0
            for eid, g in list(self._edge_ghosts.items()):
                if dur <= 0 or now - g["t0"] >= dur:
                    del self._edge_ghosts[eid]
                else:
                    active = True
            self._edge_ghosted = set(self._edge_ghosts)
        # Button press pulses (local, see _impulse_flash).
        if self._impulse_flash:
            dur = IMPULSE_FLASH_MS / 1000.0
            for nid, t0 in list(self._impulse_flash.items()):
                if dur <= 0 or now - t0 >= dur:
                    del self._impulse_flash[nid]
                else:
                    active = True
        if active:
            self.queue_draw()
        return True

    @staticmethod
    def _grow_split(points, target, fade):
        """Split a polyline by arc length into an opaque ``base`` (up to
        ``target - fade``) and a ``tail`` (from there to ``target``), both
        closed on the exact split points so the two pieces join seamlessly.

        ``target``/``fade`` are clamped to the polyline; a zero-length tail
        comes back as a single point."""
        total = sum(
            math.hypot(bx - ax, by - ay)
            for (ax, ay), (bx, by) in zip(points, points[1:])
        )
        target = max(0.0, min(target, total))
        near = max(0.0, target - fade)

        def _lerp(a, b, t):
            return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)

        base = [points[0]]
        tail = []
        acc = 0.0
        for a, b in zip(points, points[1:]):
            seg = math.hypot(b[0] - a[0], b[1] - a[1])
            if seg <= 1e-9:
                continue
            seg_end = acc + seg
            if acc < near:
                end_d = min(near, seg_end)
                p = _lerp(a, b, (end_d - acc) / seg)
                if p != base[-1]:
                    base.append(p)
            if seg_end > near:
                start_d = max(near, acc)
                end_d = min(target, seg_end)
                if end_d > start_d:
                    pa = _lerp(a, b, (start_d - acc) / seg)
                    pb = _lerp(a, b, (end_d - acc) / seg)
                    if not tail:
                        tail.append(pa)
                    tail.append(pb)
            acc = seg_end
            if acc >= target:
                break
        if not tail:
            tail = [base[-1]]
        return base, tail

    def _draw_growing_wire(self, cr, points, color, progress):
        """Stroke a connection that is drawing itself in from source to
        target: the wire is revealed up to ``progress`` of its length, and
        the leading ``WIRE_FADE`` of that run fades to transparent so the
        tip reads as "still arriving" rather than a hard cut."""
        if len(points) < 2 or progress <= 0.0:
            return
        r, g, b = color
        total = sum(
            math.hypot(bx - ax, by - ay)
            for (ax, ay), (bx, by) in zip(points, points[1:])
        )
        if total <= 0.0:
            return
        target = max(0.0, min(1.0, progress)) * total
        base, tail = self._grow_split(points, target, WIRE_FADE)

        if len(base) >= 2:
            cr.set_source_rgb(r, g, b)
            draw_square_path(cr, base)

        # Resample the (short) tail so the alpha ramp is a smooth gradient
        # even across a corner, then stroke each step at its own alpha.
        samples = [tail[0]]
        for a, c in zip(tail, tail[1:]):
            seg = math.hypot(c[0] - a[0], c[1] - a[1])
            steps = max(1, int(seg / 4.0))
            for j in range(1, steps + 1):
                t = j / steps
                samples.append((
                    a[0] + (c[0] - a[0]) * t,
                    a[1] + (c[1] - a[1]) * t,
                ))
        if len(samples) >= 2:
            cr.set_line_cap(cairo.LINE_CAP_ROUND)
            last = len(samples) - 1
            for i in range(last):
                a, c = samples[i], samples[i + 1]
                alpha = max(0.0, 1.0 - (i + 1) / last)
                cr.set_source_rgba(r, g, b, alpha)
                cr.move_to(a[0], a[1])
                cr.line_to(c[0], c[1])
                cr.stroke()
            cr.set_line_cap(cairo.LINE_CAP_BUTT)

    def _draw_node(self, cr, pal, nid, node):
        x, y = node["x"], node["y"]
        node_h = self.node_height(nid)
        node_w = self.node_width(nid)
        spec = spec_for(node["type"])

        is_conn_source = (
            self.connecting_from is not None and self.connecting_from[0] == nid
        )
        is_conn_target = self.hover_target_node == nid
        is_offline = not node.get("ready", True)
        # Module died or its interior never connected - see main.py's
        # _node_health. Distinct from is_offline (still coming up):
        # drawn with the theme's error color, solid, and a "dead"
        # badge rather than the neutral "not connected yet" one.
        is_dead = node.get("health") == "dead"
        # Structurally up but at least one edge touching it hasn't
        # landed as a live link yet (see nodes_pending_wiring above).
        # Only tracked separately from is_offline so the two don't
        # double-dash the same border; is_offline already implies
        # "don't trust this node's links yet" on its own.
        is_wiring = (not is_offline) and (nid in self.nodes_pending_wiring)
        # Stable, category-assigned theme color - same type always gets
        # the same border, and it comes from the GTK theme rather than a
        # per-process hash (see node_specs.color_name_for_node_type).
        border_color = theme_color(
            self, color_name_for_node_type(node["type"]), pal["node_border"]
        )
        # Panel ports visually take on their panel's color (not stored on
        # the node - purely cosmetic).
        port_color = None
        if node["type"] in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
            panel_id = self._panel_of_node(nid)
            if panel_id in self.panels:
                port_color = self._panel_rgb(panel_id)
                border_color = port_color

        draw_rounded_rect(cr, x, y, node_w, node_h, 8)
        cr.set_source_rgb(*pal["node_bg"])
        cr.fill_preserve()
        if is_conn_source or is_conn_target:
            cr.set_source_rgb(*pal["select"])
        elif is_dead:
            cr.set_source_rgb(*pal["error"])
        elif is_offline or is_wiring:
            cr.set_source_rgb(*pal["warning"])
        else:
            cr.set_source_rgb(*border_color)
        cr.set_line_width(2)
        if (is_offline or is_wiring) and not is_dead and not (
            is_conn_source or is_conn_target
        ):
            # Dashed rather than solid - a glance at the canvas should
            # tell "still coming up"/"still wiring" apart from a
            # category whose assigned color happens to be amber
            # (Hardware & Apps uses the theme's warning color too).
            # A tighter dash for "wiring" than "offline" so the two
            # remain visually distinguishable at a glance too.
            cr.set_dash([4.0, 3.0] if is_offline else [2.0, 2.0])
        cr.stroke()
        cr.set_dash([])

        if nid in self.selected_nodes:
            # The selection ring sits *inside* the body, and is drawn before
            # the sockets below, so a port centred on the edge is never
            # covered by it.  It used to be drawn by the overlay pass on top
            # of the nodes, which crossed every port on the node's edge.
            inset = 2.0
            draw_rounded_rect(
                cr, x + inset, y + inset,
                node_w - 2 * inset, node_h - 2 * inset, 6,
            )
            cr.set_source_rgb(*pal["select"])
            cr.set_line_width(2.5)
            cr.stroke()

        if node["type"] not in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
            # The node-type glyph in the header's top-left corner, tinted
            # with the node's category color.  Drawn before the header
            # text, which _header_line_layout insets to clear it.
            icon_type = "mute" if is_mute_node(nid) else node["type"]
            self._draw_node_icon(
                cr, icon_for_add_node_type(icon_type),
                x + 9, y + 9, self.NODE_ICON_SIZE, border_color,
            )
            self._draw_anchor_icon(
                cr, pal, x, y, node_w, nid in self.anchored_nodes
            )
            self._draw_three_dots(cr, x, y, node_w)

        self._draw_header(cr, pal, nid, node, x, y, color=port_color)

        if spec.control == "volume":
            if is_mute_node(nid):
                self._draw_mute_checkbox(cr, x, y, node_h, node["volume"])
            else:
                self._draw_volume_slider(cr, pal, x, y, node_h, node["volume"])
        elif spec.control == "gate":
            self._draw_gate_toggle(cr, nid, node["enabled"])
        elif spec.control == "switcher":
            self._draw_switcher_toggle(cr, nid, node.get("output", 0))
        elif spec.control == "boolean":
            self._draw_boolean_toggle(cr, pal, nid, node.get("output", 0))
        elif spec.control == "fallback_onoff":
            # Gate/switch on/off button.  With nothing wired into the
            # boolean ctrl input it is the interactive control (a gate
            # reflects `enabled`, a switch its stored `output` channel).
            # Once a ctrl signal is wired it stays visible but read-only
            # and white, showing the value actually in effect.  The
            # ctrl edge can appear a poll before the daemon reports the
            # resolved value, so fall back to the stored default until it
            # arrives rather than flashing "Off".
            driven = bool(node.get("bool_driven")) or bool(
                node.get("ctrl_connected")
            )
            state = node.get("bool_state")
            if node["type"] == "gate":
                stored = 1 if node.get("enabled", True) else 0
            else:
                stored = node.get("output", 0)
            output = (1 if state else 0) if (driven and state is not None) else stored
            self._draw_boolean_toggle(cr, pal, nid, output, driven=driven)
        elif spec.control == "wetdry":
            self._draw_wetdry_slider(
                cr, pal, x, y, node_h, node.get("wet_dry", 0.3)
            )
        elif spec.control == "gain":
            # Normalize's boost, drawn as a plain 0..1 slider (fraction
            # of the plugin's 0..30 dB range - see find_gain_slider_at).
            self._draw_volume_slider(cr, pal, x, y, node_h, node.get("gain", 0.5))
        elif spec.control == "sensitivity":
            self._draw_threshold_slider(
                cr, pal, x, y, node_h, node.get("sensitivity", 0.0)
            )
        elif spec.control == "impulse":
            self._draw_impulse_button(cr, nid, node)
        elif spec.control == "filter_mode":
            self._draw_filter_mode_button(cr, pal, nid, node.get("exclude", False))
        elif spec.field:
            self._draw_text_field(cr, pal, nid, self._field_value(node))
        if spec.picker:
            self._draw_path_picker(cr, pal, nid)

        # The node-body switch and the live status read-out sit with the
        # inline field, not instead of it, so they are keyed off the spec
        # rather than the control/field dispatch above.
        if spec.toggle:
            self._draw_toggle_row(cr, pal, nid, node)
        if spec.indicator:
            self._draw_play_indicator(cr, pal, nid, node)

        for i, row_kind in enumerate(self._device_rows(node)):
            self._draw_device_row(cr, pal, nid, node, i, row_kind)

        # A node whose Settings dialog has more than the generic
        # ID/label rows (Echo Cancel's module options, Noise Cancel's
        # method/dials) gets a small green gear badge in the header's
        # top-right corner, just left of the anchor badge, so it's obvious
        # there's something worth opening the menu for.  Panel ports have a
        # description row but never show the badge (right-click for their
        # menu, which includes Settings).
        if spec.settings and node["type"] not in (
            self._PORT_IN_TYPES | self._PORT_OUT_TYPES
        ):
            self._draw_settings_gear(cr, pal, x, y, node_w)

        multi_input = len(node["inputs"]) > 1
        for i in range(len(node["inputs"])):
            sx, sy = self._socket_position(nid, "in", i)
            kind = port_kind(node["type"], node["inputs"][i], "in")
            cr.set_source_rgb(*self._socket_color(pal, kind, False))
            self._trace_socket(cr, sx, sy, kind)
            cr.fill()
            # A single "in" socket is self-explanatory and every node
            # type had exactly that until EchoCancelNode - only label
            # sockets when there's more than one to tell apart (e.g.
            # "mic" vs "probe"), so ordinary nodes stay uncluttered.
            # spec.socket_labels opts a node out entirely (the symmetric
            # AND/OR gates).
            if multi_input and spec.socket_labels:
                draw_text_ellipsized(
                    cr,
                    sx + 9,
                    sy - 5,
                    node["inputs"][i],
                    self.NODE_WIDTH - 28,
                    8,
                    pal["subtext"],
                )
        multi_output = len(node["outputs"]) > 1
        for i in range(len(node["outputs"])):
            sx, sy = self._socket_position(nid, "out", i)
            is_source = self.connecting_from == (nid, i)
            kind = port_kind(node["type"], node["outputs"][i], "out")
            cr.set_source_rgb(
                *(
                    pal["select"]
                    if is_source
                    else self._socket_color(pal, kind, True)
                )
            )
            self._trace_socket(cr, sx, sy, kind)
            cr.fill()
            # Multi-output nodes get their sockets labelled inside the
            # node body: the Switcher's short "a"/"b" right-aligned in a
            # narrow box, a Split Bundle's member lines in a wider box so
            # an app name is readable.  A lone "out" socket needs no label.
            if multi_output and spec.socket_labels:
                labels = node.get("output_labels")
                if labels:
                    draw_text_ellipsized(
                        cr,
                        sx - 92,
                        sy - 5,
                        labels.get(node["outputs"][i], node["outputs"][i]),
                        80,
                        8,
                        pal["subtext"],
                    )
                else:
                    draw_text_ellipsized(
                        cr,
                        sx - 25,
                        sy - 5,
                        node["outputs"][i],
                        14,
                        8,
                        pal["subtext"],
                    )

    def _header_line_layout(self, nid, node, i):
        """(left_inset, max_width) for header line `i`, shared by
        _draw_header and the two height measurements so they can never
        disagree about how a line wraps.  The first line shares its row
        with the node-type glyph on the left and the anchor/three-dot/
        settings badges on the right, so it is inset on both sides; the
        lines below only clear the right-side content."""
        if node["type"] in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
            return 0.0, self.node_width(nid) - 20
        reserve = self.HEADER_ICON_RESERVE if i == 0 else 20
        if i == 0 and spec_for(node["type"]).settings:
            reserve += 22  # room for the settings cog too
        left = self.NODE_ICON_LEFT_RESERVE if i == 0 else 0
        # `_draw_header` draws at x + HEADER_SIDE_PAD + left, so the padding
        # has to come out of the width as well - leaving it out is what let a
        # line overlap the badges (and wrap mid-word) on a narrow node.
        return (
            float(left),
            self.node_width(nid) - reserve - left - self.HEADER_SIDE_PAD,
        )

    def _header_needed_width(self, nid, node):
        """The narrowest node width whose header lines all fit without
        breaking a word: the chrome each line carries (the type icon on the
        left, the badge reserves on the right, the side padding) plus its
        widest single word.  Multi-word text still wraps normally - this is
        what stops "Invert" rendering as two lines, "Inve"/"rt"."""
        spec = spec_for(node["type"])
        need = 0.0
        for i, (text, size, _key) in enumerate(self._header_blocks(nid, node)):
            if self._header_block_is_id(nid, text):
                continue                      # ellipsized: never grown for
            reserve = self.HEADER_ICON_RESERVE if i == 0 else 20
            if i == 0 and spec.settings:
                reserve += 22
            left = self.NODE_ICON_LEFT_RESERVE if i == 0 else 0
            chrome = reserve + left + self.HEADER_SIDE_PAD
            words = [w for w in text.split() if w]
            if not words:
                continue
            widest = max(self._text_size(w, size)[0] for w in words)
            need = max(need, chrome + widest)
        return int(need) + 1

    def _draw_header(self, cr, pal, nid, node, x, y, color=None):
        """Draw every _header_blocks() line, stacked top to bottom by each
        block's own measured height.  A label or description wraps (so
        it's always fully readable); the node id is a long opaque token
        and is ellipsized to a single line instead.  node_height() already
        grew the node to fit this same stack (via _header_extra_height,
        which uses the identical per-block measurements) before this ever
        draws, so there's no clipping against the node's bottom edge or
        the control/ports area below it."""
        text_y = y + self.HEADER_TOP_PAD
        for i, (text, font_size, color_key) in enumerate(
            self._header_blocks(nid, node)
        ):
            left, max_width = self._header_line_layout(nid, node, i)
            text_rgb = color if color is not None else pal[color_key]
            if self._header_block_is_id(nid, text):
                draw_text_ellipsized(
                    cr, x + self.HEADER_SIDE_PAD + left, text_y, text,
                    max_width, font_size, text_rgb,
                )
                block_h = self._single_line_height(font_size)
            else:
                block_h = draw_text_wrapped(
                    cr, x + self.HEADER_SIDE_PAD + left, text_y, text,
                    max_width, font_size, text_rgb, widget=self,
                )
            text_y += block_h + self.HEADER_BLOCK_GAP

    @staticmethod
    def _anchor_icon_center(x, y, width):
        """Centre of the anchor badge - shared by drawing and hit-testing
        so they can't drift.  Sits to the left of the three-dot menu,
        vertically level with it."""
        return (x + width - 33, y + 18)

    def find_anchor_icon_at(self, x, y):
        for nid, node in self.nodes.items():
            if node.get("type") in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
                continue
            icx, icy = self._anchor_icon_center(
                node["x"], node["y"], self.node_width(nid)
            )
            # Ends at width - 42..-24, just clear of the three-dot hit
            # region which starts at width - 24.
            if icx - 9 <= x <= icx + 9 and icy - 12 <= y <= icy + 12:
                return nid
        return None

    def _draw_anchor_icon(self, cr, pal, x, y, width, anchored):
        """Little anchor glyph showing whether the node is pinned.  Bright
        theme accent when anchored, dimmed when it can drift."""
        icx, icy = self._anchor_icon_center(x, y, width)
        if anchored:
            color = pal["select"]
        else:
            color = pal["subtext"]
        cr.set_source_rgb(*color)
        cr.set_line_width(1.5)
        # Ring at the top.
        cr.arc(icx, icy - 5, 2.0, 0, 2 * math.pi)
        cr.stroke()
        # Shaft.
        cr.move_to(icx, icy - 3)
        cr.line_to(icx, icy + 5)
        cr.stroke()
        # Stock (crossbar).
        cr.move_to(icx - 4, icy - 1)
        cr.line_to(icx + 4, icy - 1)
        cr.stroke()
        # Flukes / curved arms at the bottom.
        cr.arc(icx, icy + 1, 4, 0.15 * math.pi, 0.85 * math.pi)
        cr.stroke()
        if anchored:
            # Filled ring reads as "locked" at a glance.
            cr.arc(icx, icy - 5, 1.0, 0, 2 * math.pi)
            cr.fill()

    def _draw_three_dots(self, cr, x, y, width):
        dot_x = x + width - 14
        start_y = y + 12
        for i in range(3):
            cr.arc(dot_x, start_y + i * 6, 2.2, 0, 2 * math.pi)
            cr.set_source_rgb(0.7, 0.7, 0.7)
            cr.fill()

    def _node_icon_pixbuf(self, icon_name, size, rgb):
        """Render a symbolic GTK icon to a GdkPixbuf tinted `rgb`, cached
        per (name, size, color).  GTK4 has no way to paint a symbolic
        icon onto a foreign cairo context directly, so the paintable is
        snapshotted and run through a Gsk.CairoRenderer to a texture,
        then decoded to a pixbuf (which cairo *can* draw).  Failures
        (no display, missing icon) cache None so we don't retry every
        frame."""
        key = (icon_name, size, rgb)
        cache = self._node_icon_cache
        if key in cache:
            return cache[key]
        pixbuf = None
        try:
            display = self.get_display() or Gdk.Display.get_default()
            if display is None:
                # Not realized yet - don't cache a miss that would stick.
                return None
            if icon_name:
                theme = Gtk.IconTheme.get_for_display(display)
                paintable = theme.lookup_icon(
                    icon_name, None, size, 1, Gtk.TextDirection.NONE,
                    Gtk.IconLookupFlags.FORCE_SYMBOLIC,
                )
                if paintable is not None:
                    color = Gdk.RGBA()
                    color.red, color.green, color.blue = rgb
                    color.alpha = 1.0
                    snapshot = Gtk.Snapshot.new()
                    paintable.snapshot_symbolic(snapshot, size, size, [color])
                    node = snapshot.to_node()
                    if node is not None:
                        renderer = Gsk.CairoRenderer.new()
                        renderer.realize(None)
                        try:
                            # NB: no viewport argument.  Passing an
                            # explicit None is rejected by PyGObject
                            # ("Argument 1 does not allow None as a
                            # value"), which made every icon lookup fail
                            # silently and fall back to the hand-drawn
                            # glyphs.
                            texture = renderer.render_texture(node)
                        finally:
                            renderer.unrealize()
                        if texture is not None:
                            png = texture.save_to_png_bytes()
                            loader = GdkPixbuf.PixbufLoader.new_with_type("png")
                            loader.write(png.get_data())
                            loader.close()
                            pixbuf = loader.get_pixbuf()
        except Exception:
            logger.debug("Could not render node icon %r", icon_name, exc_info=True)
        cache[key] = pixbuf
        return pixbuf

    @staticmethod
    def icon_placement(x, y, size, pixbuf):
        """(offset_x, offset_y, scale) that draws `pixbuf` at exactly `size`
        world units, centred.

        The scale is derived from the pixbuf we actually got: a symbolic icon
        comes back at its *natural* size (a 16px request can yield 14px), so
        assuming the request was met made the drawn size depend on the zoom -
        the icon shrank as you zoomed in."""
        px = max(1.0, float(pixbuf.get_width()))
        scale = size / px
        drawn_h = pixbuf.get_height() * scale
        return (x, y + (size - drawn_h) / 2.0, scale)

    #: Symbolic icons are rasterised at this many pixels regardless of the
    #: size they are drawn at.  GTK returns a *different* pixbuf size for
    #: different requests - and the icon's own padding is a different
    #: fraction of it each time (13px for a 20px request, 58px for 64px) - so
    #: rasterising per zoom made the drawn glyph change size as you zoomed.
    #: One fixed, generous raster is scaled to the target size instead; the
    #: cache then holds a single entry per (icon, colour).
    ICON_RASTER_PX = 96

    def _draw_node_icon(self, cr, icon_name, x, y, size, rgb):
        """Draw a symbolic icon at exactly `size` world units, so the icon is
        the same size (and the same fraction of the button it sits in) at
        every zoom."""
        pixbuf = self._node_icon_pixbuf(icon_name, self.ICON_RASTER_PX, rgb)
        if pixbuf is None:
            return
        ox, oy, scale = self.icon_placement(x, y, size, pixbuf)
        cr.save()
        cr.translate(ox, oy)
        cr.scale(scale, scale)
        Gdk.cairo_set_source_pixbuf(cr, pixbuf, 0, 0)
        cr.paint()
        cr.restore()

    @staticmethod
    def _settings_cog_center(x, y, width):
        """Centre of the green settings cog, immediately left of the
        node's anchor badge in the header's top-right corner."""
        return (x + width - 52, y + 18)

    def _draw_settings_gear(self, cr, pal, x, y, width):
        """Small green cog beside the node's anchor badge marking "this
        node's Settings menu has important extra controls" - and the
        click target that opens it (find_settings_gear_at).  Drawn as a
        solid disc with notches (a proper little gear), NOT a thin ring
        with radial spokes."""
        cx, cy = self._settings_cog_center(x, y, width)
        cr.save()
        green = (0.18, 0.76, 0.49)  # Adwaita success green (#2ec27e)
        cr.set_source_rgb(*green)
        cr.arc(cx, cy, 8, 0, 2 * math.pi)
        cr.fill()
        # Notch teeth out of the rim by punching node-bg-colored dots
        # around the circumference - reads as a cog without any spokes.
        cr.set_source_rgb(*pal["node_bg"])
        for k in range(8):
            a = k * math.pi / 4
            cr.arc(
                cx + math.cos(a) * 6.0, cy + math.sin(a) * 6.0, 2.4, 0, 2 * math.pi
            )
            cr.fill()
        cr.set_source_rgb(*green)
        cr.arc(cx, cy, 2.2, 0, 2 * math.pi)
        cr.fill()
        cr.restore()

    def _draw_lock_button(self, cr, x, y, w, h, locked):
        """Small padlock at the right of a volume row.  Filled amber when
        locked (Patch Space re-asserts its volume every tick), a dim outline
        when unlocked (the device's own volume is left alone)."""
        color = (0.88, 0.70, 0.30) if locked else (0.5, 0.5, 0.53)
        cx = x + w / 2.0
        cy = y + h / 2.0
        body_w = w * 0.62
        body_h = h * 0.50
        bx = cx - body_w / 2.0
        by = cy - body_h / 2.0 + h * 0.12
        cr.save()
        cr.set_line_width(1.6)
        cr.set_source_rgb(*color)
        radius = body_w * 0.34
        cr.arc(cx, by, radius, math.pi, 2 * math.pi)
        cr.stroke()
        cr.rectangle(bx, by, body_w, body_h)
        if locked:
            cr.fill()
        else:
            cr.stroke()
        cr.restore()

    def _draw_force_default_button(self, cr, x, y, w, h, active):
        """Small "cage/bars" glyph to the left of the lock on a Speaker/Mic
        Line: the built-in device is *locked in* as the system default.
        Amber while the daemon keeps forcing it; dim once turned off."""
        color = (0.88, 0.70, 0.30) if active else (0.5, 0.5, 0.53)
        cr.save()
        cr.set_line_width(1.5)
        cr.set_source_rgb(*color)
        left, right = x + w * 0.20, x + w * 0.80
        top, bottom = y + h * 0.18, y + h * 0.82
        # Cage frame.
        cr.rectangle(left, top, right - left, bottom - top)
        cr.stroke()
        # Bars, poking slightly past the frame like a jail cell.
        for fx in (0.35, 0.5, 0.65):
            cr.move_to(x + w * fx, y + h * 0.10)
            cr.line_to(x + w * fx, y + h * 0.90)
            cr.stroke()
        cr.restore()

    #: A stored color may be a hex, or one of the theme's own slots written
    #: as `@blue` … `@teal`.  A slot is resolved *every time it is drawn*, so
    #: a panel colored `@blue` follows the desktop palette (stylix recolors
    #: those slots) instead of freezing today's hex - which is what the
    #: pickers store, and what the module's `color` defaults to.
    COLOR_SLOTS = {
        "blue": "blue_3",
        "green": "green_3",
        "yellow": "yellow_3",
        "red": "red_3",
        "purple": "purple_3",
        "teal": "teal_3",
    }
    DEFAULT_PANEL_COLOR = "@blue"
    #: What files written before the slots existed carried for "no color" -
    #: Adwaita's blue, i.e. what `@blue` means.  Read as `@blue`.
    LEGACY_DEFAULT_PANEL_COLOR = "#3584e4"

    def _slot_rgb(self, slot, fallback):
        """A `@slot` value as RGB, from the current theme's palette."""
        name = self.COLOR_SLOTS.get(slot)
        if name is None:
            return fallback
        return theme_color(self, name, fallback)

    def resolve_color(self, value, default_key):
        """A stored color value as RGB: a theme slot (resolved now), a hex
        used as-is, or - for nothing/the legacy default - a color derived
        from `default_key`, so uncolored things still get a palette color
        that varies between them and stays stable across restarts."""
        raw = (value or "").strip()
        if not raw or raw.lower() == self.LEGACY_DEFAULT_PANEL_COLOR:
            # Nothing stored, or what files written before the slots carried
            # for "the default": both mean the default slot, not "unset" -
            # a panel with no color of its own should be the same blue the
            # module declares, and keep following the theme.
            raw = self.DEFAULT_PANEL_COLOR
        if raw.startswith("@"):
            return self._slot_rgb(
                raw[1:].lower(), theme_class_color(self, default_key)
            )
        return self._hex_to_rgb(raw)

    def _panel_rgb(self, pid):
        """A panel's color as RGB - see `resolve_color`."""
        panel = self.panels.get(pid) or {}
        key = panel.get("stem") or panel.get("label") or pid
        return self.resolve_color(panel.get("color"), key)

    def _group_rgb(self, gid, group):
        """A group's color as RGB - see `resolve_color`."""
        key = group.get("label") or gid or "group"
        return self.resolve_color(group.get("color"), key)

    #: Fallbacks for the color pickers' presets: Adwaita's defaults for the
    #: named slots used below, for a theme that defines no named colors.
    _GROUP_COLOR_FALLBACKS = (
        (0.208, 0.518, 0.894),   # blue_3
        (0.200, 0.824, 0.478),   # green_3
        (0.965, 0.827, 0.176),   # yellow_3
        (0.878, 0.106, 0.141),   # red_3
        (0.569, 0.255, 0.675),   # purple_3
        (0.180, 0.761, 0.494),   # teal_3
    )

    def _group_colors(self):
        """The color pickers' presets, taken from the theme's own palette
        slots (Adwaita's, recolored by stylix), so a color picked here
        matches the rest of the desktop instead of being six fixed literals."""
        out = []
        for slot, name in self.COLOR_SLOTS.items():
            r, g, b = self._slot_rgb(slot, self._GROUP_COLOR_FALLBACKS[
                tuple(self.COLOR_SLOTS).index(slot)
            ])
            out.append((
                "@" + slot,
                (r, g, b),
            ))
        return tuple(out)

    @staticmethod
    def _draw_slider_bar(cr, x, y, w, h, color):
        """One slider bar - the groove or the filled part of it: still a
        rectangle, with slightly rounded ends (clamped to the bar's own
        height and length so a nearly-empty fill stays a bar, not a blob)."""
        if w <= 0:
            return
        radius = min(h / 2.0, 2.0, w / 2.0)
        cr.set_source_rgb(*color)
        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.fill()

    def _draw_wetdry_slider(self, cr, pal, x, y, node_h, mix):
        """Reverb's dry/wet mix as an inline slider on the node body
        (0 = fully dry, 1 = fully wet), styled like the volume slider
        so the same drag gesture drives it (see on_drag_begin/update/
        end's "wetdry" handling)."""
        slider_y = y + node_h - self.SLIDER_HEIGHT - 5
        slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
        slider_x = x + self.SLIDER_MARGIN

        cr.set_font_size(8)
        cr.set_source_rgb(*pal["subtext"])
        cr.move_to(slider_x, slider_y - 3)
        cr.show_text("dry/wet")

        self._draw_slider_bar(
            cr, slider_x, slider_y, slider_width, 4, pal["slider_track"]
        )
        self._draw_slider_bar(
            cr, slider_x, slider_y, slider_width * mix, 4, pal["accent"]
        )

        handle_x = slider_x + slider_width * mix
        cr.arc(handle_x, slider_y + 2, 6, 0, 2 * math.pi)
        cr.set_source_rgb(*pal["text"])
        cr.fill()

        cr.set_font_size(9)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(x + self.NODE_WIDTH - 34, slider_y - 4)
        cr.show_text(f"{int(round(mix * 100))}% wet")

    def _draw_threshold_slider(self, cr, pal, x, y, node_h, frac):
        """Sensitivity Gate's sensitivity bar (Discord-style voice
        activity): `frac` is the 0..1 slider position. The value drives
        the hidden pre/post gain-staging nodes daemon-side - see
        _apply_sensitivity_slider. A live incoming-level indicator
        layered onto the same bar is a later, cosmetic addition (see
        SensitivityGateNode's docstring)."""
        slider_y = y + node_h - self.SLIDER_HEIGHT - 5
        slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
        slider_x = x + self.SLIDER_MARGIN

        cr.set_font_size(8)
        cr.set_source_rgb(*pal["subtext"])
        cr.move_to(slider_x, slider_y - 3)
        cr.show_text("sensitivity")

        self._draw_slider_bar(
            cr, slider_x, slider_y, slider_width, 4, pal["slider_track"]
        )
        self._draw_slider_bar(
            cr, slider_x, slider_y, slider_width * frac, 4, pal["accent"]
        )

        handle_x = slider_x + slider_width * frac
        cr.arc(handle_x, slider_y + 2, 6, 0, 2 * math.pi)
        cr.set_source_rgb(*pal["text"])
        cr.fill()

        cr.set_font_size(9)
        cr.set_source_rgb(*pal["subtext"])
        cr.move_to(x + self.NODE_WIDTH - 30, slider_y - 4)
        cr.show_text(f"{int(round(frac * 100))}%")

    def _draw_volume_slider(self, cr, pal, x, y, node_h, volume):
        slider_y = y + node_h - self.SLIDER_HEIGHT - 5
        slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
        slider_x = x + self.SLIDER_MARGIN

        self._draw_slider_bar(
            cr, slider_x, slider_y, slider_width, 4, pal["slider_track"]
        )
        self._draw_slider_bar(
            cr, slider_x, slider_y, slider_width * volume, 4, pal["accent"]
        )

        handle_x = slider_x + slider_width * volume
        cr.arc(handle_x, slider_y + 2, 6, 0, 2 * math.pi)
        cr.set_source_rgb(*pal["text"])
        cr.fill()

        cr.set_font_size(9)
        cr.set_source_rgb(*pal["subtext"])
        cr.move_to(x + self.NODE_WIDTH - 30, slider_y - 2)
        cr.show_text(f"{int(volume * 100)}%")

    def _draw_check_row(self, cr, x, y, node_h, checked, label_text):
        cb_x, cb_y, size = x + 10, y + node_h - 22, 14
        cr.rectangle(cb_x, cb_y, size, size)
        cr.set_source_rgb(0.3, 0.3, 0.3)
        cr.fill_preserve()
        cr.set_source_rgb(0.7, 0.7, 0.7)
        cr.set_line_width(1)
        cr.stroke()
        if checked:
            cr.move_to(cb_x + 2, cb_y + size / 2)
            cr.line_to(cb_x + size / 2, cb_y + size - 2)
            cr.line_to(cb_x + size - 2, cb_y + 2)
            cr.set_source_rgb(0.4, 0.9, 0.4)
            cr.set_line_width(2)
            cr.stroke()
        cr.set_font_size(10)
        cr.set_source_rgb(0.8, 0.8, 0.8)
        cr.move_to(cb_x + size + 6, cb_y + size - 2)
        cr.show_text(label_text)

    def _draw_gate_toggle(self, cr, nid, enabled):
        """Big centered rounded-rect toggle for gate nodes - one large,
        obvious click target instead of a small checkbox, since a gate
        is the single thing that node does."""
        x, y, w, h = self._gate_rect(nid)
        radius = 10

        fill = (0.30, 0.72, 0.42) if enabled else (0.30, 0.30, 0.33)
        border = (0.20, 0.46, 0.28) if enabled else (0.46, 0.46, 0.49)
        text_color = (0.06, 0.16, 0.09) if enabled else (0.78, 0.78, 0.80)

        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.set_source_rgb(*fill)
        cr.fill_preserve()
        cr.set_source_rgb(*border)
        cr.set_line_width(1.5)
        cr.stroke()

        label = "OPEN" if enabled else "CLOSED"
        cr.select_font_face("sans")
        cr.set_font_size(12)
        extents = cr.text_extents(label)
        text_x = x + (w - extents.width) / 2 - extents.x_bearing
        text_y = y + (h - extents.height) / 2 - extents.y_bearing
        cr.set_source_rgb(*text_color)
        cr.move_to(text_x, text_y)
        cr.show_text(label)

    def _draw_filter_mode_button(self, cr, pal, nid, exclude):
        """The Filter node's Include/Exclude switch - the gate toggle's big
        rounded rect, captioned with the mode it is in.  Include (the
        default, the node keeps what matches) fills with the theme's success
        color; Exclude (it drops them instead) with the theme's error color -
        the same pair the On/Off button uses for its two states."""
        x, y, w, h = self._gate_rect(nid)
        radius = 10

        face = pal["error"] if exclude else pal["success"]
        fill = face
        border = tuple(c * 0.65 for c in face)
        text_color = (0.06, 0.06, 0.08)

        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.set_source_rgb(*fill)
        cr.fill_preserve()
        cr.set_source_rgb(*border)
        cr.set_line_width(1.5)
        cr.stroke()

        label = "EXCLUDE" if exclude else "INCLUDE"
        cr.select_font_face("sans")
        cr.set_font_size(12)
        extents = cr.text_extents(label)
        text_x = x + (w - extents.width) / 2 - extents.x_bearing
        text_y = y + (h - extents.height) / 2 - extents.y_bearing
        cr.set_source_rgb(*text_color)
        cr.move_to(text_x, text_y)
        cr.show_text(label)

    def _draw_switcher_toggle(self, cr, nid, output):
        """Two-segment A/B button for a Switcher node.  The selected
        output is filled green, the inactive one grey, so a glance at
        the node shows which of its two outputs is carrying audio."""
        x, y, w, h = self._gate_rect(nid)
        radius = 10
        half = w / 2

        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.set_source_rgb(0.20, 0.20, 0.22)
        cr.fill_preserve()
        cr.set_source_rgb(0.46, 0.46, 0.49)
        cr.set_line_width(1.5)
        cr.stroke()

        active = 1 if output else 0
        cr.select_font_face("sans")
        cr.set_font_size(12)
        for i, label in enumerate(("A", "B")):
            seg_x = x + i * half
            if i == active:
                draw_rounded_rect(cr, seg_x + 2, y + 2, half - 4, h - 4, radius - 2)
                cr.set_source_rgb(0.30, 0.72, 0.42)
                cr.fill()
            extents = cr.text_extents(label)
            text_x = seg_x + (half - extents.width) / 2 - extents.x_bearing
            text_y = y + (h - extents.height) / 2 - extents.y_bearing
            if i == active:
                cr.set_source_rgb(0.06, 0.16, 0.09)
            else:
                cr.set_source_rgb(0.78, 0.78, 0.80)
            cr.move_to(text_x, text_y)
            cr.show_text(label)

    def _draw_boolean_toggle(self, cr, pal, nid, output, driven=False):
        """On/Off button for the boolean source node - the A/B button's
        shape with On/Off labels.  The active segment is filled with the
        GTK theme's success color when On and its error color when Off,
        so a glance shows the value being broadcast to any gate/switcher
        wired to this node.

        ``driven`` renders the same button read-only and white: it is a
        gate/switcher whose ctrl input is wired, so the value shown is
        the one being driven into it (not something the user can click)."""
        x, y, w, h = self._gate_rect(nid)
        radius = 10
        half = w / 2

        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.set_source_rgb(0.20, 0.20, 0.22)
        cr.fill_preserve()
        if driven:
            cr.set_source_rgb(0.90, 0.90, 0.93)
        else:
            cr.set_source_rgb(0.46, 0.46, 0.49)
        cr.set_line_width(1.5)
        cr.stroke()

        # On = left/true (theme success green), Off = right/false (theme
        # error red).  A driven switch stays white - see docstring.
        active = 0 if output else 1
        if driven:
            active_color = (0.92, 0.92, 0.95)
        else:
            active_color = pal["success"] if active == 0 else pal["error"]
        cr.select_font_face("sans")
        cr.set_font_size(11)
        for i, label in enumerate(("On", "Off")):
            seg_x = x + i * half
            if i == active:
                draw_rounded_rect(cr, seg_x + 2, y + 2, half - 4, h - 4, radius - 2)
                cr.set_source_rgb(*active_color)
                cr.fill()
            extents = cr.text_extents(label)
            text_x = seg_x + (half - extents.width) / 2 - extents.x_bearing
            text_y = y + (h - extents.height) / 2 - extents.y_bearing
            if i == active:
                if driven:
                    cr.set_source_rgb(0.10, 0.10, 0.12)
                elif active == 0:
                    # Dark text reads on the success green.
                    cr.set_source_rgb(0.06, 0.16, 0.09)
                else:
                    # Light text reads on the error red.
                    cr.set_source_rgb(0.98, 0.96, 0.96)
            else:
                cr.set_source_rgb(0.78, 0.78, 0.80)
            cr.move_to(text_x, text_y)
            cr.show_text(label)

    def _draw_mute_checkbox(self, cr, x, y, node_h, volume):
        self._draw_check_row(cr, x, y, node_h, volume > 0.5, "Pass audio")

    def _draw_text_field(self, cr, pal, nid, value):
        field_x, field_y, field_w, field_h = self._field_rect(nid)

        draw_rounded_rect(cr, field_x, field_y, field_w, field_h, 4)
        cr.set_source_rgb(*pal["field_bg"])
        cr.fill_preserve()
        cr.set_source_rgb(*pal["node_border"])
        cr.set_line_width(1)
        cr.stroke()

        text = value if value else "(click to set)"
        color = pal["field_fg"] if value else pal["subtext"]
        font_size = 10

        # A field whose value is chosen from a list (the Title/Application
        # classifiers) gives up its right end to a caret, so the box reads as
        # a dropdown rather than as something to type into.
        choices = spec_for(self.nodes[nid]["type"]).field_choices
        caret_room = 18 if choices else 0

        baseline_y = field_y + (field_h - font_size) // 2
        draw_text_ellipsized(
            cr,
            field_x + 6,
            baseline_y,
            text,
            field_w - 12 - caret_room,
            font_size,
            color,
        )
        if choices:
            self._draw_field_caret(cr, pal, field_x, field_y, field_w, field_h)

    def _draw_path_picker(self, cr, pal, nid):
        """The folder button a `picker` spec draws beside its field: a
        small outlined square with a folder glyph, in the field row.  It
        opens the desktop's file chooser (portal_file_dialog.open_file),
        which is the "pick a file" UI the desktop's own file manager
        provides - the button is a short-cut to it, exactly like the
        Import action's."""
        x, y, w, h = self._path_picker_rect(nid)
        draw_rounded_rect(cr, x, y, w, h, 4)
        cr.set_source_rgb(*pal["field_bg"])
        cr.fill_preserve()
        cr.set_source_rgb(*pal["node_border"])
        cr.set_line_width(1)
        cr.stroke()
        self._draw_node_icon(
            cr, "folder-open-symbolic", x, y, self.PICKER_SIZE, pal["subtext"]
        )

    def _draw_toggle_row(self, cr, pal, nid, node):
        """The node-body switch declared by ``spec.toggle`` - an
        (attr, label) the user flips in place (the Sound Effect's Stack
        behaviour).  Drawn as the gate toggle's two-segment on/off switch,
        sized down to sit inline above the path field with its caption to
        the left: the active segment is filled (theme success green for
        On, a neutral grey for Off - "off" here is a choice, not a fault),
        the inactive one stays recessed.  Geometry comes from
        _toggle_switch_rect, which the hit-test shares."""
        attr, caption = spec_for(node["type"]).toggle
        on = bool(node.get(attr, False))
        x, y, w, h = self._toggle_switch_rect(nid)
        radius = h / 2.0
        half = w / 2.0

        cr.select_font_face("sans")
        cr.set_font_size(10)
        cr.set_source_rgb(*pal["subtext"])
        extents = cr.text_extents(caption)
        cr.move_to(node["x"] + self.FIELD_MARGIN,
                   y + (h - extents.height) / 2 - extents.y_bearing)
        cr.show_text(caption)

        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.set_source_rgb(0.20, 0.20, 0.22)
        cr.fill_preserve()
        cr.set_source_rgb(0.46, 0.46, 0.49)
        cr.set_line_width(1.0)
        cr.stroke()

        active = 0 if on else 1
        active_color = pal["success"] if on else (0.42, 0.42, 0.45)
        cr.set_font_size(9)
        for i, label in enumerate(("On", "Off")):
            seg_x = x + i * half
            if i == active:
                draw_rounded_rect(cr, seg_x + 1.5, y + 1.5, half - 3, h - 3,
                                  radius - 1.5)
                cr.set_source_rgb(*active_color)
                cr.fill()
            extents = cr.text_extents(label)
            text_x = seg_x + (half - extents.width) / 2 - extents.x_bearing
            text_y = y + (h - extents.height) / 2 - extents.y_bearing
            if i == active and on:
                # Dark text reads on the success green.
                cr.set_source_rgb(0.06, 0.16, 0.09)
            elif i == active:
                cr.set_source_rgb(0.88, 0.88, 0.90)
            else:
                cr.set_source_rgb(0.62, 0.62, 0.65)
            cr.move_to(text_x, text_y)
            cr.show_text(label)

    def _draw_play_indicator(self, cr, pal, nid, node):
        """Live "is it making sound" read-out for a node whose spec
        declares an indicator: a dot at the bottom-right of the node body
        and the number of running streams to its left.  Green while
        anything is playing, dim otherwise - a momentary trigger has no
        other way to show it happened."""
        dot_x, dot_y, dot_r, text_right = self._play_indicator_rect(nid)
        count = int(node.get("playing", 0) or 0)
        playing = count > 0
        color = pal["success"] if playing else (0.42, 0.42, 0.46)

        cr.select_font_face("sans")
        cr.set_font_size(10)
        text = str(count)
        extents = cr.text_extents(text)
        cr.set_source_rgb(*(pal["success"] if playing else (0.55, 0.55, 0.58)))
        cr.move_to(text_right - extents.width, dot_y - extents.height / 2
                   - extents.y_bearing)
        cr.show_text(text)

        cr.arc(dot_x, dot_y, dot_r, 0, 2 * math.pi)
        cr.set_source_rgb(*color)
        cr.fill()

    def _impulse_label(self, node):
        """What a Button node's face says: the label the user gave it, or
        "Trigger" - the node *type* is called "Button", which describes the
        kind of node rather than what pressing it does."""
        label = (node.get("label") or "").strip()
        if not label or label == spec_for(node["type"]).label:
            return "Trigger"
        return label

    def _draw_impulse_button(self, cr, nid, node):
        """The Button node's clickable face: the gate toggle's big
        rounded rect, grey while idle - one step lighter while the pointer is
        over it (see hover_impulse) - and pulsing to the success green for
        IMPULSE_FLASH_MS after a press (see _impulse_flash).  Captioned with
        _impulse_label: "Trigger" unless the node has been renamed."""
        x, y, w, h = self._gate_rect(nid)
        radius = 10
        t = self._impulse_flash_progress(nid)

        idle_fill = (0.30, 0.30, 0.33)
        idle_border = (0.46, 0.46, 0.49)
        if self.hover_impulse == nid:
            # Pointer-over cue: the same face, one step lighter, so the button
            # reads as pressable before it is pressed (the press pulse below
            # is what turns it green).
            idle_fill = (0.41, 0.41, 0.45)
            idle_border = (0.60, 0.60, 0.64)
        # Ease the green in and back out so the press reads as a pulse
        # rather than a one-frame blink (the ticker repaints while it runs).
        glow = 1.0 - abs(2.0 * t - 1.0) if t is not None else 0.0
        hot_fill = (0.30, 0.72, 0.42)
        hot_border = (0.20, 0.46, 0.28)
        fill = tuple(
            idle + (hot - idle) * glow for idle, hot in zip(idle_fill, hot_fill)
        )
        border = tuple(
            idle + (hot - idle) * glow
            for idle, hot in zip(idle_border, hot_border)
        )

        draw_rounded_rect(cr, x, y, w, h, radius)
        cr.set_source_rgb(*fill)
        cr.fill_preserve()
        cr.set_source_rgb(*border)
        cr.set_line_width(1.5)
        cr.stroke()

        label_text = self._impulse_label(node)
        cr.select_font_face("sans")
        cr.set_font_size(12)
        extents = cr.text_extents(label_text)
        text_x = x + (w - extents.width) / 2 - extents.x_bearing
        text_y = y + (h - extents.height) / 2 - extents.y_bearing
        if glow > 0.5:
            cr.set_source_rgb(0.06, 0.16, 0.09)
        elif self.hover_impulse == nid:
            cr.set_source_rgb(0.94, 0.94, 0.97)
        else:
            cr.set_source_rgb(0.78, 0.78, 0.80)
        cr.move_to(text_x, text_y)
        cr.show_text(label_text)

    def _draw_device_row(self, cr, pal, nid, node, row_index, row_kind):
        row_x, row_y, row_w, row_h = self._device_row_rect(nid, row_index)

        if row_kind == "volume":
            (sx, sy, sw, sh), force_rect, (lx, ly, lw, lh) = (
                self._volume_row_rects(nid)
            )
            if force_rect is not None:
                fx, fy, fw, fh = force_rect
                self._draw_force_default_button(
                    cr, fx, fy, fw, fh, bool(node.get("force_default", True))
                )
            volume = node.get("device_volume", 1.0)
            self._draw_slider_bar(
                cr, sx, sy + sh / 2 - 2, sw, 4, pal["slider_track"]
            )
            self._draw_slider_bar(
                cr, sx, sy + sh / 2 - 2, sw * volume, 4, pal["accent"]
            )
            handle_x = sx + sw * volume
            cr.arc(handle_x, sy + sh / 2, 6, 0, 2 * math.pi)
            cr.set_source_rgb(*pal["text"])
            cr.fill()
            self._draw_lock_button(
                cr, lx, ly, lw, lh, bool(node.get("volume_locked", True))
            )
            return

        if row_kind == "select":
            text = node.get("selection_label") or "(click to select)"
            placeholder = not (node.get("device_name") or node.get("app_name"))
        else:  # codec
            text = node.get("codec_label") or "(click to select codec)"
            placeholder = not node.get("codec_label")

        draw_rounded_rect(cr, row_x, row_y, row_w, row_h, 4)
        cr.set_source_rgb(*pal["field_bg"])
        cr.fill_preserve()
        cr.set_source_rgb(*pal["node_border"])
        cr.set_line_width(1)
        cr.stroke()
        color = pal["subtext"] if placeholder else pal["field_fg"]
        draw_text_ellipsized(
            cr, row_x + 6, row_y + (row_h - 10) // 2, text, row_w - 12, 10, color
        )

    # ---------- pointer / click handling ----------

    def on_motion(self, controller, x, y):
        self.track_pointer(x, y)
        wx, wy = self.to_world(x, y)

        # The Button node lights its face while the pointer is on it.  Set
        # before the branches below so the cue can't get stuck on through a
        # drag, and repaint only on the edge.
        hover = self.find_impulse_button_at(wx, wy)
        if hover != self.hover_impulse:
            self.hover_impulse = hover
            self.queue_draw()

        if self.connecting_from:
            self.drag_current_xy = (wx, wy)
            socket_hit = self.find_socket_at(wx, wy)
            self.hover_target_node = (
                socket_hit[0] if (socket_hit and socket_hit[1] == "in") else None
            )
            self.queue_draw()
            return

        if self.slider_dragging is not None:
            # Actual volume updates while dragging are driven by
            # on_drag_update() - see on_drag_begin for why this can't
            # be a click handler. Skip hover recompute mid-drag so the
            # cursor doesn't flicker.
            return

        if (
            self.find_slider_at(wx, wy) is not None
            or self.find_wetdry_slider_at(wx, wy) is not None
            or self.find_sensitivity_slider_at(wx, wy) is not None
            or self.find_gain_slider_at(wx, wy) is not None
            or self.find_device_volume_slider_at(wx, wy) is not None
        ):
            self.set_cursor(Gdk.Cursor.new_from_name("ew-resize", None))
        elif (
            self.find_field_at(wx, wy) is not None
            or self.find_volume_lock_at(wx, wy) is not None
            or self.find_mute_checkbox_at(wx, wy) is not None
            or self.find_gate_toggle_at(wx, wy) is not None
            or self.find_filter_mode_at(wx, wy) is not None
            or self.find_switcher_toggle_at(wx, wy) is not None
            or self.find_boolean_toggle_at(wx, wy) is not None
            or self.find_fallback_toggle_at(wx, wy) is not None
            or self.find_impulse_button_at(wx, wy) is not None
            or self.find_toggle_switch_at(wx, wy) is not None
            or self.find_path_picker_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
            or self.find_anchor_icon_at(wx, wy) is not None
            or self.find_settings_gear_at(wx, wy) is not None
            or self.find_group_label_at(wx, wy) is not None
            or self.find_group_action_at(wx, wy) is not None
            or self.find_group_menu_at(wx, wy) is not None
        ):
            self.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        else:
            self.set_cursor(None)

    def on_leave(self, controller):
        self.set_cursor(None)
        if self.hover_impulse is not None:
            self.hover_impulse = None
            self.queue_draw()

    def on_click(self, gesture, n_press, x, y):
        if n_press != 1:
            return
        self.grab_focus()
        self.dismiss_context_popover()
        wx, wy = self.to_world(x, y)

        # Panel reset button (read-only panels) - re-apply the file state.
        pid = self.find_panel_reset_at(wx, wy)
        if pid is not None:
            self._begin_load()
            self.client.send({"command": "reset_panel", "panel_id": pid})
            return
        # Panel anchor toggle.
        pid = self.find_panel_anchor_at(wx, wy)
        if pid is not None:
            panel = self.panels[pid]
            panel["anchored"] = not panel.get("anchored", False)
            self._mark_panel_moved(pid)
            # Un-pinning releases the box to the panel pass; if the layout
            # had settled (or hit its safety valve) nothing would move
            # without re-arming it here - the same thing toggle_node_anchor
            # does for a node.
            self.layout_awake = True
            self._settle_ticks = 0
            self.client.send(
                {"command": "set_panel_layout", "panel_id": pid,
                 "anchored": panel["anchored"]}
            )
            self.queue_draw()
            return

        # Remove this placement (X; keeps the panel file).
        pid = self.find_panel_close_at(wx, wy)
        if pid is not None:
            # Drop the placement locally so it disappears immediately, then
            # tell the daemon.  This is a *light* removal (no panel reload),
            # so don't raise the bulk-load overlay - that overlay waits on
            # node readiness and would hang around (and re-hide the graph)
            # for a poll or the load timeout.
            prefix = pid + "::"
            for nid in [
                n for n in list(self.nodes)
                if n == pid or n.startswith(prefix)
            ]:
                del self.nodes[nid]
            for eid, edge in list(self.edges.items()):
                if (
                    edge["from_node"] not in self.nodes
                    or edge["to_node"] not in self.nodes
                ):
                    del self.edges[eid]
            self.panels.pop(pid, None)
            self._panel_geo_cache.clear()
            self.client.send(
                {"command": "remove_panel_placement", "panel_id": pid}
            )
            self.queue_draw()
            return

        # Panel edit-mode toggle (refresh from file, then persist edits).
        pid = self.find_panel_edit_at(wx, wy)
        if pid is not None:
            panel = self.panels.get(pid, {})
            new_state = not bool(panel.get("edit_mode"))
            panel["edit_mode"] = new_state
            self._panel_geo_cache.clear()
            self._begin_load()
            self.client.send(
                {
                    "command": "set_panel_edit_mode",
                    "panel_id": pid,
                    "enabled": new_state,
                }
            )
            self.queue_draw()
            return

        # Panel hamburger menu.
        pid = self.find_panel_menu_at(wx, wy)
        if pid is not None:
            self._show_panel_menu(pid, x, y)
            return

        # Panel IO "+" (add an input/output port).
        hit = self.find_panel_io_plus_at(wx, wy)
        if hit is not None:
            self._prompt_add_port(hit[0], hit[1])
            return

        # Group settings hamburger (rightmost in the group row).
        gid = self.find_group_menu_at(wx, wy)
        if gid is not None:
            self.show_group_settings_dialog(gid)
            return

        # Group +/- buttons: arm a mode (click again to cancel).
        hit = self.find_group_action_at(wx, wy)
        if hit is not None:
            self._group_pick_mode = None if self._group_pick_mode == hit else hit
            self.queue_draw()
            return

        # A +/- mode is armed: the next node click adjusts membership.
        if self._group_pick_mode is not None:
            gid, action = self._group_pick_mode
            nid = self.find_node_at(wx, wy)
            if nid is not None:
                if action == "add":
                    self._add_node_to_group(gid, nid)
                else:
                    self._remove_node_from_group(gid, nid)
            self._group_pick_mode = None
            self.queue_draw()
            return

        gid = self.find_group_label_at(wx, wy)
        if gid is not None:
            self.show_group_settings_dialog(gid)
            return

        nid = self.find_settings_gear_at(wx, wy)
        if nid is not None:
            self.show_settings_dialog(nid)
            return

        nid = self.find_three_dots_at(wx, wy)
        if nid is not None:
            self.show_node_menu(nid, x, y)
            return

        nid = self.find_anchor_icon_at(wx, wy)
        if nid is not None:
            self.toggle_node_anchor(nid)
            return

        nid = self.find_force_default_at(wx, wy)
        if nid is not None:
            enabled = not bool(self.nodes[nid].get("force_default", True))
            self.nodes[nid]["force_default"] = enabled
            self._send_property(nid, "force_default", enabled)
            self.queue_draw()
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            return

        nid = self.find_volume_lock_at(wx, wy)
        if nid is not None:
            node = self.nodes[nid]
            locked = not node.get("volume_locked", True)
            node["volume_locked"] = locked
            self._send_property(nid, "volume_locked", locked)
            self.queue_draw()
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            return

        hit = self.find_device_row_at(wx, wy)
        if hit is not None:
            nid, row_kind = hit
            if row_kind == "select":
                self._open_device_select(nid, x, y)
                return
            elif row_kind == "codec":
                self._open_codec_select(nid, x, y)
                return
            # "volume" is a drag gesture, handled in on_drag_begin -
            # same reasoning as the existing process-node slider.

        nid = self.find_gate_toggle_at(wx, wy)
        if nid is not None:
            self._toggle_gate_state(nid)
            self.queue_draw()
            return

        nid = self.find_filter_mode_at(wx, wy)
        if nid is not None:
            self._toggle_filter_mode(nid)
            self.queue_draw()
            return

        nid = self.find_switcher_toggle_at(wx, wy)
        if nid is not None:
            self._toggle_switch_state(nid)
            self.queue_draw()
            return

        nid = self.find_boolean_toggle_at(wx, wy)
        if nid is not None:
            self._toggle_switch_state(nid)
            self.queue_draw()
            return

        nid = self.find_fallback_toggle_at(wx, wy)
        if nid is not None:
            if self.nodes[nid]["type"] == "gate":
                self._toggle_gate_state(nid)
            else:
                self._toggle_switch_state(nid)
            self.queue_draw()
            return

        nid = self.find_impulse_button_at(wx, wy)
        if nid is not None:
            # Momentary: fire the pulse and start the local press flash.
            # Nothing is stored optimistically - a button has no state to
            # echo - but the nodes it drives re-report their play count,
            # so refresh once the daemon has had time to react.
            self._impulse_flash[nid] = time.monotonic()
            self._send_impulse(nid)
            self.queue_draw()
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            return

        nid = self.find_toggle_switch_at(wx, wy)
        if nid is not None:
            attr = spec_for(self.nodes[nid]["type"]).toggle[0]
            node = self.nodes[nid]
            value = not bool(node.get(attr, False))
            node[attr] = value
            # Same optimistic-echo guard as the other boolean switches: a
            # poll that raced our in-flight command would otherwise flip
            # the switch back for a beat (see _accept_bool_echo).
            self._pending_bool[(nid, attr)] = value
            self._send_property(nid, attr, value)
            self.queue_draw()
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            return

        nid = self.find_mute_checkbox_at(wx, wy)
        if nid is not None:
            node = self.nodes[nid]
            node["volume"] = 0.0 if node["volume"] > 0.5 else 1.0
            self._send_set_volume(nid, node["volume"])
            self.queue_draw()
            return

        # The volume slider is intentionally NOT started here - it's a
        # drag gesture, not a click, so it's started in
        # on_drag_begin() instead (see the comment there).

        nid = self.find_path_picker_at(wx, wy)
        if nid is not None:
            self._open_path_picker(nid)
            return

        nid = self.find_field_at(wx, wy)
        if nid is not None:
            self.show_field_edit(nid, x, y)
            return

        # Clicking anywhere else on the node body does nothing special
        # - the drag gesture owns plain node clicks so nodes stay
        # draggable. Renaming lives behind the three-dot menu's
        # Settings entry instead of popping up on every click.

    def show_field_edit(self, node_id, screen_x, screen_y):
        node = self.nodes.get(node_id)
        if not node:
            return
        field = spec_for(node["type"]).field
        if not field:
            return

        if field == "title":
            # Titles come from the live graph, so ask the daemon for them and
            # open the dropdown when the answer lands (see on_titles).
            self._pending_title_select = (node_id, screen_x, screen_y)
            self.client.send({"command": "get_titles"})
            return

        if field == "app_name":
            # Same for the application names (see on_applications).
            self._pending_app_name_select = (node_id, screen_x, screen_y)
            self.client.send({"command": "get_applications"})
            return

        if field == "media_class":
            # Media class is a fixed set of PipeWire media.class
            # strings, not free text - show the same kind of
            # human-readable choice popover used for device/app/codec
            # selection (see _show_choice_popover) instead of an Entry
            # the user could type an invalid raw class string into.
            # Which choices make sense depends on direction (source vs
            # target - see node_specs.media_class_choices_for).
            def on_pick(value):
                self._send_property(node_id, field, value)
                GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

            self._show_choice_popover(
                screen_x,
                screen_y,
                "Media Class:",
                media_class_choices_for(node["type"]),
                on_pick,
            )
            return

        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        entry = Gtk.Entry()
        entry.set_text(node.get("meta", {}).get(field, "") or "")
        entry.set_width_chars(24)
        entry.set_hexpand(True)

        def apply_and_close(*_args):
            self._send_property(node_id, field, entry.get_text())
            popover.popdown()
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        entry.connect("activate", apply_and_close)
        box.append(entry)

        apply_btn = Gtk.Button(label="Apply")
        apply_btn.connect("clicked", apply_and_close)
        box.append(apply_btn)

        popover.set_child(box)
        # Focus is grabbed by popup_context_menu once the popover has
        # actually mapped - doing it here (before it is shown) leaves the
        # entry unfocused and hard to click into.
        self.popup_context_menu(popover, screen_x, screen_y, focus_widget=entry)

    def _show_choice_popover(self, screen_x, screen_y, title, choices, on_pick,
                             search_hint=None):
        """`choices` is a list of (label, value) tuples. Shared by
        device/app/codec/title selection so there's exactly one place that
        builds this kind of list, instead of four near-identical popovers
        that can drift apart.

        `search_hint` turns the list into a searchable dropdown: a search
        entry filters the rows (scrolled, since a live title list is long) as
        you type, and whatever is typed is offered as a choice of its own when
        it isn't already one - these fields match *substrings*, so "YouTube"
        has to stay settable even though every live title is longer."""
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        if title:
            lbl = Gtk.Label(label=title)
            lbl.set_halign(Gtk.Align.START)
            box.append(lbl)

        rows = []          # (button, label, value)
        entry = None
        typed_btn = None
        empty = None
        target = box
        if search_hint:
            entry = Gtk.SearchEntry()
            entry.set_placeholder_text(search_hint)
            box.append(entry)
            scroller = Gtk.ScrolledWindow()
            scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scroller.set_min_content_height(140)
            scroller.set_max_content_height(320)
            scroller.set_propagate_natural_height(True)
            listing = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            scroller.set_child(listing)
            box.append(scroller)
            target = listing
            typed_btn = Gtk.Button(label="")
            typed_btn.connect(
                "clicked",
                lambda _b: (on_pick(entry.get_text().strip()), popover.popdown()),
            )
            typed_btn.set_visible(False)
            target.append(typed_btn)

        if not choices:
            empty = Gtk.Label(label="(none found)")
            empty.set_halign(Gtk.Align.START)
            empty.set_visible(not search_hint)
            target.append(empty)
        for label, value in choices:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, v=value: (on_pick(v), popover.popdown()))
            target.append(btn)
            rows.append((btn, label, value))

        if search_hint:
            def refilter(*_args):
                matching, typed = choice_row_visibility(
                    [label for _, label, _ in rows], entry.get_text()
                )
                shown = set(matching)
                for btn, label, _value in rows:
                    btn.set_visible(label in shown)
                typed_btn.set_visible(typed)
                if typed:
                    typed_btn.set_label(f'Use "{entry.get_text().strip()}"')
                if empty is not None:
                    empty.set_visible(not shown and not typed)

            # "changed", not "search-changed": the latter is debounced by GTK
            # (~150ms), which only adds lag to filtering a list already in
            # memory.
            entry.connect("changed", refilter)
            refilter()

        popover.set_child(box)
        self.popup_context_menu(
            popover, screen_x, screen_y, focus_widget=entry or None
        )
        return popover

    def on_titles(self, titles):
        """The live stream titles arrived: open the Title classifier's
        dropdown on them."""
        pending = getattr(self, "_pending_title_select", None)
        if not pending:
            return
        nid, sx, sy = pending
        self._pending_title_select = None

        def on_pick(title, nid=nid):
            self._send_property(nid, "title", title)
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        self._show_choice_popover(
            sx, sy, "Title:", [(t, t) for t in titles], on_pick,
            search_hint="Search titles",
        )

    def _open_device_select(self, nid, screen_x, screen_y):
        node = self.nodes.get(nid)
        if not node:
            return
        ntype = node["type"]
        if ntype in ("device_input", "device_output"):
            direction = "inputs" if ntype == "device_input" else "outputs"
            self._pending_device_select = (nid, direction, screen_x, screen_y)
            self.client.send({"command": "get_hardware_devices"})
        else:
            direction = "outputs" if ntype == "app_input" else "inputs"
            self._pending_app_select = (nid, direction, screen_x, screen_y)
            self.client.send({"command": "get_applications"})

    def _open_codec_select(self, nid, screen_x, screen_y):
        self._pending_codec_select = (nid, screen_x, screen_y)
        self.client.send({"command": "get_device_profiles", "node_id": nid})

    def on_hardware_devices(self, devices):
        pending = getattr(self, "_pending_device_select", None)
        if not pending:
            return
        nid, direction, sx, sy = pending
        self._pending_device_select = None
        choices = [
            (d.get("description") or d["name"], d["name"])
            for d in devices.get(direction, [])
        ]

        def on_pick(device_name, nid=nid):
            self._send_property(nid, "device_name", device_name)
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        self._show_choice_popover(sx, sy, "Select device", choices, on_pick)

    def on_applications(self, applications):
        # The Application classifier's dropdown: every live application name,
        # in either direction, since the classifier matches members of a
        # source *or* sink bundle.
        pending_name = getattr(self, "_pending_app_name_select", None)
        if pending_name:
            nid, sx, sy = pending_name
            self._pending_app_name_select = None
            names = sorted(
                {
                    a.get("name")
                    for direction in ("inputs", "outputs")
                    for a in applications.get(direction, [])
                    if a.get("name")
                },
                key=str.lower,
            )

            def on_name_pick(app_name, nid=nid):
                self._send_property(nid, "app_name", app_name)
                GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

            self._show_choice_popover(
                sx, sy, "Application:", [(n, n) for n in names], on_name_pick,
                search_hint="Search applications",
            )
            return

        pending = getattr(self, "_pending_app_select", None)
        if not pending:
            return
        nid, direction, sx, sy = pending
        self._pending_app_select = None
        choices = [(a["name"], a["name"]) for a in applications.get(direction, [])]

        def on_pick(app_name, nid=nid):
            self._send_property(nid, "app_name", app_name)
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        self._show_choice_popover(sx, sy, "Select application", choices, on_pick)

    def on_device_profiles(self, resp):
        pending = getattr(self, "_pending_codec_select", None)
        if not pending:
            return
        nid, sx, sy = pending
        self._pending_codec_select = None
        if resp.get("node_id") != nid:
            return  # stale response for a different node
        choices = [(p["description"], p["index"]) for p in resp.get("profiles", [])]

        def on_pick(index, nid=nid):
            node = self.nodes.get(nid)
            label = next((lbl for lbl, val in choices if val == index), None)
            if node is not None and label:
                # Optimistic local update for instant feedback - the
                # daemon now stores this as node config too (see
                # main.py's _cmd_set_device_profile), so the next
                # refresh() just confirms the same value rather than
                # this being the only place it's remembered.
                node["codec_label"] = label
            self.client.send(
                {
                    "command": "set_device_profile",
                    "node_id": nid,
                    "profile_index": index,
                    "description": label or "",
                }
            )

        self._show_choice_popover(sx, sy, "Select codec / profile", choices, on_pick)

    # ---------- export / import config ----------
    # Fed by the hamburger menu main_window.py adds on top of this
    # widget. Export/import both go through the same daemon commands
    # the standalone export_config.py/apply_config.py CLI scripts use
    # (see main.py's _cmd_export_config), so a config saved from
    # either place loads back in through either place.

    def show_export_dialog(self):
        self._pending_export = True
        self.client.send({"command": "export_config"})

    def on_export_config(self, config):
        if not getattr(self, "_pending_export", False):
            return
        self._pending_export = False
        self._show_export_result_dialog(config)

    def _show_export_result_dialog(self, config):
        text = json.dumps(config, indent=2, sort_keys=True)

        dialog = Gtk.Dialog(
            title="Export PatchSpace Config",
            transient_for=self.get_root(),
            modal=True,
        )
        dialog.set_default_size(520, 420)

        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)

        # The JSON itself - a read-only, selectable text view. Text in
        # a GtkTextView is selectable/copyable (click-drag, Ctrl+A,
        # Ctrl+C) even with editable=False, which is what makes this
        # "copyable" without needing a special widget.
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        textview = Gtk.TextView()
        textview.set_editable(False)
        textview.set_monospace(True)
        textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        textview.get_buffer().set_text(text)
        scrolled.set_child(textview)
        content.append(scrolled)

        # Copy-to-clipboard convenience button, then Save underneath -
        # covers both "just paste this somewhere" and "give me a file"
        # without making either the only option.
        button_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)

        copy_btn = Gtk.Button(label="Copy to Clipboard")
        copy_btn.connect("clicked", lambda _b: self._copy_text_to_clipboard(text))
        button_box.append(copy_btn)

        save_btn = Gtk.Button(label="Save\u2026")
        save_btn.connect("clicked", lambda _b: self._save_text_to_file(text, dialog))
        button_box.append(save_btn)

        content.append(button_box)

        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.show()

    def show_import_dialog(self):
        dialog = Gtk.Dialog(
            title="Import PatchSpace Config",
            transient_for=self.get_root(),
            modal=True,
        )
        dialog.set_default_size(520, 420)

        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        textview = Gtk.TextView()
        textview.set_monospace(True)
        textview.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        buf = textview.get_buffer()
        scrolled.set_child(textview)
        content.append(scrolled)

        load_btn = Gtk.Button(label="Load from File\u2026")
        load_btn.connect("clicked", lambda _b: self._open_text_from_file(buf, dialog))
        content.append(load_btn)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Apply", Gtk.ResponseType.APPLY)
        dialog.set_default_response(Gtk.ResponseType.APPLY)
        dialog.connect("response", self._on_import_response, buf)
        dialog.show()

    def _on_import_response(self, dialog, response, buf):
        if response != Gtk.ResponseType.APPLY:
            dialog.destroy()
            return

        start, end = buf.get_bounds()
        text = buf.get_text(start, end, False)
        try:
            config = json.loads(text)
        except json.JSONDecodeError as e:
            self._show_error_dialog(f"Invalid JSON: {e}")
            return  # leave the dialog open so they can fix it

        dialog.destroy()
        self._apply_config(config)

    def _apply_config(self, config):
        """Hand a full config off to the daemon's own staged loader
        (main.py's _cmd_load_session/_load_session) instead of replaying
        it here as a burst of add_node/add_edge commands. The daemon
        brings backed/effect nodes (echo cancel, sensitivity gate, ...)
        up one at a time and only wires edges once every node has had a
        chance to settle - firing every command back-to-back from here
        skipped that settling entirely, which is what made an imported
        effect node unreliable until it was deleted and re-created by
        hand. The load runs in the background on the daemon side; the
        existing periodic get_nodes poll (see self.refresh on a
        REFRESH_INTERVAL_MS timer) picks up nodes and edges as they
        land, so there's no separate "done" signal to wait for here."""
        self.client.send({"command": "load_session", "config": config})
        self._begin_load()
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    # ------------------------------------------------------------------
    # panel files
    # ------------------------------------------------------------------
    #
    # Panels are loaded by the daemon automatically at start-up and
    # whenever one changes (main.py's _startup_load_panels /
    # _cmd_reload_panels), so there is no "import" step here.  The GUI
    # only lists/deletes the writable ones and can create a new panel
    # from the current selection.

    def reload_panels(self):
        # A panel reload is a full rebuild of the panel half; raise the
        # overlay optimistically.  The daemon's own `loading` flag can't
        # be seen here because the synchronous command blocks this
        # connection's get_nodes polls until it finishes.
        self._begin_load()
        self.client.send({"command": "reload_panels"})

    def show_create_panel_dialog(self):
        """Prompt for a name/mode and move the current selection (if any)
        into a new panel file.  With a selection the panel is sized and
        placed around those nodes; otherwise it pops up as a square in the
        middle of the view."""
        placement = self._new_panel_placement()
        dialog = Gtk.Dialog(
            title="Create Panel", transient_for=self.get_root(), modal=True
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Create", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        box.append(Gtk.Label(label="Panel name"))
        entry = Gtk.Entry()
        entry.set_text(f"panel_{int(time.time() * 1000) % 100000}")
        box.append(entry)
        readonly = Gtk.CheckButton(label="Read-only (controls reset on reload)")
        box.append(readonly)

        def _on_response(dlg, response):
            if response == Gtk.ResponseType.OK:
                name = entry.get_text().strip()
                if name:
                    x, y, w, h = placement
                    node_ids = list(self.selected_nodes)
                    # Make sure the daemon has the nodes' current positions
                    # *before* it renames them into the panel, or they'd
                    # come back at their last-saved (pickup) spot.
                    layout = {
                        nid: {
                            "x": float(self.nodes[nid]["x"]),
                            "y": float(self.nodes[nid]["y"]),
                            "anchored": nid in self.anchored_nodes,
                        }
                        for nid in node_ids
                        if nid in self.nodes
                    }
                    if layout:
                        self.client.send(
                            {"command": "set_node_layout", "layout": layout}
                        )
                    self.client.send(
                        {
                            "command": "create_panel",
                            "name": name,
                            "node_ids": node_ids,
                            "readonly": readonly.get_active(),
                            "parent_id": self._selection_panel(),
                            "x": x,
                            "y": y,
                            "w": w,
                            "h": h,
                        }
                    )
            dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.present()

    def _selection_panel(self):
        """The panel the current selection lives in (their LCA), or the root
        when there is no selection.  A new panel is nested here."""
        ids = [n for n in self.selected_nodes if n in self.nodes]
        if not ids:
            return ""
        owner = self._panel_of_node(ids[0])
        for nid in ids[1:]:
            owner = self._panel_lca(owner, self._panel_of_node(nid))
        return owner

    def _new_panel_placement(self):
        """(x, y, w, h) for a new top-level panel: fitted around the
        selected nodes when there are any, else a square centred in the
        current viewport."""
        selected = [n for n in self.selected_nodes if n in self.nodes]
        pad = self.PANEL_PADDING
        header = self.PANEL_HEADER_H
        if selected:
            minx = min(self.nodes[n]["x"] for n in selected)
            miny = min(self.nodes[n]["y"] for n in selected)
            maxx = max(
                self.nodes[n]["x"] + self.node_width(n) for n in selected
            )
            maxy = max(
                self.nodes[n]["y"] + self.node_height(n) for n in selected
            )
            return (
                minx - pad,
                miny - header - pad,
                max(200.0, (maxx - minx) + 2 * pad),
                max(140.0, (maxy - miny) + header + 2 * pad),
            )
        side = 320.0
        view_w = self.get_width() or 800
        view_h = self.get_height() or 600
        cx, cy = self.to_world(view_w / 2.0, view_h / 2.0)
        return (cx - side / 2.0, cy - side / 2.0, side, side)

    def show_panel_settings_dialog(self, panel_id):
        """Change a panel's display name and color."""
        panel = self.panels.get(panel_id)
        if panel is None or panel.get("readonly") or not panel.get("writable"):
            self._show_error_dialog("This panel is read-only.")
            return
        dialog = Gtk.Dialog(
            title="Panel Settings", transient_for=self.get_root(), modal=True
        )
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)
        dialog.set_default_size(360, -1)

        name_entry = Gtk.Entry()
        name_entry.set_text(str(panel.get("label") or self._panel_local(panel_id)))
        content.append(self._labeled_row("Name:", name_entry))

        panel_key = panel.get("stem") or panel.get("label") or pid
        color_picker = ColorPicker(
            panel.get("color") or self.DEFAULT_PANEL_COLOR,
            presets=self._group_colors(),
            resolve=lambda v: self.resolve_color(v, panel_key),
        )
        content.append(self._labeled_row("Color:", color_picker))

        autoload = Gtk.CheckButton(label="Auto-load at startup")
        autoload.set_active(bool(panel.get("auto_load", False)))
        autocontent = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        autocontent.append(autoload)
        content.append(autocontent)

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Apply", Gtk.ResponseType.APPLY)
        dialog.set_default_response(Gtk.ResponseType.APPLY)

        def _on_response(dlg, response):
            if response == Gtk.ResponseType.APPLY:
                label = name_entry.get_text().strip()
                if label:
                    self.client.send(
                        {
                            "command": "edit_panel",
                            "panel_id": panel_id,
                            "label": label,
                            "color": color_picker.get_value(),
                            "auto_load": autoload.get_active(),
                        }
                    )
                    # Optimistic local update so the header reflects the
                    # change before the next poll.
                    panel["label"] = label
                    panel["color"] = color_picker.get_value()
                    panel["auto_load"] = autoload.get_active()
                    self._panel_geo_cache.clear()
                    self.queue_draw()
            dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.present()

    def confirm_delete_panel_with_nodes(self, panel_id):
        """Ask whether to delete a panel's nodes too or move them up to the
        parent panel, then send the delete."""
        panel = self.panels.get(panel_id)
        if panel is None:
            return
        label = panel.get("label") or self._panel_local(panel_id)
        dialog = Gtk.AlertDialog()
        dialog.set_modal(True)
        dialog.set_message(f"Delete panel {label}?")
        dialog.set_detail(
            "Delete its nodes too, or keep them by moving them up into the "
            "parent panel."
        )
        # 0 = cancel, 1 = keep nodes, 2 = delete nodes.
        dialog.set_buttons(["Cancel", "Keep Nodes", "Delete Nodes"])
        dialog.set_cancel_button(0)
        dialog.set_default_button(1)
        dialog.choose(
            self.get_root(),
            None,
            lambda d, result, pid=panel_id: self._on_delete_panel_choice(
                d, result, pid
            ),
        )

    def _on_delete_panel_choice(self, dialog, result, panel_id):
        try:
            index = dialog.choose_finish(result)
        except GLib.Error:
            return
        if index == 0:
            return
        # delete_panel is a light daemon-side change (no full reload), so no
        # bulk-load overlay - the next poll reflects it.
        self.client.send(
            {
                "command": "delete_panel",
                "panel_id": panel_id,
                "keep_nodes": index == 1,
            }
        )

    def _show_panel_menu(self, panel_id, x, y):
        """The panel hamburger dropdown: settings (writable), duplicate,
        and copy/save the panel's current state as JSON."""
        panel = self.panels.get(panel_id)
        if panel is None:
            return
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        box.set_size_request(200, -1)

        def add(label, cb):
            btn = Gtk.Button(label=label)
            btn.get_child().set_wrap(False)
            btn.connect("clicked", lambda _b: (popover.popdown(), cb()))
            box.append(btn)

        if panel.get("writable") and not panel.get("readonly"):
            add("Settings\u2026", lambda: self.show_panel_settings_dialog(panel_id))
        add("Duplicate\u2026", lambda: self._prompt_clone_panel(panel_id))
        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        add("Copy as JSON", lambda: self._export_panel(panel_id, "copy"))
        add("Save to File\u2026", lambda: self._export_panel(panel_id, "save"))
        if panel.get("writable") and not panel.get("readonly"):
            box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
            add(
                "Delete\u2026",
                lambda: self.confirm_delete_panel_with_nodes(panel_id),
            )

        popover.set_child(box)
        self.popup_context_menu(popover, x, y)

    def _prompt_clone_panel(self, panel_id):
        """Ask for a name and duplicate the panel (and its current nodes)
        into a new file."""
        panel = self.panels.get(panel_id)
        if panel is None:
            return
        dialog = Gtk.Dialog(
            title="Duplicate Panel", transient_for=self.get_root(), modal=True
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Duplicate", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        box.append(Gtk.Label(label="New panel name"))
        entry = Gtk.Entry()
        entry.set_text((panel.get("label") or self._panel_local(panel_id)) + " copy")
        box.append(entry)

        def on_response(dlg, response):
            if response == Gtk.ResponseType.OK:
                name = entry.get_text().strip()
                if name:
                    self._begin_load()
                    self.client.send(
                        {
                            "command": "clone_panel",
                            "panel_id": panel_id,
                            "name": name,
                        }
                    )
            dlg.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _export_panel(self, panel_id, action):
        """Ask the daemon for the panel's current JSON, then either copy it
        to the clipboard or save it to a file."""
        self._pending_panel_export = (panel_id, action)
        self.client.send({"command": "export_panel", "panel_id": panel_id})

    def on_panel_export(self, resp):
        pending = getattr(self, "_pending_panel_export", None)
        self._pending_panel_export = None
        if pending is None:
            return
        panel_id, action = pending
        payload = resp.get("payload")
        if not isinstance(payload, dict):
            self._show_error_dialog("Could not read the panel's state.")
            return
        text = json.dumps(payload, indent=2, sort_keys=True)
        if action == "copy":
            clipboard = Gdk.Display.get_default().get_clipboard()
            provider = Gdk.ContentProvider.new_for_value(
                GObject.Value(GObject.TYPE_STRING, text)
            )
            clipboard.set_content(provider)
            return
        panel = self.panels.get(panel_id, {})
        name = panel.get("label") or self._panel_local(panel_id)
        name = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in str(name)
        ) or "panel"

        def on_path(path):
            if not path:
                return
            try:
                with open(path, "w") as f:
                    f.write(text)
            except OSError as exc:
                self._show_error_dialog(f"Could not save file: {exc}")

        save_file(
            self.get_root(), "Save Panel", name + ".json", on_path
        )

    def rebuild_graph(self):
        """Tear the daemon's PatchSpace down and rebuild it exactly as it
        is now - a user-facing "turn it off and on again" for when a
        node's live routing has gone wrong.  The daemon snapshots the
        current graph first (main.py's _cmd_rebuild), so nothing is lost;
        it stages the rebuild in the background and the periodic poll
        picks the nodes back up as they land.  The loading overlay reuses
        the bulk-load path so it's obvious work is happening."""
        self.client.send({"command": "rebuild"})
        self._begin_load()
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    # ---------- export/import helpers (clipboard, file chooser) ----------

    def _copy_text_to_clipboard(self, text):
        display = self.get_display()
        display.get_clipboard().set(text)

    def _save_text_to_file(self, text, parent_dialog):
        # Goes straight through the XDG Desktop Portal instead of
        # Gtk.FileChooserNative - see portal_file_dialog.py for why
        # (GtkFileChooserNative's own fallback path can hard-abort the
        # process on a system with no GSettings schemas compiled at
        # all).
        def on_path(path):
            if not path:
                return
            try:
                with open(path, "w") as f:
                    f.write(text)
            except OSError as e:
                self._show_error_dialog(f"Could not save file: {e}")

        save_file(
            self.get_root(),
            "Save PatchSpace Config",
            "patchspace_config.json",
            on_path,
        )

    def _open_text_from_file(self, buf, parent_dialog):
        def on_path(path):
            if not path:
                return
            try:
                with open(path) as f:
                    text = f.read()
                buf.set_text(text)
            except OSError as e:
                self._show_error_dialog(f"Could not open file: {e}")

        open_file(self.get_root(), "Load PatchSpace Config", on_path)

    def _show_error_dialog(self, message):
        dialog = Gtk.MessageDialog(
            transient_for=self.get_root(),
            modal=True,
            message_type=Gtk.MessageType.ERROR,
            buttons=Gtk.ButtonsType.OK,
            text=message,
        )
        dialog.connect("response", lambda d, r: d.destroy())
        dialog.show()

    # ---------- drag handling ----------

    def _reset_drag_state(self):
        """Forget any in-progress drag/connection.  Called at the start
        of every new press and whenever the drag gesture is cancelled,
        so a drag that ends without a usable drag-end (its sequence
        claimed by a parent controller, the button released off-widget,
        ...) can never leave the canvas stuck in "connecting" or
        "panning" mode with no way to click out of it."""
        self.connecting_from = None
        self.detaching_edge = None
        self.dragging_node = None
        self.dragging_panel = None
        self.dragging_port = None
        self.drag_node_starts = {}
        self._panel_drag_baseline = {}
        self.hover_target_node = None
        self.panning = False
        self._marquee_mode = None
        self.select_rect = None
        if self.slider_dragging is not None:
            _kind, nid = self.slider_dragging
            self.slider_dragging = None
            self.pinned_nodes.discard(nid)
            self.layout_awake = True
            self._settle_ticks = 0

    def on_drag_cancel(self, gesture, sequence):
        """The drag gesture's sequence was taken away (parent claimed
        it, window focus loss, ...) - there will be no drag-end, so
        release every bit of drag state here."""
        self._reset_drag_state()
        self.set_cursor(None)
        self.queue_draw()

    def on_drag_begin(self, gesture, start_x, start_y):
        self.grab_focus()
        # Do NOT cancel a popover that on_click just requested on this same
        # press (its popup() is deferred to an idle and it isn't visible
        # yet); drag-begin fires right after the click handler, and
        # cancelling here made the field editor never open.
        self.dismiss_context_popover(force=False)
        # A fresh press always starts from a clean slate - a previous
        # interaction that ended via ::cancel (or that never produced a
        # drag-end) must not leak its connection/node/pan state into
        # this one.
        self._reset_drag_state()
        wx, wy = self.to_world(start_x, start_y)
        self.drag_start_xy = (wx, wy)
        self.drag_current_xy = (wx, wy)

        # A group +/- mode is armed - the click is a membership pick,
        # handled entirely by on_click; never start a drag/pan under it.
        if self._group_pick_mode is not None or self._panel_pick_mode is not None:
            return

        # Panel header: the panel's own affordance, above the canvas.
        # Handled before node hit-tests so the title strip is always
        # draggable.
        pid = self.find_panel_header_at(wx, wy)
        if (
            pid is not None
            and self.find_panel_reset_at(wx, wy) is None
            and self.find_panel_anchor_at(wx, wy) is None
            and self.find_panel_menu_at(wx, wy) is None
            and self.find_panel_delete_at(wx, wy) is None
            and self.find_panel_edit_at(wx, wy) is None
            and self.find_panel_close_at(wx, wy) is None
        ):
            panel = self.panels[pid]
            self.dragging_panel = pid
            self.drag_panel_start = (wx, wy)
            self.drag_panel_origin = (panel["x"], panel["y"])
            self._drag_panel_applied = (0.0, 0.0)
            self.layout_awake = True
            self._settle_ticks = 0
            return

        # Inline controls (slider / checkboxes / three-dot menu / text
        # field) all sit *inside* a node's rectangle. Resolving every
        # control hit FIRST - before find_node_at()/panning - is what
        # stops a press on a control from also being read as "start
        # dragging the node". GestureDrag's drag-begin fires on every
        # left-button press, immediately, before any movement, so it would
        # otherwise win the race against GestureClick's "pressed"
        # handler (on_click) and grab the node out from under a
        # slider/checkbox press.
        slider_hit = self.find_slider_at(wx, wy)
        if slider_hit is not None:
            node = self.nodes[slider_hit]
            # Compute the new volume based on the click position
            slider_left = node["x"] + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            new_vol = max(0.0, min(1.0, (wx - slider_left) / slider_width))
            node["volume"] = new_vol
            self._send_set_volume(slider_hit, new_vol)  # update daemon immediately

            self.slider_dragging = ("process", slider_hit)
            self.slider_drag_start_x = wx
            self.slider_initial_volume = new_vol
            self._slider_last_sent = new_vol
            self.queue_draw()
            logger.debug("Started dragging slider for node %s", slider_hit)
            self.pinned_nodes.add(slider_hit)
            return

        device_slider_hit = self.find_device_volume_slider_at(wx, wy)
        if device_slider_hit is not None:
            nid = device_slider_hit
            row_x, row_y, row_w, row_h = self._volume_row_rects(nid)[0]
            new_vol = max(0.0, min(1.0, (wx - row_x) / row_w))
            self.nodes[nid]["device_volume"] = new_vol
            self.client.send(
                {"command": "set_device_volume", "node_id": nid, "volume": new_vol}
            )
            self.slider_dragging = ("device", nid)
            self.slider_drag_start_x = wx
            self.slider_initial_volume = new_vol
            self._slider_last_sent = new_vol
            self.queue_draw()
            self.pinned_nodes.add(nid)
            return

        wet_hit = self.find_wetdry_slider_at(wx, wy)
        if wet_hit is not None:
            node = self.nodes[wet_hit]
            slider_left = node["x"] + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            new_mix = max(0.0, min(1.0, (wx - slider_left) / slider_width))
            node["wet_dry"] = new_mix
            self._send_effect_slider(wet_hit, "wetdry", "wet_dry", new_mix)
            self.slider_dragging = ("wetdry", wet_hit)
            self.slider_drag_start_x = wx
            self.slider_initial_volume = new_mix
            self._slider_last_sent = new_mix
            self.queue_draw()
            self.pinned_nodes.add(wet_hit)
            return

        sensitivity_hit = self.find_sensitivity_slider_at(wx, wy)
        if sensitivity_hit is not None:
            node = self.nodes[sensitivity_hit]
            slider_left = node["x"] + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            new_frac = max(0.0, min(1.0, (wx - slider_left) / slider_width))
            self._apply_sensitivity_slider(sensitivity_hit, new_frac)
            self.slider_dragging = ("sensitivity", sensitivity_hit)
            self.slider_drag_start_x = wx
            self.slider_initial_volume = new_frac
            self._slider_last_sent = new_frac
            self.queue_draw()
            self.pinned_nodes.add(sensitivity_hit)
            return

        gain_hit = self.find_gain_slider_at(wx, wy)
        if gain_hit is not None:
            node = self.nodes[gain_hit]
            slider_left = node["x"] + self.SLIDER_MARGIN
            slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            new_frac = max(0.0, min(1.0, (wx - slider_left) / slider_width))
            self._apply_gain_slider(gain_hit, new_frac)
            self.slider_dragging = ("gain", gain_hit)
            self.slider_drag_start_x = wx
            self.slider_initial_volume = new_frac
            self._slider_last_sent = new_frac
            self.queue_draw()
            self.pinned_nodes.add(gain_hit)
            return

        device_row_hit = self.find_device_row_at(wx, wy)
        if (
            self.find_mute_checkbox_at(wx, wy) is not None
            or self.find_gate_toggle_at(wx, wy) is not None
            or self.find_filter_mode_at(wx, wy) is not None
            or self.find_switcher_toggle_at(wx, wy) is not None
            or self.find_boolean_toggle_at(wx, wy) is not None
            or self.find_fallback_toggle_at(wx, wy) is not None
            or self.find_impulse_button_at(wx, wy) is not None
            or self.find_toggle_switch_at(wx, wy) is not None
            or self.find_path_picker_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
            or self.find_anchor_icon_at(wx, wy) is not None
            or self.find_settings_gear_at(wx, wy) is not None
            or self.find_field_at(wx, wy) is not None
            or self.find_group_label_at(wx, wy) is not None
            or self.find_group_action_at(wx, wy) is not None
            or self.find_group_menu_at(wx, wy) is not None
            or device_row_hit is not None
        ):
            # Single-click toggles/menus, handled entirely by
            # on_click()'s "pressed" callback - just don't let this
            # drag gesture also grab the node or start a pan under them.
            return

        # Shift / Ctrl + left-drag is a modifier marquee: shift adds the
        # nodes it sweeps to the selection, ctrl removes them.  Handled
        # before nodes/sockets so a modifier-drag never moves a node or
        # starts a connection.
        mods = gesture.get_current_event_state()
        add_sel = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        rem_sel = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        if add_sel or rem_sel:
            self._marquee_mode = "add" if add_sel else "remove"
            self._marquee_base = set(self.selected_nodes)
            self._marquee_start_world = (wx, wy)
            return

        socket_hit = self.find_socket_at(wx, wy)
        if socket_hit and socket_hit[1] == "out":
            nid, _, idx = socket_hit
            self.connecting_from = (nid, idx)
            self.queue_draw()
            return

        eid = self.find_edge_at(wx, wy)
        if eid:
            edge = self.edges[eid]
            self.connecting_from = (
                edge["from_node"],
                self._edge_from_port_index(edge),
            )
            self.detaching_edge = (eid, edge["from_node"])
            self.queue_draw()
            return

        nid = self.find_node_at(wx, wy)
        if nid is not None:
            # A plain press selects the node (a click with no movement is
            # therefore "select"), while pressing one already in a
            # multi-selection keeps the selection so the whole thing
            # moves together.
            if nid not in self.selected_nodes:
                self._set_selection({nid})
            # Panel ports are locked to their bar but can be dragged
            # vertically to reorder.
            if self.nodes[nid].get("type") in (
                self._PORT_IN_TYPES | self._PORT_OUT_TYPES
            ):
                self.dragging_port = nid
                self._port_drag_start_y = self.nodes[nid].get("y") or 0.0
                return
            # Freeze each panel's size for this drag (see _panel_rect): the
            # box keeps its size on pickup and may only grow toward the
            # dragged node, so a node can't fall out of its own panel just
            # because the box shrank under it.
            self._panel_drag_baseline = {}
            for pid in self.panels:
                if pid == "":
                    continue
                r = self._panel_rect_base(pid)
                if r is not None:
                    self._panel_drag_baseline[pid] = r
            self.dragging_node = nid
            # Dragging a node that's part of a multi-node selection moves
            # the whole selection together; otherwise just that node.
            if nid in self.selected_nodes and len(self.selected_nodes) > 1:
                moving = [n for n in self.selected_nodes if n in self.nodes]
            else:
                moving = [nid]
            self.drag_node_starts = {
                n: (self.nodes[n]["x"], self.nodes[n]["y"]) for n in moving
            }
            self.drag_node_start = self.drag_node_starts[nid]
            self.layout_awake = True
            self._settle_ticks = 0
            return

        # Empty panel background: drag the panel itself.  Reaching here
        # means the press was not on a control/edge/socket/node, so any
        # point inside the box (title row or body) moves the panel.
        pid = self.find_panel_at(wx, wy)
        if pid is not None and self.find_panel_io_plus_at(wx, wy) is None:
            panel = self.panels[pid]
            self.dragging_panel = pid
            self.drag_panel_start = (wx, wy)
            self.drag_panel_origin = (panel["x"], panel["y"])
            self._drag_panel_applied = (0.0, 0.0)
            self.layout_awake = True
            self._settle_ticks = 0
            return

        # Pressing empty canvas starts a pan and drops any selection.
        self.panning = True
        self.pan_drag_start = (self.pan_x, self.pan_y)
        self._set_selection(())

    def on_drag_update(self, gesture, offset_x, offset_y):
        if self.dragging_port is not None:
            node = self.nodes.get(self.dragging_port)
            if node is not None:
                ny = self._port_drag_start_y + offset_y / self.zoom
                rect = self._panel_rect(self._panel_of_node(self.dragging_port))
                if rect is not None:
                    h = self.node_height(self.dragging_port)
                    ny = max(rect[1] + 4.0, min(rect[1] + rect[3] - h - 4.0, ny))
                node["y"] = ny
            self.queue_draw()
            return
        if self.dragging_panel is not None:
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            panel = self.panels.get(self.dragging_panel)
            if panel is not None:
                panel["x"] = self.drag_panel_origin[0] + dx
                panel["y"] = self.drag_panel_origin[1] + dy
                desired = (
                    panel["x"] - self.drag_panel_origin[0],
                    panel["y"] - self.drag_panel_origin[1],
                )
                inc = (
                    desired[0] - self._drag_panel_applied[0],
                    desired[1] - self._drag_panel_applied[1],
                )
                if inc != (0.0, 0.0):
                    self._translate_panel_local(self.dragging_panel, inc[0], inc[1])
                    self._drag_panel_applied = desired
                self._mark_panel_moved(self.dragging_panel)
                self._panel_geo_cache.clear()
                self.queue_draw()
            return
        if self._marquee_mode is not None:
            sx, sy = self._marquee_start_world
            ex = sx + offset_x / self.zoom
            ey = sy + offset_y / self.zoom
            self.select_rect = (min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey))
            inside = self._nodes_in_rect(self.select_rect)
            if self._marquee_mode == "add":
                self._set_selection(self._marquee_base | inside)
            else:
                self._set_selection(self._marquee_base - inside)
            self.queue_draw()
            return
        if self.connecting_from:
            cur_x = self.drag_start_xy[0] + offset_x / self.zoom
            cur_y = self.drag_start_xy[1] + offset_y / self.zoom
            self.drag_current_xy = (cur_x, cur_y)
            socket_hit = self.find_socket_at(cur_x, cur_y)
            self.hover_target_node = (
                socket_hit[0] if (socket_hit and socket_hit[1] == "in") else None
            )
            self.queue_draw()
            return

        if self.slider_dragging is not None:
            kind, nid = self.slider_dragging
            node = self.nodes.get(nid)
            if node is None:
                return
            if kind == "wetdry":
                slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
                delta_mix = (offset_x / self.zoom) / slider_width
                new_mix = max(
                    0.0, min(1.0, self.slider_initial_volume + delta_mix)
                )
                node["wet_dry"] = new_mix
                if abs(new_mix - self._slider_last_sent) >= VOLUME_SEND_EPSILON:
                    self._slider_last_sent = new_mix
                    self._send_effect_slider(nid, "wetdry", "wet_dry", new_mix)
                self.queue_draw()
                return
            if kind == "sensitivity":
                slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
                delta_frac = (offset_x / self.zoom) / slider_width
                new_frac = max(0.0, min(1.0, self.slider_initial_volume + delta_frac))
                if abs(new_frac - self._slider_last_sent) >= VOLUME_SEND_EPSILON:
                    self._slider_last_sent = new_frac
                    self._apply_sensitivity_slider(nid, new_frac)
                else:
                    node["sensitivity"] = new_frac
                self.queue_draw()
                return
            if kind == "gain":
                slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
                delta_frac = (offset_x / self.zoom) / slider_width
                new_frac = max(
                    0.0, min(1.0, self.slider_initial_volume + delta_frac)
                )
                if abs(new_frac - self._slider_last_sent) >= VOLUME_SEND_EPSILON:
                    self._slider_last_sent = new_frac
                    self._apply_gain_slider(nid, new_frac)
                else:
                    node["gain"] = new_frac
                self.queue_draw()
                return
            if kind == "process":
                slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            else:
                _, _, slider_width, _ = self._volume_row_rects(nid)[0]
            # Offset-based (not absolute-position-based) so this can't
            # be thrown off by anything else touching node["x"] mid-drag.
            delta_vol = (offset_x / self.zoom) / slider_width
            new_vol = max(0.0, min(1.0, self.slider_initial_volume + delta_vol))
            if kind == "process":
                node["volume"] = new_vol
            else:
                node["device_volume"] = new_vol
            # Throttled: only actually send when the value has moved
            # enough to matter, so a drag doesn't put a message on the
            # socket for every single motion event. The exact final
            # value is always sent on release regardless (on_drag_end).
            if abs(new_vol - self._slider_last_sent) >= VOLUME_SEND_EPSILON:
                self._slider_last_sent = new_vol
                if kind == "process":
                    self._send_set_volume(nid, new_vol)
                else:
                    self.client.send(
                        {
                            "command": "set_device_volume",
                            "node_id": nid,
                            "volume": new_vol,
                        }
                    )
            self.queue_draw()
            return

        if self.dragging_node is not None:
            # Offset every node in this drag (the whole selection, or
            # just the one) by the same delta.
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            for nid, (sx, sy) in self.drag_node_starts.items():
                node = self.nodes.get(nid)
                if node is not None:
                    node["x"] = sx + dx
                    node["y"] = sy + dy
            # Let the auto-fit boxes follow the node as it nudges the edges.
            self._panel_geo_cache.clear()
            self.queue_draw()
            return

        if self.panning:
            self.pan_x = self.pan_drag_start[0] + offset_x
            self.pan_y = self.pan_drag_start[1] + offset_y
            self.queue_draw()

    @staticmethod
    def _edge_id(from_node, to_node, to_port="in", from_port="out"):
        """Client-side mirror of PatchSpace._edge_id() on the daemon -
        needed here purely to predict what id an add_edge would get,
        for the "dragging onto an existing edge removes it" toggle
        below. Must stay in sync with that method."""
        base = f"{from_node}->{to_node}"
        if to_port != "in":
            base += f":{to_port}"
        if from_port != "out":
            base += f"@{from_port}"
        return base

    def _note_new_edge(self, from_node, to_node, to_port, from_port):
        """Arm the draw-in animation for a connection the user just made.
        Stored as "pending" until the daemon echoes it in a poll (the edge
        id may not exist locally yet), at which point the ticker stamps its
        birth - so the animation always starts from the first visible frame
        rather than from when the command was sent."""
        self._pending_edge_draw[
            self._edge_id(from_node, to_node, to_port, from_port)
        ] = time.monotonic()

    def _ports_compatible(self, from_nid, from_port, to_nid, to_port):
        """Whether an edge from (from_nid, from_port) to (to_nid,
        to_port) is legal.  Mirrors the daemon's add_edge check: boolean
        pairs only with boolean, impulse only with impulse and filter only
        with filter, while audio and bundle ports may pair either way (a
        bundle is a set of audio streams; one stream is a bundle of one)."""
        src = self.nodes.get(from_nid)
        dst = self.nodes.get(to_nid)
        if src is None or dst is None:
            return False
        from_kind = port_kind(src["type"], from_port, "out")
        to_kind = port_kind(dst["type"], to_port, "in")
        if (from_kind == "boolean") != (to_kind == "boolean"):
            return False
        if (from_kind == "impulse") != (to_kind == "impulse"):
            return False
        if (from_kind == "filter") != (to_kind == "filter"):
            return False
        return True

    def on_drag_end(self, gesture, offset_x, offset_y):
        # Always clear the transient drag/connection state, even if a
        # branch below raised (a stale detaching-edge lookup, a node
        # removed mid-drag, ...).  Leaking it is what left the canvas
        # unclickable after dropping a connection into empty space.
        try:
            self._handle_drag_end(gesture, offset_x, offset_y)
        finally:
            self._reset_drag_state()
            self.queue_draw()

    def _handle_drag_end(self, gesture, offset_x, offset_y):
        if self.dragging_port is not None:
            self.dragging_port = None
            # Re-sort by the dropped y and re-center the stack.
            self._mark_layout_dirty()
            self._layout_panel_ports()
            self.queue_draw()
            return
        if self.dragging_panel is not None:
            dragging = self.dragging_panel
            panel = self.panels.get(dragging)
            if panel is not None:
                # Drop over another panel (not this one or its descendants)
                # nests it inside that panel; otherwise it just moves.
                rect = self._panel_rect(dragging)
                target = None
                if rect is not None:
                    target = self.find_panel_at(
                        rect[0] + rect[2] / 2.0, rect[1] + rect[3] / 2.0,
                        exclude=dragging,
                    )
                current = panel.get("parent", "")
                if target is not None and target != current:
                    self.client.send(
                        {
                            "command": "move_panel",
                            "panel_id": dragging,
                            "parent_id": target,
                        }
                    )
                else:
                    self.client.send(
                        {
                            "command": "set_panel_layout",
                            "panel_id": dragging,
                            "x": panel["x"],
                            "y": panel["y"],
                        }
                    )
            self.dragging_panel = None
            self._panel_drag_baseline = {}
            # Nodes moved with the panel during the drag; persist their
            # absolute positions now (the daemon no longer derives them from
            # the panel move).
            self._mark_layout_dirty()
            return
        if self._marquee_mode is not None:
            # A modifier *click* (no sweep) toggles just the node under
            # the pointer; a modifier drag already updated the selection
            # live in on_drag_update.
            if offset_x * offset_x + offset_y * offset_y < 25.0:
                wx, wy = self._marquee_start_world
                nid = self.find_node_at(wx, wy)
                if nid is not None:
                    if nid in self.selected_nodes:
                        self._set_selection(self.selected_nodes - {nid})
                    else:
                        self._set_selection(self.selected_nodes | {nid})
            self.select_rect = None
            self._marquee_mode = None
            self.queue_draw()
            return

        if self.slider_dragging is not None:
            kind, nid = self.slider_dragging
            node = self.nodes.get(nid)
            self.slider_dragging = None
            self._dragging_volume = False
            self.pinned_nodes.discard(nid)
            # Re‑enable layout so the node can spring back
            self.layout_awake = True
            self._settle_ticks = 0
            if node is not None:
                if kind == "process":
                    self._send_set_volume(nid, node["volume"])
                elif kind == "wetdry":
                    self._send_effect_slider(nid, "wetdry", "wet_dry", node["wet_dry"])
                elif kind == "sensitivity":
                    self._apply_sensitivity_slider(nid, node["sensitivity"])
                elif kind == "gain":
                    self._apply_gain_slider(nid, node.get("gain", 0.5))
                else:
                    self.client.send(
                        {
                            "command": "set_device_volume",
                            "node_id": nid,
                            "volume": node["device_volume"],
                        }
                    )
            self.refresh()
            return

        if self.connecting_from:
            out_nid, out_idx = self.connecting_from
            out_node = self.nodes.get(out_nid)
            source_port = (
                out_node["outputs"][out_idx]
                if out_node is not None and out_idx < len(out_node["outputs"])
                else "out"
            )
            end_x = self.drag_start_xy[0] + offset_x / self.zoom
            end_y = self.drag_start_xy[1] + offset_y / self.zoom
            socket_hit = self.find_socket_at(end_x, end_y)
            target_nid = (
                socket_hit[0] if (socket_hit and socket_hit[1] == "in") else None
            )
            # Which named input socket was actually hit (e.g.
            # EchoCancelNode's "mic" vs "probe") - defaults to "in"
            # for every ordinary single-input node type, same as
            # Edge.to_port's own default.
            target_port = (
                self.nodes[target_nid]["inputs"][socket_hit[2]]
                if target_nid is not None
                else "in"
            )

            if self.detaching_edge:
                eid, from_nid = self.detaching_edge
                # The edge can vanish under us (a get_nodes poll landing
                # mid-drag, the daemon re-syncing) - treat that as "the
                # thing we were dragging is gone" and just stop.
                old_edge = self.edges.get(eid)
                if old_edge is None:
                    return
                old_to_nid = old_edge["to_node"]
                old_to_port = old_edge.get("to_port", "in")
                if target_nid is not None:
                    if (
                        target_nid != old_to_nid or target_port != old_to_port
                    ) and target_nid != from_nid:
                        self._retire_edge(eid)
                        self.client.send({"command": "remove_edge", "edge_id": eid})
                        if self._ports_compatible(
                            from_nid, source_port, target_nid, target_port
                        ):
                            self._note_new_edge(
                                from_nid, target_nid, target_port, source_port
                            )
                            self.client.send(
                                {
                                    "command": "add_edge",
                                    "from_node": from_nid,
                                    "to_node": target_nid,
                                    "to_port": target_port,
                                    "from_port": source_port,
                                }
                            )
                        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
                else:
                    self._retire_edge(eid)
                    self.client.send({"command": "remove_edge", "edge_id": eid})
                    GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            elif target_nid is not None:
                if out_nid != target_nid:
                    existing_eid = self._edge_id(
                        out_nid, target_nid, target_port, source_port
                    )
                    if existing_eid in self.edges:
                        self._retire_edge(existing_eid)
                        self.client.send(
                            {"command": "remove_edge", "edge_id": existing_eid}
                        )
                    elif self._ports_compatible(
                        out_nid, source_port, target_nid, target_port
                    ):
                        self._note_new_edge(
                            out_nid, target_nid, target_port, source_port
                        )
                        self.client.send(
                            {
                                "command": "add_edge",
                                "from_node": out_nid,
                                "to_node": target_nid,
                                "to_port": target_port,
                                "from_port": source_port,
                            }
                        )
                    GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

            self.connecting_from = None
            self.detaching_edge = None
            self.hover_target_node = None
            self.queue_draw()
            return

        if self.dragging_node is not None:
            dragged = self.dragging_node
            # Decide the target from the *drag-time* geometry (baseline +
            # capped growth) while the drag state is still active.  Once we
            # clear it every panel re-fits to include the dropped node, so
            # the source panel would always contain it and a node could
            # never be dragged out.
            self._panel_geo_cache.clear()
            self._reparent_after_drag(dragged)
            self.dragging_node = None
            self._panel_geo_cache.clear()
            # Persist where the user dropped it.
            self._mark_layout_dirty()
            self.queue_draw()
            return

        self.panning = False

    def _reparent_after_drag(self, dragged):
        """If a dragged node was dropped inside a different panel, move it
        (and the rest of the selection) into that panel.  The daemon
        refuses and the poll snaps it back when the source or target panel
        is read-only."""
        node = self.nodes.get(dragged)
        if node is None:
            return
        cx = node["x"] + self.node_width(dragged) / 2
        cy = node["y"] + self.node_height(dragged) / 2
        target = self._panel_drop_target(cx, cy)
        current = dragged.rsplit("::", 1)[0] if "::" in dragged else ""
        if target == current:
            return
        members = [n for n in self.selected_nodes if n in self.nodes]
        if dragged not in members:
            members = [dragged]
        self.client.send(
            {"command": "move_nodes", "panel_id": target, "node_ids": members}
        )
        # Persist the dropped positions under their *new* ids right away.
        # The debounced layout save still holds the old ids (the rename
        # poll hasn't arrived yet), so its set_node_layout is ignored after
        # the rename and the daemon would keep the pickup positions - the
        # "nodes teleport back to where I picked them up" bug.
        layout = {}
        for nid in members:
            node = self.nodes.get(nid)
            if node is None:
                continue
            local = nid.rsplit("::", 1)[-1]
            new_id = f"{target}::{local}" if target else local
            layout[new_id] = {
                "x": float(node["x"]),
                "y": float(node["y"]),
                "anchored": nid in self.anchored_nodes,
            }
        if layout:
            self.client.send({"command": "set_node_layout", "layout": layout})
    # ---------- context menus ----------

    def show_node_menu(self, node_id, screen_x, screen_y):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)

        del_btn = Gtk.Button(label="Delete")
        del_btn.connect("clicked", self._on_delete_node, node_id, popover)
        box.append(del_btn)

        sett_btn = Gtk.Button(label="Settings")
        sett_btn.connect("clicked", self._on_open_settings, node_id, popover)
        box.append(sett_btn)

        popover.set_child(box)
        self.popup_context_menu(popover, screen_x, screen_y)

    def _on_delete_node(self, button, node_id, popover):
        popover.popdown()
        self._start_node_ghost(node_id)
        self.client.send({"command": "remove_node", "node_id": node_id})
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def confirm_delete_selection(self):
        """Ask before deleting every selected node (the floating
        bottom-right delete button; see main_window)."""
        node_ids = [n for n in self.selected_nodes if n in self.nodes]
        if not node_ids:
            return
        plural = "s" if len(node_ids) != 1 else ""
        confirm = Gtk.AlertDialog()
        confirm.set_modal(True)
        confirm.set_message(f"Delete {len(node_ids)} node{plural}?")
        confirm.set_detail(
            "Their edges are removed with them. This cannot be undone."
        )
        confirm.set_buttons(["Cancel", "Delete"])
        confirm.set_cancel_button(0)
        confirm.set_default_button(1)
        confirm.choose(
            self.get_root(),
            None,
            lambda d, result, ids=node_ids: self._on_delete_selection_chosen(
                d, result, ids
            ),
        )

    def _on_delete_selection_chosen(self, dialog, result, node_ids):
        try:
            index = dialog.choose_finish(result)
        except GLib.Error:
            return
        if index != 1:
            return
        for nid in node_ids:
            if nid in self.nodes:
                self._start_node_ghost(nid)
                self.client.send({"command": "remove_node", "node_id": nid})
        self._set_selection(())
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def _on_open_settings(self, button, node_id, popover):
        popover.popdown()
        self.show_settings_dialog(node_id)

    # ---------- settings dialog (data-driven from node_specs) ----------

    @staticmethod
    def _labeled_row(label_text, widget):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        if label_text:
            lbl = Gtk.Label(label=label_text)
            lbl.set_halign(Gtk.Align.START)
            box.append(lbl)
        box.append(widget)
        return box

    @staticmethod
    def _focus_and_select(entry):
        """A handler for a dialog's "map" signal that puts the cursor in
        `entry` with its text selected, so opening a settings dialog lands
        the user straight in the label field ready to overtype it.  Done
        on map (not right after construction) because focus can't be
        grabbed until the dialog's surface is actually on screen."""
        def _on_map(_widget):
            entry.grab_focus()
            entry.select_region(0, -1)
        return _on_map

    def show_settings_dialog(self, node_id):
        node = self.nodes.get(node_id)
        if not node:
            return
        spec = spec_for(node["type"])

        dialog = Gtk.Dialog(
            title=f"Settings \u2014 {type_label(node['type'], node_id)} ({node_id})",
            transient_for=self.get_root(),
            modal=True,
        )
        dialog.set_default_size(-1, -1)
        dialog.set_resizable(True)

        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)

        # Node ID field - editable rename. Validated on Apply (see
        # _validate_new_node_id) rather than as-you-type, since
        # "already in use" can only be judged against the *other*
        # nodes, not the text in isolation.
        id_entry = Gtk.Entry()
        id_entry.set_text(node_id)
        id_entry.set_hexpand(True)
        id_entry.set_tooltip_text(
            "The node's unique id. Renaming it updates every edge that "
            "references it."
        )
        content.append(self._labeled_row("Node ID:", id_entry))

        id_error_label = Gtk.Label(label="")
        id_error_label.set_halign(Gtk.Align.START)
        id_error_label.add_css_class("error")
        id_error_label.set_visible(False)
        content.append(id_error_label)

        # Label field
        label_entry = Gtk.Entry()
        label_entry.set_text(node.get("label", ""))
        label_entry.set_hexpand(True)
        label_entry.set_tooltip_text("The name shown on the node in the canvas.")
        content.append(self._labeled_row("Label:", label_entry))

        # Keep the node id in step with the label: editing the label
        # re-slugs the id (preserving any panel namespace prefix), so the
        # two don't drift.  Validation still runs on Apply.
        def _label_to_id(*_a):
            text = label_entry.get_text().strip()
            if not text:
                return
            local = "".join(
                c if (c.isalnum() or c in "-_.") else "_" for c in text
            ).strip("_") or "node"
            prefix, sep, _old = node_id.rpartition("::")
            id_entry.set_text(f"{prefix}::{local}" if sep else local)

        label_entry.connect("changed", _label_to_id)

        # Control widget (gate / volume)
        control_widget = None
        min_spin = None
        max_spin = None

        if spec.control == "gate":
            control_widget = Gtk.CheckButton(label="Enabled")
            control_widget.set_active(node.get("enabled", True))
            content.append(control_widget)

        elif spec.control == "volume":
            if is_mute_node(node_id):
                control_widget = Gtk.CheckButton(label="Pass audio")
                control_widget.set_active(node.get("volume", 1.0) > 0.5)
                content.append(control_widget)
            else:
                # Volume slider (shows actual value)
                actual_min = node.get("volume_min", 0.0)
                actual_max = node.get("volume_max", 1.0)
                fraction = node.get("volume", 1.0)
                actual_value = actual_min + (actual_max - actual_min) * fraction

                scale = Gtk.Scale.new_with_range(
                    Gtk.Orientation.HORIZONTAL, actual_min, actual_max, 0.01
                )
                scale.set_value(actual_value)
                scale.set_hexpand(True)
                scale.set_draw_value(True)
                scale.set_value_pos(Gtk.PositionType.RIGHT)
                content.append(self._labeled_row("Volume:", scale))

                # Min/Max spin buttons
                min_spin = Gtk.SpinButton.new_with_range(-60.0, 60.0, 0.1)
                min_spin.set_value(actual_min)
                min_spin.set_hexpand(True)
                max_spin = Gtk.SpinButton.new_with_range(-60.0, 60.0, 0.1)
                max_spin.set_value(actual_max)
                max_spin.set_hexpand(True)

                content.append(self._labeled_row("Min gain:", min_spin))
                content.append(self._labeled_row("Max gain:", max_spin))

                # Store references so response handler can access them
                control_widget = (scale, min_spin, max_spin)

        # Field entry (for text fields), or a dropdown of human-
        # readable names for media_class (a fixed set of raw
        # PipeWire media.class strings, direction-dependent - see
        # node_specs.media_class_choices_for). _on_settings_response
        # tells the two apart via spec.field rather than isinstance(),
        # since that's the same thing it already checks to decide
        # which property to send.
        field_entry = None
        if spec.field == "media_class":
            current_raw = node.get("meta", {}).get(spec.field, "") or ""
            media_class_choices = media_class_choices_for(node["type"])
            media_class_values = [raw for _label, raw in media_class_choices]
            field_entry = Gtk.DropDown.new_from_strings(
                [label for label, _raw in media_class_choices]
            )
            try:
                selected_index = media_class_values.index(current_raw)
            except ValueError:
                selected_index = 0
            field_entry.set_selected(selected_index)
            field_entry.set_hexpand(True)
            content.append(
                self._labeled_row(
                    FIELD_LABELS.get(spec.field, spec.field + ":"), field_entry
                )
            )
        elif spec.field:
            field_entry = Gtk.Entry()
            field_entry.set_text(node.get("meta", {}).get(spec.field, "") or "")
            field_entry.set_hexpand(True)
            content.append(
                self._labeled_row(
                    FIELD_LABELS.get(spec.field, spec.field + ":"), field_entry
                )
            )

        # Additional per-node settings rows (spec.settings): advanced
        # properties that don't deserve an inline field on the node body
        # but should still be editable from this dialog - currently the
        # echo-cancel module options (library/aec.args/monitor.mode) and
        # NoiseCancelNode's VAD dial. Each entry is a 3-tuple
        # (attr, label, kind) or a 4-tuple with an `extra` dict for the
        # kinds that need bounds/choices; the response handler sends the
        # matching set_node_property only when its value changed.
        param_widgets = []
        if spec.settings:
            for row in spec.settings:
                attr, label, kind = row[0], row[1], row[2]
                extra = dict(row[3]) if len(row) > 3 else {}
                # Every settings row gets a hover description: the spec's
                # per-attr text when it has one, else the row label.
                tip = setting_tooltip(node["type"], attr, label)
                if kind == "bool":
                    widget = Gtk.CheckButton(label=label)
                    widget.set_active(bool(node.get("meta", {}).get(attr, False)))
                    content.append(widget)
                elif kind == "number":
                    lo = extra.get("min", 0.0)
                    hi = extra.get("max", 100.0)
                    step = extra.get("step", 1.0)
                    widget = Gtk.SpinButton.new_with_range(lo, hi, step)
                    try:
                        current = float(node.get("meta", {}).get(attr, 0.0) or 0.0)
                    except (TypeError, ValueError):
                        current = 0.0
                    widget.set_value(current)
                    widget.set_hexpand(True)
                    content.append(self._labeled_row(label, widget))
                elif kind == "choice":
                    choices = extra.get("choices", [])
                    values = [v for _l, v in choices]
                    labels = [l for l, _v in choices]
                    widget = Gtk.DropDown.new_from_strings(labels)
                    current = node.get("meta", {}).get(attr, "")
                    try:
                        widget.set_selected(values.index(current))
                    except ValueError:
                        widget.set_selected(0)
                    widget.set_hexpand(True)
                    content.append(self._labeled_row(label, widget))
                else:  # text
                    widget = Gtk.Entry()
                    widget.set_text(node.get("meta", {}).get(attr, "") or "")
                    widget.set_hexpand(True)
                    content.append(self._labeled_row(label, widget))
                widget.set_tooltip_text(tip)
                param_widgets.append((attr, kind, widget, extra))

        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Apply", Gtk.ResponseType.APPLY)
        dialog.set_default_response(Gtk.ResponseType.APPLY)
        dialog.connect(
            "response",
            self._on_settings_response,
            node_id,
            spec,
            id_entry,
            id_error_label,
            label_entry,
            control_widget,
            field_entry,
            param_widgets,
        )
        dialog.connect("map", self._focus_and_select(label_entry))
        dialog.show()

    def _validate_new_node_id(self, old_id, new_id):
        """None if `new_id` is a valid rename target for `old_id`, an
        error message to show in the dialog otherwise."""
        if not new_id:
            return "Node ID can't be empty."
        if new_id == old_id:
            return None
        if new_id in self.nodes:
            return f"\u201c{new_id}\u201d is already in use."
        if is_mute_node(old_id) and not is_mute_node(new_id):
            # A mute switch is told apart from a plain volume slider
            # purely by its "mute_" id prefix (see
            # node_specs.is_mute_node) - losing the prefix would
            # silently turn it back into a slider.
            return "Mute switch IDs must start with \u201cmute_\u201d."
        return None

    def _rename_node_local(self, old_id, new_id):
        """Optimistically rename a node in the local model, the same
        pattern _on_settings_response already uses for label/volume/
        gate edits: update our own copy immediately so the UI reflects
        it without waiting on the next get_nodes poll, while the
        actual command (below) makes it durable on the daemon side."""
        node = self.nodes.pop(old_id)
        self.nodes[new_id] = node

        for edge in self.edges.values():
            if edge["from_node"] == old_id:
                edge["from_node"] = new_id
            if edge["to_node"] == old_id:
                edge["to_node"] = new_id

        vel = self.force_layout.velocities.pop(old_id, None)
        if vel is not None:
            self.force_layout.velocities[new_id] = vel

        if old_id in self.pinned_nodes:
            self.pinned_nodes.discard(old_id)
            self.pinned_nodes.add(new_id)

        if old_id in self.anchored_nodes:
            self.anchored_nodes.discard(old_id)
            self.anchored_nodes.add(new_id)

        if old_id in self.selected_nodes:
            self.selected_nodes.discard(old_id)
            self.selected_nodes.add(new_id)

        if old_id in self._user_created_nodes:
            self._user_created_nodes.discard(old_id)
            self._user_created_nodes.add(new_id)

        for group in self.groups.values():
            if old_id in group["nodes"]:
                group["nodes"].discard(old_id)
                group["nodes"].add(new_id)

        if self.dragging_node == old_id:
            self.dragging_node = new_id

        self._prev_node_ids.discard(old_id)
        self._prev_node_ids.add(new_id)

    def _on_settings_response(
        self,
        dialog,
        response,
        node_id,
        spec,
        id_entry,
        id_error_label,
        label_entry,
        control_widget,
        field_entry,
        param_widgets=None,
    ):
        if response == Gtk.ResponseType.APPLY:
            node = self.nodes.get(node_id)
            if not node:
                dialog.destroy()
                return

            # Node ID rename - validated and applied first, since
            # everything else below sends its command keyed on
            # whichever id is current by the time it runs.
            new_id = id_entry.get_text().strip()
            if new_id != node_id:
                error = self._validate_new_node_id(node_id, new_id)
                if error:
                    id_error_label.set_label(error)
                    id_error_label.set_visible(True)
                    return  # leave the dialog open so it can be fixed
                self._rename_node_local(node_id, new_id)
                self.client.send(
                    {
                        "command": "rename_node",
                        "old_node_id": node_id,
                        "new_node_id": new_id,
                    }
                )
                node_id = new_id
                node = self.nodes[node_id]
                self.queue_draw()

            # Label
            new_label = label_entry.get_text()
            if new_label != node.get("label", ""):
                node["label"] = new_label
                self._send_property(node_id, "label", new_label)

            # Gate or Volume
            if spec.control == "gate":
                if control_widget is not None:
                    new_en = control_widget.get_active()
                    if new_en != node.get("enabled", True):
                        node["enabled"] = new_en
                        self._send_set_gate(node_id, new_en)

            elif spec.control == "volume":
                if control_widget is not None:
                    if is_mute_node(node_id):
                        # Mute is a checkbox (on/off)
                        new_vol = 1.0 if control_widget.get_active() else 0.0
                        if new_vol != node.get("volume", 1.0):
                            node["volume"] = new_vol
                            self._send_set_volume(node_id, new_vol)
                    else:
                        # control_widget is a tuple (scale, min_spin, max_spin)
                        scale, min_spin, max_spin = control_widget
                        new_min = min_spin.get_value()
                        new_max = max_spin.get_value()
                        if new_min > new_max:
                            new_min, new_max = new_max, new_min

                        actual = scale.get_value()
                        actual = max(new_min, min(new_max, actual))
                        fraction = (
                            (actual - new_min) / (new_max - new_min)
                            if new_max != new_min
                            else 1.0
                        )

                        # Only update if changed
                        if (
                            new_min != node.get("volume_min", 0.0)
                            or new_max != node.get("volume_max", 1.0)
                            or fraction != node.get("volume", 1.0)
                        ):
                            node["volume"] = fraction
                            node["volume_min"] = new_min
                            node["volume_max"] = new_max
                            # Send range first (daemon will re‑apply volume)
                            self._send_volume_range(node_id, new_min, new_max)
                            self._send_set_volume(node_id, fraction)

            # Field (text, or the media_class dropdown's raw value)
            if spec.field and field_entry is not None:
                if spec.field == "media_class":
                    media_class_values = [
                        raw
                        for _label, raw in media_class_choices_for(node["type"])
                    ]
                    idx = field_entry.get_selected()
                    new_value = (
                        media_class_values[idx]
                        if 0 <= idx < len(media_class_values)
                        else node.get("meta", {}).get(spec.field, "")
                    )
                    self._send_property(node_id, spec.field, new_value)
                else:
                    self._send_property(node_id, spec.field, field_entry.get_text())

            # Extra per-node settings rows (spec.settings - e.g. the
            # echo-cancel module options or NoiseCancelNode's
            # method/dials). Each changed value goes out as a
            # set_node_property; the daemon recreates the backing for
            # load-time options so they reach the live graph. Update our
            # own meta copy so the dialog re-opening immediately reflects
            # the change even before the next poll.
            if param_widgets:
                meta = node.setdefault("meta", {})
                for attr, kind, widget, extra in param_widgets:
                    if kind == "bool":
                        new_value = bool(widget.get_active())
                        if bool(meta.get(attr, False)) != new_value:
                            meta[attr] = new_value
                            self._send_property(node_id, attr, new_value)
                    elif kind == "number":
                        new_value = widget.get_value()
                        try:
                            old_value = float(meta.get(attr, 0.0) or 0.0)
                        except (TypeError, ValueError):
                            old_value = 0.0
                        if new_value != old_value:
                            meta[attr] = new_value
                            self._send_property(node_id, attr, new_value)
                            if attr == "sensitivity":
                                # The inline slider draws from the node's
                                # top-level value, so mirror the Settings
                                # edit there too - otherwise the bar would
                                # keep showing the old position until the
                                # interior reload finishes and the next
                                # get_nodes poll lands.
                                node["sensitivity"] = new_value
                    elif kind == "choice":
                        choices = extra.get("choices", [])
                        values = [v for _l, v in choices]
                        idx = widget.get_selected()
                        new_value = (
                            values[idx]
                            if 0 <= idx < len(values)
                            else meta.get(attr, "")
                        )
                        if meta.get(attr, "") != new_value:
                            meta[attr] = new_value
                            self._send_property(node_id, attr, new_value)
                    else:  # text
                        new_value = widget.get_text()
                        if (meta.get(attr, "") or "") != new_value:
                            meta[attr] = new_value
                            self._send_property(node_id, attr, new_value)

            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        dialog.destroy()

    # ---------- right button: marquee select + context menu ----------

    def _notify_selection_changed(self):
        for cb in self.on_selection_changed:
            try:
                cb()
            except Exception:
                pass

    def _mark_layout_dirty(self):
        """Queue a debounced push of the canvas layout (positions +
        anchored flags) to the daemon, which persists it through
        export/import and the last-session cache.  Debounced so a drag,
        the physics settling, or a burst of anchor toggles all collapse
        into a single command."""
        if self._layout_save_source:
            GLib.source_remove(self._layout_save_source)
        self._layout_save_source = GLib.timeout_add(
            LAYOUT_SAVE_DEBOUNCE_MS, self._flush_layout_save
        )

    def _flush_layout_save(self):
        self._layout_save_source = 0
        if not self.nodes:
            return False
        # Panel placement first, then absolute node positions: the daemon
        # treats node positions as authoritative and does not move nodes
        # when a panel moves, so sending nodes last leaves the two
        # consistent even if an autosave lands between the commands.
        for pid, panel in self.panels.items():
            if pid == "":
                continue
            self.client.send(
                {
                    "command": "set_panel_layout",
                    "panel_id": pid,
                    "x": float(panel["x"]),
                    "y": float(panel["y"]),
                    "w": float(panel["w"]),
                    "h": float(panel["h"]),
                    "anchored": bool(panel.get("anchored", False)),
                }
            )
        layout = {
            nid: {
                "x": float(node["x"]),
                "y": float(node["y"]),
                "anchored": nid in self.anchored_nodes,
            }
            for nid, node in self.nodes.items()
        }
        self.client.send({"command": "set_node_layout", "layout": layout})
        return False

    def _nodes_in_rect(self, rect):
        """Ids of every node whose rectangle overlaps the selection
        box (x1, y1, x2, y2) in world coords."""
        x1, y1, x2, y2 = rect
        found = set()
        for nid, node in self.nodes.items():
            nx, ny = node["x"], node["y"]
            if (
                nx < x2
                and nx + self.node_width(nid) > x1
                and ny < y2
                and ny + self.node_height(nid) > y1
            ):
                found.add(nid)
        return found

    def _set_selection(self, ids):
        ids = set(ids)
        if ids == self.selected_nodes:
            return
        self.selected_nodes = ids
        self._notify_selection_changed()
        self.queue_draw()

    # ---------- node groups ----------

    @staticmethod
    def _hex_to_rgb(value):
        value = (value or "#3584e4").lstrip("#")
        if len(value) != 6:
            value = "3584e4"
        try:
            return tuple(int(value[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
        except ValueError:
            return (0.2, 0.5, 0.9)

    def _text_size(self, text, font_size, bold=False):
        layout = self.create_pango_layout(text or "")
        weight = "bold " if bold else ""
        layout.set_font_description(
            Pango.FontDescription.from_string(f"sans {weight}{font_size}")
        )
        return layout.get_pixel_size()

    @staticmethod
    def _group_merge_key(gid):
        """Groups that share a local id merge across namespace boundaries
        (an imperative ``grp`` and a declarative ``file::grp``, or the
        same id in two files): one outline, every contributing group's
        title stacked.  The namespace separator is ``::``."""
        return gid.split("::", 1)[1] if "::" in gid else gid

    def _merged_groups(self):
        """{merge_key: merged_group} for the current frame.

        A merged group unions the members of every same-local-id group and
        keeps their individual titles/colors (``titles``) plus the list of
        real group ids (``gids``).  ``id`` is the primary full id used for
        settings/actions.  Cached per frame alongside the other group
        geometry (cleared at the top of on_draw)."""
        cached = self._group_geo_cache.get("merged")
        if cached is not None:
            return cached
        merged: dict = {}
        for gid, group in self.groups.items():
            key = self._group_merge_key(gid)
            entry = merged.get(key)
            if entry is None:
                entry = {
                    "id": gid,
                    "key": key,
                    "label": group.get("label") or gid,
                    "color": group.get("color"),
                    "nodes": set(),
                    "gids": [],
                    "titles": [],
                }
                merged[key] = entry
            entry["gids"].append(gid)
            entry["titles"].append(
                {
                    "gid": gid,
                    "label": group.get("label") or gid,
                    "color": group.get("color") or self.DEFAULT_PANEL_COLOR,
                }
            )
            entry["nodes"] |= set(group.get("nodes", ()))
        self._group_geo_cache["merged"] = merged
        return merged

    def _raw_group_bounds(self, group):
        """Union of a group's member node rectangles, no padding, or None
        if it has no live members.  Memoised for the current frame (the
        group dicts are rebuilt on every daemon poll, so key on identity)."""
        key = ("raw", id(group))
        if key in self._group_geo_cache:
            return self._group_geo_cache[key]
        ids = [nid for nid in group.get("nodes", ()) if nid in self.nodes]
        if not ids:
            self._group_geo_cache[key] = None
            return None
        x1 = min(self.nodes[n]["x"] for n in ids)
        y1 = min(self.nodes[n]["y"] for n in ids)
        x2 = max(self.nodes[n]["x"] + self.node_width(n) for n in ids)
        y2 = max(self.nodes[n]["y"] + self.node_height(n) for n in ids)
        result = (x1, y1, x2, y2)
        self._group_geo_cache[key] = result
        return result

    def _group_title_block_height(self, group):
        """Height of a (possibly merged) group's stacked title block: every
        title's label line, the gaps between them, and one id line."""
        titles = group.get("titles") or [
            {"label": group.get("label") or group.get("id", "")}
        ]
        gap = 2
        label_h = sum(self._text_size(t["label"], 12)[1] for t in titles)
        _iw, ih = self._text_size(group.get("key", group.get("id", "")), 9)
        return label_h + gap * (len(titles) - 1) + gap + ih

    def _group_header_height(self, gid, group):
        """Vertical space a group's name block occupies *above* its
        dotted-box top: the 6px gap the header is drawn with, the stacked
        title block, and GROUP_NAME_BUMP of breathing room so a container's
        own edge can't touch the enclosed name."""
        return 6 + self._group_title_block_height(group) + self.GROUP_NAME_BUMP

    def _encloses(self, outer_raw, inner_raw):
        """Whether `outer_raw` (member bounds) contains `inner_raw`, within
        GROUP_ENCLOSE_TOLERANCE - see that constant for why the slack
        matters."""
        if outer_raw is None or inner_raw is None:
            return False
        ox1, oy1, ox2, oy2 = outer_raw
        ix1, iy1, ix2, iy2 = inner_raw
        t = self.GROUP_ENCLOSE_TOLERANCE
        return (
            ox1 - t <= ix1
            and oy1 - t <= iy1
            and ix2 <= ox2 + t
            and iy2 <= oy2 + t
        )

    def _enclosed_group_ids(self, gid):
        """Ids of the other groups whose member bounds this group fully
        encloses (its children for layout purposes).  Memoised per frame."""
        key = ("enc", gid)
        if key in self._group_geo_cache:
            return self._group_geo_cache[key]
        merged = self._merged_groups()
        own = self._raw_group_bounds(merged[gid])
        if own is None:
            self._group_geo_cache[key] = []
            return []
        result = [
            other_id
            for other_id, other in merged.items()
            if other_id != gid
            and self._encloses(own, self._raw_group_bounds(other))
        ]
        self._group_geo_cache[key] = result
        return result

    def _group_bounds(self, gid, group, _seen=None):
        """A group's dotted-box rectangle, or None if it has no live
        members.

        The box is GROUP_PADDING around the group's own member nodes, then
        grown by GROUP_SPACING (on every side) around each group it
        encloses - and, on top only, by that child's whole *name block*
        (see _group_header_height), so a nested group's label/id/chip sit
        inside its container instead of poking out over its top edge.
        Children are resolved recursively, so the clearance compounds
        through any depth of nesting."""
        top = _seen is None
        if top:
            key = ("bounds", gid)
            if key in self._group_geo_cache:
                return self._group_geo_cache[key]
        raw = self._raw_group_bounds(group)
        if raw is None:
            if top:
                self._group_geo_cache[("bounds", gid)] = None
            return None
        # Guard against identical/mutually-containing member bounds, which
        # would otherwise recurse forever.
        seen = set(_seen or ())
        if gid in seen:
            return None
        seen.add(gid)

        p = self.GROUP_PADDING
        x1, y1, x2, y2 = raw
        x1 -= p
        y1 -= p
        x2 += p
        y2 += p
        for child_id in self._enclosed_group_ids(gid):
            child = self._merged_groups()[child_id]
            cb = self._group_bounds(child_id, child, seen)
            if cb is None:
                continue
            cx1, cy1, cx2, cy2 = cb
            head = self._group_header_height(child_id, child)
            x1 = min(x1, cx1 - self.GROUP_SPACING)
            y1 = min(y1, cy1 - head - self.GROUP_SPACING)
            x2 = max(x2, cx2 + self.GROUP_SPACING)
            y2 = max(y2, cy2 + self.GROUP_SPACING)
        result = (x1, y1, x2, y2)
        if top:
            self._group_geo_cache[("bounds", gid)] = result
        return result

    def _build_group_header(self, gid, group, top):
        """Geometry of one (possibly merged) group's stacked titles / id /
        color chip / +/- block for a given `top` (or the naive top above
        its box when None).  Pure; the collision-resolved top comes from
        _all_group_header_layouts.

        A merged group draws every contributing group's title on its own
        line (each in that group's color), then a single id line."""
        bounds = self._group_bounds(gid, group)
        if bounds is None:
            return None
        x1, y1, x2, _y2 = bounds
        titles = group.get("titles") or [
            {"label": group.get("label") or gid, "color": group.get("color")}
        ]
        gap = 2
        line_sizes = [self._text_size(t["label"], 12) for t in titles]
        id_text = group.get("key", gid)
        iw, ih = self._text_size(id_text, 9)
        label_w = max([iw] + [w for w, _h in line_sizes])
        block_h = (
            sum(h for _w, h in line_sizes) + gap * (len(titles) - 1) + gap + ih
        )
        if top is None:
            top = y1 - 6 - block_h
        mid_y = top + block_h / 2
        chip = 12
        # Color chip, the +/- membership buttons and a settings hamburger
        # live on the *right* edge of the group's box, mirroring the panel
        # title row (the hamburger at the very right).
        btn = 15
        btn_y = mid_y - btn / 2
        menu_x = x2 - btn
        rem_x = menu_x - 3 - btn
        add_x = rem_x - 3 - btn
        chip_x = add_x - 8 - chip
        chip_y = mid_y - chip / 2
        add_rect = (add_x, btn_y, add_x + btn, btn_y + btn)
        rem_rect = (rem_x, btn_y, rem_x + btn, btn_y + btn)
        menu_rect = (menu_x, btn_y, menu_x + btn, btn_y + btn)
        return {
            "gid": gid,
            "primary_gid": group.get("id", gid),
            "x": x1,
            "top": top,
            "block_h": block_h,
            "titles": titles,
            "line_sizes": line_sizes,
            "id_text": id_text,
            "label": group.get("label") or gid,
            "color": group.get("color"),
            "lw": label_w,
            "lh": line_sizes[0][1] if line_sizes else 0,
            "iw": iw,
            "ih": ih,
            "gap": gap,
            "chip": chip,
            "chip_x": chip_x,
            "chip_y": chip_y,
            "add_rect": add_rect,
            "rem_rect": rem_rect,
            "menu_rect": menu_rect,
            # Clickable label/id hotspot (the title block on the left)...
            "rect": (
                x1 - 2, top - 2, x1 + label_w + 2, top + block_h + 2,
            ),
            # ...and the whole row (through the right-edge buttons), which
            # is what the collision pass keeps clear of other blocks.
            "extent": (x1 - 2, top - 2, x2 + 2, top + block_h + 2),
        }

    def _all_group_header_layouts(self):
        """{gid: header info} for every group, with colliding headers
        stacked so their names can never collapse together.

        Groups may overlap (share members) without one enclosing the
        other - a broad "Config" group and a narrower "Noise Cancel
        Config" sharing most of it, say - so their boxes' top-left corners,
        and therefore their naive header positions, can coincide and draw
        both names in the same spot.  Each colliding header is pushed up a
        block so they read as a stack above their boxes.  Cached per frame
        (cleared at the top of on_draw)."""
        cached = self._group_geo_cache.get("headers")
        if cached is not None:
            return cached
        naive = []
        for gid, group in self._merged_groups().items():
            info = self._build_group_header(gid, group, None)
            if info is not None:
                naive.append(info)
        naive.sort(key=lambda i: i["top"])
        placed: list = []
        for info in naive:
            top = info["top"]
            block_h = info["block_h"]
            left = info["extent"][0]
            right = info["extent"][2]
            bottom = top + block_h
            moved = True
            while moved:
                moved = False
                for p in placed:
                    pl, _pt, pr, pb = p["extent"]
                    if left < pr and pl < right and top < pb and p["top"] < bottom:
                        # 4px, not 0: each extent carries 2px of padding on
                        # each side, so clearing the *rects* needs 2+2.
                        top = p["top"] - block_h - 4
                        bottom = top + block_h
                        moved = True
            placed.append(
                self._build_group_header(
                    info["gid"], self._merged_groups()[info["gid"]], top
                )
            )
        result = {info["gid"]: info for info in placed}
        self._group_geo_cache["headers"] = result
        return result

    def _group_header_layout(self, gid, group):
        """Cached, collision-resolved header geometry for one group - one
        source of truth for both drawing and hit-testing."""
        return self._all_group_header_layouts().get(gid)

    def find_group_action_at(self, x, y):
        """(primary_group_id, "add"|"remove") for the +/- header buttons."""
        for gid, group in self._merged_groups().items():
            info = self._group_header_layout(gid, group)
            if info is None:
                continue
            for action, rect in (
                ("add", info["add_rect"]),
                ("remove", info["rem_rect"]),
            ):
                if rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]:
                    return (info["primary_gid"], action)
        return None

    def find_group_menu_at(self, x, y):
        """primary group id for the settings hamburger in the group row."""
        for gid, group in self._merged_groups().items():
            info = self._group_header_layout(gid, group)
            if info is None:
                continue
            r = info.get("menu_rect")
            if r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return info["primary_gid"]
        return None

    def find_group_label_at(self, x, y):
        for gid, group in self._merged_groups().items():
            info = self._group_header_layout(gid, group)
            if info is None:
                continue
            rx1, ry1, rx2, ry2 = info["rect"]
            if rx1 <= x <= rx2 and ry1 <= y <= ry2:
                return info["primary_gid"]
        return None

    # ---------- panels ----------

    PANEL_HEADER_H = 26
    PANEL_PADDING = 26
    # Panels auto-fit their contents in every direction (like groups) with
    # no manual resize; this is the minimum they shrink to, a square.
    PANEL_MIN_SIDE = 320.0
    # Panel IO: a thin bar straddling each edge, with a "+" at its foot.
    PANEL_IO_BAR_W = 16.0
    PANEL_IO_GAP = 8.0
    PANEL_IO_PORT_H = 44.0
    # Between-port gap: deliberately the same as the bar's overhang past
    # the top/bottom port, so the spacing reads evenly.
    PANEL_IO_BAR_PAD = 14.0
    PANEL_IO_PORT_GAP = PANEL_IO_BAR_PAD
    PANEL_IO_MARGIN = 20.0
    # While dragging a node, its panel may grow this far past the size it
    # had at drag start (it never shrinks during the drag).
    PANEL_DRAG_GROW = 280.0
    # How far past its *declared* placement a panel's box may auto-fit while
    # physics is settling its members (per side).  Node physics moves nodes,
    # and the box follows its contents, so without a cap a cloud of
    # unanchored nodes would grow its panel without bound; past this the
    # wall (PANEL_WALL_PULL) holds the members inside the box instead.
    PANEL_PHYSICS_GROW = 240.0
    # Fraction of a wall overshoot corrected per step.  Soft enough that a
    # spring pulling outward doesn't end up glued to the border, firm enough
    # that a node left far outside walks back in within a few frames.
    PANEL_WALL_PULL = 0.25

    @staticmethod
    def _panel_local(pid):
        return pid.rsplit("::", 1)[-1] if pid else "root"

    @staticmethod
    def _panel_of_node(nid):
        return nid.rsplit("::", 1)[0] if "::" in nid else ""

    @staticmethod
    def _panel_lca(a, b):
        if a == b:
            return a
        pa = a.split("::") if a else []
        pb = b.split("::") if b else []
        common = []
        for x, y in zip(pa, pb):
            if x != y:
                break
            common.append(x)
        return "::".join(common)

    def _panel_title_font(self):
        """World-unit font size for a panel's floating title.

        Drawing is scaled by the view zoom, so a fixed world size grows
        without bound on screen.  Clamp the *screen* size so the title
        scales with zoom up to a point and then stays legible and out of
        the way."""
        z = max(self.zoom, 1e-6)
        return min(14.0, 40.0 / z)

    def _panel_growth_limits(self, pid):
        """(max_w, max_h) the room a panel's *physics* may spread into, or
        (None, None) for a panel with no declared size.

        Derived from the panel's *placement* width/height - which the widget
        never rewrites (it only ever writes back x/y), so this is a stable
        reference even while the box moves - plus `PANEL_PHYSICS_GROW` per
        side.  `_wall_nodes_into_panels` is what enforces it, by pulling
        unanchored members back inside.

        This is deliberately *not* a cap on the drawn box (see
        `_panel_rect_base`): the box always has to enclose its contents, and
        the placement is only the size the panel was created with.  What it
        bounds is the unbounded part - a cloud of unanchored nodes settled by
        physics, which the wall keeps inside this room."""
        panel = self.panels.get(pid)
        if panel is None:
            return (None, None)
        w = float(panel.get("w") or 0.0)
        h = float(panel.get("h") or 0.0)
        if w <= 0.0 or h <= 0.0:
            return (None, None)
        return (
            w + 2.0 * self.PANEL_PHYSICS_GROW,
            h + 2.0 * self.PANEL_PHYSICS_GROW,
        )

    def _panel_rect_base(self, pid):
        """(x, y, w, h) absolute box for a panel from its contents only
        (nodes, child panels, ports, groups) - the auto-fit before routed
        wires are folded in.  See _panel_rect for the expanded box."""
        cached = self._panel_geo_cache.get(pid)
        if cached is not None:
            return cached
        panel = self.panels.get(pid)
        if panel is None:
            return None
        ax, ay = self._panel_absolute(pid)
        pad = self.PANEL_PADDING
        side = self.PANEL_MIN_SIDE
        # While a node is being dragged, the panel is held at the size it
        # had when the drag started (baseline) and may only *grow* toward
        # the dragged node, and only up to PANEL_DRAG_GROW past the
        # baseline.  That's the "pick a node up and the panel keeps its
        # size; nudge the edge to make room; drag well past it to leave"
        # behaviour, and it stops the box from shrinking under the node
        # (which used to drop nodes out of their own panel).
        baseline = self._panel_drag_baseline.get(pid) if self.dragging_node else None
        dragging = set(self.drag_node_starts) if self.dragging_node else set()
        minx = miny = maxx = maxy = None
        for nid in self._panel_direct_nodes(pid):
            # Without a baseline (normal draw) the actively-dragged nodes
            # don't count, so a panel doesn't stretch to swallow a drop.
            if baseline is None and nid in dragging:
                continue
            node = self.nodes.get(nid)
            if node is None:
                continue
            # Panel ports straddle the edge (centered on the IO bar); they
            # must not push the box outward.
            if node.get("type") in self._PORT_IN_TYPES | self._PORT_OUT_TYPES:
                continue
            nx, ny = node["x"], node["y"]
            nr, nb = nx + self.node_width(nid), ny + self.node_height(nid)
            minx = nx if minx is None else min(minx, nx)
            miny = ny if miny is None else min(miny, ny)
            maxx = nr if maxx is None else max(maxx, nr)
            maxy = nb if maxy is None else max(maxy, nb)
        for child in panel.get("children", []):
            cr_rect = self._panel_rect_base(child)
            if cr_rect is None:
                continue
            cx, cy, cw, ch = cr_rect
            # No translation here: a panel's box is in *canvas* coordinates
            # (nodes carry absolute x/y, and `_draw_panel_boxes` draws every
            # rect raw), unlike `panel.x`/`panel.y`, which are
            # parent-relative and only folded back in `_panel_absolute` for
            # the empty-panel fallback below.
            #
            # The child's *box* right/bottom, remembered before the title
            # fold below shifts `cy` upward - otherwise `cy + ch` would
            # under-count the bottom by the title gap.
            right = cx + cw
            bottom = cy + ch
            # A child panel's title row (title + action buttons) floats
            # *above* its box, just like a group's title - fold it into the
            # parent's content bounds so the master panel grows to enclose
            # it instead of letting the title poke out of the top edge.
            child_hdr = self._panel_header_rects(child, cr_rect)
            cy = min(cy, child_hdr["header"][1])
            # A child's IO ports straddle its edge and are excluded from its
            # own auto-fit, so fold them (plus the bar overhang) into the
            # parent's bounds - otherwise the ports poke out of the master
            # panel.
            pad = self.PANEL_IO_BAR_PAD
            for cnid in self._panel_direct_nodes(child):
                cnode = self.nodes.get(cnid)
                if cnode is None or cnode.get("type") not in (
                    self._PORT_IN_TYPES | self._PORT_OUT_TYPES
                ):
                    continue
                cx = min(cx, cnode["x"] - pad)
                cy = min(cy, cnode["y"] - pad)
                right = max(
                    right, cnode["x"] + self.node_width(cnid) + pad
                )
                bottom = max(
                    bottom, cnode["y"] + self.node_height(cnid) + pad
                )
            minx = cx if minx is None else min(minx, cx)
            miny = cy if miny is None else min(miny, cy)
            maxx = right if maxx is None else max(maxx, right)
            maxy = bottom if maxy is None else max(maxy, bottom)
        # Groups owned by this panel (all members inside it) contribute
        # their outline *and* the title block above it, so the panel box
        # leaves room for a group's title and edges instead of clipping
        # them.
        for gid, group in self._merged_groups().items():
            members = [n for n in (group.get("nodes") or []) if n in self.nodes]
            if not members:
                continue
            owner = self._panel_of_node(members[0])
            for m in members[1:]:
                owner = self._panel_lca(owner, self._panel_of_node(m))
            if owner != pid:
                continue
            gb = self._group_bounds(gid, group)
            if gb is None:
                continue
            gx1, gy1, gx2, gy2 = gb
            info = self._group_header_layout(gid, group)
            gtop = info["top"] if info is not None else gy1
            minx = gx1 if minx is None else min(minx, gx1)
            miny = gtop if miny is None else min(miny, gtop)
            maxx = gx2 if maxx is None else max(maxx, gx2)
            maxy = gy2 if maxy is None else max(maxy, gy2)
        if minx is None:
            base = baseline if baseline is not None else (ax, ay, side, side)
            port_in = self._panel_port_nodes(pid, "in")
            port_out = self._panel_port_nodes(pid, "out")
            if port_in or port_out:
                reserve = self.NODE_WIDTH / 2.0 + self.PANEL_IO_MARGIN
                bx, by, bw, bh = base
                left = bx - (reserve if port_in else 0.0)
                right = bx + bw + (reserve if port_out else 0.0)
                base = (left, by, right - left, bh)
            rect = base
        else:
            # Tight fit around the contents, like a group, with the square
            # minimum centred on the content when it's smaller.
            left = minx - pad
            top = miny - pad
            right = maxx + pad
            bottom = maxy + pad
            if baseline is not None:
                # Never shrink below the baseline; expand toward the
                # dragged node by at most PANEL_DRAG_GROW per side.
                bl, bt = baseline[0], baseline[1]
                br, bb = bl + baseline[2], bt + baseline[3]
                grow = self.PANEL_DRAG_GROW
                left = min(bl, max(left, bl - grow))
                top = min(bt, max(top, bt - grow))
                right = max(br, min(right, br + grow))
                bottom = max(bb, min(bottom, bb + grow))
            # Reserve room inside each edge for a port's inner half plus a
            # margin, so ports (centered on the edge) never overlap nodes.
            port_in = self._panel_port_nodes(pid, "in")
            port_out = self._panel_port_nodes(pid, "out")
            if port_in or port_out:
                reserve = self.NODE_WIDTH / 2.0 + self.PANEL_IO_MARGIN
                if port_in:
                    left = min(left, minx - reserve)
                if port_out:
                    right = max(right, maxx + reserve)
                tall = (
                    port_in if len(port_in) >= len(port_out) else port_out
                )
                heights = [self.node_height(nid) for nid, _n in tall]
                stack_h = (
                    sum(heights)
                    + self.PANEL_IO_PORT_GAP * max(0, len(heights) - 1)
                )
                center = (top + bottom) / 2.0
                top = min(top, center - stack_h / 2.0 - self.PANEL_IO_MARGIN)
                bottom = max(
                    bottom, center + stack_h / 2.0 + self.PANEL_IO_MARGIN
                )
            if right - left < side:
                grow = (side - (right - left)) / 2.0
                left -= grow
                right += grow
            if bottom - top < side:
                grow = (side - (bottom - top)) / 2.0
                top -= grow
                bottom += grow
            # No size cap here on purpose.  The declared placement is the
            # *room physics may spread into* (see `_panel_growth_limits`,
            # enforced by `_wall_nodes_into_panels`), not a limit on the
            # drawn box: it is a value the widget never rewrites, so it is
            # often just the size the panel was created with (420x260 for the
            # root, 320x320 for a daemon-made panel).  Clipping to it made a
            # panel under-fit its own contents - visible the moment a panel
            # had no IO ports to force the box out, and worst on a parent
            # whose child panels had been dragged apart, since children can't
            # be walled back in the way members can.
            rect = (left, top, right - left, bottom - top)
        self._panel_geo_cache[pid] = rect
        return rect

    def _panel_rect(self, pid):
        """The panel's drawn box: its content auto-fit (_panel_rect_base)
        grown to enclose the wires routed *inside* it this frame
        (``_wire_bounds``, set in on_draw) so a detour never spills out of
        its panel.  Falls back to the base rect when there are no wires."""
        cached = self._wire_panel_cache.get(pid)
        if cached is not None:
            return cached
        base = self._panel_rect_base(pid)
        if base is None:
            return None
        bounds = self._wire_bounds.get(pid)
        if bounds is None:
            rect = base
        else:
            bx1, by1, bx2, by2 = bounds
            pad = WIRE_PAD + 4
            x1 = min(base[0], bx1 - pad)
            y1 = min(base[1], by1 - pad)
            x2 = max(base[0] + base[2], bx2 + pad)
            y2 = max(base[1] + base[3], by2 + pad)
            side = self.PANEL_MIN_SIDE
            if x2 - x1 < side:
                grow = (side - (x2 - x1)) / 2.0
                x1 -= grow
                x2 += grow
            if y2 - y1 < side:
                grow = (side - (y2 - y1)) / 2.0
                y1 -= grow
                y2 += grow
            rect = (x1, y1, x2 - x1, y2 - y1)
        self._wire_panel_cache[pid] = rect
        return rect

    def _panel_header_rects(self, pid, rect):
        """Hit/draw geometry for a panel's title row, which sits just above
        the box (like a group's title).  The title is on the left; the
        action buttons are on the right, the hamburger menu at the very
        edge, with the delete (or Reset on read-only) button and then the
        physics-stop toggle to its left.  Buttons are rounded squares that
        scale with the title font."""
        x, y, w, h = rect
        panel = self.panels[pid]
        font = self._panel_title_font()
        label = panel.get("label") or self._panel_local(pid)
        _tw, th = self._text_size(label, font, bold=True)
        d = max(18.0, th * 1.6)
        # Sit the title/buttons a little further above the box so the row
        # clears the border and the buttons don't crowd the top edge.
        gap = max(18.0, font * 1.2)
        top = y - th - gap
        right_edge = x + w
        menu = (right_edge - d, top, right_edge, top + d)
        close = (menu[0] - gap - d, top, menu[0] - gap, top + d)
        reset = None
        edit = None
        btn_left = close[0]
        if panel.get("readonly"):
            reset = (btn_left - gap - d, top, btn_left - gap, top + d)
            btn_left = reset[0]
        elif panel.get("writable"):
            edit = (btn_left - gap - d, top, btn_left - gap, top + d)
            btn_left = edit[0]
        anchor = (btn_left - gap - d, top, btn_left - gap, top + d)
        title = (x, top, max(x, anchor[0] - gap), top + th)
        header = (x, top, right_edge, top + d)
        return {
            "header": header, "reset": reset,
            "anchor": anchor, "menu": menu, "close": close, "edit": edit,
            "title": title,
        }

    _PORT_IN_TYPES = frozenset({"panel_in", "bool_panel_in"})
    _PORT_OUT_TYPES = frozenset({"panel_out", "bool_panel_out"})

    def _panel_port_nodes(self, pid, direction):
        """[(nid, node)] for a panel's *own* input (direction "in") or
        output ("out") port nodes, top-to-bottom.  Descendant panels'
        ports are excluded - each belongs to its own panel's bar."""
        types = self._PORT_IN_TYPES if direction == "in" else self._PORT_OUT_TYPES
        found = []
        for nid in self._panel_direct_nodes(pid):
            node = self.nodes.get(nid)
            if node is not None and node.get("type") in types:
                found.append((nid, node))
        found.sort(key=lambda t: t[1].get("y") or 0.0)
        return found

    def _panel_io_rects(self, pid, rect):
        """Bar + "+" rectangles for a panel's left (inputs) and right
        (outputs) edge."""
        x, y, w, h = rect
        bar_w = self.PANEL_IO_BAR_W
        gap = self.PANEL_IO_GAP
        pad = self.PANEL_IO_BAR_PAD

        def one(edge_x, direction):
            ports = self._panel_port_nodes(pid, direction)
            if ports:
                top = min((n.get("y") or y) for _n, n in ports) - pad
                bottom = max(
                    (n.get("y") or y) + self.node_height(nid)
                    for nid, n in ports
                ) + pad
            else:
                top = y + h / 2.0 - bar_w
                bottom = y + h / 2.0
            bar = (edge_x - bar_w / 2.0, top, edge_x + bar_w / 2.0, bottom)
            plus = (
                edge_x - bar_w / 2.0, bottom + gap,
                edge_x + bar_w / 2.0, bottom + gap + bar_w,
            )
            return bar, plus

        in_bar, in_plus = one(x, "in")
        out_bar, out_plus = one(x + w, "out")
        return {
            "in_bar": in_bar, "in_plus": in_plus,
            "out_bar": out_bar, "out_plus": out_plus,
        }

    def _layout_panel_ports(self):
        """Center each panel's port nodes on its edges (vertically stacked
        around the box's middle) and persist any that moved.  Called after
        polls/layout so ports follow the panel as it grows with content.
        Stacked by each port's real height with a PANEL_IO_PORT_GAP gap."""
        if self.dragging_port is not None:
            # A port is mid-reorder; don't fight the pointer.
            return
        gap = self.PANEL_IO_PORT_GAP
        moved = False
        for pid in list(self.panels):
            if pid == "":
                continue
            # Content-only box: ports must not chase the wire-expanded rect
            # (that would move their sockets, changing the wires, changing
            # the box - a feedback loop that never settles).
            rect = self._panel_rect_base(pid)
            if rect is None:
                continue
            center_y = rect[1] + rect[3] / 2.0
            for direction in ("in", "out"):
                ports = self._panel_port_nodes(pid, direction)
                if not ports:
                    continue
                heights = [self.node_height(nid) for nid, _n in ports]
                total = sum(heights) + gap * (len(ports) - 1)
                y = center_y - total / 2.0
                edge = rect[0] if direction == "in" else rect[0] + rect[2]
                for (nid, node), h in zip(ports, heights):
                    x = edge - self.node_width(nid) / 2.0
                    if (
                        abs((node.get("x") or 0.0) - x) > 0.5
                        or abs((node.get("y") or 0.0) - y) > 0.5
                    ):
                        node["x"] = x
                        node["y"] = y
                        moved = True
                    y += h + gap
        if moved:
            self._mark_layout_dirty()

    def find_panel_io_plus_at(self, x, y):
        for pid in self._panel_order_top_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            io = self._panel_io_rects(pid, self._panel_rect_base(pid))
            for direction, key in (("in", "in_plus"), ("out", "out_plus")):
                x1, y1, x2, y2 = io[key]
                if x1 - 2 <= x <= x2 + 2 and y1 - 2 <= y <= y2 + 2:
                    return pid, direction
        return None

    def _prompt_add_port(self, pid, direction):
        dialog = Gtk.Dialog(
            title="Add Panel Port", transient_for=self.get_root(), modal=True
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Add", Gtk.ResponseType.OK)
        box = dialog.get_content_area()
        box.set_spacing(6)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(12)
        box.append(Gtk.Label(label="Name"))
        name_entry = Gtk.Entry()
        name_entry.set_text("input" if direction == "in" else "output")
        box.append(name_entry)
        kind = Gtk.DropDown.new_from_strings(["Audio", "Boolean"])
        box.append(self._labeled_row("Type:", kind))

        def on_response(dlg, response):
            if response == Gtk.ResponseType.OK:
                name = name_entry.get_text().strip() or (
                    "input" if direction == "in" else "output"
                )
                self._add_panel_port(pid, direction, kind.get_selected() == 1, name)
            dlg.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _add_panel_port(self, pid, direction, boolean, name):
        node_type = (
            "bool_panel_in" if (boolean and direction == "in")
            else "bool_panel_out" if boolean
            else "panel_in" if direction == "in"
            else "panel_out"
        )
        local = "".join(
            c if (c.isalnum() or c in "-_.") else "_" for c in name
        ) or node_type
        nid = f"{pid}::{local}" if pid else local
        base = nid
        n = 2
        while nid in self.nodes:
            nid = f"{base}_{n}"
            n += 1
        rect = self._panel_rect(pid)
        if rect is None:
            return
        # Labelled port nodes render at the normal node width; center on the
        # edge.  _layout_panel_ports re-centers the whole stack on the next
        # poll (once the daemon reports the new node back).
        width = self.NODE_WIDTH
        x = (
            rect[0] - width / 2.0
            if direction == "in"
            else rect[0] + rect[2] - width / 2.0
        )
        y = rect[1] + rect[3] / 2.0 - self.PANEL_IO_PORT_H / 2.0
        self.client.send(
            {
                "command": "add_node",
                "node_type": node_type,
                "node_id": nid,
                "config": {
                    "port_name": name, "label": name, "anchored": True,
                    "x": x, "y": y,
                },
            }
        )
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def _draw_panel_grid(self, cr, x, y, w, h, rgb):
        spacing = 40
        cr.set_source_rgba(rgb[0], rgb[1], rgb[2], 0.35)
        cr.set_line_width(1.0 / max(self.zoom, 1e-6))
        gx = math.ceil(x / spacing) * spacing
        while gx <= x + w:
            cr.move_to(gx, y)
            cr.line_to(gx, y + h)
            gx += spacing
        gy = math.ceil(y / spacing) * spacing
        while gy <= y + h:
            cr.move_to(x, gy)
            cr.line_to(x + w, gy)
            gy += spacing
        cr.stroke()

    def _draw_panel_boxes(self, cr, pal):
        # Bottom to top: ancestors first, then nested panels on top of
        # them; later placements of the same depth draw last.  Hit tests
        # walk the reverse (see _panel_order_top_first).
        for pid in self._panel_paint_order():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            if not self._rect_visible(
                rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]
            ):
                continue
            x, y, w, h = rect
            panel = self.panels[pid]
            r, g, b = self._panel_rgb(pid)
            cr.save()
            draw_rounded_rect(cr, x, y, w, h, 12)
            cr.clip()
            # Backing at the *canvas* opacity (not fully opaque), so a panel
            # is exactly as see-through as the grid behind it - like the
            # nodes - and the color tint on top is only for color-coding.
            backing = max(0.0, min(1.0, constants.CANVAS_BG_ALPHA))
            if backing < 1.0:
                cr.set_source_rgba(*pal["node_bg"], backing)
            else:
                cr.set_source_rgb(*pal["node_bg"])
            cr.rectangle(x, y, w, h)
            cr.fill()
            cr.set_source_rgba(r, g, b, 0.10)
            cr.rectangle(x, y, w, h)
            cr.fill()
            self._draw_panel_grid(cr, x, y, w, h, (r, g, b))
            cr.restore()
            cr.set_source_rgb(r, g, b)
            cr.set_line_width(2.0)
            if panel.get("readonly"):
                cr.set_dash([6.0, 4.0], 0.0)
            draw_rounded_rect(cr, x, y, w, h, 12)
            cr.stroke()
            cr.set_dash([])
            if panel.get("edit_mode"):
                # Darken the panel and stamp a big "EDIT MODE" water-mark
                # behind its contents, sized to (roughly) fill the box.
                cr.save()
                draw_rounded_rect(cr, x, y, w, h, 12)
                cr.clip()
                cr.set_source_rgba(0.0, 0.0, 0.0, 0.28)
                cr.rectangle(x, y, w, h)
                cr.fill()
                text = "EDIT MODE"
                cr.select_font_face("sans")
                size = max(10.0, h * 0.5)
                cr.set_font_size(size)
                ext = cr.text_extents(text)
                while ext.width > w * 0.92 and size > 8.0:
                    size *= 0.9
                    cr.set_font_size(size)
                    ext = cr.text_extents(text)
                cr.set_source_rgba(1.0, 1.0, 1.0, 0.16)
                cr.move_to(
                    x + (w - ext.width) / 2.0 - ext.x_bearing,
                    y + (h - ext.height) / 2.0 - ext.y_bearing,
                )
                cr.show_text(text)
                cr.restore()
            # IO bars straddling the left (inputs) and right (outputs) edges.
            io = self._panel_io_rects(pid, self._panel_rect_base(pid))
            cr.set_line_width(1.2)
            for key in ("in_bar", "out_bar"):
                bx1, by1, bx2, by2 = io[key]
                draw_rounded_rect(cr, bx1, by1, bx2 - bx1, by2 - by1, 4)
                # Same opacity as the panel body, then the color wash.
                backing = max(0.0, min(1.0, constants.CANVAS_BG_ALPHA))
                if backing < 1.0:
                    cr.set_source_rgba(*pal["node_bg"], backing)
                else:
                    cr.set_source_rgb(*pal["node_bg"])
                cr.fill_preserve()
                cr.set_source_rgba(r, g, b, 0.30)
                cr.fill_preserve()
                cr.set_source_rgb(r, g, b)
                cr.stroke()

    def _draw_panel_headers(self, cr, pal):
        # Same paint order as the boxes so headers layer with their panel.
        for pid in self._panel_paint_order():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            if not self._rect_visible(
                rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3]
            ):
                continue
            _x, _y, _w, _h = rect
            panel = self.panels[pid]
            geo = self._panel_header_rects(pid, rect)
            r, g, b = self._panel_rgb(pid)
            font = self._panel_title_font()
            # Title, drawn like a group title (floating above the box, in
            # the panel's color, scaling with zoom up to a point).
            tx1, ty1, _tx2, _ty2 = geo["title"]
            draw_text_unbounded(
                cr, tx1, ty1,
                panel.get("label") or self._panel_local(pid),
                font, (r, g, b), bold=True,
            )
            # Physics-stop toggle, immediately left of the settings/reset
            # button at the far right.  Same rounded-square outline style
            # as the group +/- buttons; filled when the panel is pinned.
            self._draw_panel_button(
                cr, pal, geo["anchor"], (r, g, b),
                active=bool(panel.get("anchored")), glyph="pause",
            )
            if geo.get("edit") is not None:
                self._draw_panel_button(
                    cr, pal, geo["edit"], (r, g, b),
                    active=bool(panel.get("edit_mode")), glyph="pencil",
                )
            if geo.get("reset") is not None:
                self._draw_panel_button(
                    cr, pal, geo["reset"], (r, g, b),
                    active=False, glyph="reset",
                )
            # Remove this placement (X), then the hamburger menu.
            self._draw_panel_button(
                cr, pal, geo["close"], (r, g, b),
                active=False, glyph="close",
            )
            self._draw_panel_button(
                cr, pal, geo["menu"], (r, g, b),
                active=False, glyph="hamburger",
            )
            # IO "+" buttons at the foot of each edge bar.
            io = self._panel_io_rects(pid, self._panel_rect_base(pid))
            self._draw_panel_button(
                cr, pal, io["in_plus"], (r, g, b), active=False, glyph="plus",
            )
            self._draw_panel_button(
                cr, pal, io["out_plus"], (r, g, b), active=False, glyph="plus",
            )

    def _draw_panel_button(self, cr, pal, rect, color, active=False,
                           glyph="hamburger"):
        """One panel action button: a small rounded square with the same
        outline style as the group +/- buttons (filled node_bg + colored
        border, or filled with the color when active)."""
        x1, y1, x2, y2 = rect
        size = x2 - x1
        draw_rounded_rect(cr, x1, y1, size, y2 - y1, 3)
        if active:
            cr.set_source_rgb(*color)
            cr.fill()
        else:
            cr.set_source_rgb(*pal["node_bg"])
            cr.fill_preserve()
            cr.set_source_rgb(*color)
            cr.set_line_width(1.0)
            cr.stroke()
        ink = (0.06, 0.06, 0.07) if active else color
        cr.set_source_rgb(*ink)
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        # Two of these are real GTK symbolic icons rather than hand-drawn
        # glyphs: the arc-with-nub "reset" read as a crescent and the
        # parallelogram "pencil" as an angled rectangle, so use the theme's
        # own view-refresh / document-edit.  If the icon can't be rendered
        # (no display, theme missing it) the hand-drawn versions below still
        # draw, so a button is never blank.
        if glyph in ("reset", "pencil"):
            name = ("view-refresh-symbolic" if glyph == "reset"
                    else "document-edit-symbolic")
            pad = size * 0.14
            inner = size - 2 * pad
            if self._node_icon_pixbuf(name, inner, ink) is not None:
                self._draw_node_icon(cr, name, x1 + pad, y1 + pad, inner, ink)
                return
        if glyph == "pause":
            bar_w = max(1.5, size * 0.12)
            bar_h = size * 0.42
            cr.rectangle(cx - size * 0.16 - bar_w / 2, cy - bar_h / 2, bar_w, bar_h)
            cr.rectangle(cx + size * 0.16 - bar_w / 2, cy - bar_h / 2, bar_w, bar_h)
            cr.fill()
        elif glyph == "hamburger":
            cr.set_line_width(max(1.4, size * 0.10))
            for dy in (-1, 0, 1):
                cr.move_to(cx - size * 0.22, cy + dy * size * 0.18)
                cr.line_to(cx + size * 0.22, cy + dy * size * 0.18)
            cr.stroke()
        elif glyph == "trash":
            cr.set_line_width(max(1.4, size * 0.10))
            # Lid + handle.
            cr.move_to(cx - size * 0.22, cy - size * 0.18)
            cr.line_to(cx + size * 0.22, cy - size * 0.18)
            cr.move_to(cx - size * 0.07, cy - size * 0.18)
            cr.line_to(cx - size * 0.07, cy - size * 0.28)
            cr.line_to(cx + size * 0.07, cy - size * 0.28)
            cr.line_to(cx + size * 0.07, cy - size * 0.18)
            # Can body.
            cr.move_to(cx - size * 0.16, cy - size * 0.18)
            cr.line_to(cx - size * 0.12, cy + size * 0.26)
            cr.line_to(cx + size * 0.12, cy + size * 0.26)
            cr.line_to(cx + size * 0.16, cy - size * 0.18)
            cr.stroke()
        elif glyph == "plus":
            cr.set_line_width(max(1.4, size * 0.12))
            cr.move_to(cx - size * 0.20, cy)
            cr.line_to(cx + size * 0.20, cy)
            cr.move_to(cx, cy - size * 0.20)
            cr.line_to(cx, cy + size * 0.20)
            cr.stroke()
        elif glyph == "close":
            cr.set_line_width(max(1.4, size * 0.12))
            cr.move_to(cx - size * 0.20, cy - size * 0.20)
            cr.line_to(cx + size * 0.20, cy + size * 0.20)
            cr.move_to(cx + size * 0.20, cy - size * 0.20)
            cr.line_to(cx - size * 0.20, cy + size * 0.20)
            cr.stroke()
        elif glyph == "pencil":
            cr.set_line_width(max(1.4, size * 0.10))
            # A diagonal pencil body with a tip.
            cr.move_to(cx - size * 0.22, cy + size * 0.22)
            cr.line_to(cx + size * 0.16, cy - size * 0.24)
            cr.line_to(cx + size * 0.24, cy - size * 0.16)
            cr.line_to(cx - size * 0.14, cy + size * 0.28)
            cr.close_path()
            cr.stroke()
            cr.move_to(cx - size * 0.22, cy + size * 0.22)
            cr.line_to(cx - size * 0.14, cy + size * 0.28)
            cr.stroke()
        else:  # reset - a circular arrow
            cr.set_line_width(max(1.4, size * 0.10))
            rad = size * 0.22
            cr.arc(cx, cy, rad, -1.0, 2.3)
            cr.stroke()
            hx = cx + rad * math.cos(2.3)
            hy = cy + rad * math.sin(2.3)
            cr.move_to(hx - 2, hy - 1)
            cr.line_to(hx + 1, hy - 2)
            cr.line_to(hx + 1, hy + 1)
            cr.close_path()
            cr.fill()

    def _draw_group_boxes(self, cr, pal):
        # Draw enclosing groups first (most nested children first), so a
        # nested group's box ends up on top of its container's.
        ordered = sorted(
            self._merged_groups().items(),
            key=lambda kv: len(self._enclosed_group_ids(kv[0])),
            reverse=True,
        )
        for gid, group in ordered:
            bounds = self._group_bounds(gid, group)
            if bounds is None:
                continue
            x1, y1, x2, y2 = bounds
            if not self._rect_visible(x1, y1, x2, y2):
                continue
            r, g, b = self._group_rgb(gid, group)
            cr.set_source_rgb(r, g, b)
            cr.set_line_width(1.5)
            cr.set_dash([2.0, 4.0], 0.0)
            draw_rounded_rect(cr, x1, y1, x2 - x1, y2 - y1, 12)
            cr.stroke()
        cr.set_dash([])

    def _draw_group_headers(self, cr, pal):
        for gid, group in self._merged_groups().items():
            bounds = self._group_bounds(gid, group)
            if bounds is not None and not self._rect_visible(*bounds):
                continue
            info = self._group_header_layout(gid, group)
            if info is None:
                continue
            # Stacked titles: each contributing group's label on its own
            # line, in that group's color (unbounded, so a title always
            # reads in full - see draw_text_unbounded).
            y = info["top"]
            for title, (_tw, th) in zip(info["titles"], info["line_sizes"]):
                r, g, b = self._hex_to_rgb(title.get("color"))
                draw_text_unbounded(
                    cr, info["x"], y, title["label"], 12, (r, g, b)
                )
                y += th + info["gap"]
            draw_text_unbounded(
                cr, info["x"], y, info["id_text"], 9, pal["subtext"]
            )

            r, g, b = self._group_rgb(gid, group)
            draw_rounded_rect(
                cr, info["chip_x"], info["chip_y"], info["chip"], info["chip"], 3
            )
            cr.set_source_rgb(r, g, b)
            cr.fill()

            self._draw_group_button(
                cr, pal, info["add_rect"], "+",
                (info["primary_gid"], "add"), (r, g, b),
            )
            self._draw_group_button(
                cr, pal, info["rem_rect"], "\u2212",
                (info["primary_gid"], "remove"), (r, g, b),
            )
            # Settings hamburger at the very right of the group row.
            self._draw_panel_button(
                cr, pal, info["menu_rect"], (r, g, b),
                active=False, glyph="hamburger",
            )

    def _draw_group_button(self, cr, pal, rect, symbol, mode, color):
        x1, y1, x2, y2 = rect
        armed = self._group_pick_mode == mode
        draw_rounded_rect(cr, x1, y1, x2 - x1, y2 - y1, 3)
        if armed:
            cr.set_source_rgb(*color)
            cr.fill()
        else:
            cr.set_source_rgb(*pal["node_bg"])
            cr.fill_preserve()
            cr.set_source_rgb(*color)
            cr.set_line_width(1)
            cr.stroke()
        # A plus / minus glyph centred in the button.
        fg = (0.06, 0.06, 0.07) if armed else color
        cr.set_source_rgb(*fg)
        cr.set_line_width(1.6)
        cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
        cr.move_to(cx - 4, cy)
        cr.line_to(cx + 4, cy)
        if symbol == "+":
            cr.move_to(cx, cy - 4)
            cr.line_to(cx, cy + 4)
        cr.stroke()

    def _new_group_id(self):
        base = f"group_{int(time.time() * 1000)}"
        gid = base
        n = 2
        while gid in self.groups:
            gid = f"{base}_{n}"
            n += 1
        return gid

    def _exact_duplicate_group(self, node_ids, exclude_gid=None):
        """The id of an existing group whose member set is *exactly*
        `node_ids`, or None.  Groups are allowed to overlap; only an
        identical membership set is disallowed, so this is an exact
        frozenset comparison, not a subset one."""
        wanted = frozenset(node_ids)
        for gid, group in self.groups.items():
            if gid == exclude_gid:
                continue
            if frozenset(group.get("nodes", ())) == wanted:
                return gid
        return None

    def create_group_from_selection(self):
        """Tool-panel action: wrap the selected nodes in a new group."""
        ids = [nid for nid in self.selected_nodes if nid in self.nodes]
        if not ids:
            return
        # No two groups may hold exactly the same nodes (overlap is fine).
        if self._exact_duplicate_group(ids) is not None:
            return
        gid = self._new_group_id()
        color = self.GROUP_COLORS[len(self.groups) % len(self.GROUP_COLORS)]
        label = "Group"
        # Nodes may belong to any number of groups - membership here is
        # additive, so other groups are left untouched.
        self.groups[gid] = {"label": label, "color": color, "nodes": set(ids)}
        self._pending_groups.add(gid)
        self.client.send(
            {"command": "add_group", "group_id": gid, "label": label,
             "color": color, "nodes": ids}
        )
        self._set_selection(())
        self.queue_draw()

    def _merged_member_gids(self, gid):
        """Every real group id that renders as one merged group with `gid`
        (same local id).  A +/- click applies to all of them so the merge
        stays coherent."""
        key = self._group_merge_key(gid)
        return [g for g in self.groups if self._group_merge_key(g) == key]

    def _add_node_to_group(self, gid, nid):
        if nid not in self.nodes:
            return
        for real_gid in self._merged_member_gids(gid):
            group = self.groups.get(real_gid)
            if group is None or nid in group["nodes"]:
                continue
            # Adding this node must not turn this group into a copy of
            # another (unless it is a merged sibling, whose duplicate is
            # fine).
            dup = self._exact_duplicate_group(
                group["nodes"] | {nid}, exclude_gid=real_gid
            )
            if dup is not None and self._group_merge_key(dup) != self._group_merge_key(real_gid):
                continue
            # Additive: a node can be in several groups at once.
            group["nodes"].add(nid)
            # Keep this group locally authoritative until the daemon echoes
            # the change, so a racing poll can't flicker membership back.
            self._pending_groups.add(real_gid)
            self.client.send(
                {"command": "set_group", "group_id": real_gid,
                 "nodes": sorted(group["nodes"])}
            )
        self.queue_draw()

    def _remove_node_from_group(self, gid, nid):
        for real_gid in self._merged_member_gids(gid):
            group = self.groups.get(real_gid)
            if group is None or nid not in group["nodes"]:
                continue
            # Removing this node must not leave this group identical to
            # another (merged siblings excepted).
            dup = self._exact_duplicate_group(
                group["nodes"] - {nid}, exclude_gid=real_gid
            )
            if dup is not None and self._group_merge_key(dup) != self._group_merge_key(real_gid):
                continue
            group["nodes"].discard(nid)
            self._pending_groups.add(real_gid)
            self.client.send(
                {"command": "set_group", "group_id": real_gid,
                 "nodes": sorted(group["nodes"])}
            )
        self.queue_draw()

    def show_group_settings_dialog(self, gid):
        group = self.groups.get(gid)
        if group is None:
            return

        dialog = Gtk.Dialog(
            title=f"Group \u2014 {gid}",
            transient_for=self.get_root(),
            modal=True,
        )
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)

        id_entry = Gtk.Entry()
        id_entry.set_text(gid)
        id_entry.set_hexpand(True)
        id_entry.set_tooltip_text("The group's unique id.")
        content.append(self._labeled_row("Group ID:", id_entry))

        label_entry = Gtk.Entry()
        label_entry.set_text(group.get("label", ""))
        label_entry.set_hexpand(True)
        label_entry.set_tooltip_text("The title shown above the group box.")
        content.append(self._labeled_row("Label:", label_entry))

        # A self-drawn HSV picker (see color_picker.py) rather than
        # Gtk.ColorButton, which aborts on systems with no GSettings
        # schemas.
        group_key = group.get("label") or gid or "group"
        color_picker = ColorPicker(
            group.get("color") or self.DEFAULT_PANEL_COLOR,
            presets=self._group_colors(),
            resolve=lambda v: self.resolve_color(v, group_key),
        )
        color_picker.set_tooltip_text("The group's outline / title color.")
        content.append(self._labeled_row("Color:", color_picker))

        dialog.add_button("Delete", Gtk.ResponseType.REJECT)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Apply", Gtk.ResponseType.APPLY)

        def on_response(dlg, response):
            if response == Gtk.ResponseType.APPLY:
                new_id = id_entry.get_text().strip()
                if not new_id or (new_id != gid and new_id in self.groups):
                    return  # empty or duplicate id - leave dialog open
                new_color = color_picker.get_value()
                self._apply_group_edit(
                    gid, new_id, label_entry.get_text(), new_color
                )
            elif response == Gtk.ResponseType.REJECT:
                self.groups.pop(gid, None)
                self._pending_groups.discard(gid)
                self.client.send({"command": "remove_group", "group_id": gid})
                self.queue_draw()
            dlg.destroy()

        dialog.connect("response", on_response)
        dialog.connect("map", self._focus_and_select(label_entry))
        dialog.present()

    def _apply_group_edit(self, gid, new_id, label, color):
        group = self.groups.pop(gid, None)
        if group is None:
            return
        group["label"] = label
        group["color"] = color
        if new_id in self.groups:
            self.groups[gid] = group  # duplicate guard; don't clobber
            return
        self.groups[new_id] = group
        # Keep both ids locally authoritative until the daemon echoes the
        # rename: it still reports the old id for a poll or two, and we
        # must neither flicker back to the old label nor resurrect the old
        # group (see update_from_daemon's pending-group handling).
        self._pending_groups.add(new_id)
        if new_id != gid:
            self._pending_groups.add(gid)
        self.client.send(
            {"command": "set_group", "group_id": gid, "new_group_id": new_id,
             "label": label, "color": color, "nodes": sorted(group["nodes"])}
        )
        self.queue_draw()

    def on_right_drag_begin(self, gesture, start_x, start_y):
        self.dismiss_context_popover()
        # A right press *during* a left drag cancels that drag and swallows
        # the click (no marquee, no menu): a held node snaps back to where
        # it was picked up, and a wire being dragged is just put back.
        cancelled = False
        if self.dragging_node is not None and self.drag_node_starts:
            for nid, (sx, sy) in self.drag_node_starts.items():
                node = self.nodes.get(nid)
                if node is not None:
                    node["x"] = sx
                    node["y"] = sy
            cancelled = True
        if self.connecting_from is not None or self.detaching_edge is not None:
            cancelled = True
        if not cancelled:
            # No in-progress left drag to cancel: release any lingering
            # gesture so a previous press can't fight the marquee/menu.
            # (When we *are* cancelling, leave the left gesture alone - it
            # is still holding the button, and resetting it from the right
            # gesture's callback can wedge GTK's implicit grab, after which
            # the canvas stops receiving clicks.)
            self._drag_gesture.reset()
        self._reset_drag_state()
        self._right_drag_cancelled = cancelled
        if cancelled:
            self._panel_geo_cache.clear()
            self.queue_draw()
            return
        self._right_drag_start_widget = (start_x, start_y)
        self._right_drag_start_world = self.to_world(start_x, start_y)
        self._right_drag_moved = False
        try:
            self._right_drag_mods = gesture.get_current_event_state()
        except Exception:
            self._right_drag_mods = Gdk.ModifierType(0)
        self._right_marquee_base = set(self.selected_nodes)
        self.select_rect = None

    def on_right_drag_update(self, gesture, offset_x, offset_y):
        if getattr(self, "_right_drag_cancelled", False):
            return
        # Small dead-zone so a click with a pixel of jitter still opens
        # the menu instead of drawing a 1x1 selection box.
        if offset_x * offset_x + offset_y * offset_y < 25.0:
            return
        self._right_drag_moved = True
        sx, sy = self._right_drag_start_world
        ex = sx + offset_x / self.zoom
        ey = sy + offset_y / self.zoom
        self.select_rect = (min(sx, ex), min(sy, ey), max(sx, ex), max(sy, ey))
        swept = self._nodes_in_rect(self.select_rect)
        # Modifier marquee: Shift adds the swept nodes to the selection at
        # drag start, Ctrl removes them; a bare right-drag replaces.
        mods = getattr(self, "_right_drag_mods", Gdk.ModifierType(0))
        if mods & Gdk.ModifierType.SHIFT_MASK:
            self._set_selection(self._right_marquee_base | swept)
        elif mods & Gdk.ModifierType.CONTROL_MASK:
            self._set_selection(self._right_marquee_base - swept)
        else:
            self._set_selection(swept)
        # _set_selection only redraws when the set changed; the marquee
        # rectangle itself moves on every update.
        self.queue_draw()

    def on_right_drag_end(self, gesture, offset_x, offset_y):
        if getattr(self, "_right_drag_cancelled", False):
            self._right_drag_cancelled = False
            self.select_rect = None
            self.queue_draw()
            return
        moved = self._right_drag_moved
        self._right_drag_moved = False
        self.select_rect = None
        if moved:
            self.queue_draw()
            return
        # Shift/Ctrl + right-click toggles the node under the pointer in
        # the selection instead of opening the menu.
        mods = getattr(self, "_right_drag_mods", Gdk.ModifierType(0))
        add_sel = bool(mods & Gdk.ModifierType.SHIFT_MASK)
        rem_sel = bool(mods & Gdk.ModifierType.CONTROL_MASK)
        if add_sel or rem_sel:
            wx, wy = self._right_drag_start_world
            nid = self.find_node_at(wx, wy)
            if nid is not None:
                if add_sel:
                    self._set_selection(self.selected_nodes | {nid})
                else:
                    self._set_selection(self.selected_nodes - {nid})
                self.queue_draw()
                return
        # No movement -> it was a plain right-click; open the menu.
        x, y = self._right_drag_start_widget
        self._open_context_menu(x, y)

    def on_right_drag_cancel(self, gesture, sequence):
        self._right_drag_cancelled = False
        self._right_drag_moved = False
        self.select_rect = None
        self.queue_draw()

    def _open_context_menu(self, x, y):
        self.grab_focus()
        wx, wy = self.to_world(x, y)

        eid = self.find_edge_at(wx, wy)
        if eid:
            self._retire_edge(eid)
            self.client.send({"command": "remove_edge", "edge_id": eid})
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            return

        nid = self.find_node_at(wx, wy, require_ready=False)
        if nid:
            self.show_node_menu(nid, x, y)
            return

        self.show_add_node_menu(x, y)

    # ---------- anchoring ----------

    def _is_anchored(self, nid) -> bool:
        return nid in self.anchored_nodes

    def toggle_node_anchor(self, nid, anchored=None):
        """Flip (or set) one node's anchored state.  Anchored means the
        force layout treats it as pinned: it never moves under physics,
        but still repels/attracts its neighbours, and the user can
        still drag it directly."""
        if nid not in self.nodes:
            return
        want = (nid not in self.anchored_nodes) if anchored is None else bool(anchored)
        if want == (nid in self.anchored_nodes):
            return
        if want:
            self.anchored_nodes.add(nid)
        else:
            self.anchored_nodes.discard(nid)
            # Let physics pick it back up from wherever it currently is.
            self.force_layout.velocities[nid] = [0.0, 0.0]
        self.layout_awake = True
        self._settle_ticks = 0
        self._mark_layout_dirty()
        self._notify_selection_changed()
        self.queue_draw()

    def toggle_anchor_selected(self):
        """Tool-panel action: anchor the selection, or unanchor it if
        every selected node is already anchored."""
        ids = [nid for nid in self.selected_nodes if nid in self.nodes]
        if not ids:
            return
        want = not all(nid in self.anchored_nodes for nid in ids)
        for nid in ids:
            self.toggle_node_anchor(nid, want)

    def show_add_node_menu(self, x, y):
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(6)
        box.set_margin_end(6)
        for label, ntype in ADD_NODE_MENU_ITEMS:
            btn = Gtk.Button()
            btn.set_has_frame(False)
            content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
            content.append(Gtk.Image.new_from_icon_name(icon_for_add_node_type(ntype)))
            row_label = Gtk.Label(label=label)
            row_label.set_halign(Gtk.Align.START)
            content.append(row_label)
            btn.set_child(content)
            desc = description_for(ntype)
            btn.set_tooltip_text(f"{label}\n{desc}" if desc else label)
            btn.connect("clicked", self._on_add_node, ntype, popover)
            box.append(btn)
        popover.set_child(box)
        self.popup_context_menu(popover, x, y)

    def _build_add_node_command(self, node_type):
        """Node-type-specific (real_type, node_id, config) for an
        add_node command. Shared by the right-click popover
        (_on_add_node) and the drag-and-drop side panel (add_node_at)
        so there's exactly one place that knows each type's default
        config, instead of the two staying in sync by hand."""
        # "Mute Switch" is a real "volume" node under the hood (so it
        # actually attenuates audio via the backing volume processor),
        # just presented with a checkbox instead of a slider - told
        # apart purely by its id prefix (see node_specs.is_mute_node),
        # no backend changes needed.
        is_mute = node_type == "mute"
        real_type = "volume" if is_mute else node_type
        # Every node, including Speaker/Mic Line, gets a unique id - any
        # number of lines may exist, all referencing the same built-in
        # virtual device.
        self._node_seq += 1
        node_id = (
            f"{'mute' if is_mute else 'node'}_{int(time.time() * 1000)}"
            f"_{self._node_seq}"
        )

        # Default label: reuses type_label()'s own mute-switch check
        # (is_mute_node(node_id), keyed off the "mute_" id prefix we
        # just chose above) so "Mute Switch" comes out right without
        # a second special case here, then de-duplicated against every
        # label currently on the canvas so a second Volume node reads
        # as "Volume 2" instead of an indistinguishable "Volume".
        config: dict = {}
        if real_type not in self._COMPACT_NODE_TYPES:
            # Compact nodes get no default label: a bare splitter is a
            # blank square, and the boolean gates already show their type
            # name at the top, so a default label would just duplicate it.
            # Everything else takes its plain type name (never "Volume 2",
            # "Gate 3", ...).
            config["label"] = type_label(real_type, node_id)
        if node_type in ("regex_input", "regex_output"):
            config["pattern"] = ".*"
        elif node_type == "media_class_input":
            config["media_class"] = "Audio/Sink"
        elif node_type == "media_class_output":
            config["media_class"] = "Stream/Input/Audio"
        elif node_type in ("description_input", "description_output"):
            config["description"] = "description"
        elif node_type == "regex_classifier":
            config["pattern"] = ".*"
        elif node_type == "media_class_classifier":
            # A classifier's side isn't known when it is created, so the
            # source-side default is just a starting point.
            config["media_class"] = "Stream/Output/Audio"
        elif node_type == "description_classifier":
            config["description"] = "description"
        elif node_type in ("volume", "mute"):
            config["backing_node_name"] = f"volume_{node_id}"
            config["initial_volume"] = 1.0
        elif node_type in (
            "echo_cancel",
            "light_noise_cancel",
            "noise_cancel",
            "reverb",
            "normalize",
        ):
            # No inline field/control for these (see NODE_TYPE_SPECS) -
            # just a real backing name, like volume/virtual_speaker/
            # virtual_mic above. Everything else (which LADSPA plugin
            # a noise_cancel/reverb node runs) is a daemon-side default
            # a user can override from this node's settings dialog if
            # it doesn't match what's installed on their system - see
            # NoiseCancelNode/ReverbNode's docstrings in patchSpace.py.
            config["backing_node_name"] = f"{node_type}_{node_id}"
        elif node_type == "sound_effect":
            # Start the path at the home directory (the daemon expands a
            # leading "~" at play time, and the field's folder button
            # stores what it picks in the same form), so the box reads as
            # a path you complete rather than an unexplained blank.
            config["path"] = "~/"
        elif node_type in ("device_input", "device_output"):
            config["device_name"] = ""
        elif node_type in ("app_input", "app_output"):
            config["app_name"] = ""
        elif node_type in ("virtual_speaker", "virtual_mic"):
            config["backing_node_name"] = f"{node_type}_{node_id}"
            config["device_label"] = (
                "Virtual Speaker" if node_type == "virtual_speaker" else "Virtual Mic"
            )
        elif node_type in ("patchspace_device", "patchspace_mic_device"):
            # No config beyond the generic (de-duplicated) label set
            # above: any number of Speaker/Mic Line nodes may exist and
            # they all reference the same built-in virtual device.
            pass
        # exclude_filter deliberately gets no default pattern here (it
        # falls through to config={}, i.e. an empty "pattern" once the
        # daemon fills in its default) - an empty pattern means
        # "excludes nothing yet" (see ExcludeFilterNode.exclude_filter
        # on the daemon side), so a freshly added filter node passes
        # its whole upstream chain through unchanged until the user
        # edits its regex, instead of defaulting to ".*" like
        # regex_input/regex_output above and silently blocking
        # everything downstream the moment it's added.

        return real_type, node_id, config

    def _on_add_node(self, button, node_type, popover):
        real_type, node_id, config = self._build_add_node_command(node_type)
        # Remember this as user-spawned so update_from_daemon anchors it
        # by default (loaded/builtin nodes are not anchored).
        self._user_created_nodes.add(node_id)
        view_w = self.get_width() or 800
        view_h = self.get_height() or 600
        cx, cy = self.to_world(view_w / 2.0, view_h / 2.0)
        self._add_placeholder_node(real_type, node_id, config, cx, cy)
        self.client.send(
            {
                "command": "add_node",
                "node_type": real_type,
                "node_id": node_id,
                "config": config,
            }
        )
        popover.popdown()
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def _add_placeholder_node(self, real_type, node_id, config, cx, cy):
        """Show a node the instant the user adds it, translucent and
        non-interactable, until the daemon reports it ``ready``.

        ``(cx, cy)`` is the world point the node should be centred on (the
        drop point, or the view centre for a menu add) - the real node size
        is used, so a tall node (Echo Cancel) lands under the cursor rather
        than offset by a guessed height.

        The daemon holds its ``add_node`` reply until a heavy node has
        actually spawned, so a ``get_nodes`` poll can't reveal the node
        before then - without this optimistic placeholder nothing at all
        appears until it is ready."""
        if node_id in self.nodes:
            return
        ntype = normalize_node_type(real_type)
        spec = spec_for(ntype)
        self.nodes[node_id] = {
            "type": ntype,
            "x": float(cx),
            "y": float(cy),
            "inputs": spec.inputs,
            "outputs": spec.outputs,
            "meta": dict(config),
            "label": config.get("label", ""),
            "enabled": True,
            "output": 0,
            "bool_driven": False,
            "bool_state": None,
            "volume": 1.0,
            "wet_dry": 0.3,
            "level": 25.0,
            "sensitivity": 0.0,
            "gain": 0.5,
            "device_volume": 1.0,
            "device_name": "",
            "app_name": "",
            "playing": 0,
            "overlap": False,
            "connected": False,
            "is_bluetooth": False,
            "selection_label": "",
            "ready": False,
            "health": "starting",
            "profile_index": None,
            "codec_label": "",
            "volume_locked": True,
            "force_default": True,
            "placeholder": True,
        }
        self._node_h_cache.pop(node_id, None)
        self._node_w_cache.pop(node_id, None)
        # Centre on the requested point using the node's real size once the
        # spec/label are known.
        self.nodes[node_id]["x"] = cx - self.node_width(node_id) / 2.0
        self.nodes[node_id]["y"] = cy - self.node_height(node_id) / 2.0
        # User-created nodes are pinned by default (same rule the poll's
        # new-node path applies; the placeholder pre-empts that branch).
        self.anchored_nodes.add(node_id)
        self._placeholder_since[node_id] = time.monotonic()
        self._mark_layout_dirty()
        self.queue_draw()

    def add_node_at(self, node_type, wx, wy):
        """Add a node of `node_type`, remembering (wx, wy) - world
        coordinates - as where it should land once the daemon reports
        it back (see _pending_positions / update_from_daemon). Used by
        the add-node side panel's drop handler."""
        real_type, node_id, config = self._build_add_node_command(node_type)
        self._user_created_nodes.add(node_id)
        self._add_placeholder_node(real_type, node_id, config, wx, wy)
        self.client.send(
            {
                "command": "add_node",
                "node_type": real_type,
                "node_id": node_id,
                "config": config,
            }
        )
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def _on_node_type_dropped(self, drop_target, value, x, y):
        wx, wy = self.to_world(x, y)
        if isinstance(value, str) and value.startswith("panel:"):
            self.place_panel_at(value[len("panel:"):], wx, wy)
            return True
        self.add_node_at(value, wx, wy)
        return True

    # ---------- placing panels (links) ----------

    def place_panel_at_view_center(self, stem):
        """Place a new placement of ``stem`` centered in the viewport, as a
        top-level panel (so it starts on the topmost layer)."""
        view_w = self.get_width() or 800
        view_h = self.get_height() or 600
        cx, cy = self.to_world(view_w / 2.0, view_h / 2.0)
        self._send_place_panel(stem, cx, cy, "")

    def place_panel_at(self, stem, wx, wy):
        """Place a placement centered on (wx, wy); nest into the panel under
        the drop, else the root."""
        parent = self.find_panel_at(wx, wy) or ""
        self._send_place_panel(stem, wx, wy, parent)

    def _send_place_panel(self, stem, cx, cy, parent_id):
        half = self.PANEL_MIN_SIDE / 2.0
        self.client.send(
            {
                "command": "place_panel",
                "stem": stem,
                "x": cx - half,
                "y": cy - half,
                "parent_id": parent_id,
            }
        )

    # ---------- add-node side panel (click or drag-and-drop source) ----------

    def _add_node_at_view_center(self, node_type):
        cx = (self.get_width() or 800) / 2
        cy = (self.get_height() or 600) / 2
        wx, wy = self.to_world(cx, cy)
        self.add_node_at(node_type, wx, wy)

    def _build_add_node_row(self, label, node_type):
        """One row in the add-node panel: a flat, full-width button
        that adds `node_type` at the view center on a plain click, and
        is also a drag source that places it wherever it's dropped on
        the canvas (see _on_node_type_dropped). A GtkDragSource only
        claims the pointer once the drag threshold is crossed, so the
        two don't fight each other - a click-without-moving still
        fires "clicked" normally."""
        row = Gtk.Button()
        row.set_has_frame(False)
        row.add_css_class("flat")

        content = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        content.append(Gtk.Image.new_from_icon_name(icon_for_add_node_type(node_type)))
        row_label = Gtk.Label(label=label)
        row_label.set_halign(Gtk.Align.START)
        row_label.set_hexpand(True)
        # Ellipsize only as a last resort, if the label genuinely
        # doesn't fit the panel's fixed width (see build_add_node_panel
        # - the ScrolledWindow's min/max content width is what actually
        # pins the panel's width now, not this). No max-width-chars
        # here: that caps the label's own size *request*, and with an
        # over-aggressive value every row showed nothing but "..." -
        # the tooltip below is just a hover-friendly backup, not a
        # substitute for the visible name.
        row_label.set_ellipsize(Pango.EllipsizeMode.END)
        desc = description_for(node_type)
        row.set_tooltip_text(f"{label}\n{desc}" if desc else label)
        content.append(row_label)
        row.set_child(content)

        row.connect("clicked", lambda _b: self._add_node_at_view_center(node_type))

        drag_source = Gtk.DragSource.new()
        drag_source.set_actions(Gdk.DragAction.COPY)
        drag_source.connect(
            "prepare",
            lambda source, x, y: Gdk.ContentProvider.new_for_value(
                GObject.Value(GObject.TYPE_STRING, node_type)
            ),
        )
        row.add_controller(drag_source)

        return row

    def build_add_node_panel(self):
        """The add-node side panel: every node type in one scrollable,
        collapsible-by-category list. Click a row to drop that node at
        the view's center, or drag it onto the canvas to place it
        exactly where you want it. This is an alternative to the
        right-click "add node" popover (show_add_node_menu), not a
        replacement for it - both end up at _build_add_node_command().

        Sized by the Gtk.Paned in main_window._build_patchspace_page,
        not by this widget itself: the panel just fills whatever width
        the paned handle gives it (ADD_NODE_PANEL_MIN_WIDTH is set as
        a floor via set_size_request so the paned's shrink-start-child
        =False can't squeeze it away entirely), and rows ellipsize if
        that width is ever too narrow for a long label.
        """
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        panel.add_css_class("side-panel-fill")
        panel.set_size_request(ADD_NODE_PANEL_MIN_WIDTH, -1)
        panel.set_hexpand(True)
        # Padding is CSS (.side-panel-fill), inside the opaque background;
        # GTK margins would leave a transparent gap at the window edge.
        panel.set_vexpand(True)

        title = Gtk.Label(label="Add Node")
        title.set_halign(Gtk.Align.START)
        title.add_css_class("heading")
        panel.append(title)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_hexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        categories_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        categories_box.set_margin_top(6)

        for category_name, items in ADD_NODE_CATEGORIES:
            header_label = Gtk.Label(label=f"{category_name} ({len(items)})")
            header_label.set_halign(Gtk.Align.START)
            header_label.set_ellipsize(Pango.EllipsizeMode.END)
            header_label.set_hexpand(True)

            expander = Gtk.Expander()
            expander.set_label_widget(header_label)
            expander.set_expanded(True)

            rows_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
            rows_box.set_margin_start(6)
            rows_box.set_margin_top(4)
            rows_box.set_margin_bottom(6)
            for label, node_type in items:
                rows_box.append(self._build_add_node_row(label, node_type))

            expander.set_child(rows_box)
            categories_box.append(expander)

        scroller.set_child(categories_box)
        panel.append(scroller)

        return panel
