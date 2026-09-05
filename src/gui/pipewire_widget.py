"""
pipewire_widget.py

The "PipeWire Graph" tab: a read-mostly view of the live PipeWire
graph (as reported by the daemon's get_graph/pw-dump-backed state),
with drag-to-connect / drag-to-detach / right-click-to-disconnect on
ports. It has no inline node controls (no sliders/checkboxes) - that's
PatchSpaceGraphWidget's job - so it's a straightforward, unchanged
port of the original widget onto the shared helpers.
"""

from __future__ import annotations

import math
import random

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from constants import (
    REFRESH_INTERVAL_MS,
    LAYOUT_TICK_MS,
    LAYOUT_SETTLE_TICKS,
    LAYOUT_SETTLE_EPSILON,
    POST_MUTATION_REFRESH_MS,
)
from render_utils import (
    theme_palette,
    theme_class_color,
    draw_rounded_rect,
    draw_text,
    draw_text_ellipsized,
    draw_bezier_link,
    draw_grid_background,
)
from force_layout import ForceLayout
from view_mixin import GraphViewMixin

# Standard PipeWire/ALSA channel positions, in the order we want them
# drawn top-to-bottom. Ports aren't guaranteed to be created in this
# order (their ids depend on creation order, which varies run to
# run) - sorting the drawn list by this instead of by port id is what
# keeps "left" pinned above "right" instead of flipping between runs.
CHANNEL_ORDER = {
    "FL": 0,
    "FC": 1,
    "FR": 2,
    "SL": 3,
    "SR": 4,
    "RL": 5,
    "RC": 6,
    "RR": 7,
    "LFE": 8,
    "MONO": 9,
}


def _channel_sort_key(port: dict) -> tuple:
    channel = (port.get("channel") or "").upper()
    # Unknown/missing channel names fall back to id order among
    # themselves, but always after every recognized channel.
    return (CHANNEL_ORDER.get(channel, 100), port["id"])


class PipeWireGraphWidget(Gtk.DrawingArea, GraphViewMixin):
    NODE_WIDTH = 250
    PORT_ROW_H = 20
    PORT_TOP_PAD = 60

    def __init__(self, client):
        super().__init__()
        self.client = client
        self.nodes = {}
        self.links = []
        self.node_positions = {}

        self.pan_x = 0.0
        self.pan_y = 0.0
        self.panning = False
        self.pan_drag_start = (0.0, 0.0)

        self.dragging_node = None
        self.drag_node_start = (0, 0)

        self.connecting_from = None
        self.detaching_link = None
        self.drag_start_xy = (0, 0)
        self.drag_current_xy = (0, 0)
        self.hover_target_port = None

        self.force_layout = ForceLayout(
            spring_length=280, repulsion=70000, flow_gap=500, flow_k=0.035
        )
        self.layout_awake = True
        self._settle_ticks = 0
        self._prev_node_ids = set()
        self._prev_link_set = set()

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

        motion = Gtk.EventControllerMotion()
        motion.connect("motion", self.on_motion)
        self.add_controller(motion)

        self._init_view_controls()

        GLib.timeout_add(REFRESH_INTERVAL_MS, self.refresh)
        GLib.timeout_add(LAYOUT_TICK_MS, self.on_layout_tick)

    def refresh(self):
        self.client.send({"command": "get_graph"})
        return True

    def update_graph(self, graph):
        nodes = {}
        ports = graph.get("ports", {})
        for nid, ndata in graph.get("nodes", {}).items():
            nid = int(nid)
            nodes[nid] = {"info": ndata, "inputs": [], "outputs": []}

        for pid, pdata in ports.items():
            pid = int(pid)
            node_id = pdata.get("node_id")
            if node_id in nodes:
                direction = pdata.get("direction")
                port = {
                    "id": pid,
                    "node_id": node_id,
                    "name": pdata.get("name"),
                    "direction": direction,
                    "channel": pdata.get("channel"),
                }
                (
                    nodes[node_id]["inputs"]
                    if direction == "in"
                    else nodes[node_id]["outputs"]
                ).append(port)

        for n in nodes.values():
            n["inputs"].sort(key=_channel_sort_key)
            n["outputs"].sort(key=_channel_sort_key)

        cols = max(1, int(math.sqrt(max(1, len(nodes)))))
        next_slot = len(self.node_positions)
        for nid in nodes:
            if nid not in self.node_positions:
                col = next_slot % cols
                row = next_slot // cols
                self.node_positions[nid] = (
                    20 + col * (self.NODE_WIDTH + 100) + random.uniform(-15, 15),
                    20 + row * 180 + random.uniform(-15, 15),
                )
                next_slot += 1

        for nid in list(self.node_positions.keys()):
            if nid not in nodes:
                del self.node_positions[nid]

        self.nodes = nodes
        new_links = [
            (l["output_port"], l["input_port"]) for l in graph.get("links", [])
        ]
        self.links = new_links

        new_node_ids = set(nodes.keys())
        new_link_set = set(new_links)
        if new_node_ids != self._prev_node_ids or new_link_set != self._prev_link_set:
            self.layout_awake = True
            self._settle_ticks = 0
        self._prev_node_ids = new_node_ids
        self._prev_link_set = new_link_set
        self.force_layout.prune(new_node_ids)

        self.queue_draw()

    def on_layout_tick(self):
        if not self.layout_awake or len(self.nodes) < 2:
            return True

        sizes = {
            nid: (self.NODE_WIDTH, self.node_height(node))
            for nid, node in self.nodes.items()
        }
        port_to_node = {}
        for nid, node in self.nodes.items():
            for p in node["inputs"] + node["outputs"]:
                port_to_node[p["id"]] = nid

        edges = []
        for out_id, in_id in self.links:
            a = port_to_node.get(out_id)
            b = port_to_node.get(in_id)
            if a is not None and b is not None:
                edges.append((a, b))

        pinned = {self.dragging_node} if self.dragging_node is not None else set()
        max_delta = self.force_layout.step(
            self.nodes.keys(), self.node_positions, sizes, edges, pinned
        )

        if max_delta < LAYOUT_SETTLE_EPSILON and self.dragging_node is None:
            self._settle_ticks += 1
            if self._settle_ticks > LAYOUT_SETTLE_TICKS:
                self.layout_awake = False
        else:
            self._settle_ticks = 0

        self.queue_draw()
        return True

    def node_height(self, node):
        rows = max(len(node["inputs"]), len(node["outputs"]), 1)
        base = max(90, self.PORT_TOP_PAD + rows * self.PORT_ROW_H + 10)
        if node["info"].get("description"):
            base += 20
        return base

    def on_draw(self, area, cr, w, h):
        pal = theme_palette(self)
        cr.set_source_rgb(*pal["bg"])
        cr.paint()

        cr.save()
        self.apply_view_transform(cr)
        draw_grid_background(cr, pal, self.pan_x, self.pan_y, self.zoom, w, h)
        cr.restore()

        if not self.nodes:
            draw_text(cr, 20, 30, "No PipeWire graph data", 12, pal["subtext"])
            return

        cr.save()
        self.apply_view_transform(cr)

        for nid, node in self.nodes.items():
            x, y = self.node_positions[nid]
            info = node["info"]
            name = info.get("name") or str(nid)
            media = info.get("media_class") or ""
            desc = info.get("description", "")
            node_h = self.node_height(node)

            border_color = theme_class_color(self, media, pal["node_border"])

            draw_rounded_rect(cr, x, y, self.NODE_WIDTH, node_h, 8)
            cr.set_source_rgb(*pal["node_bg"])
            cr.fill_preserve()
            cr.set_source_rgb(*border_color)
            cr.set_line_width(2)
            cr.stroke()

            draw_text_ellipsized(
                cr,
                x + 10,
                y + 20,
                media or "unknown",
                self.NODE_WIDTH - 20,
                10,
                pal["subtext"],
            )

            if desc:
                draw_text_ellipsized(
                    cr, x + 10, y + 38, desc, self.NODE_WIDTH - 20, 12, pal["text"]
                )
                draw_text_ellipsized(
                    cr, x + 10, y + 56, name, self.NODE_WIDTH - 20, 10, pal["subtext"]
                )
            else:
                draw_text_ellipsized(
                    cr, x + 10, y + 38, name, self.NODE_WIDTH - 20, 12, pal["text"]
                )

            top_pad = self.PORT_TOP_PAD + (20 if desc else 0)
            in_y = y + top_pad
            for port in node["inputs"]:
                is_target = (
                    self.hover_target_port is not None
                    and self.hover_target_port["id"] == port["id"]
                )
                r = 7 if is_target else 5
                cr.set_source_rgb(*(pal["select"] if is_target else pal["input_port"]))
                cr.arc(x, in_y, r, 0, 2 * math.pi)
                cr.fill()
                label = f"{port['name']} ({port['id']})"
                draw_text_ellipsized(
                    cr, x + 10, in_y + 4, label, self.NODE_WIDTH - 20, 9, pal["text"]
                )
                in_y += self.PORT_ROW_H

            out_y = y + top_pad
            for port in node["outputs"]:
                is_source = (
                    self.connecting_from is not None
                    and self.connecting_from["id"] == port["id"]
                )
                r = 7 if is_source else 5
                cr.set_source_rgb(*(pal["select"] if is_source else pal["output_port"]))
                cr.arc(x + self.NODE_WIDTH, out_y, r, 0, 2 * math.pi)
                cr.fill()
                label = f"{port['name']} ({port['id']})"
                draw_text_ellipsized(
                    cr,
                    x + self.NODE_WIDTH - 10 - 8 * len(label),
                    out_y + 4,
                    label,
                    self.NODE_WIDTH - 20,
                    9,
                    pal["text"],
                )
                out_y += self.PORT_ROW_H

        cr.set_source_rgb(*pal["link"])
        cr.set_line_width(2)
        for out_id, in_id in self.links:
            if self.detaching_link == (out_id, in_id):
                continue
            out_coord = self.find_port_coord(out_id)
            in_coord = self.find_port_coord(in_id)
            if out_coord and in_coord:
                draw_bezier_link(cr, *out_coord, *in_coord)

        if self.connecting_from:
            start = self.find_port_coord(self.connecting_from["id"])
            if start:
                cr.set_source_rgb(*pal["pending_link"])
                cr.set_line_width(2)
                draw_bezier_link(cr, *start, *self.drag_current_xy)

        cr.restore()

    def find_port_coord(self, port_id):
        for nid, node in self.nodes.items():
            for port in node["inputs"] + node["outputs"]:
                if port["id"] == port_id:
                    x, y = self.node_positions[nid]
                    desc = node["info"].get("description", "")
                    top_pad = self.PORT_TOP_PAD + (20 if desc else 0)
                    if port["direction"] == "in":
                        idx = node["inputs"].index(port)
                        return (x, y + top_pad + idx * self.PORT_ROW_H)
                    idx = node["outputs"].index(port)
                    return (x + self.NODE_WIDTH, y + top_pad + idx * self.PORT_ROW_H)
        return None

    def find_port_at(self, x, y):
        for nid, node in self.nodes.items():
            nx, ny = self.node_positions[nid]
            desc = node["info"].get("description", "")
            top_pad = self.PORT_TOP_PAD + (20 if desc else 0)
            for i, port in enumerate(node["inputs"]):
                px, py = nx, ny + top_pad + i * self.PORT_ROW_H
                if math.hypot(x - px, y - py) < 9:
                    return port
            for i, port in enumerate(node["outputs"]):
                px, py = nx + self.NODE_WIDTH, ny + top_pad + i * self.PORT_ROW_H
                if math.hypot(x - px, y - py) < 9:
                    return port
        return None

    def find_node_at(self, x, y):
        for nid, node in self.nodes.items():
            nx, ny = self.node_positions[nid]
            nh = self.node_height(node)
            if nx <= x <= nx + self.NODE_WIDTH and ny <= y <= ny + nh:
                return nid
        return None

    def find_link_at(self, x, y):
        threshold = 6
        for link in self.links:
            c1 = self.find_port_coord(link[0])
            c2 = self.find_port_coord(link[1])
            if c1 and c2:
                dx, dy = c2[0] - c1[0], c2[1] - c1[1]
                if dx == 0 and dy == 0:
                    dist = math.hypot(x - c1[0], y - c1[1])
                else:
                    t = max(
                        0,
                        min(
                            1,
                            ((x - c1[0]) * dx + (y - c1[1]) * dy) / (dx * dx + dy * dy),
                        ),
                    )
                    dist = math.hypot(x - (c1[0] + t * dx), y - (c1[1] + t * dy))
                if dist < threshold:
                    return link
        return None

    def on_motion(self, controller, x, y):
        self.track_pointer(x, y)
        if self.connecting_from:
            wx, wy = self.to_world(x, y)
            self.drag_current_xy = (wx, wy)
            port = self.find_port_at(wx, wy)
            self.hover_target_port = (
                port if (port and port["direction"] == "in") else None
            )
            self.queue_draw()

    def on_drag_begin(self, gesture, start_x, start_y):
        self.grab_focus()
        wx, wy = self.to_world(start_x, start_y)
        self.drag_start_xy = (wx, wy)
        self.drag_current_xy = (wx, wy)

        port = self.find_port_at(wx, wy)
        if port and port["direction"] == "out":
            self.connecting_from = port
            self.queue_draw()
            return

        link = self.find_link_at(wx, wy)
        if link:
            out_id, in_id = link
            out_port = self._port_by_id(out_id)
            if out_port:
                self.connecting_from = out_port
                self.detaching_link = (out_id, in_id)
                self.queue_draw()
                return

        nid = self.find_node_at(wx, wy)
        if nid is not None:
            self.dragging_node = nid
            self.drag_node_start = self.node_positions[nid]
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
            port = self.find_port_at(cur_x, cur_y)
            self.hover_target_port = (
                port if (port and port["direction"] == "in") else None
            )
            self.queue_draw()
            return

        if self.dragging_node is not None:
            nx = self.drag_node_start[0] + offset_x / self.zoom
            ny = self.drag_node_start[1] + offset_y / self.zoom
            self.node_positions[self.dragging_node] = (nx, ny)
            self.queue_draw()
            return

        if self.panning:
            self.pan_x = self.pan_drag_start[0] + offset_x
            self.pan_y = self.pan_drag_start[1] + offset_y
            self.queue_draw()

    def _send_link_undo_pair(self, forward_cmd, undo_cmd):
        """Send `forward_cmd` now and register `undo_cmd` (also
        re-sent through the client) as the undo action, refreshing the
        graph after each so the drawn state catches up to the daemon."""
        self.client.send(forward_cmd)
        GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        def _undo():
            self.client.send(undo_cmd)
            GLib.timeout_add(POST_MUTATION_REFRESH_MS, self.refresh)

        self.push_undo(_undo)

    def on_drag_end(self, gesture, offset_x, offset_y):
        if self.connecting_from:
            end_x = self.drag_start_xy[0] + offset_x / self.zoom
            end_y = self.drag_start_xy[1] + offset_y / self.zoom
            target = self.find_port_at(end_x, end_y)

            if self.detaching_link:
                out_id, old_in_id = self.detaching_link
                if target and target["direction"] == "in":
                    new_in_id = target["id"]
                    if new_in_id != old_in_id:
                        self._send_link_undo_pair(
                            {
                                "command": "disconnect_ports",
                                "output_port": out_id,
                                "input_port": old_in_id,
                            },
                            {
                                "command": "connect_ports",
                                "output_port": out_id,
                                "input_port": old_in_id,
                            },
                        )
                        self.client.send(
                            {
                                "command": "connect_ports",
                                "output_port": out_id,
                                "input_port": new_in_id,
                            }
                        )
                else:
                    self._send_link_undo_pair(
                        {
                            "command": "disconnect_ports",
                            "output_port": out_id,
                            "input_port": old_in_id,
                        },
                        {
                            "command": "connect_ports",
                            "output_port": out_id,
                            "input_port": old_in_id,
                        },
                    )
            elif target and target["direction"] == "in":
                out_id = self.connecting_from["id"]
                in_id = target["id"]
                if (out_id, in_id) in self.links:
                    self._send_link_undo_pair(
                        {
                            "command": "disconnect_ports",
                            "output_port": out_id,
                            "input_port": in_id,
                        },
                        {
                            "command": "connect_ports",
                            "output_port": out_id,
                            "input_port": in_id,
                        },
                    )
                else:
                    self._send_link_undo_pair(
                        {
                            "command": "connect_ports",
                            "output_port": out_id,
                            "input_port": in_id,
                        },
                        {
                            "command": "disconnect_ports",
                            "output_port": out_id,
                            "input_port": in_id,
                        },
                    )

            self.connecting_from = None
            self.detaching_link = None
            self.hover_target_port = None
            self.queue_draw()
            return

        if self.dragging_node is not None:
            nid, old_pos, new_pos = (
                self.dragging_node,
                self.drag_node_start,
                self.node_positions[self.dragging_node],
            )
            if old_pos != new_pos:

                def _undo(nid=nid, old_pos=old_pos):
                    self.node_positions[nid] = old_pos
                    self.layout_awake = True
                    self._settle_ticks = 0
                    self.queue_draw()

                self.push_undo(_undo)

        self.dragging_node = None
        self.panning = False

    def _port_by_id(self, port_id):
        for node in self.nodes.values():
            for port in node["inputs"] + node["outputs"]:
                if port["id"] == port_id:
                    return port
        return None

    def on_right_click(self, gesture, n_press, x, y):
        if n_press != 1:
            return
        self.grab_focus()
        wx, wy = self.to_world(x, y)
        link = self.find_link_at(wx, wy)
        if link:
            out_id, in_id = link
            self._send_link_undo_pair(
                {
                    "command": "disconnect_ports",
                    "output_port": out_id,
                    "input_port": in_id,
                },
                {
                    "command": "connect_ports",
                    "output_port": out_id,
                    "input_port": in_id,
                },
            )
