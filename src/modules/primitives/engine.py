"""
engine.py — the primitives module's arithmetic and logic.

Every node of this module runs in this process, so evaluation is local: pull
each node's inputs from the nodes feeding it, compute its outputs, repeat
until nothing changes.  The pass count is bounded, so a cycle settles on its
last stable values instead of spinning (the graph the user draws may well
have one, and a hung module would take the whole playground with it).
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

#: How many passes a single evaluation may take before it is declared
#: settled.  Longer than the longest sensible chain; a cycle simply stops
#: changing after the first pass or two.
MAX_PASSES = 64


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


class Engine:
    """The module's nodes, edges and current values."""

    def __init__(self) -> None:
        #: instance_id -> {"type_id": str, "config": {...}}
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, str]] = []
        #: instance_id -> {port: value}
        self.values: Dict[str, Dict[str, Any]] = {}

    # -- the graph -------------------------------------------------------

    def add_nodes(self, nodes: Iterable[Dict[str, Any]]) -> None:
        for node in nodes:
            instance_id = str(node.get("instance_id") or "")
            if not instance_id:
                continue
            self.nodes[instance_id] = {
                "type_id": str(node.get("type_id") or ""),
                "config": dict(node.get("config") or {}),
            }
        self.evaluate()

    def remove_nodes(self, instance_ids: Iterable[str]) -> None:
        for instance_id in instance_ids:
            self.nodes.pop(instance_id, None)
            self.values.pop(instance_id, None)
            self.edges = [
                e for e in self.edges
                if e["from_node"] != instance_id and e["to_node"] != instance_id
            ]
        self.evaluate()

    def set_control(self, instance_id: str, key: str, value: Any) -> bool:
        node = self.nodes.get(instance_id)
        if node is None:
            return False
        node["config"][str(key)] = value
        self.evaluate()
        return True

    def apply_edge(self, edge: Dict[str, Any], op: str) -> None:
        from_node = str(edge.get("from_node") or "")
        to_node = str(edge.get("to_node") or "")
        record = {
            "from_node": from_node,
            "from_port": str(edge.get("from_port") or "out"),
            "to_node": to_node,
            "to_port": str(edge.get("to_port") or "in"),
        }
        if op == "remove":
            self.edges = [e for e in self.edges if e != record]
        elif record not in self.edges:
            self.edges.append(record)
        self.evaluate()

    # -- evaluation ------------------------------------------------------

    def evaluate(self) -> None:
        """Recompute every node's outputs until they stop changing."""
        previous = {k: dict(v) for k, v in self.values.items()}
        for _ in range(MAX_PASSES):
            snapshot = {k: dict(v) for k, v in self.values.items()}
            for instance_id, node in self.nodes.items():
                self.values[instance_id] = self._compute(instance_id, node, snapshot)
            if self.values == snapshot:
                break
        # Nothing to publish if nothing moved.
        if self.values == previous:
            self.values = previous

    def _inputs(self, instance_id: str, snapshot: Dict[str, Dict[str, Any]]
                ) -> Dict[str, Any]:
        incoming: Dict[str, Any] = {}
        for edge in self.edges:
            if edge["to_node"] != instance_id:
                continue
            source = snapshot.get(edge["from_node"], {})
            if edge["from_port"] in source:
                incoming[edge["to_port"]] = source[edge["from_port"]]
        return incoming

    def _compute(self, instance_id: str, node: Dict[str, Any],
                 snapshot: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        type_id = node["type_id"]
        config = node["config"]
        inputs = self._inputs(instance_id, snapshot)

        if type_id == "number":
            return {"out": _number(config.get("value"))}
        if type_id == "slider":
            return {"out": min(1.0, max(0.0, _number(config.get("value"))))}
        if type_id == "toggle":
            return {"out": _boolean(config.get("on"))}
        if type_id == "display":
            value = inputs.get("in", config.get("value"))
            return {"in": _number(value), "out": _number(value)}
        if type_id == "invert":
            return {"out": not _boolean(inputs.get("a"))}
        if type_id == "math":
            a, b = _number(inputs.get("a")), _number(inputs.get("b"))
            op = str(config.get("op") or "add")
            if op == "subtract":
                return {"out": a - b}
            if op == "multiply":
                return {"out": a * b}
            if op == "divide":
                return {"out": (a / b) if b else 0.0}
            return {"out": a + b}
        if type_id == "compare":
            a, b = _number(inputs.get("a")), _number(inputs.get("b"))
            op = str(config.get("op") or "<")
            result = {
                "<": a < b, "<=": a <= b, "==": a == b,
                "!=": a != b, ">=": a >= b, ">": a > b,
            }.get(op, a < b)
            return {"out": result}
        if type_id == "logic":
            a, b = _boolean(inputs.get("a")), _boolean(inputs.get("b"))
            op = str(config.get("op") or "and")
            if op == "or":
                return {"out": a or b}
            if op == "xor":
                return {"out": a != b}
            return {"out": a and b}
        return {}
