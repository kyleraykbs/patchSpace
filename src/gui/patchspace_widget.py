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

import json
import logging
import math
import random
import time

from gi.repository import Gtk, Gdk, GLib, GObject, Pango

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
    ZOOM_MIN,
    ZOOM_MAX,
)
from render_utils import (
    theme_palette,
    theme_color,
    draw_rounded_rect,
    draw_text_ellipsized,
    draw_text_unbounded,
    draw_text_wrapped,
    wrapped_text_height,
    draw_bezier_link,
    draw_grid_background,
)
from force_layout import ForceLayout
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

logger = logging.getLogger(__name__)


class PatchSpaceGraphWidget(Gtk.DrawingArea, GraphViewMixin):
    NODE_WIDTH = 180
    NODE_HEIGHT = 80
    # Top padding before the first header line, and the gap between
    # each wrapped header line thereafter (type label, then
    # description/label/id - see _draw_header/_header_blocks).
    HEADER_TOP_PAD = 14
    HEADER_BLOCK_GAP = 4
    SLIDER_HEIGHT = 16
    SLIDER_MARGIN = 10
    FIELD_HEIGHT = 22
    FIELD_MARGIN = 10
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
    # A *compact* node (see _COMPACT_NODE_TYPES: splitter and the boolean
    # logic gates) with no label renders as a plain square this many
    # pixels on a side (just the three-dot menu and, dimmed, the anchor
    # badge), so it stays out of the way instead of occupying a full node.
    # Give it a label and it grows back out to NODE_WIDTH to wrap the text.
    SPLITTER_MIN_SIZE = 64
    # Padding a group's dotted box leaves around its member nodes, and
    # the default colour palette new groups cycle through.
    GROUP_PADDING = 26
    # Extra inset per group a group encloses, so a group that surrounds
    # other groups leaves a visible gap instead of drawing its dotted box
    # right on top of theirs.
    GROUP_SPACING = 18
    # Extra clearance a container leaves above an enclosed group's name
    # block (label/id/colour chip), on top of that block's own height, so
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
        self._right_marquee_base = set()
        # Panel-file dialogs: last listing, the open dialog, and the
        # "open the panel list once it arrives" handshake.
        self._pending_panel_list = False
        self._panel_files = []
        self._panel_dialog = None
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
        self.resizing_panel = None
        self.resize_start = (0.0, 0.0)
        self.resize_orig = (0.0, 0.0)
        self._panel_geo_cache: dict = {}
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
        # is just the gap between them.  The left-to-right flow bias is for
        # node chains, so it's off for boxes.
        self.panel_force_layout = ForceLayout(
            repulsion=200000,
            spring_length=120,
            flow_gap=0,
            flow_k=0.0,
            repulsion_cutoff=2000,
            size_aware_springs=True,
        )
        self.layout_awake = True
        self._settle_ticks = 0
        # Consecutive awake layout ticks since the last settle/sleep -
        # capped in on_layout_tick so a non-converging layout can't
        # spin the CPU forever (see that method).
        self._awake_ticks = 0
        self._prev_node_ids = set()
        self._prev_edge_set = set()
        self._prev_panel_ids = set()

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
            self._needs_initial_fit = False
            # Deferred: the console collapse above changes the canvas
            # height, and fitting before GTK re-lays-out would centre
            # against the old (shorter) canvas and leave the graph sitting
            # high once the console actually disappears.
            self._schedule_fit(80)

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
        view_w = self.get_width()
        view_h = self.get_height()
        if view_w <= 1 or view_h <= 1:
            # Called from inside on_draw, before GTK exposes the
            # allocation via get_width()/get_height() - use the size we
            # were just handed.
            view_w, view_h = getattr(self, "_last_view_size", (0, 0))
        if view_w <= 1 or view_h <= 1:
            return

        min_x = min(n["x"] for n in self.nodes.values())
        min_y = min(n["y"] for n in self.nodes.values())
        max_x = max(n["x"] + self.node_width(nid) for nid, n in self.nodes.items())
        max_y = max(n["y"] + self.node_height(nid) for nid, n in self.nodes.items())

        world_w = max(1.0, max_x - min_x)
        world_h = max(1.0, max_y - min_y)
        usable_w = max(1.0, view_w - 2.0 * margin)
        usable_h = max(1.0, view_h - 2.0 * margin)
        zoom = min(usable_w / world_w, usable_h / world_h)
        self.zoom = max(ZOOM_MIN, min(ZOOM_MAX, zoom))

        self.pan_x = view_w / 2.0 - ((min_x + max_x) / 2.0) * self.zoom
        self.pan_y = view_h / 2.0 - ((min_y + max_y) / 2.0) * self.zoom
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
        echoes exactly what we sent.  Label/colour/mode/membership always
        come from the daemon."""
        live = set()
        for p in daemon_panels:
            pid = p.get("id", "")
            live.add(pid)
            if pid == self.dragging_panel or pid == self.resizing_panel:
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
        daemon_nodes = data.get("nodes", {})
        daemon_edges = data.get("edges", {})
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
                del self.nodes[nid]
                self._pending_effect_slider.pop(nid, None)
                for key in [k for k in self._pending_bool if k[0] == nid]:
                    del self._pending_bool[key]
                self.anchored_nodes.discard(nid)
                self.selected_nodes.discard(nid)
                self._user_created_nodes.discard(nid)

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
                # Update all fields except volume if this node is being dragged
                node["type"] = ntype
                node["inputs"] = spec.inputs
                node["outputs"] = spec.outputs
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
                node["bool_state"] = ndata.get("bool_state")
                node["device_name"] = ndata.get("device_name", "")
                node["app_name"] = ndata.get("app_name", "")
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

        self.edges = {
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
        ctrl_connected = {
            e["to_node"]
            for e in self.edges.values()
            if e.get("to_port") == "ctrl"
        }
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

        # A bulk import has no explicit "done" reply (see main.py's
        # _cmd_load_session); infer completion from the node health the
        # daemon reports each poll.
        self._update_loading_state(daemon_nodes)

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
        two panels together instead."""
        max_delta = 0.0
        for pid in [""] + [p for p in self.panels if p]:
            members = self._panel_member_nodes(pid)
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
            delta = self.force_layout.step(members, positions, sizes, edges, pinned)
            max_delta = max(max_delta, delta)
            for nid in members:
                self.nodes[nid]["x"] = positions[nid][0] + ox
                self.nodes[nid]["y"] = positions[nid][1] + oy

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
            positions = {
                pid: (self.panels[pid]["x"], self.panels[pid]["y"])
                for pid in pids
            }
            sizes = {}
            for pid in pids:
                rect = self._panel_rect(pid)
                sizes[pid] = (rect[2], rect[3]) if rect else (420.0, 260.0)
            kid_set = set(pids)
            edges = []
            for e in self.edges.values():
                a = panel_of(e["from_node"])
                b = panel_of(e["to_node"])
                if a != b and a in kid_set and b in kid_set:
                    edges.append((a, b))
            pinned = {pid for pid in pids if self.panels[pid].get("anchored")}
            if self.dragging_panel in kid_set:
                pinned.add(self.dragging_panel)
            delta = self.panel_force_layout.step(pids, positions, sizes, edges, pinned)
            max_delta = max(max_delta, delta)
            for pid in pids:
                if self.dragging_panel == pid:
                    continue
                nx, ny = positions[pid]
                dx = nx - self.panels[pid]["x"]
                dy = ny - self.panels[pid]["y"]
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
            # the type label (i == 0) shares its row with the
            # three-dot menu icon, so it wraps at a narrower width
            # than the lines below it.
            max_width = self.node_width(node_id) - (
                self.HEADER_ICON_RESERVE if i == 0 else 20
            )
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
        {"splitter", "boolean_and", "boolean_or", "boolean_invert"}
    )

    @classmethod
    def _is_compact_node(cls, node) -> bool:
        return node.get("type") in cls._COMPACT_NODE_TYPES

    def _node_width_for(self, node):
        """Width a node renders at: a compact node (splitter / boolean
        logic gate) with no label collapses to a square
        (SPLITTER_MIN_SIZE); every other node - and a labelled compact
        one, which wraps its text - uses the normal node width."""
        if self._is_compact_node(node) and not node.get("label"):
            if node["type"] == "splitter":
                return self.SPLITTER_MIN_SIZE
            # A gate still shows its type name at the top beside the
            # three-dot menu, so it must be wide enough for that text -
            # otherwise the name would wrap into a sliver.
            tw, _ = self._text_size(type_label(node["type"], ""), 10)
            return max(
                self.SPLITTER_MIN_SIZE, int(tw) + self.HEADER_ICON_RESERVE + 16
            )
        return self.NODE_WIDTH

    def node_width(self, node_id):
        cached = self._node_w_cache.get(node_id)
        if cached is None:
            cached = self._node_width_for(self.nodes[node_id])
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
            return len(rows) * (self.FIELD_HEIGHT + 4) + 5
        spec = spec_for(node["type"])
        if spec.control == "fallback_onoff":
            # The on/off button is always shown: interactive while
            # nothing is wired into the ctrl input, and a read-only white
            # state indicator once a boolean signal drives the node.
            return self.GATE_AREA_HEIGHT
        if spec.control in ("gate", "switcher", "boolean"):
            return self.GATE_AREA_HEIGHT
        if spec.has_extra_row:
            return 25
        return 0

    def _header_stack_height(self, node_id, node):
        """Pixel height of everything drawn in the node's header block,
        measured the same way _draw_header stacks it (identical per-line
        widths and gaps) - used to keep labelled sockets clear of it."""
        blocks = self._header_blocks(node_id, node)
        total = self.HEADER_TOP_PAD
        for i, (text, font_size, _color) in enumerate(blocks):
            max_width = self.node_width(node_id) - (
                self.HEADER_ICON_RESERVE if i == 0 else 20
            )
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

        if len(node.get("inputs", [])) <= 1:
            offset = 0
            if node.get("meta", {}).get("description") or node.get("label"):
                offset += 20
            offset += self._device_header_bonus(node)
            offset += self._header_extra_height(node_id)
            offset += self._bottom_control_height(node)
            return offset, offset

        # Labelled sockets (more than one input, e.g. Echo Cancel):
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

    def _hit_nodes(self, x=None, y=None):
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
        every node, top-first."""
        if x is not None and y is not None:
            for nid, node in reversed(self.nodes.items()):
                if node["x"] <= x <= node["x"] + self.node_width(nid) and node[
                    "y"
                ] <= y <= node["y"] + self.node_height(nid):
                    return iter(((nid, node),))
        return reversed(self.nodes.items())

    # ---------- panel hit tests ----------

    def _panel_order_deepest_first(self):
        return sorted(self.panels, key=lambda p: p.count("::"), reverse=True)

    def find_panel_at(self, x, y):
        for pid in self._panel_order_deepest_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            px, py, pw, ph = rect
            if px <= x <= px + pw and py <= y <= py + ph:
                return pid
        return None

    def find_panel_resize_at(self, x, y):
        for pid in self._panel_order_deepest_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x1, y1, x2, y2 = self._panel_header_rects(pid, rect)["resize"]
            if x1 - 4 <= x <= x2 + 4 and y1 - 4 <= y <= y2 + 4:
                return pid
        return None

    def find_panel_reset_at(self, x, y):
        for pid, panel in self.panels.items():
            if pid == "" or not panel.get("readonly"):
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            r = self._panel_header_rects(pid, rect)["reset"]
            if r is not None and r[0] <= x <= r[2] and r[1] <= y <= r[3]:
                return pid
        return None

    def find_panel_anchor_at(self, x, y):
        for pid in self._panel_order_deepest_first():
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
        for pid in self._panel_order_deepest_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x1, y1, x2, y2 = self._panel_header_rects(pid, rect)["header"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return pid
        return None

    def find_panel_settings_at(self, x, y):
        for pid in self._panel_order_deepest_first():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x1, y1, x2, y2 = self._panel_header_rects(pid, rect)["settings"]
            if x1 <= x <= x2 and y1 <= y <= y2:
                return pid
        return None

    def find_node_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
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
        """The settings badge in a node's bottom-right corner - returns
        the node whose Settings dialog should open when it's clicked.
        Only nodes with spec.settings (extra controls beyond the
        generic ID/label rows) draw one, so nothing to hit otherwise."""
        for nid, node in self._hit_nodes(x, y):
            if not spec_for(node["type"]).settings:
                continue
            node_h = self.node_height(nid)
            gx, gy = node["x"] + self.NODE_WIDTH - 20, node["y"] + node_h - 20
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
        if ntype in ("patchbay_device", "patchbay_mic_device"):
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
        return node.get("type") in ("patchbay_device", "patchbay_mic_device")

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
        for nid, node in self._hit_nodes(x, y):
            dot_x = node["x"] + self.node_width(nid) - 14
            dot_y = node["y"] + 12
            if dot_x - 10 <= x <= dot_x + 10 and dot_y - 10 <= y <= dot_y + 22:
                return nid
        return None

    def find_field_at(self, x, y):
        for nid, node in self._hit_nodes(x, y):
            if not spec_for(node["type"]).field:
                continue
            node_h = self.node_height(nid)
            field_y = node["y"] + node_h - self.FIELD_HEIGHT - 5
            field_x = node["x"] + self.FIELD_MARGIN
            field_w = self.NODE_WIDTH - 2 * self.FIELD_MARGIN
            if (
                field_x <= x <= field_x + field_w
                and field_y <= y <= field_y + self.FIELD_HEIGHT
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
        pal = theme_palette(self)
        cr.set_source_rgb(*pal["bg"])
        cr.paint()

        cr.save()
        self.apply_view_transform(cr)

        draw_grid_background(cr, pal, self.pan_x, self.pan_y, self.zoom, w, h)

        # Panels are the outermost containers, then group annotations.
        self._draw_panel_boxes(cr, pal)

        # Group boxes sit behind the graph; their headers are drawn on
        # top of the nodes further down so the label stays clickable.
        self._draw_group_boxes(cr, pal)

        cr.set_source_rgb(*pal["link"])
        cr.set_line_width(2)
        for eid, edge in self.edges.items():
            if self.detaching_edge and self.detaching_edge[0] == eid:
                continue
            if edge["from_node"] not in self.nodes or edge["to_node"] not in self.nodes:
                continue
            out_x, out_y, in_x, in_y = self._edge_endpoints(edge)
            src_node = self.nodes[edge["from_node"]]
            is_bool = (
                port_kind(
                    src_node["type"], edge.get("from_port", "out"), "out"
                )
                == "boolean"
            )
            cr.set_source_rgb(
                *(pal["boolean_port"] if is_bool else pal["link"])
            )
            draw_bezier_link(cr, out_x, out_y, in_x, in_y)

        for nid, node in self.nodes.items():
            self._draw_node(cr, pal, nid, node)

        # Highlight the current marquee selection, then the rubber-band
        # rectangle itself, above the nodes so both stay visible.
        for nid in self.selected_nodes:
            node = self.nodes.get(nid)
            if node is None:
                continue
            draw_rounded_rect(
                cr, node["x"], node["y"], self.node_width(nid), self.node_height(nid), 8
            )
            cr.set_source_rgb(*pal["select"])
            cr.set_line_width(3)
            cr.stroke()

        # Group labels/ids/colour chips on top of the nodes.
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
                cr.set_source_rgb(*pal["pending_link"])
                cr.set_line_width(2)
                draw_bezier_link(cr, sx, sy, *self.drag_current_xy)

        cr.restore()

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
        # drawn with the theme's error colour, solid, and a "dead"
        # badge rather than the neutral "not connected yet" one.
        is_dead = node.get("health") == "dead"
        # Structurally up but at least one edge touching it hasn't
        # landed as a live link yet (see nodes_pending_wiring above).
        # Only tracked separately from is_offline so the two don't
        # double-dash the same border; is_offline already implies
        # "don't trust this node's links yet" on its own.
        is_wiring = (not is_offline) and (nid in self.nodes_pending_wiring)
        # Stable, category-assigned theme colour - same type always gets
        # the same border, and it comes from the GTK theme rather than a
        # per-process hash (see node_specs.color_name_for_node_type).
        border_color = theme_color(
            self, color_name_for_node_type(node["type"]), pal["node_border"]
        )

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
            # category whose assigned colour happens to be amber
            # (Hardware & Apps uses the theme's warning colour too).
            # A tighter dash for "wiring" than "offline" so the two
            # remain visually distinguishable at a glance too.
            cr.set_dash([4.0, 3.0] if is_offline else [2.0, 2.0])
        cr.stroke()
        cr.set_dash([])

        self._draw_anchor_icon(
            cr, pal, x, y, node_w, nid in self.anchored_nodes
        )
        self._draw_three_dots(cr, x, y, node_w)

        self._draw_header(cr, pal, nid, node, x, y)

        if spec.control == "volume":
            if is_mute_node(nid):
                self._draw_mute_checkbox(cr, x, y, node_h, node["volume"])
            else:
                self._draw_volume_slider(cr, x, y, node_h, node["volume"])
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
            driven = bool(node.get("ctrl_connected"))
            state = node.get("bool_state")
            if node["type"] == "gate":
                stored = 1 if node.get("enabled", True) else 0
            else:
                stored = node.get("output", 0)
            output = (1 if state else 0) if (driven and state is not None) else stored
            self._draw_boolean_toggle(cr, pal, nid, output, driven=driven)
        elif spec.control == "wetdry":
            self._draw_wetdry_slider(cr, x, y, node_h, node.get("wet_dry", 0.3))
        elif spec.control == "gain":
            # Normalize's boost, drawn as a plain 0..1 slider (fraction
            # of the plugin's 0..30 dB range - see find_gain_slider_at).
            self._draw_volume_slider(cr, x, y, node_h, node.get("gain", 0.5))
        elif spec.control == "sensitivity":
            self._draw_threshold_slider(
                cr, x, y, node_h, node.get("sensitivity", 0.0)
            )
        elif spec.field:
            self._draw_text_field(cr, x, y, node_h, self._field_value(node))

        for i, row_kind in enumerate(self._device_rows(node)):
            self._draw_device_row(cr, nid, node, i, row_kind)

        # A node whose Settings dialog has more than the generic
        # ID/label rows (Echo Cancel's module options, Noise Cancel's
        # method/dials) gets a small gear badge in its bottom-right
        # corner, so it's obvious there's something worth opening the
        # menu for - the whole point of the badge is that otherwise
        # those dials are invisible until someone happens to right-
        # click. Clicking the badge opens Settings directly (see
        # find_settings_gear_at/on_click).
        if spec.settings:
            self._draw_settings_gear(cr, pal, x, y, node_h)

        multi_input = len(node["inputs"]) > 1
        for i in range(len(node["inputs"])):
            sx, sy = self._socket_position(nid, "in", i)
            is_bool = port_kind(node["type"], node["inputs"][i], "in") == "boolean"
            cr.set_source_rgb(
                *(pal["boolean_port"] if is_bool else pal["input_port"])
            )
            cr.arc(sx, sy, self.SOCKET_RADIUS, 0, 2 * math.pi)
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
            is_bool = port_kind(node["type"], node["outputs"][i], "out") == "boolean"
            cr.set_source_rgb(
                *(
                    pal["select"]
                    if is_source
                    else (pal["boolean_port"] if is_bool else pal["output_port"])
                )
            )
            cr.arc(sx, sy, self.SOCKET_RADIUS, 0, 2 * math.pi)
            cr.fill()
            # Multi-output nodes (today only the Switcher's "a"/"b") get
            # their sockets labelled, right-aligned inside the node body;
            # a lone "out" socket is self-explanatory and left unlabelled.
            if multi_output and spec.socket_labels:
                draw_text_ellipsized(
                    cr,
                    sx - 25,
                    sy - 5,
                    node["outputs"][i],
                    14,
                    8,
                    pal["subtext"],
                )

    def _draw_header(self, cr, pal, nid, node, x, y):
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
            # The first line (the type label) shares its row with the
            # three-dot menu icon in the top-right corner, so it gets
            # a narrower width than every line below it.
            max_width = self.node_width(nid) - (
                self.HEADER_ICON_RESERVE if i == 0 else 20
            )
            if self._header_block_is_id(nid, text):
                draw_text_ellipsized(
                    cr, x + 10, text_y, text, max_width, font_size,
                    pal[color_key],
                )
                block_h = self._single_line_height(font_size)
            else:
                block_h = draw_text_wrapped(
                    cr, x + 10, text_y, text, max_width, font_size,
                    pal[color_key],
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

    def _draw_settings_gear(self, cr, pal, x, y, node_h):
        """Small cog in the node's bottom-right corner marking "this
        node's Settings menu has important extra controls" - and the
        click target that opens it (find_settings_gear_at). Drawn as a
        solid disc with notches (a proper little gear), NOT a thin ring
        with radial spokes - the spokes read as a stray yellow line
        across the node body."""
        cx = x + self.NODE_WIDTH - 20
        cy = y + node_h - 20
        cr.save()
        amber = (0.88, 0.70, 0.30)
        cr.set_source_rgb(*amber)
        cr.arc(cx, cy, 8, 0, 2 * math.pi)
        cr.fill()
        # Notch teeth out of the rim by punching node-bg-coloured dots
        # around the circumference - reads as a cog without any spokes.
        cr.set_source_rgb(*pal["node_bg"])
        for k in range(8):
            a = k * math.pi / 4
            cr.arc(
                cx + math.cos(a) * 6.0, cy + math.sin(a) * 6.0, 2.4, 0, 2 * math.pi
            )
            cr.fill()
        cr.set_source_rgb(*amber)
        cr.arc(cx, cy, 2.2, 0, 2 * math.pi)
        cr.fill()
        cr.restore()

    def _draw_lock_button(self, cr, x, y, w, h, locked):
        """Small padlock at the right of a volume row.  Filled amber when
        locked (PatchBay re-asserts its volume every tick), a dim outline
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

    def _draw_wetdry_slider(self, cr, x, y, node_h, mix):
        """Reverb's dry/wet mix as an inline slider on the node body
        (0 = fully dry, 1 = fully wet), styled like the volume slider
        so the same drag gesture drives it (see on_drag_begin/update/
        end's "wetdry" handling)."""
        slider_y = y + node_h - self.SLIDER_HEIGHT - 5
        slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
        slider_x = x + self.SLIDER_MARGIN

        cr.set_font_size(8)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(slider_x, slider_y - 3)
        cr.show_text("dry/wet")

        cr.set_source_rgb(0.3, 0.3, 0.3)
        cr.rectangle(slider_x, slider_y, slider_width, 4)
        cr.fill()

        cr.set_source_rgb(0.95, 0.72, 0.25)
        cr.rectangle(slider_x, slider_y, slider_width * mix, 4)
        cr.fill()

        handle_x = slider_x + slider_width * mix
        cr.arc(handle_x, slider_y + 2, 6, 0, 2 * math.pi)
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.fill()

        cr.set_font_size(9)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(x + self.NODE_WIDTH - 34, slider_y - 4)
        cr.show_text(f"{int(round(mix * 100))}% wet")

    def _draw_threshold_slider(self, cr, x, y, node_h, frac):
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
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(slider_x, slider_y - 3)
        cr.show_text("sensitivity")

        cr.set_source_rgb(0.3, 0.3, 0.3)
        cr.rectangle(slider_x, slider_y, slider_width, 4)
        cr.fill()

        cr.set_source_rgb(0.45, 0.78, 0.95)
        cr.rectangle(slider_x, slider_y, slider_width * frac, 4)
        cr.fill()

        handle_x = slider_x + slider_width * frac
        cr.arc(handle_x, slider_y + 2, 6, 0, 2 * math.pi)
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.fill()

        cr.set_font_size(9)
        cr.set_source_rgb(0.6, 0.6, 0.6)
        cr.move_to(x + self.NODE_WIDTH - 30, slider_y - 4)
        cr.show_text(f"{int(round(frac * 100))}%")

    def _draw_volume_slider(self, cr, x, y, node_h, volume):
        slider_y = y + node_h - self.SLIDER_HEIGHT - 5
        slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
        slider_x = x + self.SLIDER_MARGIN

        cr.set_source_rgb(0.3, 0.3, 0.3)
        cr.rectangle(slider_x, slider_y, slider_width, 4)
        cr.fill()

        cr.set_source_rgb(0.4, 0.7, 0.9)
        cr.rectangle(slider_x, slider_y, slider_width * volume, 4)
        cr.fill()

        handle_x = slider_x + slider_width * volume
        cr.arc(handle_x, slider_y + 2, 6, 0, 2 * math.pi)
        cr.set_source_rgb(0.9, 0.9, 0.9)
        cr.fill()

        cr.set_font_size(9)
        cr.set_source_rgb(0.6, 0.6, 0.6)
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
        GTK theme's success colour when On and its error colour when Off,
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

    def _draw_text_field(self, cr, x, y, node_h, value):
        field_y = y + node_h - self.FIELD_HEIGHT - 5
        field_w = self.NODE_WIDTH - 2 * self.FIELD_MARGIN
        field_x = x + self.FIELD_MARGIN

        draw_rounded_rect(cr, field_x, field_y, field_w, self.FIELD_HEIGHT, 4)
        cr.set_source_rgb(0.14, 0.14, 0.15)
        cr.fill_preserve()
        cr.set_source_rgb(0.42, 0.42, 0.45)
        cr.set_line_width(1)
        cr.stroke()

        text = value if value else "(click to set)"
        color = (0.85, 0.85, 0.86) if value else (0.5, 0.5, 0.53)
        font_size = 10

        baseline_y = field_y + (self.FIELD_HEIGHT - font_size) // 2
        draw_text_ellipsized(
            cr,
            field_x + 6,
            baseline_y,
            text,
            field_w - 12,
            font_size,
            color,
        )

    def _draw_device_row(self, cr, nid, node, row_index, row_kind):
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
            cr.set_source_rgb(0.3, 0.3, 0.3)
            cr.rectangle(sx, sy + sh / 2 - 2, sw, 4)
            cr.fill()
            cr.set_source_rgb(0.4, 0.7, 0.9)
            cr.rectangle(sx, sy + sh / 2 - 2, sw * volume, 4)
            cr.fill()
            handle_x = sx + sw * volume
            cr.arc(handle_x, sy + sh / 2, 6, 0, 2 * math.pi)
            cr.set_source_rgb(0.9, 0.9, 0.9)
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
        cr.set_source_rgb(0.14, 0.14, 0.15)
        cr.fill_preserve()
        cr.set_source_rgb(0.42, 0.42, 0.45)
        cr.set_line_width(1)
        cr.stroke()
        color = (0.5, 0.5, 0.53) if placeholder else (0.85, 0.85, 0.86)
        draw_text_ellipsized(
            cr, row_x + 6, row_y + (row_h - 10) // 2, text, row_w - 12, 10, color
        )

    # ---------- pointer / click handling ----------

    def on_motion(self, controller, x, y):
        self.track_pointer(x, y)
        wx, wy = self.to_world(x, y)

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
            or self.find_switcher_toggle_at(wx, wy) is not None
            or self.find_boolean_toggle_at(wx, wy) is not None
            or self.find_fallback_toggle_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
            or self.find_anchor_icon_at(wx, wy) is not None
            or self.find_settings_gear_at(wx, wy) is not None
            or self.find_group_label_at(wx, wy) is not None
            or self.find_group_action_at(wx, wy) is not None
        ):
            self.set_cursor(Gdk.Cursor.new_from_name("pointer", None))
        else:
            self.set_cursor(None)

    def on_leave(self, controller):
        self.set_cursor(None)

    def on_click(self, gesture, n_press, x, y):
        if n_press != 1:
            return
        self.grab_focus()
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
            self.client.send(
                {"command": "set_panel_layout", "panel_id": pid,
                 "anchored": panel["anchored"]}
            )
            self.queue_draw()
            return

        # Panel settings (rename / recolour).
        pid = self.find_panel_settings_at(wx, wy)
        if pid is not None:
            self.show_panel_settings_dialog(pid)
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
        self.popup_context_menu(popover, screen_x, screen_y)
        entry.grab_focus()

    def _show_choice_popover(self, screen_x, screen_y, title, choices, on_pick):
        """`choices` is a list of (label, value) tuples. Shared by
        device/app/codec selection so there's exactly one place that
        builds this kind of list, instead of three near-identical
        popovers that can drift apart."""
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
        if not choices:
            empty = Gtk.Label(label="(none found)")
            empty.set_halign(Gtk.Align.START)
            box.append(empty)
        for label, value in choices:
            btn = Gtk.Button(label=label)
            btn.connect("clicked", lambda _b, v=value: (on_pick(v), popover.popdown()))
            box.append(btn)

        popover.set_child(box)
        self.popup_context_menu(popover, screen_x, screen_y)

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

    def show_panels_dialog(self):
        self._pending_panel_list = True
        self.client.send({"command": "list_panels"})

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
                    self._begin_load()
                    self.client.send(
                        {
                            "command": "create_panel",
                            "name": name,
                            "node_ids": list(self.selected_nodes),
                            "readonly": readonly.get_active(),
                            "x": x,
                            "y": y,
                            "w": w,
                            "h": h,
                        }
                    )
            dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.present()

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
        """Change a panel's display name and colour."""
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

        color_picker = ColorPicker(
            panel.get("color", self.GROUP_COLORS[0]), presets=self.GROUP_COLORS
        )
        content.append(self._labeled_row("Color:", color_picker))

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
                            "color": color_picker.get_hex(),
                        }
                    )
                    # Optimistic local update so the header reflects the
                    # change before the next poll.
                    panel["label"] = label
                    panel["color"] = color_picker.get_hex()
                    self._panel_geo_cache.clear()
                    self.queue_draw()
            dlg.destroy()

        dialog.connect("response", _on_response)
        dialog.present()

    def on_panels_list(self, resp):
        # Cache the listing so the dialog can render without another
        # round trip.
        self._panel_files = resp.get("files", [])
        if not self._pending_panel_list:
            return
        self._pending_panel_list = False
        self._show_panels_dialog(resp)

    def _select_panel_nodes(self, node_ids):
        """Select the given full-id nodes on the canvas."""
        ids = {n for n in node_ids if n in self.nodes}
        if not ids:
            self._show_error_dialog("Those nodes are not on the canvas.")
            return
        self._set_selection(ids)
        self.zoom_to_fit()

    def _panel_swatch(self, color):
        area = Gtk.DrawingArea()
        area.set_content_width(16)
        area.set_content_height(16)
        area.set_valign(Gtk.Align.CENTER)

        def draw(_area, cr, w, h):
            r, g, b = self._hex_to_rgb(color)
            cr.set_source_rgb(r, g, b)
            cr.arc(w / 2.0, h / 2.0, min(w, h) / 2.0 - 1, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgb(0.1, 0.1, 0.1)
            cr.set_line_width(1.0)
            cr.arc(w / 2.0, h / 2.0, min(w, h) / 2.0 - 1, 0, 2 * math.pi)
            cr.stroke()

        area.set_draw_func(draw)
        return area

    def _show_panels_dialog(self, resp):
        dialog = Gtk.Dialog(
            title="Panels",
            transient_for=self.get_root(),
            modal=False,
        )
        self._panel_dialog = dialog
        dialog.set_default_size(560, 460)

        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)

        lines = ["Panel directories:"]
        for entry in resp.get("directories", []):
            mode = "read-write" if entry.get("writable") else "read-only"
            lines.append(
                "  {} <span alpha='60%'>({})</span>".format(
                    GLib.markup_escape_text(str(entry.get("path", ""))), mode
                )
            )
        root = resp.get("root")
        if root:
            lines.append(
                "  root: {}".format(GLib.markup_escape_text(str(root)))
            )
        info = Gtk.Label()
        info.set_xalign(0)
        info.set_wrap(True)
        info.set_markup("\n".join(lines))
        content.append(info)

        scrolled = Gtk.ScrolledWindow()
        scrolled.set_vexpand(True)
        scrolled.set_hexpand(True)
        listbox = Gtk.ListBox()
        listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scrolled.set_child(listbox)

        files = resp.get("files", [])
        if not files:
            placeholder = Gtk.Label(label="No panel files found.")
            placeholder.set_margin_top(12)
            listbox.append(placeholder)
        for entry in files:
            listbox.append(self._panel_row(entry, dialog))
        content.append(scrolled)

        select_group_btn = Gtk.Button(label="Select Group\u2026")
        select_group_btn.set_tooltip_text(
            "Select the nodes of one of the canvas groups"
        )
        select_group_btn.connect(
            "clicked", lambda _b, d=dialog: self._prompt_select_group(d)
        )
        content.append(select_group_btn)

        dialog.add_button("Reload", Gtk.ResponseType.APPLY)
        dialog.add_button("Close", Gtk.ResponseType.CLOSE)
        dialog.connect("response", self._on_panels_dialog_response)
        dialog.connect("destroy", self._on_panels_dialog_destroy)
        dialog.show()

    def _prompt_select_group(self, parent):
        groups = list(self.groups.items())
        if not groups:
            self._show_error_dialog("There are no groups on the canvas.")
            return
        dialog = Gtk.Dialog(
            title="Select Group", transient_for=parent, modal=True
        )
        content = dialog.get_content_area()
        content.set_spacing(6)
        content.set_margin_top(10)
        content.set_margin_bottom(10)
        content.set_margin_start(10)
        content.set_margin_end(10)
        dropdown = Gtk.DropDown.new_from_strings(
            [g.get("label", gid) for gid, g in groups]
        )
        content.append(self._labeled_row("Group:", dropdown))
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Select", Gtk.ResponseType.APPLY)
        dialog.set_default_response(Gtk.ResponseType.APPLY)

        def on_response(dlg, response):
            if response == Gtk.ResponseType.APPLY:
                idx = dropdown.get_selected()
                if 0 <= idx < len(groups):
                    self._select_panel_nodes(groups[idx][1].get("nodes", []))
            dlg.destroy()

        dialog.connect("response", on_response)
        dialog.show()

    def _panel_row(self, entry, dialog):
        row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        row.set_margin_top(6)
        row.set_margin_bottom(6)
        row.set_margin_start(6)
        row.set_margin_end(6)

        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        header.append(self._panel_swatch(entry.get("color", "#3584e4")))
        name = Gtk.Label()
        name.set_xalign(0)
        name.set_hexpand(True)
        name.set_ellipsize(Pango.EllipsizeMode.END)
        name.set_markup(
            "<b>{}</b>  <span size='small' alpha='60%'>{}</span>".format(
                GLib.markup_escape_text(str(entry.get("label", "?"))),
                GLib.markup_escape_text(str(entry.get("id", ""))),
            )
        )
        header.append(name)

        select_btn = Gtk.Button(label="Select")
        select_btn.set_tooltip_text("Select this panel's nodes on the canvas")
        select_btn.connect(
            "clicked",
            lambda _b, e=entry: self._select_panel_nodes(e.get("nodes", [])),
        )
        header.append(select_btn)

        if entry.get("readonly"):
            reset_btn = Gtk.Button(label="Reset")
            reset_btn.set_tooltip_text(
                "Revert this read-only panel's nodes, edges and placement"
            )
            reset_btn.connect(
                "clicked",
                lambda _b, e=entry: self._reset_panel_from_dialog(e),
            )
            header.append(reset_btn)
            ro_tag = Gtk.Label(label="read-only")
            ro_tag.add_css_class("dim-label")
            header.append(ro_tag)
        elif entry.get("writable"):
            delete_btn = Gtk.Button(label="Delete")
            delete_btn.set_tooltip_text(
                "Remove this panel and every node it defines"
            )
            delete_btn.connect(
                "clicked",
                lambda _b, e=entry, d=dialog: self._confirm_delete_panel(e, d),
            )
            header.append(delete_btn)

        row.append(header)

        nodes = entry.get("nodes", [])
        names = ", ".join(str(n).rsplit("::", 1)[-1] for n in nodes[:8])
        if len(nodes) > 8:
            names += ", \u2026"
        detail = Gtk.Label(
            label=f"{len(nodes)} node(s), {len(entry.get('children', []))} "
            f"child panel(s): {names}"
        )
        detail.set_xalign(0)
        detail.set_wrap(True)
        detail.add_css_class("dim-label")
        row.append(detail)
        return row

    def _reset_panel_from_dialog(self, entry):
        self._begin_load()
        self.client.send({"command": "reset_panel", "panel_id": entry.get("id")})

    def _confirm_delete_panel(self, entry, parent):
        confirm = Gtk.AlertDialog()
        confirm.set_modal(True)
        confirm.set_message(f"Delete panel {entry.get('label', entry.get('id'))}?")
        confirm.set_detail(
            "Every node and edge defined by this panel will be removed."
        )
        confirm.set_buttons(["Cancel", "Delete"])
        confirm.set_cancel_button(0)
        confirm.set_default_button(1)
        confirm.choose(
            parent,
            None,
            lambda d, result, e=entry: self._on_delete_panel_chosen(d, result, e),
        )

    def _on_delete_panel_chosen(self, dialog, result, entry):
        try:
            index = dialog.choose_finish(result)
        except GLib.Error:
            return
        if index == 1:
            self._begin_load()
            self.client.send(
                {"command": "delete_panel", "panel_id": entry.get("id")}
            )

    def _on_panels_dialog_response(self, dialog, response):
        if response == Gtk.ResponseType.APPLY:
            self._pending_panel_list = True
            self.client.send({"command": "list_panels"})
        dialog.destroy()

    def _on_panels_dialog_destroy(self, dialog):
        if getattr(self, "_panel_dialog", None) is dialog:
            self._panel_dialog = None


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
        self.resizing_panel = None
        self.drag_node_starts = {}
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

        # Panel resize handle / header: the panel's own affordances, above
        # the canvas.  Handled before node hit-tests so the header strip is
        # always draggable.
        pid = self.find_panel_resize_at(wx, wy)
        if pid is not None:
            rect = self._panel_rect(pid)
            self.resizing_panel = pid
            self.resize_start = (wx, wy)
            self.resize_orig = (rect[2], rect[3])
            return
        pid = self.find_panel_header_at(wx, wy)
        if (
            pid is not None
            and self.find_panel_reset_at(wx, wy) is None
            and self.find_panel_anchor_at(wx, wy) is None
            and self.find_panel_settings_at(wx, wy) is None
        ):
            panel = self.panels[pid]
            self.dragging_panel = pid
            self.drag_panel_start = (wx, wy)
            self.drag_panel_origin = (panel["x"], panel["y"])
            self._drag_panel_applied = (0.0, 0.0)
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
            or self.find_switcher_toggle_at(wx, wy) is not None
            or self.find_boolean_toggle_at(wx, wy) is not None
            or self.find_fallback_toggle_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
            or self.find_anchor_icon_at(wx, wy) is not None
            or self.find_settings_gear_at(wx, wy) is not None
            or self.find_field_at(wx, wy) is not None
            or self.find_group_label_at(wx, wy) is not None
            or self.find_group_action_at(wx, wy) is not None
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

        # Pressing empty canvas starts a pan and drops any selection.
        self.panning = True
        self.pan_drag_start = (self.pan_x, self.pan_y)
        self._set_selection(())

    def on_drag_update(self, gesture, offset_x, offset_y):
        if self.resizing_panel is not None:
            dx = offset_x / self.zoom
            dy = offset_y / self.zoom
            panel = self.panels.get(self.resizing_panel)
            if panel is not None:
                panel["w"] = max(140.0, self.resize_orig[0] + dx)
                panel["h"] = max(90.0, self.resize_orig[1] + dy)
                self._panel_geo_cache.pop(self.resizing_panel, None)
                self._mark_panel_moved(self.resizing_panel)
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

    def _ports_compatible(self, from_nid, from_port, to_nid, to_port):
        """Whether an edge from (from_nid, from_port) to (to_nid,
        to_port) is legal: both ports must be the same kind (audio with
        audio, boolean with boolean).  Mirrors the daemon's add_edge
        check so the GUI never sends a connection it knows will fail."""
        src = self.nodes.get(from_nid)
        dst = self.nodes.get(to_nid)
        if src is None or dst is None:
            return False
        return port_kind(src["type"], from_port, "out") == port_kind(
            dst["type"], to_port, "in"
        )

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
        if self.resizing_panel is not None:
            panel = self.panels.get(self.resizing_panel)
            if panel is not None:
                self.client.send(
                    {
                        "command": "set_panel_layout",
                        "panel_id": self.resizing_panel,
                        "w": panel["w"],
                        "h": panel["h"],
                    }
                )
            self.resizing_panel = None
            self._mark_layout_dirty()
            return
        if self.dragging_panel is not None:
            panel = self.panels.get(self.dragging_panel)
            if panel is not None:
                self.client.send(
                    {
                        "command": "set_panel_layout",
                        "panel_id": self.dragging_panel,
                        "x": panel["x"],
                        "y": panel["y"],
                    }
                )
            self.dragging_panel = None
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
                        self.client.send({"command": "remove_edge", "edge_id": eid})
                        if self._ports_compatible(
                            from_nid, source_port, target_nid, target_port
                        ):
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
                    self.client.send({"command": "remove_edge", "edge_id": eid})
                    GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            elif target_nid is not None:
                if out_nid != target_nid:
                    existing_eid = self._edge_id(
                        out_nid, target_nid, target_port, source_port
                    )
                    if existing_eid in self.edges:
                        self.client.send(
                            {"command": "remove_edge", "edge_id": existing_eid}
                        )
                    elif self._ports_compatible(
                        out_nid, source_port, target_nid, target_port
                    ):
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
            self.dragging_node = None
            # Persist where the user dropped it.
            self._mark_layout_dirty()
            self._reparent_after_drag(dragged)
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
        pid = self.find_panel_at(cx, cy)
        target = pid if pid is not None else ""
        current = dragged.rsplit("::", 1)[0] if "::" in dragged else ""
        if target == current:
            return
        members = [n for n in self.selected_nodes if n in self.nodes]
        if dragged not in members:
            members = [dragged]
        self.client.send(
            {"command": "move_nodes", "panel_id": target, "node_ids": members}
        )

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

    def _text_size(self, text, font_size):
        layout = self.create_pango_layout(text or "")
        layout.set_font_description(
            Pango.FontDescription.from_string(f"sans {font_size}")
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
        keeps their individual titles/colours (``titles``) plus the list of
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
                    "color": group.get("color") or self.GROUP_COLORS[0],
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
        colour chip / +/- block for a given `top` (or the naive top above
        its box when None).  Pure; the collision-resolved top comes from
        _all_group_header_layouts.

        A merged group draws every contributing group's title on its own
        line (each in that group's colour), then a single id line."""
        bounds = self._group_bounds(gid, group)
        if bounds is None:
            return None
        x1, y1, _x2, _y2 = bounds
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
        chip_x = x1 + label_w + 8
        chip_y = mid_y - chip / 2
        # Two small +/- buttons past the colour chip to add/remove
        # members by clicking nodes.
        btn = 15
        btn_y = mid_y - btn / 2
        add_x = chip_x + chip + 8
        add_rect = (add_x, btn_y, add_x + btn, btn_y + btn)
        rem_x = add_x + btn + 3
        rem_rect = (rem_x, btn_y, rem_x + btn, btn_y + btn)
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
            # Clickable label/id/chip hotspot...
            "rect": (x1 - 2, top - 2, chip_x + chip + 2, top + block_h + 2),
            # ...and the whole block including the +/- buttons, which is
            # what the collision pass keeps clear of other blocks.
            "extent": (x1 - 2, top - 2, rem_x + btn + 2, top + block_h + 2),
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
    PANEL_TRIANGLE = 16
    PANEL_PADDING = 26

    @staticmethod
    def _panel_local(pid):
        return pid.rsplit("::", 1)[-1] if pid else "root"

    def _panel_title_font(self):
        """World-unit font size for a panel's floating title.

        Drawing is scaled by the view zoom, so a fixed world size grows
        without bound on screen.  Clamp the *screen* size so the title
        scales with zoom up to a point and then stays legible and out of
        the way."""
        z = max(self.zoom, 1e-6)
        return min(14.0, 30.0 / z)

    def _panel_rect(self, pid):
        """(x, y, w, h) absolute box for a panel: its explicit placement,
        auto-grown to contain its member nodes and child panels."""
        cached = self._panel_geo_cache.get(pid)
        if cached is not None:
            return cached
        panel = self.panels.get(pid)
        if panel is None:
            return None
        ax, ay = self._panel_absolute(pid)
        w = float(panel.get("w", 420.0))
        h = float(panel.get("h", 260.0))
        # Nodes being dragged right now must not count towards a panel's
        # auto-grown bounds: otherwise the source panel stretches to follow
        # the cursor and swallows a drop meant for the panel underneath.
        dragging = set(self.drag_node_starts) if self.dragging_node else set()
        minx = miny = maxx = maxy = None
        for nid in self._panel_member_nodes(pid):
            if nid in dragging:
                continue
            node = self.nodes.get(nid)
            if node is None:
                continue
            nx, ny = node["x"], node["y"]
            nr, nb = nx + self.node_width(nid), ny + self.node_height(nid)
            minx = nx if minx is None else min(minx, nx)
            miny = ny if miny is None else min(miny, ny)
            maxx = nr if maxx is None else max(maxx, nr)
            maxy = nb if maxy is None else max(maxy, nb)
        for child in panel.get("children", []):
            cr_rect = self._panel_rect(child)
            if cr_rect is None:
                continue
            cx, cy, cw, ch = cr_rect
            minx = cx if minx is None else min(minx, cx)
            miny = cy if miny is None else min(miny, cy)
            maxx = cx + cw if maxx is None else max(maxx, cx + cw)
            maxy = cy + ch if maxy is None else max(maxy, cy + ch)
        if minx is not None:
            w = max(w, (maxx - ax) + self.PANEL_PADDING)
            h = max(h, (maxy - ay) + self.PANEL_PADDING)
        rect = (ax, ay, w, h)
        self._panel_geo_cache[pid] = rect
        return rect

    def _panel_header_rects(self, pid, rect):
        """Hit/draw geometry for a panel's floating title row, which sits
        just above the box (like a group's title): the physics-stop (pin)
        circle on the left, the title, then the settings/reset circle."""
        x, y, w, h = rect
        panel = self.panels[pid]
        font = self._panel_title_font()
        label = panel.get("label") or self._panel_local(pid)
        tw, th = self._text_size(label, font)
        d = max(16.0, th * 1.5)
        gap = max(6.0, font * 0.5)
        top = y - th - gap
        anchor = (x, top, x + d, top + d)
        tx = x + d + gap
        title = (tx, top, tx + tw, top + th)
        right = tx + tw
        reset = None
        settings = None
        if panel.get("readonly"):
            reset = (right + gap, top, right + gap + d, top + d)
            right = reset[2]
        elif panel.get("writable"):
            settings = (right + gap, top, right + gap + d, top + d)
            right = settings[2]
        resize = (
            x + w - self.PANEL_TRIANGLE, y + h - self.PANEL_TRIANGLE,
            x + w, y + h,
        )
        header = (x, top, right, top + d)
        return {
            "header": header, "resize": resize, "reset": reset,
            "anchor": anchor, "settings": settings, "title": title,
        }

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
        # Deepest first so a nested panel's box sits on top.
        for pid in sorted(self.panels, key=lambda p: p.count("::"), reverse=True):
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x, y, w, h = rect
            panel = self.panels[pid]
            r, g, b = self._hex_to_rgb(panel.get("color"))
            cr.save()
            draw_rounded_rect(cr, x, y, w, h, 12)
            cr.clip()
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

    def _draw_panel_headers(self, cr, pal):
        for pid, panel in self.panels.items():
            if pid == "":
                continue
            rect = self._panel_rect(pid)
            if rect is None:
                continue
            x, y, w, h = rect
            geo = self._panel_header_rects(pid, rect)
            r, g, b = self._hex_to_rgb(panel.get("color"))
            font = self._panel_title_font()
            # Title, drawn like a group title (floating above the box, in
            # the panel's colour, scaling with zoom up to a point).
            tx1, ty1, _tx2, _ty2 = geo["title"]
            draw_text_unbounded(
                cr, tx1, ty1,
                panel.get("label") or self._panel_local(pid),
                font, (r, g, b),
            )
            # Physics-stop (pin) circle to the left of the title: filled
            # when the panel is pinned, with a pause glyph.
            ax1, ay1, ax2, ay2 = geo["anchor"]
            acx, acy = (ax1 + ax2) / 2.0, (ay1 + ay2) / 2.0
            ar = (ax2 - ax1) / 2.0
            bcx = max(2.0, ar * 0.55)
            cr.set_line_width(1.6)
            if panel.get("anchored"):
                cr.set_source_rgb(r, g, b)
                cr.arc(acx, acy, ar, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(0.05, 0.05, 0.06)
            else:
                cr.set_source_rgb(0.05, 0.05, 0.06)
                cr.arc(acx, acy, ar, 0, 2 * math.pi)
                cr.stroke()
            bar_h = ar
            bar_w = max(1.5, ar * 0.28)
            cr.rectangle(acx - ar * 0.45 - bar_w / 2, acy - bar_h / 2, bar_w, bar_h)
            cr.rectangle(acx + ar * 0.45 - bar_w / 2, acy - bar_h / 2, bar_w, bar_h)
            cr.fill()
            # Settings (writable) / Reset (read-only) circle, right of the
            # title.
            circle = geo["settings"] if geo["settings"] is not None else geo["reset"]
            if circle is not None:
                cx1, cy1, cx2, cy2 = circle
                ccx, ccy = (cx1 + cx2) / 2.0, (cy1 + cy2) / 2.0
                crr = (cx2 - cx1) / 2.0
                cr.set_source_rgb(0.96, 0.96, 0.96)
                cr.arc(ccx, ccy, crr, 0, 2 * math.pi)
                cr.fill()
                cr.set_source_rgb(0.1, 0.1, 0.12)
                cr.set_line_width(1.5)
                if geo["settings"] is not None:
                    # Three-dot "menu" glyph.
                    dot = max(1.0, crr * 0.16)
                    for dy in (-1, 0, 1):
                        cr.arc(ccx, ccy + dy * crr * 0.5, dot, 0, 2 * math.pi)
                        cr.fill()
                else:
                    # Reset: a circular arrow.
                    cr.arc(ccx, ccy, crr * 0.55, -1.0, 2.3)
                    cr.stroke()
                    a = 2.3
                    hx, hy = ccx + crr * 0.55 * math.cos(a), ccy + crr * 0.55 * math.sin(a)
                    cr.move_to(hx - 3, hy - 1)
                    cr.line_to(hx + 1, hy - 3)
                    cr.line_to(hx + 1, hy + 2)
                    cr.close_path()
                    cr.fill()
            # Resize triangle bottom-right.
            tx1, ty1, tx2, ty2 = geo["resize"]
            cr.set_source_rgba(r, g, b, 0.95)
            cr.move_to(tx2, ty2)
            cr.line_to(tx1, ty2)
            cr.line_to(tx2, ty1)
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
            r, g, b = self._hex_to_rgb(group.get("color"))
            cr.set_source_rgb(r, g, b)
            cr.set_line_width(1.5)
            cr.set_dash([2.0, 4.0], 0.0)
            draw_rounded_rect(cr, x1, y1, x2 - x1, y2 - y1, 12)
            cr.stroke()
        cr.set_dash([])

    def _draw_group_headers(self, cr, pal):
        for gid, group in self._merged_groups().items():
            info = self._group_header_layout(gid, group)
            if info is None:
                continue
            # Stacked titles: each contributing group's label on its own
            # line, in that group's colour (unbounded, so a title always
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

            r, g, b = self._hex_to_rgb(group.get("color"))
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
        color_picker = ColorPicker(
            group.get("color", self.GROUP_COLORS[0]), presets=self.GROUP_COLORS
        )
        color_picker.set_tooltip_text("The group's outline / title colour.")
        content.append(self._labeled_row("Color:", color_picker))

        dialog.add_button("Delete", Gtk.ResponseType.REJECT)
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Apply", Gtk.ResponseType.APPLY)

        def on_response(dlg, response):
            if response == Gtk.ResponseType.APPLY:
                new_id = id_entry.get_text().strip()
                if not new_id or (new_id != gid and new_id in self.groups):
                    return  # empty or duplicate id - leave dialog open
                new_color = color_picker.get_hex()
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
        # A right press often follows (or is chorded during) a left
        # drag; force any lingering left drag to release so its grab
        # can't fight the marquee/menu.
        self._drag_gesture.reset()
        self._reset_drag_state()
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
        self._right_drag_moved = False
        self.select_rect = None
        self.queue_draw()

    def _open_context_menu(self, x, y):
        self.grab_focus()
        wx, wy = self.to_world(x, y)

        eid = self.find_edge_at(wx, wy)
        if eid:
            self.client.send({"command": "remove_edge", "edge_id": eid})
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            return

        nid = self.find_node_at(wx, wy)
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
        elif node_type in ("device_input", "device_output"):
            config["device_name"] = ""
        elif node_type in ("app_input", "app_output"):
            config["app_name"] = ""
        elif node_type in ("virtual_speaker", "virtual_mic"):
            config["backing_node_name"] = f"{node_type}_{node_id}"
            config["device_label"] = (
                "Virtual Speaker" if node_type == "virtual_speaker" else "Virtual Mic"
            )
        elif node_type in ("patchbay_device", "patchbay_mic_device"):
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

    def add_node_at(self, node_type, wx, wy):
        """Add a node of `node_type`, remembering (wx, wy) - world
        coordinates - as where it should land once the daemon reports
        it back (see _pending_positions / update_from_daemon). Used by
        the add-node side panel's drop handler."""
        real_type, node_id, config = self._build_add_node_command(node_type)
        self._user_created_nodes.add(node_id)
        half_w = (
            self.SPLITTER_MIN_SIZE if real_type == "splitter" else self.NODE_WIDTH
        ) / 2
        self._pending_positions[node_id] = (
            wx - half_w,
            wy - self.NODE_HEIGHT / 2,
        )
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
        self.add_node_at(value, wx, wy)
        return True

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
        panel.set_size_request(ADD_NODE_PANEL_MIN_WIDTH, -1)
        panel.set_hexpand(True)
        panel.set_margin_top(10)
        panel.set_margin_bottom(10)
        panel.set_margin_start(10)
        panel.set_margin_end(10)
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
