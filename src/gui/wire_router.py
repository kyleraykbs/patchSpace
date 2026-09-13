"""Orthogonal wire routing that steers edges around node rectangles.

Pure geometry (no GTK) so it can be unit-tested directly.  The graph
canvas uses it from PatchSpaceGraphWidget.on_draw: an edge is drawn as a
plain bezier when the straight path is clear, and only when that path
would cut through a node does ``route`` compute a short orthogonal detour
around the obstacles.
"""

from __future__ import annotations

import heapq
import math
from typing import Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Rect = Tuple[float, float, float, float]

# Grid pitch (world units) and how far obstacles are inflated so a wire
# keeps visible clearance from a node's border.
CELL = 36.0
PAD = 12.0
# How far outside the two endpoints the search grid extends, so there is
# room to route around a node sitting directly between them.
MARGIN = 220.0
# Extra A* cost for changing direction, so paths prefer straight runs.
TURN_COST = 0.6
# Half-thickness of the keep-out strips laid around already-routed wires so
# a new wire keeps visible clearance from them.  The router also inflates
# obstacles by PAD, so parallel wires end up about SPACING + PAD apart.
SPACING = 20.0
# How far a wire runs straight out of a socket before it may turn (the
# little horizontal stub that makes a connection read as plugged in), and
# the absolute minimum that stub may be shortened to.
STUB = 18.0
MIN_STUB = 14.0

_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1))


def _segment_hits_rect(
    x1: float, y1: float, x2: float, y2: float,
    rx1: float, ry1: float, rx2: float, ry2: float,
) -> bool:
    """Whether segment (x1,y1)-(x2,y2) intersects the axis-aligned rect."""
    dx = x2 - x1
    dy = y2 - y1
    t0, t1 = 0.0, 1.0
    # Liang-Barsky: clip the segment against the four slab boundaries.
    for p, q in (
        (-dx, x1 - rx1), (dx, rx2 - x1), (-dy, y1 - ry1), (dy, ry2 - y1),
    ):
        if p == 0.0:
            if q < 0.0:
                return False
            continue
        r = q / p
        if p < 0.0:
            if r > t1:
                return False
            if r > t0:
                t0 = r
        else:
            if r < t0:
                return False
            if r < t1:
                t1 = r
    return True


def segment_blocked(
    x1: float, y1: float, x2: float, y2: float,
    rects: Iterable[Rect], pad: float = PAD,
) -> bool:
    """Whether the straight segment passes through any (inflated) rect."""
    for rx1, ry1, rx2, ry2 in rects:
        if _segment_hits_rect(
            x1, y1, x2, y2, rx1 - pad, ry1 - pad, rx2 + pad, ry2 + pad
        ):
            return True
    return False


def orthogonalize(points: Sequence[Point]) -> List[Point]:
    """Insert L-elbows so every segment is axis-aligned ("square" routing).

    Only the segments into/out of the exact socket endpoints are ever
    diagonal (the A* grid points already differ on one axis), so this is
    cheap.  Wires leave and enter horizontally, matching the side-facing
    sockets."""
    pts = list(points)
    if len(pts) < 2:
        return pts
    out = [pts[0]]
    for k in range(1, len(pts)):
        px, py = pts[k]
        lx, ly = out[-1]
        if abs(px - lx) > 1e-6 and abs(py - ly) > 1e-6:
            # Last hop: turn into a horizontal approach; otherwise run
            # horizontally out of the previous point first.
            out.append((lx, py) if k == len(pts) - 1 else (px, ly))
        out.append((px, py))
    return out


def polyline_rects(points: Sequence[Point], half: float = SPACING) -> List[Rect]:
    """Thin keep-out rectangles for each segment of a routed wire, so a
    later wire can be routed to keep its distance (see SPACING)."""
    rects: List[Rect] = []
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        rects.append((
            min(ax, bx) - half, min(ay, by) - half,
            max(ax, bx) + half, max(ay, by) + half,
        ))
    return rects


def simplify(points: Sequence[Point]) -> List[Point]:
    """Drop near-duplicate and collinear midpoints from a polyline."""
    out: List[Point] = []
    for p in points:
        if out and math.hypot(p[0] - out[-1][0], p[1] - out[-1][1]) < 0.5:
            continue
        out.append(p)
    if len(out) < 3:
        return out
    simplified = [out[0]]
    for i in range(1, len(out) - 1):
        ax, ay = simplified[-1]
        bx, by = out[i]
        cx, cy = out[i + 1]
        cross = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
        if abs(cross) > 1e-6:
            simplified.append(out[i])
    simplified.append(out[-1])
    return simplified


def route(
    sx: float, sy: float, ex: float, ey: float,
    obstacles: Iterable[Rect],
    clear_rects: Iterable[Rect] = (),
    cell: float = CELL,
    pad: float = PAD,
    margin: float = MARGIN,
) -> Optional[List[Point]]:
    """A short orthogonal path from (sx,sy) to (ex,ey) avoiding `obstacles`,
    or None if no route was found (caller falls back to a straight bezier).

    ``clear_rects`` are holes punched in the blocked grid: corridors that
    let a wire exit its own node's socket even though that node is one of
    the obstacles.

    Returns world-space points; callers may append the exact socket
    endpoints around the result."""
    clear_rects = list(clear_rects)
    obstacles = list(obstacles)
    base_minx = min(sx, ex) - margin
    base_miny = min(sy, ey) - margin
    # Orient the grid so the start point lands exactly on a cell centre.
    # Otherwise the first cell centre can sit a fraction of a cell behind
    # the socket and the wire leaves with a tiny backward jog.
    minx = sx - round((sx - base_minx) / cell) * cell
    miny = sy - round((sy - base_miny) / cell) * cell
    maxx = max(sx, ex) + margin
    maxy = max(sy, ey) + margin
    cols = int(math.ceil((maxx - minx) / cell)) + 1
    rows = int(math.ceil((maxy - miny) / cell)) + 1
    if cols < 2 or rows < 2:
        return None

    def cell_of(x: float, y: float):
        return (
            min(cols - 1, max(0, int((x - minx) / cell))),
            min(rows - 1, max(0, int((y - miny) / cell))),
        )

    blocked = set()
    for ox1, oy1, ox2, oy2 in obstacles:
        ox1 -= pad
        oy1 -= pad
        ox2 += pad
        oy2 += pad
        i1, j1 = cell_of(ox1, oy1)
        i2, j2 = cell_of(ox2, oy2)
        for i in range(i1, i2 + 1):
            for j in range(j1, j2 + 1):
                if ox1 <= minx + i * cell <= ox2 and oy1 <= miny + j * cell <= oy2:
                    blocked.add((i, j))

    # Punch the socket corridors back out of the blocked grid so a wire can
    # leave/enter a node that is itself listed as an obstacle.
    for cx1, cy1, cx2, cy2 in clear_rects:
        i1, j1 = cell_of(cx1, cy1)
        i2, j2 = cell_of(cx2, cy2)
        for i in range(i1, i2 + 1):
            for j in range(j1, j2 + 1):
                blocked.discard((i, j))

    start = cell_of(sx, sy)
    goal = cell_of(ex, ey)
    blocked.discard(start)
    blocked.discard(goal)
    if start == goal:
        return [(sx, sy), (ex, ey)]

    def heuristic(i: int, j: int) -> float:
        return abs(i - goal[0]) + abs(j - goal[1])

    # A* over (cell, incoming direction) so turns can be penalised.
    best = {(start[0], start[1], -1): 0.0}
    came = {}
    heap = [(heuristic(*start), 0.0, start[0], start[1], -1)]
    found = None
    while heap:
        f, g, i, j, direction = heapq.heappop(heap)
        if (i, j) == goal:
            found = (i, j, direction)
            break
        if g > best.get((i, j, direction), math.inf) + 1e-9:
            continue
        for nd, (dx, dy) in enumerate(_DIRS):
            ni, nj = i + dx, j + dy
            if not (0 <= ni < cols and 0 <= nj < rows):
                continue
            if (ni, nj) in blocked:
                continue
            turn = 0.0 if direction in (-1, nd) else TURN_COST
            ng = g + 1.0 + turn
            key = (ni, nj, nd)
            if ng < best.get(key, math.inf) - 1e-9:
                best[key] = ng
                came[key] = (i, j, direction)
                heapq.heappush(
                    heap, (ng + heuristic(ni, nj), ng, ni, nj, nd)
                )
    if found is None:
        return None

    cells = []
    node = found
    while node in came:
        cells.append((node[0], node[1]))
        node = came[node]
    cells.append((node[0], node[1]))
    cells.reverse()
    points = [(sx, sy)]
    points += [(minx + i * cell, miny + j * cell) for i, j in cells[1:-1]]
    points.append((ex, ey))
    return simplify(orthogonalize(points))
