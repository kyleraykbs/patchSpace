"""
force_layout.py

Force-directed layout used by both graph widgets to auto-arrange
nodes: mutual repulsion, spring edges, a left-to-right "flow" bias so
signal chains read source -> sink, a light pull toward the shared
centroid, and a final overlap-resolution pass so dense clusters don't
end up with visually stacked nodes.
"""

from __future__ import annotations

import math
import random
from collections import defaultdict


class ForceLayout:
    def __init__(
        self,
        repulsion=100000.0,
        spring_length=450.0,
        spring_k=0.02,
        center_k=0.0015,
        damping=0.80,
        max_speed=40.0,
        flow_gap=400.0,
        flow_k=0.025,
        repulsion_cutoff=1200.0,
        size_aware_springs=False,
        overlap_padding=14.0,
    ):
        self.repulsion = repulsion
        self.spring_length = spring_length
        self.spring_k = spring_k
        self.center_k = center_k
        self.damping = damping
        self.max_speed = max_speed
        self.flow_gap = flow_gap
        self.flow_k = flow_k
        # Beyond this distance, repulsion (which falls off as 1/dist^2)
        # is negligible. Pairs farther apart than this are skipped
        # entirely instead of computed and rounded down to ~0 - this is
        # what makes grid bucketing below pay off on a large graph,
        # where most pairs are farther apart than this anyway.
        self.repulsion_cutoff = repulsion_cutoff
        # When True, an edge's rest length grows by each end's half-size
        # projected onto the edge direction, so large boxes (panels) spring
        # to sit edge-to-edge instead of overlapping at a fixed distance.
        self.size_aware_springs = size_aware_springs
        # Minimum gap the overlap-resolution pass leaves between two
        # rectangles (see _resolve_overlaps).  Nodes use a small value;
        # panels use a larger one so their boxes don't touch.
        self.overlap_padding = overlap_padding
        self.velocities: dict = {}

    def _ensure(self, node_id):
        if node_id not in self.velocities:
            self.velocities[node_id] = [0.0, 0.0]

    def prune(self, valid_ids) -> None:
        valid_ids = set(valid_ids)
        for nid in list(self.velocities.keys()):
            if nid not in valid_ids:
                del self.velocities[nid]

    @staticmethod
    def _build_grid(ids, points, cell_size):
        """Bucket ids into a grid of `cell_size`-sized cells, keyed by
        (col, row), based on `points[id]`. Used for both the repulsion
        pass (centers, cell_size ~ repulsion_cutoff) and the overlap
        pass (top-left corners, cell_size ~ max node dimension) - same
        technique, different granularity."""
        grid = defaultdict(list)
        for nid in ids:
            x, y = points[nid]
            grid[(int(x // cell_size), int(y // cell_size))].append(nid)
        return grid

    @staticmethod
    def _grid_pairs(ids, grid, cell_size, points):
        """Yield each candidate (a, b) pair exactly once: every id
        paired with every other id in its own cell or one of the 8
        neighboring cells. Anything farther apart than ~cell_size is
        never yielded, which is the whole point - the caller is
        responsible for a final precise distance/overlap check, this
        is just cheap pre-filtering."""
        seen = set()
        for (cx, cy), cell_ids in grid.items():
            neighbors = []
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbors.extend(grid.get((cx + dx, cy + dy), ()))
            for a in cell_ids:
                for b in neighbors:
                    if a == b:
                        continue
                    pair = (a, b) if a < b else (b, a)
                    if pair in seen:
                        continue
                    seen.add(pair)
                    yield pair

    def step(self, node_ids, positions, sizes, edges, pinned=None) -> float:
        pinned = pinned or set()
        ids = list(node_ids)
        if len(ids) < 2:
            return 0.0

        centers = {}
        for nid in ids:
            x, y = positions[nid]
            w, h = sizes.get(nid, (160, 80))
            centers[nid] = (x + w / 2.0, y + h / 2.0)

        forces = {nid: [0.0, 0.0] for nid in ids}

        # ---- repulsion: grid-bucketed instead of all-pairs ----
        cell_size = max(self.repulsion_cutoff, 1.0)
        grid = self._build_grid(ids, centers, cell_size)
        cutoff_sq = self.repulsion_cutoff * self.repulsion_cutoff

        for a, b in self._grid_pairs(ids, grid, cell_size, centers):
            ax, ay = centers[a]
            bx, by = centers[b]
            dx = ax - bx
            dy = ay - by
            dist_sq = dx * dx + dy * dy
            if dist_sq > cutoff_sq:
                continue
            if dist_sq < 1.0:
                dx = (random.random() - 0.5) or 0.01
                dy = (random.random() - 0.5) or 0.01
                dist_sq = dx * dx + dy * dy
            dist = math.sqrt(dist_sq)
            force = self.repulsion / dist_sq
            fx = (dx / dist) * force
            fy = (dy / dist) * force
            forces[a][0] += fx
            forces[a][1] += fy
            forces[b][0] -= fx
            forces[b][1] -= fy

        for a, b in edges:
            if a not in centers or b not in centers or a == b:
                continue
            ax, ay = centers[a]
            bx, by = centers[b]
            dx = bx - ax
            dy = by - ay
            dist = math.hypot(dx, dy) or 1.0
            rest = self.spring_length
            if self.size_aware_springs:
                ux, uy = dx / dist, dy / dist
                aw, ah = sizes.get(a, (160.0, 80.0))
                bw, bh = sizes.get(b, (160.0, 80.0))
                rest += (
                    abs(ux) * aw / 2.0 + abs(uy) * ah / 2.0
                    + abs(ux) * bw / 2.0 + abs(uy) * bh / 2.0
                )
            stretch = dist - rest
            force = stretch * self.spring_k
            fx = (dx / dist) * force
            fy = (dy / dist) * force
            forces[a][0] += fx
            forces[a][1] += fy
            forces[b][0] -= fx
            forces[b][1] -= fy

            if dx < self.flow_gap:
                deficit = self.flow_gap - dx
                push = deficit * self.flow_k
                forces[a][0] -= push
                forces[b][0] += push

        cx = sum(c[0] for c in centers.values()) / len(centers)
        cy = sum(c[1] for c in centers.values()) / len(centers)
        for nid in ids:
            x, y = centers[nid]
            forces[nid][0] += (cx - x) * self.center_k
            forces[nid][1] += (cy - y) * self.center_k

        max_delta = 0.0
        for nid in ids:
            self._ensure(nid)
            vel = self.velocities[nid]
            if nid in pinned:
                vel[0] = 0.0
                vel[1] = 0.0
                continue
            vel[0] = (vel[0] + forces[nid][0]) * self.damping
            vel[1] = (vel[1] + forces[nid][1]) * self.damping
            speed = math.hypot(vel[0], vel[1])
            if speed > self.max_speed:
                scale = self.max_speed / speed
                vel[0] *= scale
                vel[1] *= scale
            x, y = positions[nid]
            positions[nid] = (x + vel[0], y + vel[1])
            max_delta = max(max_delta, abs(vel[0]), abs(vel[1]))

        overlap_delta = self._resolve_overlaps(ids, positions, sizes, pinned)
        return max(max_delta, overlap_delta)

    def _resolve_overlaps(self, ids, positions, sizes, pinned) -> float:
        """Directly separate any pair of overlapping node rectangles by
        half the overlap each. This is a position correction, not a
        force - it runs after the physics step and converges within a
        few ticks regardless of how tightly a cluster has packed
        together, which pure force integration doesn't guarantee (see
        module docstring)."""
        max_shift = 0.0
        pad = self.overlap_padding
        cell_size = max((w for w, _ in sizes.values()), default=200) * 1.5
        corners = {nid: positions[nid] for nid in ids}
        grid = self._build_grid(ids, corners, cell_size)

        for a, b in self._grid_pairs(ids, grid, cell_size, corners):
            ax, ay = positions[a]
            aw, ah = sizes.get(a, (160, 80))
            bx, by = positions[b]
            bw, bh = sizes.get(b, (160, 80))

            # Inflate the overlap by `pad` so the correction separates the
            # rectangles to a visible gap, not just to touching.
            overlap_x = min(ax + aw, bx + bw) - max(ax, bx) + pad
            overlap_y = min(ay + ah, by + bh) - max(ay, by) + pad
            if overlap_x <= 0 or overlap_y <= 0:
                continue

            a_pinned, b_pinned = a in pinned, b in pinned
            if a_pinned and b_pinned:
                continue

            # Separate along the axis with the smaller overlap - the
            # cheaper correction, and it avoids nodes jumping
            # diagonally across the canvas for a mostly-horizontal
            # overlap (or vice versa).
            if overlap_x < overlap_y:
                push = overlap_x / 2.0
                a_dir, b_dir = (-1, 1) if ax < bx else (1, -1)
                if not a_pinned:
                    positions[a] = (ax + a_dir * push, ay)
                if not b_pinned:
                    positions[b] = (bx + b_dir * push, by)
            else:
                push = overlap_y / 2.0
                a_dir, b_dir = (-1, 1) if ay < by else (1, -1)
                if not a_pinned:
                    positions[a] = (ax, ay + a_dir * push)
                if not b_pinned:
                    positions[b] = (bx, by + b_dir * push)

            max_shift = max(max_shift, push)

        return max_shift
