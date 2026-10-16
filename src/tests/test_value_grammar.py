"""One value grammar: the vocabulary, the connection rule, and the relay
(agents/SPACES.md §19).

After S3 and S5 a value can reach a consumer two ways — an auto-declared
`drives_control` port (S3) and a declared value input port (S5's
`display.text`) — and they must not diverge.  These tests put the two paths
side by side and hold them to one vocabulary (`api.VALUE_KINDS`), one
connection decision (`graph.kinds_compatible`), one listing of the ports that
take a value (`api.value_slots`), one acceptance rule (`api.value_problem`,
asked by the daemon and by the consuming module) and one resolution order
(edge, then stored, then the node's own default).

    * the kind matrix, swept whole, accepts and refuses exactly as it did;
    * a `choice` port pairs only with a `choice` port;
    * a `choice` value naming an option the consumer does not declare is
      refused, with the reason, and nothing is applied;
    * both paths resolve a slot the same way, and the real protocol carries
      both to the same place (a producer in one module, the consumer in
      another, over a socket).
"""

import time
from typing import Any, Dict, List

import pytest

from central import module_api as api
from central import graph as g
from central.graph import Graph
from central.protocol import Connection, ProtocolHandler
from central.registry import Registry
from modules.primitives import types
from modules.primitives.engine import Engine
from modules.primitives.main import Primitives

from tests.test_module_host import DaemonSide

#: Every kind a port may declare, plus the two things that are not kinds:
#: nothing, and a kind nobody declared.
MATRIX_KINDS = list(api.PORT_KINDS) + ["", "widget"]


# ----------------------------------------------------------------------
# one vocabulary, one connection decision
# ----------------------------------------------------------------------

def test_the_vocabularies_are_one():
    """The value vocabulary is the four §19 names, the node kinds are the
    rest, and every kind a port may declare is one of the two — so the
    connection rules, the relay and the type validation are written against
    one list rather than three."""
    assert api.VALUE_KINDS == ("number", "boolean", "text", "choice")
    assert set(api.PORT_KINDS) == set(api.NODE_KINDS) | set(api.VALUE_KINDS)
    assert len(api.PORT_KINDS) == len(set(api.PORT_KINDS))
    for kind in api.VALUE_KINDS:
        assert api.is_value_kind(kind)
    for kind in api.NODE_KINDS:
        assert not api.is_value_kind(kind)


@pytest.mark.parametrize("from_kind", MATRIX_KINDS)
@pytest.mark.parametrize("to_kind", MATRIX_KINDS)
def test_the_kind_matrix_accepts_and_refuses_exactly_as_before(from_kind, to_kind):
    """The daemon's rule over *every* kind pair, pinned: two declared kinds
    pair when they are the same kind or both stream kinds (audio/bundle), and
    nothing else pairs — a value kind never lands on a stream and an
    undeclared kind pairs with nothing at all (agents/SPACES.md §19).  This
    is the matrix that must not move."""
    declared = from_kind in api.PORT_KINDS and to_kind in api.PORT_KINDS
    linkable = from_kind in g.LINKABLE_KINDS and to_kind in g.LINKABLE_KINDS
    expected = bool(declared and (linkable or from_kind == to_kind))
    assert g.kinds_compatible(from_kind, to_kind) is expected, \
        (from_kind, to_kind)


def test_a_choice_port_pairs_only_with_a_choice_port():
    """§19 names it: the option list is not consulted when two ports are
    judged — the *kind* is — so `choice` pairs with `choice` and with nothing
    else, and what the options are is the relay's business (below)."""
    assert g.kinds_compatible("choice", "choice") is True
    for other in api.PORT_KINDS:
        if other != "choice":
            assert g.kinds_compatible("choice", other) is False
            assert g.kinds_compatible(other, "choice") is False


def test_an_undeclared_kind_pairs_with_nothing_not_even_itself():
    """The strict reading of "only declared kinds may pair": the daemon does
    not invent a connection out of two copies of a kind nothing declared
    (the display's mirror of this function states the same rule)."""
    assert g.kinds_compatible("widget", "widget") is False
    assert g.kinds_compatible("widget", "number") is False


def test_a_declaration_must_name_its_options():
    """A `choice` that names no options can take no value: refused where it
    is declared, rather than swallowing every value later (§19)."""
    ok, message = api.validate_type_decl({
        "type_id": "t", "schema": 1,
        "controls": [{"key": "mode", "widget": "choice"}],
    })
    assert not ok and "options" in message

    ok, message = api.validate_type_decl({
        "type_id": "t", "schema": 1,
        "ports": [{"name": "mode", "kind": "choice", "direction": "in"}],
    })
    assert not ok and "options" in message

    # Declared where it belongs, it validates — and `choices` is a value
    # port's field, not a stream's.
    ok, message = api.validate_type_decl({
        "type_id": "t", "schema": 1,
        "ports": [{"name": "mode", "kind": "choice", "direction": "in",
                   "choices": ["a", "b"]}],
    })
    assert ok, message
    ok, message = api.validate_type_decl({
        "type_id": "t", "schema": 1,
        "ports": [{"name": "in", "kind": "audio", "direction": "in",
                   "choices": ["a", "b"]}],
    })
    assert not ok and "value port" in message


def test_the_one_listing_is_the_daemons_and_the_consumers():
    """`api.value_slots` is *the* listing of the ports that take a value: the
    daemon builds it when a type is declared and the consuming module builds
    it from the declarations it announced, so the two sides of the relay
    cannot disagree about which ports are slots — only the *value* is a
    question, and that is `api.value_problem`."""
    reg = Registry()
    reg.attach("primitives", "primitives", "1.0")
    assert reg.declare_types("primitives", types.TYPES)[0] == []
    reg.place("d1", "display")
    reg.place("m1", "math")

    for type_id in ("display", "math", "number", "invert"):
        decl = next(t for t in types.TYPES if t["type_id"] == type_id)
        assert reg.type_decl(type_id).slots == api.value_slots(
            decl["ports"], decl["controls"]
        ), type_id

    # ...and the spelled-out cases: a driven control, a declared value port,
    # and a port that is neither.
    assert reg.value_slot("d1", "in")["control"] == "in"
    assert reg.value_slot("d1", "text") == {
        "name": "text", "kind": "text", "control": "", "choices": (),
    }
    assert reg.value_slot("m1", "op")["choices"] == (
        "add", "subtract", "multiply", "divide",
    )
    assert reg.value_slot("m1", "out") is None
    assert reg.value_slot("d1", "nobody") is None


# ----------------------------------------------------------------------
# one resolution order, for both paths
# ----------------------------------------------------------------------

#: (port, kind, a value to store, what drives it, the declared default).
#: `display.in` is the S3 path (a `drives_control` port auto-declared for the
#: node's own control); `display.text` is the S5 one (a declared value port
#: with no control).  One test, both paths.
SLOTS = [
    ("in", "number", 0.5, 0.75, 0.0),
    ("text", "text", "stored", "driven", ""),
]


@pytest.mark.parametrize("port,kind,stored,driven,default", SLOTS)
def test_every_value_slot_resolves_the_same_way(port, kind, stored, driven, default):
    """The order §19 fixes — **edge, then stored, then the node's own
    default** — read through the consumer's one implementation (`given`), for
    the S3 path (a driven control) and the S5 path (a declared value port)
    alike.  The two paths are one test body, parameterised: if either ever
    grows its own rank, this fails."""
    engine = Engine(types.TYPES)
    engine.add_nodes([
        {"instance_id": "src", "type_id": "string" if kind == "text" else "number"},
        {"instance_id": "d1", "type_id": "display"},
    ])
    assert engine.slot("display", port)["kind"] == kind

    def shown() -> Any:
        """What the node reports for the slot.  The display reports `text`
        only when it holds one, so the declared default (no text) shows as an
        absent key rather than an empty one."""
        return engine.values["d1"].get(port, default)

    # 3rd rank: nothing stored and nothing wired — the node's own default.
    assert shown() == default

    # 2nd rank: a stored value (the user set it, or the daemon relayed one
    # into the empty slot) beats the default.
    assert engine.set_control("d1", port, stored) is True
    assert shown() == stored

    # 1st rank: a wire beats the stored value.
    engine.nodes["src"]["config"]["value"] = driven
    engine.apply_edge({"from_node": "src", "to_node": "d1", "to_port": port,
                       "from_port": "out"}, "add")
    assert shown() == driven

    # ...and when the wire goes, the driven slot is cleared (the daemon hands
    # the frozen value back — see the end-to-end test below), so the node's
    # default stands again until something sets the slot.
    engine.apply_edge({"from_node": "src", "to_node": "d1", "to_port": port,
                       "from_port": "out"}, "remove")
    assert port not in engine.nodes["d1"]["config"]
    assert shown() == default
    engine.set_control("d1", port, stored)
    assert shown() == stored


#: A consumer whose choice is a **declared value port** — no control behind
#: it — so the refusal below is exercised on the S5 path as well as on the S3
#: one (`math.op`).
CHOOSER = {
    "type_id": "chooser",
    "schema": 1,
    "ports": [{"name": "mode", "kind": "choice", "direction": "in",
               "choices": ["a", "b"]}],
    "display": {"name": "Chooser"},
}


@pytest.mark.parametrize("instance_id,type_id,port,other", [
    ("m1", "math", "op", "multiply"),     # a `drives_control` choice port
    ("c1", "chooser", "mode", "a"),       # a declared choice value port
])
def test_the_consumers_read_of_a_choice_is_the_daemons(
    instance_id, type_id, port, other,
):
    """The consumer refuses a value its declaration cannot take — through the
    same rule the daemon asks before delivering one — and it refuses it on
    either path: nothing is applied, and the slot keeps what it had."""
    engine = Engine(types.TYPES + [CHOOSER])
    engine.add_nodes([{"instance_id": instance_id, "type_id": type_id}])

    # An option the declaration names applies ...
    assert engine.set_control(instance_id, port, other) is True
    assert engine.nodes[instance_id]["config"][port] == other

    # ... and one it does not is refused, with the reason, leaving the slot
    # exactly as it was.
    assert engine.set_control(instance_id, port, "frobnicate") is False
    assert engine.nodes[instance_id]["config"][port] == other
    assert engine.refuses(type_id, port, "frobnicate")

    # A value for a node this module does not run is refused, not a crash,
    # and a port that is no value slot takes anything (it is not ours to
    # judge — a `setting`, a button, a knob's range).
    assert engine.set_control("nope", port, "frobnicate") is False
    assert engine.refuses(type_id, "nobody", "frobnicate") == ""
    assert engine.set_control(instance_id, "nobody", "anything") is True


# ----------------------------------------------------------------------
# the relay: what the daemon delivers, and what it refuses
# ----------------------------------------------------------------------

#: A producer of every value kind, so an edge can carry one to either path.
SOURCE = {
    "type_id": "value_source",
    "schema": 1,
    "ports": [
        {"name": "num", "kind": "number", "direction": "out"},
        {"name": "txt", "kind": "text", "direction": "out"},
        {"name": "mode", "kind": "choice", "direction": "out"},
    ],
    "display": {"name": "Value Source"},
}


class Consumer:
    """A module that owns primitives nodes, over the daemon's own side of the
    protocol: every message it is sent is logged and applied exactly as
    `modules/primitives/main.py` applies it (`control_set` and `value_set`
    both land in `Engine.set_control`, the node's one slot per input)."""

    def __init__(self, name: str = "sink") -> None:
        self.name = name
        self.engine = Engine(types.TYPES)
        self.log: List[Dict[str, Any]] = []

    def deliver(self, message: Dict[str, Any]) -> None:
        self.log.append(message)
        kind = message.get("type")
        if kind == "control_set":
            self.engine.set_control(str(message.get("instance_id") or ""),
                                    str(message.get("key") or ""),
                                    message.get("value"))
        elif kind == "value_set":
            self.engine.set_control(str(message.get("instance_id") or ""),
                                    str(message.get("port") or ""),
                                    message.get("value"))
        elif kind == "edge_apply":
            self.engine.apply_edge(message.get("edge") or {},
                                   str(message.get("op") or "add"))
        elif kind == "node_spawn":
            self.engine.add_nodes(message.get("nodes") or [])

    def of(self, kind: str) -> List[Dict[str, Any]]:
        return [m for m in self.log if m.get("type") == kind]

    def clear(self) -> None:
        self.log.clear()


class Rig:
    """The daemon's real handler between an in-process producer and an
    in-process primitives consumer."""

    def __init__(self) -> None:
        self.registry = Registry()
        self.handler = ProtocolHandler(self.registry, Graph())
        self.source = Consumer("values")
        self.consumer = Consumer("sink")
        self.registry.attach("values", "values", "1.0")
        self.registry.attach("sink", "sink", "1.0")
        for consumer in (self.source, self.consumer):
            self.handler.register_module(Connection(
                role=api.ROLE_MODULE, module_id=consumer.name,
                name=consumer.name, deliver=consumer.deliver,
            ))
        assert self.registry.declare_types("values", [SOURCE])[0] == []
        assert self.registry.declare_types("sink", types.TYPES)[0] == []
        self.registry.announce("values", [{"instance_id": "p1",
                                           "type_id": "value_source"}])
        self.registry.announce("sink", [
            {"instance_id": "d1", "type_id": "display"},
            {"instance_id": "m1", "type_id": "math"},
        ])
        # The module is running them: the daemon spawned these, which is what
        # the consumer's own engine is handed in `main._spawn`.
        self.consumer.engine.add_nodes([
            {"instance_id": "d1", "type_id": "display"},
            {"instance_id": "m1", "type_id": "math"},
        ])
        self.display = Connection(role=api.ROLE_DISPLAY, name="patchspaceGui")

    def wire(self, from_node: str, to_node: str, to_port: str,
             from_port: str = "out", op: str = "add") -> Dict[str, Any]:
        return self.handler.handle(
            api.node_edge(from_node, to_node, to_port, from_port, op),
            self.display,
        )

    def report(self, values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        return self.handler.handle(
            api.node_values(values),
            Connection(role=api.ROLE_MODULE, module_id="values"),
        )

    def set_control(self, instance_id: str, key: str, value: Any) -> Dict[str, Any]:
        return self.handler.handle(
            api.node_control(instance_id, key, value), self.display,
        )


def test_the_two_spellings_are_one_relay():
    """A producer drives a `drives_control` port and a declared value port:
    the delivery is spelled `control_set` for the first (the consumer's
    existing callback) and `value_set` for the second (its `on_value`), and
    both end in the same place — the slot named for the port, holding the
    value.

    Then the order: the edge wins over what the slot held, and pulling it
    puts that stored value back — one capture and one restore for the two
    paths, because the port name is the slot either way.
    """
    rig = Rig()
    # The consumer's slots hold something before any wire: one set through its
    # control, one under the port's own name.
    assert rig.set_control("d1", "in", 0.5)["type"] == "ok"
    assert rig.set_control("d1", "text", "stored")["type"] == "ok"
    rig.consumer.clear()

    assert rig.wire("p1", "d1", "in", "num")["type"] == "ok"
    assert rig.wire("p1", "d1", "text", "txt")["type"] == "ok"
    rig.report({"p1": {"num": 0.25, "txt": "pattern"}})

    assert [m["key"] for m in rig.consumer.of("control_set")] == ["in"]
    assert [m["port"] for m in rig.consumer.of("value_set")] == ["text"]
    # One shape: the slot holds it, and the node reports it under the port's
    # own name.
    assert rig.consumer.engine.nodes["d1"]["config"] == {
        "in": 0.25, "text": "pattern",
    }
    assert rig.consumer.engine.values["d1"] == {
        "in": 0.25, "out": 0.25, "text": "pattern",
    }

    # Pulling each wire: the consumer is told to drop it, and then the stored
    # value comes back — the same two messages for both paths.
    for port, from_port, stored in (("in", "num", 0.5),
                                    ("text", "txt", "stored")):
        rig.consumer.clear()
        assert rig.wire("p1", "d1", port, from_port, op="remove")["type"] == "ok"
        assert [m["op"] for m in rig.consumer.of("edge_apply")] == ["remove"]
        restored = rig.consumer.of("control_set") + rig.consumer.of("value_set")
        assert [m["value"] for m in restored] == [stored], port
        assert rig.consumer.engine.nodes["d1"]["config"][port] == stored, port
        assert rig.consumer.engine.values["d1"][port] == stored, port

    # And a slot nothing ever held: nothing is restored, so the node's own
    # default stands (the third rank — the daemon does not invent a value).
    rig2 = Rig()
    assert rig2.wire("p1", "d1", "text", "txt")["type"] == "ok"
    rig2.report({"p1": {"txt": "driven"}})
    rig2.consumer.clear()
    assert rig2.wire("p1", "d1", "text", "txt", op="remove")["type"] == "ok"
    assert rig2.consumer.of("value_set") == []
    assert "text" not in rig2.consumer.engine.nodes["d1"]["config"]
    assert "text" not in rig2.consumer.engine.values["d1"]


def test_an_undeclared_choice_option_is_refused_with_a_reason():
    """A `choice` travels as its option, and one the consumer does not declare
    is refused — by the daemon, with the reason, and by the consumer — rather
    than silently kept.  Nothing is applied: the node keeps the option it
    had."""
    rig = Rig()
    assert rig.wire("p1", "m1", "op", "mode")["type"] == "ok"
    rig.set_control("m1", "op", "multiply")
    rig.consumer.clear()

    reply = rig.report({"p1": {"mode": "frobnicate"}})

    assert reply["refused"] == [
        "m1.op: 'frobnicate' is not one of add, subtract, multiply, divide",
    ]
    assert rig.consumer.of("control_set") == []
    assert rig.consumer.engine.nodes["m1"]["config"]["op"] == "multiply"

    # The display setting the same thing by hand is refused too, and the
    # daemon's stored value for the control is left alone.
    bad = rig.set_control("m1", "op", "frobnicate")
    assert bad["type"] == "error" and "not one of" in bad["message"]
    assert rig.registry.local_slot_value("m1", "op") == (True, "multiply")


def test_a_declared_choice_option_applies():
    """The other half: an option the consumer does declare arrives and is
    applied, through the same relay and the same slot."""
    rig = Rig()
    assert rig.wire("p1", "m1", "op", "mode")["type"] == "ok"
    rig.consumer.clear()
    assert rig.report({"p1": {"mode": "multiply"}}) == {
        "type": "ok", "ref": "node_values",
    }
    assert [m["value"] for m in rig.consumer.of("control_set")] == ["multiply"]
    assert rig.consumer.engine.nodes["m1"]["config"]["op"] == "multiply"

    # ...and the value it computes with follows the option it named.
    rig.set_control("m1", "a", 6)
    rig.set_control("m1", "b", 7)
    assert rig.consumer.engine.values["m1"]["out"] == 42.0


# ----------------------------------------------------------------------
# the whole thing over the real protocol
# ----------------------------------------------------------------------

def _pump_until(module, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        module.host.pump(timeout=0.1)
        if predicate():
            return True
    return False


def _drive(side, module, producer, from_port, port, value, attempts=5):
    """Report a producer's value, and wait for the consumer's slot to follow.

    A report is a *level*, not an event: a producer reports what it computes
    whenever it changes, and a report is superseded by the next one — which is
    what makes retrying honest here rather than a workaround.  The retry exists
    at all because of `ModuleHost._call`'s reply-read hole (`agents/NOTES.md`):
    the module reads whatever line is queued as the reply it waits for, so a
    push in flight while it publishes what it just applied can be swallowed.
    """
    for _ in range(attempts):
        assert side.handler.handle(
            api.node_values({"p1": {from_port: value}}), producer
        ) == {"type": "ok", "ref": "node_values"}
        if _pump_until(
            module, lambda: module.engine.nodes["d1"]["config"].get(port) == value,
            timeout=1.0,
        ):
            return
    raise AssertionError(
        f"{port} never followed {from_port}={value!r}; the consumer holds "
        f"{module.engine.nodes['d1']['config'].get(port)!r}"
    )


def test_a_driven_control_and_a_declared_value_port_end_the_same_way(tmp_path):
    """The end-to-end claim of §19, over a real socket: a producer *in
    another module* drives `display.in` (a `drives_control` port) and
    `display.text` (a declared value port), and the consumer's resulting
    state is the same shape in both cases — the slot named for the port holds
    the value, and the node reports it under that name.

    Nothing below is local to one module: the consumer is the primitives
    module over its own socket and the producer is a second module on the
    daemon's handler, so both values have crossed the real protocol.  (The
    other two ranks of the order — what a slot holds with no wire, and what
    it goes back to when the wire is pulled — are proved in
    `test_every_value_slot_resolves_the_same_way` and, on the daemon's own
    side, in `test_the_two_spellings_are_one_relay`.)
    """
    side = DaemonSide(str(tmp_path / "ps.sock"))
    module = Primitives(socket_path=str(tmp_path / "ps.sock"))
    try:
        assert module.host.connect() is True
        assert side.handler.spawn_instance("d1", "display")["status"] == "ok"
        assert _pump_until(module, lambda: "d1" in module.engine.nodes)

        # A second module, in-process on the daemon's own handler: it owns the
        # node whose reported values the relay carries.
        producer = Connection(role=api.ROLE_MODULE, module_id="values",
                              name="values", deliver=lambda message: None)
        side.handler.register_module(producer)
        side.registry.attach("values", "values", "1.0")
        assert side.registry.declare_types("values", [SOURCE])[0] == []
        side.registry.announce("values", [{"instance_id": "p1",
                                           "type_id": "value_source"}])

        display = Connection()
        side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)

        # Before any drive: the display's own defaults.
        assert _pump_until(
            module, lambda: side.registry.instance("d1").values.get("out") == 0.0
        )
        assert module.engine.values["d1"] == {"in": 0.0, "out": 0.0}

        # The producer drives both: one wire onto the control port, one onto
        # the declared value port.  The kinds pair, so the daemon accepts.
        assert side.handler.handle(
            api.node_edge("p1", "d1", "in", "num"), display)["type"] == "ok"
        assert side.handler.handle(
            api.node_edge("p1", "d1", "text", "txt"), display)["type"] == "ok"

        for from_port, port, driven in (("num", "in", 0.25),
                                        ("txt", "text", "pattern")):
            _drive(side, module, producer, from_port, port, driven)
            # Same shape for both paths: the slot named for the port holds the
            # value, and the node reports it under that name.
            assert module.engine.nodes["d1"]["config"][port] == driven, port
            assert module.engine.values["d1"][port] == driven, port
            # The daemon's record of what the node computes follows the
            # module's report asynchronously (the module publishes it): wait
            # for it rather than assuming the report has landed.
            assert _pump_until(
                module,
                lambda p=port, v=driven: side.registry.instance("d1").values.get(p) == v,
                timeout=2.0,
            ), port
            assert side.registry.instance("d1").values[port] == driven, port
    finally:
        module.host.close()
        side.stop()