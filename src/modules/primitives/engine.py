"""
engine.py — the primitives module's arithmetic and logic.

Every node of this module runs in this process, so evaluation is local: pull
each node's inputs from the nodes feeding it, compute its outputs, repeat
until nothing changes.  The pass count is bounded, so a cycle settles on its
last stable values instead of spinning (the graph the user draws may well
have one, and a hung module would take the whole playground with it).

**One slot per input.**  A node's input port and the control it is named for
are the same thing (agents/SPACES.md §14): the port's name is the control
key, so a value that arrives on a wire, a value the daemon relayed
(`control_set`, or `value_set` for a raw port) and a value the user set all
land in `config[<port>]`, and an input reads whichever of the two it finds —
the wire first, the control second.
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


def _text(value: Any) -> str:
    """A text value: a string node holds one, and anything else is its own
    spelling (a text port never refuses what it is handed)."""
    return "" if value is None else str(value)


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
        """One control of one node — and one *input* of it, because a port
        that drives a control is named for it (§14: the name is the key), so
        the daemon's relay (`control_set`, and `value_set` for a raw port)
        lands where the evaluator reads a wired input.

        The stored value is also what the node holds when nothing drives it;
        the daemon hands back the value it froze when the drive began, so a
        control the user had set survives a wire being plugged in and
        unplugged (see :meth:`_release_drive`)."""
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
            if record in self.edges:
                self.edges = [e for e in self.edges if e != record]
                self._release_drive(record)
        elif record not in self.edges:
            self.edges.append(record)
        self.evaluate()

    def _release_drive(self, record: Dict[str, str]) -> None:
        """A driver went away: unless another edge still feeds that port,
        forget the value it pushed into the control the port names, so the
        input falls back to the node's own default.

        The daemon relays a producer's value *as that control's value*, and
        the module cannot tell it from a value the user set — so what a
        control goes back to is the daemon's business: it froze the control's
        local value when the drive began and restores it after it has told
        the module the edge is gone (`node_edge` forwards `edge_apply` and
        only then binds the control edge), so clearing here cannot wipe a
        value the daemon is about to put back.  A control nothing has ever
        set has no value to restore, and this is then exactly the node's
        default (`0.0` for an unfed operand, as it was before the port could
        be driven at all).
        """
        node = self.nodes.get(record["to_node"])
        if node is None:
            return
        port = record["to_port"]
        if any(e["to_node"] == record["to_node"] and e["to_port"] == port
               for e in self.edges):
            return
        node["config"].pop(port, None)

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
        """What is wired to one node's input ports right now.

        An edge from a node of another module has no value here (its owner
        holds it, and the daemon relays what it reports), so such a port
        simply has no entry and the control it names stands in."""
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

        def given(port: str, default: Any = None) -> Any:
            """What feeds one input port: the value wired to it, else what
            the control it is named for holds (the daemon's relay, or the
            value the user set), else nothing."""
            if port in inputs:
                return inputs[port]
            return config.get(port, default)

        if type_id == "number":
            return {"out": _number(config.get("value"))}
        if type_id == "const":
            return {"out": _number(config.get("value"))}
        if type_id == "slider":
            return {"out": min(1.0, max(0.0, _number(config.get("value"))))}
        if type_id == "toggle":
            return {"out": _boolean(config.get("on"))}
        if type_id == "string":
            return {"out": _text(config.get("value"))}
        if type_id == "display":
            value = _number(given("in"))
            shown: Dict[str, Any] = {"in": value, "out": value}
            text = _text(given("text"))
            if text:
                shown["text"] = text
            return shown
        if type_id == "invert":
            return {"out": not _boolean(given("a"))}
        if type_id == "math":
            a, b = _number(given("a")), _number(given("b"))
            op = str(config.get("op") or "add")
            if op == "subtract":
                return {"out": a - b}
            if op == "multiply":
                return {"out": a * b}
            if op == "divide":
                return {"out": (a / b) if b else 0.0}
            return {"out": a + b}
        if type_id == "compare":
            a, b = _number(given("a")), _number(given("b"))
            op = str(config.get("op") or "<")
            result = {
                "<": a < b, "<=": a <= b, "==": a == b,
                "!=": a != b, ">=": a >= b, ">": a > b,
            }.get(op, a < b)
            return {"out": result}
        if type_id == "logic":
            a, b = _boolean(given("a")), _boolean(given("b"))
            op = str(config.get("op") or "and")
            if op == "or":
                return {"out": a or b}
            if op == "xor":
                return {"out": a != b}
            return {"out": a and b}
        return {}