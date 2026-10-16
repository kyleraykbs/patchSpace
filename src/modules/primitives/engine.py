"""
engine.py — the primitives module's arithmetic and logic.

Every node of this module runs in this process, so evaluation is local: pull
each node's inputs from the nodes feeding it, compute its outputs, repeat
until nothing changes.  The pass count is bounded, so a cycle settles on its
last stable values instead of spinning (the graph the user draws may well
have one, and a hung module would take the whole playground with it).

**One slot per input, and one resolution order.**  A node's input port and the
control it is named for are the same thing (agents/SPACES.md §14): the port's
name is the control key, so a value that arrives on a wire, a value the daemon
relayed (`control_set`, or `value_set` for a raw value port) and a value the
user set all land in `config[<port>]`.  An input reads **the edge first, the
stored value second, the node's own default third** (agents/SPACES.md §19) —
for every value slot, whether it is a driven control (`math.a`) or a declared
value port (`display.text`), because the two are one mechanism seen from two
sides.

Which ports *are* value slots comes from this module's own declarations
(`api.value_slots`), the same listing the daemon builds when it decides what
the relay may deliver, so a value a slot's declaration cannot take — a
`choice` option it does not name — is refused here too
(`api.value_problem`), with the reason, and nothing is applied.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from central import module_api as api

logger = logging.getLogger(__name__)

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

    def __init__(self, declarations: Iterable[Dict[str, Any]] = ()) -> None:
        #: instance_id -> {"type_id": str, "config": {...}}
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, str]] = []
        #: instance_id -> {port: value}
        self.values: Dict[str, Dict[str, Any]] = {}
        #: type_id -> port name -> the value slot that port is: *this*
        #: module's own declaration read through the one grammar
        #: (`api.value_slots`, agents/SPACES.md §19), so the module knows
        #: exactly which ports take a value and what each accepts — the same
        #: listing the daemon builds to decide what the relay may deliver.
        #: Empty for an engine handed no declarations (a bare evaluation
        #: loop): with nothing declared there is nothing to check against.
        self.slots: Dict[str, Dict[str, Dict[str, Any]]] = {}
        for decl in declarations or ():
            if not isinstance(decl, dict):
                continue
            type_id = str(decl.get("type_id") or "")
            if not type_id:
                continue
            ports = list(decl.get("ports") or [])
            controls = list(decl.get("controls") or [])
            self.slots[type_id] = api.value_slots(ports, controls)

    # -- the value grammar (agents/SPACES.md §19) ------------------------

    def slot(self, type_id: str, port: str) -> Optional[Dict[str, Any]]:
        """The value slot a port of this type is, or ``None`` (an audio
        port, a plain setting, a port this module does not declare)."""
        return self.slots.get(str(type_id), {}).get(str(port))

    def refuses(self, type_id: str, port: str, value: Any) -> str:
        """Why this value cannot land in this port, or ``""``.

        ``""`` for a port that is no value slot (nothing here constrains a
        `setting` or a gain knob's range — that is the node's own business),
        and ``""`` for a slot that takes what it is given.  A `choice` is the
        one value a declaration constrains: it travels as its option, and an
        option the consumer does not declare is refused rather than silently
        kept (or silently coerced to a default by whatever reads it)."""
        slot = self.slot(type_id, port)
        if slot is None:
            return ""
        problem = api.value_problem(slot["kind"], slot["choices"], value)
        if not problem:
            return ""
        return f"{type_id}.{port}: {problem}"

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
        the daemon's relay (`control_set`, and `value_set` for a raw value
        port) lands exactly where the evaluator reads a wired input.

        **One door for every value that arrives from outside**, and one
        check: a value the slot's declaration cannot take — a `choice` option
        this module does not declare — is refused with the reason and *not*
        stored, so nothing is applied ([§19] — the daemon refuses to deliver
        one too, and both ask `api.value_problem`).

        The stored value is also what the node holds when nothing drives it;
        the daemon hands back the value it froze when the drive began, so a
        control the user had set survives a wire being plugged in and
        unplugged (see :meth:`_release_drive`)."""
        node = self.nodes.get(instance_id)
        if node is None:
            return False
        problem = self.refuses(node["type_id"], key, value)
        if problem:
            logger.warning("refused a value: %s", problem)
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
            """What feeds one input port — **the one resolution order**
            (agents/SPACES.md §19), for a driven control and a declared value
            port alike:

            1. the value **wired** to the port (a producer in this module
               reports it here; a producer in another module reaches the same
               slot as `control_set`/`value_set`, which land in `config` until
               the wiring is redone);
            2. else what the port's slot **holds** — the daemon's relayed
               value, or the value the user set;
            3. else the **node's own default** (`default`, or whatever this
               node reads an unfed slot as).

            A value a slot's declaration cannot take is not read at all: the
            rank below it stands instead, so an undeclared `choice` option
            arriving on a wire is refused rather than silently kept — the same
            `api.value_problem` the daemon asks before it delivers one."""
            if port in inputs and not self.refuses(type_id, port, inputs[port]):
                return inputs[port]
            if port in config and not self.refuses(type_id, port, config[port]):
                return config[port]
            return default

        if type_id == "number":
            return {"out": _number(given("value"))}
        if type_id == "const":
            return {"out": _number(given("value"))}
        if type_id == "slider":
            return {"out": min(1.0, max(0.0, _number(given("value"))))}
        if type_id == "toggle":
            return {"out": _boolean(given("on"))}
        if type_id == "string":
            return {"out": _text(given("value"))}
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
            op = str(given("op", "add") or "add")
            if op == "subtract":
                return {"out": a - b}
            if op == "multiply":
                return {"out": a * b}
            if op == "divide":
                return {"out": (a / b) if b else 0.0}
            return {"out": a + b}
        if type_id == "compare":
            a, b = _number(given("a")), _number(given("b"))
            op = str(given("op", "<") or "<")
            result = {
                "<": a < b, "<=": a <= b, "==": a == b,
                "!=": a != b, ">=": a >= b, ">": a > b,
            }.get(op, a < b)
            return {"out": result}
        if type_id == "logic":
            a, b = _boolean(given("a")), _boolean(given("b"))
            op = str(given("op", "and") or "and")
            if op == "or":
                return {"out": a or b}
            if op == "xor":
                return {"out": a != b}
            return {"out": a and b}
        return {}