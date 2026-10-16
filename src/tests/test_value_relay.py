"""A node's parameter as an input: the value relay (agents/SPACES.md §14).

Every value control a module declares also becomes an input port on its node
type — named for the control, `drives_control: true` — so wiring a producer's
output onto it and having that producer's reported value drive the consumer's
parameter are the same mechanism as a user setting the control.  These tests
put the real daemon-side handler (`ProtocolHandler` + `Registry` + `Graph`)
between two in-process modules that own nodes, and read the messages each one
is handed: the producer side, the relay, and what happens when the wire goes
away.

The relay is written as `control_set` (the callback every module already has),
so a driven control and a user-set one reach the consumer the same way.
"""

from typing import Any, Dict, List, Tuple

from central import module_api as api
from central.graph import Graph
from central.main import PatchSpaceDaemon
from central.protocol import Connection, ProtocolHandler
from central.registry import Registry

#: A value source: reports what it holds on `out`.
NUMBER_SOURCE = {
    "type_id": "number_source",
    "schema": 1,
    "ports": [{"name": "out", "kind": "number", "direction": "out"}],
    "controls": [{"key": "value", "widget": "slider", "label": "Value"}],
    "display": {"name": "Number"},
}

#: A consumer whose `gain` is a number, its `note` text and its `mode` a
#: choice; `label` is a generic `setting`, so it is not routable at all.
CONTROL_SINK = {
    "type_id": "control_sink",
    "schema": 1,
    "ports": [{"name": "in", "kind": "number", "direction": "in"}],
    "controls": [
        {"key": "gain", "widget": "gain"},
        {"key": "note", "widget": "field"},
        {"key": "mode", "widget": "choice", "choices": ["a", "b"]},
        {"key": "label", "widget": "setting"},
    ],
    "display": {"name": "Sink"},
}

#: A consumer that declares its own `drives_control` port: the same control,
#: but a bus of drivers rather than one.
CONTROL_BUS = {
    "type_id": "control_bus",
    "schema": 1,
    "ports": [
        {"name": "gain", "kind": "number", "direction": "in",
         "drives_control": True, "cardinality": "many"},
    ],
    "controls": [{"key": "gain", "widget": "gain"}],
    "display": {"name": "Bus"},
}

#: A type that declares no controls at all: the pre-S3 declaration.
OLD_STYLE = {
    "type_id": "old_style",
    "schema": 1,
    "ports": [
        {"name": "in", "kind": "number", "direction": "in"},
        {"name": "out", "kind": "number", "direction": "out"},
    ],
    "display": {"name": "Old Style"},
}


class Rig:
    """The handler with two modules that own nodes, and what each is sent.

    `values` owns the producers (a node of every value type), `sink` owns the
    consumers.  Nothing here touches PipeWire, a socket or a module process:
    a `Connection` with a `deliver` callable is how the daemon reaches a
    module that runs in-process (which is what the in-process provider is).
    """

    def __init__(self) -> None:
        self.registry = Registry()
        self.topology = Graph()
        self.handler = ProtocolHandler(self.registry, self.topology)
        self.log: List[Tuple[str, Dict[str, Any]]] = []
        for name in ("values", "sink"):
            self.registry.attach(name, name, "1.0")
            self.handler.register_module(Connection(
                role=api.ROLE_MODULE, module_id=name, name=name,
                deliver=(lambda message, who=name: self.log.append((who, message))),
            ))
        self.registry.declare_types("values", [NUMBER_SOURCE, OLD_STYLE])
        self.registry.declare_types("sink", [CONTROL_SINK, CONTROL_BUS])
        self.registry.announce("values", [
            {"instance_id": "n1", "type_id": "number_source"},
            {"instance_id": "old1", "type_id": "old_style"},
        ])
        self.registry.announce("sink", [
            {"instance_id": "s1", "type_id": "control_sink",
             "config": {"gain": 0.25}},
            {"instance_id": "bus1", "type_id": "control_bus"},
        ])
        self.display = Connection(role=api.ROLE_DISPLAY, name="patchspaceGui")

    # -- driving the daemon ---------------------------------------------

    def wire(self, from_node: str, to_node: str, to_port: str,
             from_port: str = "out", op: str = "add") -> Dict[str, Any]:
        return self.handler.handle(
            api.node_edge(from_node, to_node, to_port, from_port, op), self.display
        )

    def report(self, values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """What a producer module reports (`node_values`)."""
        return self.handler.handle(
            api.node_values(values),
            Connection(role=api.ROLE_MODULE, module_id="values"),
        )

    def set_control(self, instance_id: str, key: str, value: Any) -> Dict[str, Any]:
        """The user setting a control (what a display sends)."""
        return self.handler.handle(
            api.node_control(instance_id, key, value), self.display
        )

    def to(self, who: str, kind: str) -> List[Dict[str, Any]]:
        return [message for name, message in self.log
                if name == who and message.get("type") == kind]

    def clear(self) -> None:
        self.log.clear()


def test_wiring_a_producer_onto_a_control_port_drives_the_consumer():
    """A value control is an input port, so a producer's reported value
    arrives at the consumer exactly as a user's `node_control` would."""
    rig = Rig()

    # The port is a real input port of the consumer's *declaration*.
    assert rig.registry.port_kind("s1", "gain", "in") == "number"
    assert rig.registry.port_drives_control("s1", "gain") == "gain"

    reply = rig.wire("n1", "s1", "gain")
    assert reply["type"] == "ok", reply
    # Both ends are told to realize the edge (the wiring bookkeeping), and
    # nothing else: the producer has not reported a value yet.
    assert [e["op"] for e in rig.to("values", "edge_apply")] == ["add"]
    assert [e["op"] for e in rig.to("sink", "edge_apply")] == ["add"]
    assert rig.to("sink", "control_set") == []
    rig.clear()

    # The producer reports what it computes: the consumer's control follows.
    rig.report({"n1": {"out": 0.7}})
    assert rig.to("values", "control_set") == []
    assert rig.to("sink", "control_set") == [
        {"type": "control_set", "instance_id": "s1", "key": "gain", "value": 0.7},
    ]
    rig.clear()

    # ...and keeps following, report after report.
    rig.report({"n1": {"out": 0.1}})
    assert [m["value"] for m in rig.to("sink", "control_set")] == [0.1]


def test_a_producer_that_already_reported_drives_the_control_on_the_wire():
    """Wiring carries no value, so the daemon relays the one the producer
    last reported (what the graph remembers for it) as soon as the edge is
    there — otherwise the control would sit at the old value until the next
    report."""
    rig = Rig()
    rig.report({"n1": {"out": 0.5}})
    rig.clear()

    rig.wire("n1", "s1", "gain")

    assert [m["value"] for m in rig.to("sink", "control_set")] == [0.5]


def test_unwiring_puts_the_control_back_to_its_stored_value():
    """The driver wins while it is wired; when it goes, the control is what
    the user last set it to (and that set is part of the instance's config,
    which is what a re-attaching module is handed)."""
    rig = Rig()
    rig.set_control("s1", "gain", 0.9)
    assert rig.registry.instance("s1").config["gain"] == 0.9
    rig.clear()

    rig.wire("n1", "s1", "gain")
    rig.report({"n1": {"out": 0.1}})
    assert [m["value"] for m in rig.to("sink", "control_set")] == [0.1]
    rig.clear()

    reply = rig.wire("n1", "s1", "gain", op="remove")
    assert reply["type"] == "ok"
    assert [e["op"] for e in rig.to("sink", "edge_apply")] == ["remove"]
    assert rig.to("sink", "control_set") == [
        {"type": "control_set", "instance_id": "s1", "key": "gain", "value": 0.9},
    ]

    # The driver is gone: a later report drives nothing.
    rig.clear()
    rig.report({"n1": {"out": 0.2}})
    assert rig.to("sink", "control_set") == []


def test_the_stored_value_is_whatever_the_control_held_when_the_drive_began():
    """A value the module announced for the control counts as stored (that is
    what it held), and a control nothing has ever given a value — no config,
    no user set — has nothing to go back to: the daemon restores nothing
    rather than inventing one."""
    rig = Rig()  # s1 was announced `config={"gain": 0.25}`

    rig.wire("n1", "s1", "gain")
    rig.report({"n1": {"out": 0.4}})
    rig.clear()
    rig.wire("n1", "s1", "gain", op="remove")

    assert [m["value"] for m in rig.to("sink", "control_set")] == [0.25]

    # `bus1` was announced with no config at all, and nobody has set its
    # `gain`: unwiring leaves it alone.
    rig.clear()
    rig.wire("n1", "bus1", "gain")
    rig.report({"n1": {"out": 0.3}})
    rig.clear()

    rig.wire("n1", "bus1", "gain", op="remove")

    assert [e["op"] for e in rig.to("sink", "edge_apply")] == ["remove"]
    assert rig.to("sink", "control_set") == []
    assert rig.registry.local_slot_value("bus1", "gain") == (False, None)


def test_a_control_takes_one_driver_unless_its_port_declares_many():
    """The auto-declared port declares `one` — a parameter has one producer —
    and a module that wants a bus says `many` in its own declaration."""
    rig = Rig()
    assert rig.wire("n1", "s1", "gain")["type"] == "ok"
    refused = rig.wire("old1", "s1", "gain")
    assert refused["type"] == "error" and "already driven" in refused["message"]

    # The bus port takes the second driver, relays it, and — when that one is
    # unwired — relays what is still driving rather than restoring a value
    # that a live driver is overriding.
    assert rig.wire("n1", "bus1", "gain")["type"] == "ok"
    assert rig.wire("old1", "bus1", "gain")["type"] == "ok"
    rig.report({"n1": {"out": 0.1}, "old1": {"out": 0.2}})
    rig.clear()

    rig.wire("old1", "bus1", "gain", op="remove")

    assert [m["value"] for m in rig.to("sink", "control_set")] == [0.1]


def test_every_value_port_an_edge_points_at_gets_the_value():
    """Which ports a reported value reaches is the **kind vocabulary**, not a
    flag (agents/SPACES.md §19): `s1.in` is a number input port, so it is a
    value slot exactly as `s1.gain` is — delivered as a `value_set` (the
    `on_value` path) where a control is delivered as a `control_set`, which is
    the only difference between the two.  A value reported on a port nothing
    consumes still goes nowhere."""
    rig = Rig()
    assert rig.report({"n1": {"out": 1.0}}) == {"type": "ok", "ref": "node_values"}
    assert rig.to("sink", "control_set") == []
    assert rig.to("sink", "value_set") == []

    # `s1.in` is a plain number input — a value port, not a control.
    assert rig.registry.port_drives_control("s1", "in") == ""
    assert rig.registry.value_slot("s1", "in")["kind"] == "number"
    assert rig.wire("n1", "s1", "in")["type"] == "ok"
    # Wiring carries no value of its own: the daemon relays what the producer
    # last reported (1.0, above) — exactly as it does onto a control port —
    # and every later report follows it.
    assert [m["value"] for m in rig.to("sink", "value_set")] == [1.0]
    rig.report({"n1": {"out": 2.0}})
    assert rig.to("sink", "control_set") == []
    assert [m["value"] for m in rig.to("sink", "value_set")] == [1.0, 2.0]

    # A port no edge leaves carries nothing, whatever was reported on it.
    assert rig.handler.relay_values({"n1": {"nobody": 3.0}}) == 0
    assert rig.registry.value_slot("n1", "out") is None      # an output
    assert rig.registry.value_slot("s1", "gain")["control"] == "gain"


def test_a_declaration_with_no_controls_is_untouched():
    """An old-style type keeps exactly the ports it declared, and no port
    drives a control — but a value port it *did* declare is reachable by the
    relay like any other (§19), which is why this one arrives at `values`
    (the module that declared and announced the type) as a `value_set`."""
    rig = Rig()
    assert rig.registry.type_decl("old_style").ports == OLD_STYLE["ports"]
    assert rig.registry.port_drives_control("old1", "in") == ""
    assert rig.wire("n1", "old1", "in")["type"] == "ok"
    rig.report({"n1": {"out": 5.0}})
    assert rig.to("sink", "control_set") == []
    assert [m["value"] for m in rig.to("values", "value_set")] == [5.0]


def test_a_delivered_control_never_reaches_a_node_whose_module_is_gone():
    """The relay is the same bus as every other daemon→module message: a
    node nothing is running has no one to drive."""
    rig = Rig()
    rig.wire("n1", "s1", "gain")
    rig.registry.detach("sink")

    assert rig.report({"n1": {"out": 0.3}}) == {"type": "ok", "ref": "node_values"}
    assert rig.to("sink", "control_set") == []


# ----------------------------------------------------------------------
# the daemon's own node lifecycle
# ----------------------------------------------------------------------

def test_removing_a_producer_puts_the_controls_it_drove_back():
    """Deleting a node takes its edges with it, and a control only that edge
    was driving goes back to its stored value (agents/SPACES.md §14)."""
    daemon = PatchSpaceDaemon()
    sent: List[Dict[str, Any]] = []
    for name in ("values", "sink"):
        daemon._protocol.register_module(Connection(
            role=api.ROLE_MODULE, module_id=name, name=name,
            deliver=(lambda message, who=name: sent.append(message)),
        ))
    daemon.registry.attach("values", "values", "1.0")
    daemon.registry.attach("sink", "sink", "1.0")
    assert daemon.registry.declare_types("values", [NUMBER_SOURCE])[0] == []
    assert daemon.registry.declare_types("sink", [CONTROL_SINK])[0] == []
    daemon.registry.announce("values", [{"instance_id": "n1",
                                         "type_id": "number_source"}])
    daemon.registry.announce("sink", [{"instance_id": "s1",
                                       "type_id": "control_sink"}])
    display = Connection(role=api.ROLE_DISPLAY, name="patchspaceGui")
    daemon.handle_command(api.node_control("s1", "gain", 0.6), display)
    daemon.handle_command(api.node_edge("n1", "s1", "gain", "out"), display)
    daemon.handle_command(api.node_values({"n1": {"out": 0.2}}),
                          Connection(role=api.ROLE_MODULE, module_id="values"))
    sent.clear()

    reply = daemon.handle_command({"command": "remove_node", "node_id": "n1"})

    assert reply.get("status") == "ok", reply
    assert daemon.topology.edges == {}
    assert [m["value"] for m in sent if m["type"] == "control_set"] == [0.6]