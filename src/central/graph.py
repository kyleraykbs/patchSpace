"""
graph.py — the generic graph: spaces, node placements, edges, and the one
authority on what may connect to what.

Ownership split (agents/PLAN.md):

* the **registry** owns node identity — which instance exists, what type
  provides it, its config, whether it is online;
* this module owns **structure** — which space a node sits in, where it
  is, and the edges between instances.

Node records are looked up by id, never duplicated here.  The edge rules
live in exactly one place (``kinds_compatible`` plus the cardinality rule
in ``Graph.can_connect``) so that the old three-way mirror — the
model, the GUI's ``node_catalog`` and ``session_repair`` each re-checking
compatibility — has one authority to defer to.  Which *kinds* may pair is
decided here; whether the *value* on a wire may land on the port
(a `choice` option the consumer does not declare) is the other half of
agents/SPACES.md §19 and lives in ``central.module_api.value_problem``,
which the relay and the consuming module both ask.

How many drivers a receiving port takes is **declared, never assumed**:
``cardinality: "one" | "many"`` in the type declaration (S4, agents/SPACES.md
§15).  This module holds no kind list of its own (``SINGLE_DRIVER_KINDS`` is
gone) — the caller passes the registry's resolution in
(``cardinality_of=Registry.port_cardinality``), exactly as it passes port
kinds, and a port that declares nothing keeps the pre-S4 rule.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from central import module_api as api

#: Port kinds that interoperate with each other: a bundle is a set of
#: audio streams and one stream is a bundle of one (see the bundle nodes).
#: Every other kind pairs only with itself — including every value kind
#: (`central.module_api.VALUE_KINDS`: `number`, `boolean`, `text`, `choice`),
#: so a `choice` port pairs with a `choice` port and nothing else, and a
#: value never lands on a stream (agents/SPACES.md §19).
LINKABLE_KINDS = frozenset({"audio", "bundle"})


def kinds_compatible(from_kind: str, to_kind: str) -> bool:
    """Whether an output of ``from_kind`` may drive an input of ``to_kind`` —
    **the one** place that decides it (agents/SPACES.md §19).

    Only *declared* kinds may pair (`central.module_api.PORT_KINDS`), and only
    with themselves — except audio and bundle, which mix either way.  An
    unknown kind pairs with nothing, including another copy of itself: the
    vocabulary is what a pair is judged against, so a kind nothing declared
    is not a connection the daemon invents.  (The legacy rule let an unknown
    kind pair with anything; the registry-era rule is strict, which is what a
    numeric/boolean primitive needs to stay honest — and what the display's
    own mirror of this function states.)

    A value kind pairs only with itself, so a `choice` port pairs only with a
    `choice` port.  Whether a *value* may land on the port is the other half
    of §19 and lives in `central.module_api.value_problem`, asked by the relay
    and by the consuming module.
    """
    if not from_kind or not to_kind:
        return False
    if from_kind not in api.PORT_KINDS or to_kind not in api.PORT_KINDS:
        return False
    if from_kind in LINKABLE_KINDS and to_kind in LINKABLE_KINDS:
        return True
    return from_kind == to_kind


def edge_id(from_node: str, to_node: str, to_port: str = "in",
            from_port: str = "out") -> str:
    """Stable id for an edge — the historical ``a->b`` form for the common
    single-in/single-out case, ``:port`` for a named target and ``@port``
    for a named source (matching ``PatchSpace._edge_id`` exactly, so
    persisted edges survive the cutover)."""
    base = f"{from_node}->{to_node}"
    if to_port != "in":
        base += f":{to_port}"
    if from_port != "out":
        base += f"@{from_port}"
    return base


@dataclass
class Edge:
    from_node: str
    to_node: str
    to_port: str = "in"
    from_port: str = "out"

    @property
    def id(self) -> str:
        return edge_id(self.from_node, self.to_node, self.to_port, self.from_port)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "from_node": self.from_node,
            "to_node": self.to_node,
            "to_port": self.to_port,
            "from_port": self.from_port,
        }


@dataclass
class Space:
    """A routing region.  Phase 4 merges spaces with spaces (a blueprint
    plus per-instance diffs); today it is the grouping the placement and
    edge ownership hang off, with ``main`` as the default."""

    space_id: str
    label: str = ""
    parent: str = ""            # nested spaces (space-in-space) once phase 4 lands
    readonly: bool = False
    anchored: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space_id": self.space_id,
            "label": self.label,
            "parent": self.parent,
            "readonly": self.readonly,
            "anchored": self.anchored,
        }


@dataclass
class Placement:
    """Where an instance sits — structure, not identity.

    Two space-ish ids live here and they are **not** the same thing yet:
    ``space`` is this topology's routing region (`graph.Space.space_id`,
    default ``main``), while ``space_id`` is the *container* the node sits in
    (`spaces.py`'s tree, a ``Parent::Child`` path, ``""`` for the root - the
    same id the command protocol calls ``space_id``).  S1 merges the two
    models; until then both are carried, as before, under their own names.
    """

    space: str = "main"
    x: float = 0.0
    y: float = 0.0
    anchored: bool = False
    space_id: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "space": self.space,
            "x": self.x,
            "y": self.y,
            "anchored": self.anchored,
            "space_id": self.space_id,
        }


def _kind_of_none(_instance: str, _port: str, _direction: str) -> str:
    return ""


def _cardinality_none(_instance: str, _port: str, _direction: str) -> str:
    """No resolver given: nothing is declared, so nothing is restricted."""
    return api.CARDINALITY_MANY


class Graph:
    """Spaces, placements and edges, with the connection rules."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.spaces: Dict[str, Space] = {"main": Space("main", label="Main")}
        self.placements: Dict[str, Placement] = {}
        self.edges: Dict[str, Edge] = {}

    # -- spaces ----------------------------------------------------------

    def add_space(self, space_id: str, label: str = "", parent: str = "",
                  readonly: bool = False) -> Space:
        with self._lock:
            space = Space(space_id, label=label or space_id, parent=parent,
                          readonly=readonly)
            self.spaces[space_id] = space
            return space

    def remove_space(self, space_id: str) -> List[str]:
        """Drop a space and return the node ids that were sitting in it
        (their placements go with it)."""
        with self._lock:
            if space_id == "main":
                raise ValueError("the default space cannot be removed")
            self.spaces.pop(space_id, None)
            orphaned = [nid for nid, p in self.placements.items() if p.space == space_id]
            for node_id in orphaned:
                self.placements.pop(node_id, None)
            for eid in [eid for eid, e in self.edges.items()
                        if e.from_node in orphaned or e.to_node in orphaned]:
                self.edges.pop(eid, None)
            return orphaned

    # -- placements ------------------------------------------------------

    def place(self, instance_id: str, space: str = "main", x: float = 0.0,
              y: float = 0.0, anchored: bool = False, space_id: str = "") -> Placement:
        with self._lock:
            if space not in self.spaces:
                self.add_space(space)
            placement = self.placements.get(instance_id) or Placement()
            placement.space = space
            placement.x = x
            placement.y = y
            placement.anchored = anchored
            placement.space_id = space_id
            self.placements[instance_id] = placement
            return placement

    def placement(self, instance_id: str) -> Optional[Placement]:
        with self._lock:
            return self.placements.get(instance_id)

    def forget(self, instance_id: str) -> List[str]:
        """Drop a node's placement and every edge it took part in; returns
        the removed edge ids."""
        with self._lock:
            self.placements.pop(instance_id, None)
            removed = [eid for eid, e in self.edges.items()
                       if e.from_node == instance_id or e.to_node == instance_id]
            for eid in removed:
                self.edges.pop(eid, None)
            return removed

    # -- edges -----------------------------------------------------------

    def edges_into(self, to_node: str, to_port: str = "") -> List[Edge]:
        with self._lock:
            return [e for e in self.edges.values()
                    if e.to_node == to_node and (not to_port or e.to_port == to_port)]

    def edges_from(self, from_node: str, from_port: str = "") -> List[Edge]:
        """Every edge leaving one node (or one of its output ports).  The
        consumer side of the value relay looks a producer's reported port up
        here (agents/SPACES.md §14)."""
        with self._lock:
            return [e for e in self.edges.values()
                    if e.from_node == from_node
                    and (not from_port or e.from_port == from_port)]

    def edges_touching(self, instance_id: str) -> List[Edge]:
        with self._lock:
            return [e for e in self.edges.values()
                    if e.from_node == instance_id or e.to_node == instance_id]

    def can_connect(
        self,
        from_node: str,
        to_node: str,
        to_port: str = "in",
        from_port: str = "out",
        kind_of: Callable[[str, str, str], str] = _kind_of_none,
        cardinality_of: Callable[[str, str, str], str] = _cardinality_none,
    ) -> Tuple[bool, str]:
        """Whether the edge may be made.  ``kind_of``/``cardinality_of`` are
        how the caller (the daemon, via the registry) answers "what kind of
        port is this, and how many drivers does it take" — the graph holds
        no type knowledge of its own, so a port that declares ``one`` is
        refused a second driver and one that declares ``many`` is not.

        The declaration is the *only* thing refused: what a receiving node
        does with several drivers (``merge: mix|sum|last``) is the module's
        business, not the daemon's.
        """
        with self._lock:
            if from_node == to_node:
                return False, "a node cannot be wired to itself"
            from_kind = kind_of(from_node, from_port, "out")
            to_kind = kind_of(to_node, to_port, "in")
            if not from_kind:
                return False, f"{from_node}.{from_port} is not an output"
            if not to_kind:
                return False, f"{to_node}.{to_port} is not an input"
            if not kinds_compatible(from_kind, to_kind):
                return False, (
                    f"cannot connect a {from_kind} output to a {to_kind} input"
                )
            if cardinality_of(to_node, to_port, "in") != api.CARDINALITY_MANY:
                for existing in self.edges.values():
                    if existing.to_node == to_node and existing.to_port == to_port:
                        return False, f"{to_node}.{to_port} is already driven"
            return True, ""

    def connect(
        self,
        from_node: str,
        to_node: str,
        to_port: str = "in",
        from_port: str = "out",
        kind_of: Callable[[str, str, str], str] = _kind_of_none,
        cardinality_of: Callable[[str, str, str], str] = _cardinality_none,
    ) -> Edge:
        ok, message = self.can_connect(from_node, to_node, to_port, from_port,
                                       kind_of, cardinality_of)
        if not ok:
            raise ValueError(message)
        with self._lock:
            edge = Edge(from_node, to_node, to_port, from_port)
            self.edges[edge.id] = edge
            return edge

    def disconnect(self, from_node: str, to_node: str = "", to_port: str = "in",
                   from_port: str = "out") -> bool:
        """Remove an edge by its full id (``to_node`` empty) or by endpoints."""
        with self._lock:
            eid = (
                from_node
                if not to_node
                else edge_id(from_node, to_node, to_port, from_port)
            )
            return self.edges.pop(eid, None) is not None

    # -- persistence -----------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "spaces": {sid: s.to_dict() for sid, s in self.spaces.items()},
                "placements": {nid: p.to_dict() for nid, p in self.placements.items()},
                "edges": {eid: e.to_dict() for eid, e in self.edges.items()},
            }

    def load(self, data: Dict[str, Any]) -> None:
        with self._lock:
            for sid, space in (data.get("spaces") or {}).items():
                self.spaces[sid] = Space(
                    sid,
                    label=space.get("label", ""),
                    parent=space.get("parent", ""),
                    readonly=bool(space.get("readonly", False)),
                    anchored=bool(space.get("anchored", False)),
                )
            self.spaces.setdefault("main", Space("main", label="Main"))
            for nid, placement in (data.get("placements") or {}).items():
                self.placements[nid] = Placement(
                    space=placement.get("space", "main"),
                    x=float(placement.get("x", 0.0)),
                    y=float(placement.get("y", 0.0)),
                    anchored=bool(placement.get("anchored", False)),
                    # A snapshot dict written before S0 spells this `panel`
                    # (read, never written - the one-release window).
                    space_id=placement.get(
                        "space_id", placement.get("panel", "")
                    ),
                )
            self.edges = {}
            for eid, edge in (data.get("edges") or {}).items():
                record = Edge(
                    str(edge.get("from_node", "")),
                    str(edge.get("to_node", "")),
                    str(edge.get("to_port", "in")),
                    str(edge.get("from_port", "out")),
                )
                self.edges[eid] = record
