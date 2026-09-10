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
import math
import random
import time

from gi.repository import Gtk, Gdk, GLib, GObject, Pango

from constants import (
    REFRESH_INTERVAL_MS,
    LAYOUT_TICK_MS,
    LAYOUT_SETTLE_TICKS,
    LAYOUT_SETTLE_EPSILON,
    POST_MUTATION_REFRESH_MS,
    VOLUME_SEND_EPSILON,
    ADD_NODE_PANEL_MIN_WIDTH,
    GRAPH_CANVAS_MIN_SIZE,
)
from render_utils import (
    theme_palette,
    theme_class_color,
    draw_rounded_rect,
    draw_text_ellipsized,
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
    is_mute_node,
    type_label,
    icon_for_add_node_type,
)
from portal_file_dialog import open_file, save_file


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
    # Minimum pixel gap between consecutive socket centres on a node
    # that labels its sockets (multi-input types such as Echo Cancel).
    # Big enough that the label text next to one socket never runs into
    # the socket/label of its neighbour once the sockets are pushed
    # below the header (see _socket_margins/_socket_position).
    SOCKET_MIN_STEP = 22

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.nodes = {}
        self.edges = {}

        self.pan_x = 0.0
        self.pan_y = 0.0
        self.panning = False
        self._dragging_volume = False
        self.pan_drag_start = (0.0, 0.0)

        self.dragging_node = None
        self.drag_node_start = (0, 0)

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

        self.pinned_nodes = set()

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
        self.layout_awake = True
        self._settle_ticks = 0
        # Consecutive awake layout ticks since the last settle/sleep -
        # capped in on_layout_tick so a non-converging layout can't
        # spin the CPU forever (see that method).
        self._awake_ticks = 0
        self._prev_node_ids = set()
        self._prev_edge_set = set()

        self.set_draw_func(self.on_draw)
        self.set_size_request(*GRAPH_CANVAS_MIN_SIZE)
        self.set_hexpand(True)
        self.set_vexpand(True)
        self.set_can_focus(True)

        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self.on_drag_begin)
        drag.connect("drag-update", self.on_drag_update)
        drag.connect("drag-end", self.on_drag_end)
        self.add_controller(drag)

        right_click = Gtk.GestureClick(button=3)
        right_click.connect("pressed", self.on_right_click)
        self.add_controller(right_click)

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

    # ---------- daemon state -> local model ----------

    def update_from_daemon(self, data):
        daemon_nodes = data.get("nodes", {})
        daemon_edges = data.get("edges", {})

        for nid in list(self.nodes.keys()):
            if nid not in daemon_nodes:
                del self.nodes[nid]
                self._pending_effect_slider.pop(nid, None)

        for nid, ndata in daemon_nodes.items():
            ntype = normalize_node_type(ndata.get("type"))
            spec = spec_for(ntype)
            if nid not in self.nodes:
                slot = len(self.nodes)
                if nid in self._pending_positions:
                    px, py = self._pending_positions.pop(nid)
                else:
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
                    "volume": ndata.get("volume", 1.0),
                    "wet_dry": ndata.get("wet_dry", 0.3),
                    "level": ndata.get("level", 25.0),
                    # Sensitivity Gate's 0..1 slider value. Persisted
                    # daemon-side (the daemon fans it out to the hidden
                    # pre/post volume nodes it owns - see main.py's
                    # _apply_sensitivity), so it survives reloads and is
                    # refreshed here like any other daemon field.
                    "sensitivity": ndata.get("sensitivity", 0.0),
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
                    "device_volume": ndata.get("device_volume", 1.0),
                    "profile_index": ndata.get("profile_index"),
                    "codec_label": ndata.get("profile_description", ""),
                }
            else:
                node = self.nodes[nid]
                # Update all fields except volume if this node is being dragged
                node["type"] = ntype
                node["inputs"] = spec.inputs
                node["outputs"] = spec.outputs
                node["meta"] = ndata
                node["label"] = ndata.get("label", "")
                node["enabled"] = ndata.get("enabled", True)
                node["device_name"] = ndata.get("device_name", "")
                node["app_name"] = ndata.get("app_name", "")
                node["connected"] = ndata.get("connected", False)
                node["is_bluetooth"] = ndata.get("is_bluetooth", False)
                node["selection_label"] = ndata.get("selection_label", "")
                node["ready"] = ndata.get("ready", True)
                # Only update volume if not dragging this node
                # Only update volume if not dragging this node
                if self.slider_dragging != ("process", nid):
                    node["volume"] = ndata.get("volume", 1.0)
                else:
                    print(f"Skipping volume update for dragged node {nid}")
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
                node["profile_index"] = ndata.get("profile_index")
                node["codec_label"] = ndata.get("profile_description", "")

        self.edges = {
            eid: {
                "from_node": edata["from_node"],
                "to_node": edata["to_node"],
                "to_port": edata.get("to_port", "in"),
            }
            for eid, edata in daemon_edges.items()
        }

        new_node_ids = set(self.nodes.keys())
        new_edge_set = {
            (e["from_node"], e["to_node"], e["to_port"]) for e in self.edges.values()
        }
        if new_node_ids != self._prev_node_ids or new_edge_set != self._prev_edge_set:
            self.layout_awake = True
            self._settle_ticks = 0
        self._prev_node_ids = new_node_ids
        self._prev_edge_set = new_edge_set
        self.force_layout.prune(new_node_ids)

        self.queue_draw()

    def on_layout_tick(self):
        if not self.layout_awake or len(self.nodes) < 2:
            return True

        positions = {nid: (n["x"], n["y"]) for nid, n in self.nodes.items()}
        sizes = {nid: (self.NODE_WIDTH, self.node_height(nid)) for nid in self.nodes}
        edges = [
            (e["from_node"], e["to_node"])
            for e in self.edges.values()
            if e["from_node"] in self.nodes and e["to_node"] in self.nodes
        ]
        pinned = set(self.pinned_nodes)
        if self.dragging_node is not None:
            pinned.add(self.dragging_node)
        max_delta = self.force_layout.step(
            self.nodes.keys(), positions, sizes, edges, pinned
        )

        for nid, (x, y) in positions.items():
            self.nodes[nid]["x"] = x
            self.nodes[nid]["y"] = y

        if max_delta < LAYOUT_SETTLE_EPSILON and self.dragging_node is None:
            self._settle_ticks += 1
            if self._settle_ticks > LAYOUT_SETTLE_TICKS:
                self.layout_awake = False
                self._awake_ticks = 0
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

        self.queue_draw()
        return True

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
        if not node.get("ready", True):
            # Backed node (echo cancel, noise cancel, volume, ...) whose
            # real PipeWire objects haven't all been confirmed present
            # yet - see main.py's _node_is_ready. Appended last so it
            # never displaces the identifying blocks above it.
            blocks.append(("\u25cf not connected yet", 9, "warning"))
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

    def _header_extra_height(self, node_id):
        """Extra vertical room node_id's header needs beyond what
        node_height()'s fixed base already budgets for single-line
        text - 0 unless a label, description, or node id is long
        enough to wrap onto more than one line at the node's current
        width, in which case this is exactly enough to fit every
        wrapped line without clipping (see _draw_header, which stacks
        blocks using these same measurements)."""
        node = self.nodes[node_id]
        extra = 0.0
        for i, (text, font_size, _color) in enumerate(
            self._header_blocks(node_id, node)
        ):
            # Must match _draw_header's per-line max_width exactly -
            # the type label (i == 0) shares its row with the
            # three-dot menu icon, so it wraps at a narrower width
            # than the lines below it.
            max_width = self.NODE_WIDTH - (34 if i == 0 else 20)
            wrapped_h = wrapped_text_height(self, text, max_width, font_size)
            extra += max(0.0, wrapped_h - self._single_line_height(font_size))
        return extra

    def node_height(self, node_id):
        node = self.nodes[node_id]
        base = self._base_node_height(node_id)
        # A node that labels its input sockets (multi-input types such
        # as Echo Cancel) needs enough room below the header to spread
        # those sockets out - see _socket_margins/_socket_position.
        if len(node.get("inputs", [])) > 1:
            top, bottom = self._socket_margins(node_id, node)
            need = top + bottom + self.SOCKET_MIN_STEP * (len(node["inputs"]) + 1)
            base = max(base, int(need))
        return base

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
        rows = self._device_rows(node)
        if rows:
            base += len(rows) * (self.FIELD_HEIGHT + 4) + 5
        elif spec_for(node["type"]).control == "gate":
            base += self.GATE_AREA_HEIGHT
        elif spec_for(node["type"]).has_extra_row:
            base += 25
        return base

    def _header_stack_height(self, node_id, node):
        """Pixel height of everything drawn in the node's header block,
        measured the same way _draw_header stacks it (identical per-line
        widths and gaps) - used to keep labelled sockets clear of it."""
        blocks = self._header_blocks(node_id, node)
        total = self.HEADER_TOP_PAD
        for i, (text, font_size, _color) in enumerate(blocks):
            max_width = self.NODE_WIDTH - (34 if i == 0 else 20)
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
        for them. A node that labels its input sockets reserves the
        wrapped header text on top instead, so "mic"/"probe" (and their
        labels) sit below the title/label instead of on top of it, with
        only a small pad at the bottom."""
        if len(node.get("inputs", [])) <= 1:
            offset = 0
            if node.get("meta", {}).get("description") or node.get("label"):
                offset += 20
            offset += self._device_header_bonus(node)
            offset += self._header_extra_height(node_id)
            rows = self._device_rows(node)
            if rows:
                offset += len(rows) * (self.FIELD_HEIGHT + 4) + 5
            elif spec_for(node["type"]).control == "gate":
                offset += self.GATE_AREA_HEIGHT
            elif spec_for(node["type"]).has_extra_row:
                offset += 25
            return offset, offset

        top = self._header_stack_height(node_id, node) + 4
        return top, 8

    def _socket_position(self, node_id, direction, index):
        """The single source of truth for where a socket circle is (and
        therefore where an edge endpoint, hover highlight, and click hit-
        test must point too), so drawing and hit-testing can never drift
        apart. Distributed across the vertical band between
        _socket_margins()'s top and bottom insets."""
        node = self.nodes[node_id]
        ports = node["inputs"] if direction == "in" else node["outputs"]
        x = node["x"] if direction == "in" else node["x"] + self.NODE_WIDTH
        total = len(ports)
        if total == 0:
            return (x, node["y"] + self.node_height(node_id) / 2)
        top, bottom = self._socket_margins(node_id, node)
        band = self.node_height(node_id) - top - bottom
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
        out_x, out_y = self._socket_position(edge["from_node"], "out", 0)
        in_x, in_y = self._socket_position(
            edge["to_node"], "in", self._edge_to_port_index(edge)
        )
        return out_x, out_y, in_x, in_y

    def find_node_at(self, x, y):
        for nid, node in self.nodes.items():
            if node["x"] <= x <= node["x"] + self.NODE_WIDTH and node["y"] <= y <= node[
                "y"
            ] + self.node_height(nid):
                return nid
        return None

    def find_socket_at(self, x, y):
        for nid, node in self.nodes.items():
            for i in range(len(node["inputs"])):
                sx, sy = self._socket_position(nid, "in", i)
                if math.hypot(x - sx, y - sy) < 9:
                    return (nid, "in", i)
            for i in range(len(node["outputs"])):
                sx, sy = self._socket_position(nid, "out", i)
                if math.hypot(x - sx, y - sy) < 9:
                    return (nid, "out", i)
        return None

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
        for nid, node in self.nodes.items():
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
        for nid, node in self.nodes.items():
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

    def find_sensitivity_slider_at(self, x, y):
        """Sensitivity Gate's 0..1 gain-staging slider - same bottom-of-
        node-body geometry as the volume/wetdry sliders, control ==
        "sensitivity". It sends the value to the daemon (which drives the
        hidden pre/post Volume nodes it owns) rather than to this node's
        own threshold - see _apply_sensitivity_slider and
        node_specs.py's sensitivity_gate spec comment for why."""
        for nid, node in self.nodes.items():
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
        for nid, node in self.nodes.items():
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

    def find_device_row_at(self, x, y):
        for nid, node in self.nodes.items():
            rows = self._device_rows(node)
            for i, row_kind in enumerate(rows):
                rx, ry, rw, rh = self._device_row_rect(nid, i)
                if rx <= x <= rx + rw and ry <= y <= ry + rh:
                    return (nid, row_kind)
        return None

    def find_device_volume_slider_at(self, x, y):
        hit = self.find_device_row_at(x, y)
        if hit and hit[1] == "volume":
            return hit[0]
        return None

    def find_gate_toggle_at(self, x, y):
        for nid, node in self.nodes.items():
            if spec_for(node["type"]).control != "gate":
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
        for nid, node in self.nodes.items():
            if not predicate(node, nid):
                continue
            node_h = self.node_height(nid)
            cb_x, cb_y = node["x"] + 10, node["y"] + node_h - 22
            if cb_x <= x <= cb_x + size and cb_y <= y <= cb_y + size:
                return nid
        return None

    def find_three_dots_at(self, x, y):
        for nid, node in self.nodes.items():
            dot_x, dot_y = node["x"] + self.NODE_WIDTH - 14, node["y"] + 12
            if dot_x - 10 <= x <= dot_x + 10 and dot_y - 10 <= y <= dot_y + 22:
                return nid
        return None

    def find_field_at(self, x, y):
        for nid, node in self.nodes.items():
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

    # ---------- drawing ----------

    def on_draw(self, area, cr, w, h):
        pal = theme_palette(self)
        cr.set_source_rgb(*pal["bg"])
        cr.paint()

        cr.save()
        self.apply_view_transform(cr)

        draw_grid_background(cr, pal, self.pan_x, self.pan_y, self.zoom, w, h)

        cr.set_source_rgb(*pal["link"])
        cr.set_line_width(2)
        for eid, edge in self.edges.items():
            if self.detaching_edge and self.detaching_edge[0] == eid:
                continue
            if edge["from_node"] not in self.nodes or edge["to_node"] not in self.nodes:
                continue
            out_x, out_y, in_x, in_y = self._edge_endpoints(edge)
            cr.set_source_rgb(*pal["link"])
            draw_bezier_link(cr, out_x, out_y, in_x, in_y)

        for nid, node in self.nodes.items():
            self._draw_node(cr, pal, nid, node)

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
        spec = spec_for(node["type"])

        is_conn_source = (
            self.connecting_from is not None and self.connecting_from[0] == nid
        )
        is_conn_target = self.hover_target_node == nid
        is_offline = not node.get("ready", True)
        border_color = theme_class_color(self, node["type"], pal["node_border"])

        draw_rounded_rect(cr, x, y, self.NODE_WIDTH, node_h, 8)
        cr.set_source_rgb(*pal["node_bg"])
        cr.fill_preserve()
        if is_conn_source or is_conn_target:
            cr.set_source_rgb(*pal["select"])
        elif is_offline:
            cr.set_source_rgb(*pal["warning"])
        else:
            cr.set_source_rgb(*border_color)
        cr.set_line_width(2)
        if is_offline and not (is_conn_source or is_conn_target):
            # Dashed rather than solid - a glance at the canvas should
            # tell "still coming up" apart from "this node's type has an
            # amber accent colour" (theme_class_color can pick amber too).
            cr.set_dash([4.0, 3.0])
        cr.stroke()
        cr.set_dash([])

        self._draw_three_dots(cr, x, y)

        self._draw_header(cr, pal, nid, node, x, y)

        if spec.control == "volume":
            if is_mute_node(nid):
                self._draw_mute_checkbox(cr, x, y, node_h, node["volume"])
            else:
                self._draw_volume_slider(cr, x, y, node_h, node["volume"])
        elif spec.control == "gate":
            self._draw_gate_toggle(cr, nid, node["enabled"])
        elif spec.control == "wetdry":
            self._draw_wetdry_slider(cr, x, y, node_h, node.get("wet_dry", 0.3))
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
            cr.set_source_rgb(*pal["input_port"])
            cr.arc(sx, sy, 6, 0, 2 * math.pi)
            cr.fill()
            # A single "in" socket is self-explanatory and every node
            # type had exactly that until EchoCancelNode - only label
            # sockets when there's more than one to tell apart (e.g.
            # "mic" vs "probe"), so ordinary nodes stay uncluttered.
            if multi_input:
                draw_text_ellipsized(
                    cr,
                    sx + 9,
                    sy - 5,
                    node["inputs"][i],
                    self.NODE_WIDTH - 28,
                    8,
                    pal["subtext"],
                )
        for i in range(len(node["outputs"])):
            sx, sy = self._socket_position(nid, "out", i)
            is_source = self.connecting_from == (nid, i)
            cr.set_source_rgb(*(pal["select"] if is_source else pal["output_port"]))
            cr.arc(sx, sy, 6, 0, 2 * math.pi)
            cr.fill()

    def _draw_header(self, cr, pal, nid, node, x, y):
        """Draw every _header_blocks() line, wrapped (not
        ellipsized - see draw_text_wrapped) and stacked top to
        bottom by each block's own measured height, so a long label
        or node id is always fully visible instead of cut off with
        "...". node_height() already grew the node to
        fit this same stack (via _header_extra_height, which uses the
        identical per-block measurements) before this ever draws, so
        there's no clipping against the node's bottom edge or the
        control/ports area below it."""
        text_y = y + self.HEADER_TOP_PAD
        for i, (text, font_size, color_key) in enumerate(
            self._header_blocks(nid, node)
        ):
            # The first line (the type label) shares its row with the
            # three-dot menu icon in the top-right corner, so it gets
            # a narrower width than every line below it.
            max_width = self.NODE_WIDTH - (34 if i == 0 else 20)
            block_h = draw_text_wrapped(
                cr, x + 10, text_y, text, max_width, font_size, pal[color_key]
            )
            text_y += block_h + self.HEADER_BLOCK_GAP

    def _draw_three_dots(self, cr, x, y):
        dot_x = x + self.NODE_WIDTH - 14
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
            volume = node.get("device_volume", 1.0)
            cr.set_source_rgb(0.3, 0.3, 0.3)
            cr.rectangle(row_x, row_y + row_h / 2 - 2, row_w, 4)
            cr.fill()
            cr.set_source_rgb(0.4, 0.7, 0.9)
            cr.rectangle(row_x, row_y + row_h / 2 - 2, row_w * volume, 4)
            cr.fill()
            handle_x = row_x + row_w * volume
            cr.arc(handle_x, row_y + row_h / 2, 6, 0, 2 * math.pi)
            cr.set_source_rgb(0.9, 0.9, 0.9)
            cr.fill()
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
        ):
            self.set_cursor(Gdk.Cursor.new_from_name("ew-resize", None))
        elif (
            self.find_field_at(wx, wy) is not None
            or self.find_mute_checkbox_at(wx, wy) is not None
            or self.find_gate_toggle_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
            or self.find_settings_gear_at(wx, wy) is not None
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

        nid = self.find_settings_gear_at(wx, wy)
        if nid is not None:
            self.show_settings_dialog(nid)
            return

        nid = self.find_three_dots_at(wx, wy)
        if nid is not None:
            self.show_node_menu(nid, x, y)
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
            node = self.nodes[nid]
            node["enabled"] = not node["enabled"]
            self._send_set_gate(nid, node["enabled"])
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
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def show_import_last_session(self):
        """Import from the daemon's auto-saved session cache (see
        main.py's _auto_export_session, which writes this file after
        every structural/config change). This is the ONLY thing that
        ever triggers loading that cache - the daemon never loads it on
        its own startup - so restarting the daemon or the GUI never
        silently replaces whatever's currently open; the previous
        session only comes back if this button is clicked.

        The daemon reads its own cache file and stages it (see
        _apply_config's note on why staging matters) - this used to
        read the file directly off disk from here and replay it as
        individual commands, which both bypassed the staging and kept
        the cache path duplicated between constants.py and main.py for
        no reason beyond "the GUI needs it too"."""
        self.client.send({"command": "load_session"})
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

    def on_drag_begin(self, gesture, start_x, start_y):
        self.grab_focus()
        wx, wy = self.to_world(start_x, start_y)
        self.drag_start_xy = (wx, wy)
        self.drag_current_xy = (wx, wy)

        # Inline controls (slider / checkboxes / three-dot menu / text
        # field) all sit *inside* a node's rectangle. Resolving every
        # control hit FIRST - before find_node_at()/panning - is what
        # stops a press on a control from also being read as "start
        # dragging the node". GestureDrag's drag-begin fires on every
        # button press, immediately, before any movement, so it would
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
            print(f"Started dragging slider for node {slider_hit}")
            self.pinned_nodes.add(slider_hit)
            return

        device_slider_hit = self.find_device_volume_slider_at(wx, wy)
        if device_slider_hit is not None:
            nid = device_slider_hit
            rows = self._device_rows(self.nodes[nid])
            row_x, row_y, row_w, row_h = self._device_row_rect(
                nid, rows.index("volume")
            )
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

        device_row_hit = self.find_device_row_at(wx, wy)
        if (
            self.find_mute_checkbox_at(wx, wy) is not None
            or self.find_gate_toggle_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
            or self.find_settings_gear_at(wx, wy) is not None
            or self.find_field_at(wx, wy) is not None
            or (device_row_hit is not None and device_row_hit[1] != "volume")
        ):
            # Single-click toggles/menus, handled entirely by
            # on_click()'s "pressed" callback - just don't let this
            # drag gesture also grab the node or start a pan under them.
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
            self.connecting_from = (edge["from_node"], 0)
            self.detaching_edge = (eid, edge["from_node"])
            self.queue_draw()
            return

        nid = self.find_node_at(wx, wy)
        if nid is not None:
            self.dragging_node = nid
            self.drag_node_start = (self.nodes[nid]["x"], self.nodes[nid]["y"])
            self.layout_awake = True
            self._settle_ticks = 0
            return

        self.panning = True
        self.pan_drag_start = (self.pan_x, self.pan_y)

    def on_drag_update(self, gesture, offset_x, offset_y):
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
            if kind == "process":
                slider_width = self.NODE_WIDTH - 2 * self.SLIDER_MARGIN
            else:
                rows = self._device_rows(node)
                _, _, slider_width, _ = self._device_row_rect(nid, rows.index("volume"))
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
            node = self.nodes[self.dragging_node]
            node["x"] = self.drag_node_start[0] + offset_x / self.zoom
            node["y"] = self.drag_node_start[1] + offset_y / self.zoom
            self.queue_draw()
            return

        if self.panning:
            self.pan_x = self.pan_drag_start[0] + offset_x
            self.pan_y = self.pan_drag_start[1] + offset_y
            self.queue_draw()

    @staticmethod
    def _edge_id(from_node, to_node, to_port="in"):
        """Client-side mirror of PatchSpace._edge_id() on the daemon -
        needed here purely to predict what id an add_edge would get,
        for the "dragging onto an existing edge removes it" toggle
        below. Must stay in sync with that method."""
        if to_port == "in":
            return f"{from_node}->{to_node}"
        return f"{from_node}->{to_node}:{to_port}"

    def on_drag_end(self, gesture, offset_x, offset_y):
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
                old_to_nid = self.edges[eid]["to_node"]
                old_to_port = self.edges[eid].get("to_port", "in")
                if target_nid is not None:
                    if (
                        target_nid != old_to_nid or target_port != old_to_port
                    ) and target_nid != from_nid:
                        self.client.send({"command": "remove_edge", "edge_id": eid})
                        self.client.send(
                            {
                                "command": "add_edge",
                                "from_node": from_nid,
                                "to_node": target_nid,
                                "to_port": target_port,
                            }
                        )
                        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
                else:
                    self.client.send({"command": "remove_edge", "edge_id": eid})
                    GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            elif target_nid is not None:
                out_nid, _ = self.connecting_from
                if out_nid != target_nid:
                    existing_eid = self._edge_id(out_nid, target_nid, target_port)
                    if existing_eid in self.edges:
                        self.client.send(
                            {"command": "remove_edge", "edge_id": existing_eid}
                        )
                    else:
                        self.client.send(
                            {
                                "command": "add_edge",
                                "from_node": out_nid,
                                "to_node": target_nid,
                                "to_port": target_port,
                            }
                        )
                    GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

            self.connecting_from = None
            self.detaching_edge = None
            self.hover_target_node = None
            self.queue_draw()
            return

        if self.dragging_node is not None:
            self.dragging_node = None
            self.queue_draw()
            return

        self.panning = False

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

    # ---------- right-click / add node ----------

    def on_right_click(self, gesture, n_press, x, y):
        if n_press != 1:
            return
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
            btn.connect("clicked", self._on_add_node, ntype, popover)
            box.append(btn)
        popover.set_child(box)
        self.popup_context_menu(popover, x, y)

    def _unique_default_label(self, base_label):
        """`base_label` itself if no currently-known node already
        carries that label, otherwise the lowest-numbered "base_label
        N" (N >= 2) that isn't taken yet - "Volume", then "Volume 2",
        "Volume 3", ... Checked against self.nodes' live "label"
        values (not node ids, which are timestamp-based and never
        collide on their own), so this is really asking "would this
        default be ambiguous to a human looking at the canvas" -
        independent of type, so a "Volume" node and a differently
        typed node someone manually renamed to "Volume" still count
        as a clash."""
        existing = {n.get("label", "") for n in self.nodes.values()}
        if base_label not in existing:
            return base_label
        n = 2
        while f"{base_label} {n}" in existing:
            n += 1
        return f"{base_label} {n}"

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
        if node_type == "patchbay_device":
            # Fixed, readable id instead of a timestamped one - there's
            # only one such node conceptually (it always points at the
            # daemon's single built-in virtual sink, see
            # PatchBayDeviceNode's docstring), and add_node is
            # idempotent on an id that already exists (main.py's
            # _cmd_add_node returns already_existed=True instead of
            # duplicating), so re-clicking "Add" is harmless.
            node_id = "patchbay_speaker"
        elif node_type == "patchbay_mic_device":
            node_id = "patchbay_mic"
        else:
            node_id = f"{'mute' if is_mute else 'node'}_{int(time.time() * 1000)}"

        # Default label: reuses type_label()'s own mute-switch check
        # (is_mute_node(node_id), keyed off the "mute_" id prefix we
        # just chose above) so "Mute Switch" comes out right without
        # a second special case here, then de-duplicated against every
        # label currently on the canvas so a second Volume node reads
        # as "Volume 2" instead of an indistinguishable "Volume". The
        # two idempotent singleton types below (patchbay_device/
        # patchbay_mic_device) overwrite this with their own fixed
        # label, which is correct - they're id-locked to one instance,
        # so there's nothing to disambiguate.
        config = {"label": self._unique_default_label(type_label(real_type, node_id))}
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
        elif node_type in ("echo_cancel", "noise_cancel", "reverb"):
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
            # These two are otherwise no-config nodes (see
            # PatchBayDeviceNode/PatchBayMicDeviceNode's docstrings in
            # patchSpace.py) - "label" is the generic display-name
            # property every node type already supports (set via
            # setattr in main.py's add_node handling, read back by
            # _draw_node() below), so a short human-readable default
            # here needs no daemon changes, unlike device_label above
            # which only those two backed node types understand.
            config["label"] = (
                "Speaker Line" if node_type == "patchbay_device" else "Mic Line"
            )
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
        self._pending_positions[node_id] = (
            wx - self.NODE_WIDTH / 2,
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
        row.set_tooltip_text(label)
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
