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

from gi.repository import Gtk, Gdk, GLib, GObject

from constants import (
    REFRESH_INTERVAL_MS,
    LAYOUT_TICK_MS,
    LAYOUT_SETTLE_TICKS,
    LAYOUT_SETTLE_EPSILON,
    POST_MUTATION_REFRESH_MS,
    SESSION_CACHE_PATH,
    VOLUME_SEND_EPSILON,
)
from render_utils import (
    theme_palette,
    theme_class_color,
    draw_rounded_rect,
    draw_text_ellipsized,
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
)
from portal_file_dialog import open_file, save_file


class PatchSpaceGraphWidget(Gtk.DrawingArea, GraphViewMixin):
    NODE_WIDTH = 180
    NODE_HEIGHT = 80
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

        self.pinned_nodes = set()

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
        self._prev_node_ids = set()
        self._prev_edge_set = set()

        self.set_draw_func(self.on_draw)
        self.set_size_request(800, 600)
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

    def _send_property(self, node_id, prop, value):
        self.client.send(
            {
                "command": "set_node_property",
                "node_id": node_id,
                "property": prop,
                "value": value,
            }
        )

    # ---------- daemon state -> local model ----------

    def update_from_daemon(self, data):
        daemon_nodes = data.get("nodes", {})
        daemon_edges = data.get("edges", {})

        for nid in list(self.nodes.keys()):
            if nid not in daemon_nodes:
                del self.nodes[nid]

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
                    "device_volume": 1.0,
                    "device_name": ndata.get("device_name", ""),
                    "app_name": ndata.get("app_name", ""),
                    "connected": ndata.get("connected", False),
                    "is_bluetooth": ndata.get("is_bluetooth", False),
                    "selection_label": ndata.get("selection_label", ""),
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
                # Only update volume if not dragging this node
                # Only update volume if not dragging this node
                if self.slider_dragging != ("process", nid):
                    node["volume"] = ndata.get("volume", 1.0)
                else:
                    print(f"Skipping volume update for dragged node {nid}")
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
            eid: {"from_node": edata["from_node"], "to_node": edata["to_node"]}
            for eid, edata in daemon_edges.items()
        }

        new_node_ids = set(self.nodes.keys())
        new_edge_set = {(e["from_node"], e["to_node"]) for e in self.edges.values()}
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
        else:
            self._settle_ticks = 0

        self.queue_draw()
        return True

    # ---------- geometry ----------

    def node_height(self, node_id):
        node = self.nodes[node_id]
        base = self.NODE_HEIGHT
        if node.get("meta", {}).get("description") or node.get("label"):
            base += 20
        rows = self._device_rows(node)
        if rows:
            base += len(rows) * (self.FIELD_HEIGHT + 4) + 5
        elif spec_for(node["type"]).control == "gate":
            base += self.GATE_AREA_HEIGHT
        elif spec_for(node["type"]).has_extra_row:
            base += 25
        return base

    def _node_offset(self, node):
        """Vertical offset from the node's top edge to where its
        sockets start, so ports never overlap the description/label
        row or an inline control."""
        offset = 0
        if node.get("meta", {}).get("description") or node.get("label"):
            offset += 20
        rows = self._device_rows(node)
        if rows:
            offset += len(rows) * (self.FIELD_HEIGHT + 4) + 5
        elif spec_for(node["type"]).control == "gate":
            offset += self.GATE_AREA_HEIGHT
        elif spec_for(node["type"]).has_extra_row:
            offset += 25
        return offset

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

    def get_socket_position_with_offset(self, node_id, direction, index, offset):
        node = self.nodes[node_id]
        ports = node["inputs"] if direction == "in" else node["outputs"]
        x = node["x"] if direction == "in" else node["x"] + self.NODE_WIDTH
        total = len(ports)
        spacing = (
            (self.node_height(node_id) - 2 * offset) / (total + 1)
            if total
            else self.NODE_HEIGHT / 2
        )
        y = node["y"] + offset + spacing * (index + 1)
        return (x, y)

    def get_socket_position(self, node_id, direction, index):
        return self.get_socket_position_with_offset(node_id, direction, index, 0)

    def find_node_at(self, x, y):
        for nid, node in self.nodes.items():
            if node["x"] <= x <= node["x"] + self.NODE_WIDTH and node["y"] <= y <= node[
                "y"
            ] + self.node_height(nid):
                return nid
        return None

    def find_socket_at(self, x, y):
        for nid, node in self.nodes.items():
            offset = self._node_offset(node)
            for i in range(len(node["inputs"])):
                sx, sy = self.get_socket_position_with_offset(nid, "in", i, offset)
                if math.hypot(x - sx, y - sy) < 9:
                    return (nid, "in", i)
            for i in range(len(node["outputs"])):
                sx, sy = self.get_socket_position_with_offset(nid, "out", i, offset)
                if math.hypot(x - sx, y - sy) < 9:
                    return (nid, "out", i)
        return None

    def find_edge_at(self, x, y):
        threshold = 6
        for eid, edge in self.edges.items():
            if edge["from_node"] not in self.nodes or edge["to_node"] not in self.nodes:
                continue
            out_x, out_y = self.get_socket_position(edge["from_node"], "out", 0)
            in_x, in_y = self.get_socket_position(edge["to_node"], "in", 0)
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
            out_x, out_y = self.get_socket_position(edge["from_node"], "out", 0)
            in_x, in_y = self.get_socket_position(edge["to_node"], "in", 0)
            cr.set_source_rgb(*pal["link"])
            draw_bezier_link(cr, out_x, out_y, in_x, in_y)

        for nid, node in self.nodes.items():
            self._draw_node(cr, pal, nid, node)

        if self.connecting_from:
            nid, idx = self.connecting_from
            node = self.nodes.get(nid)
            if node:
                sx, sy = self.get_socket_position_with_offset(
                    nid, "out", idx, self._node_offset(node)
                )
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
        border_color = theme_class_color(self, node["type"], pal["node_border"])

        draw_rounded_rect(cr, x, y, self.NODE_WIDTH, node_h, 8)
        cr.set_source_rgb(*pal["node_bg"])
        cr.fill_preserve()
        cr.set_source_rgb(
            *(pal["select"] if (is_conn_source or is_conn_target) else border_color)
        )
        cr.set_line_width(2)
        cr.stroke()

        self._draw_three_dots(cr, x, y)

        draw_text_ellipsized(
            cr,
            x + 10,
            y + 20,
            type_label(node["type"], nid),
            self.NODE_WIDTH - 34,
            10,
            pal["subtext"],
        )

        label = node.get("label", "")
        desc = node.get("meta", {}).get("description", "")
        if desc:
            draw_text_ellipsized(
                cr, x + 10, y + 38, desc, self.NODE_WIDTH - 20, 12, pal["text"]
            )
            draw_text_ellipsized(
                cr,
                x + 10,
                y + 56,
                label or str(nid),
                self.NODE_WIDTH - 20,
                10 if label else 9,
                pal["subtext"],
            )
        elif label:
            draw_text_ellipsized(
                cr, x + 10, y + 38, label, self.NODE_WIDTH - 20, 12, pal["text"]
            )
            draw_text_ellipsized(
                cr, x + 10, y + 56, str(nid), self.NODE_WIDTH - 20, 9, pal["subtext"]
            )
        else:
            draw_text_ellipsized(
                cr, x + 10, y + 38, str(nid), self.NODE_WIDTH - 20, 12, pal["text"]
            )

        if spec.control == "volume":
            if is_mute_node(nid):
                self._draw_mute_checkbox(cr, x, y, node_h, node["volume"])
            else:
                self._draw_volume_slider(cr, x, y, node_h, node["volume"])
        elif spec.control == "gate":
            self._draw_gate_toggle(cr, nid, node["enabled"])
        elif spec.field:
            self._draw_text_field(cr, x, y, node_h, self._field_value(node))

        for i, row_kind in enumerate(self._device_rows(node)):
            self._draw_device_row(cr, nid, node, i, row_kind)

        offset = self._node_offset(node)
        for i in range(len(node["inputs"])):
            sx, sy = self.get_socket_position_with_offset(nid, "in", i, offset)
            cr.set_source_rgb(*pal["input_port"])
            cr.arc(sx, sy, 6, 0, 2 * math.pi)
            cr.fill()
        for i in range(len(node["outputs"])):
            sx, sy = self.get_socket_position_with_offset(nid, "out", i, offset)
            is_source = self.connecting_from == (nid, i)
            cr.set_source_rgb(*(pal["select"] if is_source else pal["output_port"]))
            cr.arc(sx, sy, 6, 0, 2 * math.pi)
            cr.fill()

    def _draw_three_dots(self, cr, x, y):
        dot_x = x + self.NODE_WIDTH - 14
        start_y = y + 12
        for i in range(3):
            cr.arc(dot_x, start_y + i * 6, 2.2, 0, 2 * math.pi)
            cr.set_source_rgb(0.7, 0.7, 0.7)
            cr.fill()

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

        if self.find_slider_at(wx, wy) is not None:
            self.set_cursor(Gdk.Cursor.new_from_name("ew-resize", None))
        elif (
            self.find_field_at(wx, wy) is not None
            or self.find_mute_checkbox_at(wx, wy) is not None
            or self.find_gate_toggle_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
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
        """Replay a config dict as add_node/add_edge commands - same
        idempotent shape apply_config.py's CLI script sends, just
        issued over this widget's already-open connection instead of
        a fresh one. add_node/add_edge on the daemon side both return
        already_existed=True instead of erroring or duplicating, so
        importing a config that overlaps what's already open is
        safe."""
        for node_id, node_cfg in config.get("nodes", {}).items():
            self.client.send(
                {
                    "command": "add_node",
                    "node_type": node_cfg.get("type"),
                    "node_id": node_id,
                    "config": node_cfg.get("params", {}),
                }
            )
        for edge in config.get("edges", []):
            self.client.send(
                {
                    "command": "add_edge",
                    "from_node": edge.get("from"),
                    "to_node": edge.get("to"),
                }
            )
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

    def show_import_last_session(self):
        """Import from the daemon's auto-saved session cache (see
        main.py's _auto_export_session, which writes this file after
        every structural/config change). This is the ONLY thing that
        ever reads that cache - the daemon never loads it on its own
        startup - so restarting the daemon or the GUI never silently
        replaces whatever's currently open; the previous session only
        comes back if this button is clicked.

        Reads the file directly off disk rather than going through the
        daemon: the daemon writes to a fixed, non-user-chosen path (see
        constants.SESSION_CACHE_PATH), so there's nothing here for a
        portal file-picker dialog (see portal_file_dialog.py) to add
        over a plain open() - that's only needed for save_file()/
        open_file()'s user-chosen paths elsewhere in this file."""
        try:
            with open(SESSION_CACHE_PATH) as f:
                config = json.load(f)
        except FileNotFoundError:
            self._show_error_dialog("No previous session found.")
            return
        except (OSError, json.JSONDecodeError) as e:
            self._show_error_dialog(f"Could not load last session: {e}")
            return
        self._apply_config(config)

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

        device_row_hit = self.find_device_row_at(wx, wy)
        if (
            self.find_mute_checkbox_at(wx, wy) is not None
            or self.find_gate_toggle_at(wx, wy) is not None
            or self.find_three_dots_at(wx, wy) is not None
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

            if self.detaching_edge:
                eid, from_nid = self.detaching_edge
                old_to_nid = self.edges[eid]["to_node"]
                if target_nid is not None:
                    if target_nid != old_to_nid and target_nid != from_nid:
                        self.client.send({"command": "remove_edge", "edge_id": eid})
                        self.client.send(
                            {
                                "command": "add_edge",
                                "from_node": from_nid,
                                "to_node": target_nid,
                            }
                        )
                        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
                else:
                    self.client.send({"command": "remove_edge", "edge_id": eid})
                    GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)
            elif target_nid is not None:
                out_nid, _ = self.connecting_from
                if out_nid != target_nid:
                    existing_eid = f"{out_nid}->{target_nid}"
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
            btn = Gtk.Button(label=label)
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
        node_id = f"{'mute' if is_mute else 'node'}_{int(time.time() * 1000)}"

        config = {}
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
        elif node_type in ("device_input", "device_output"):
            config["device_name"] = ""
        elif node_type in ("app_input", "app_output"):
            config["app_name"] = ""
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
        content.append(Gtk.Image.new_from_icon_name("list-add-symbolic"))
        row_label = Gtk.Label(label=label)
        row_label.set_halign(Gtk.Align.START)
        row_label.set_hexpand(True)
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
        """
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        panel.set_size_request(220, -1)
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
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        categories_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        categories_box.set_margin_top(6)

        for category_name, items in ADD_NODE_CATEGORIES:
            expander = Gtk.Expander(label=f"{category_name} ({len(items)})")
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
