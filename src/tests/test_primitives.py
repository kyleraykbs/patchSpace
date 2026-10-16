"""The primitives module: its arithmetic, and the whole loop a user's node
takes — the daemon creates it, the module runs it, the display sees the
value."""

import time

import pytest

from central import module_api as api
from central.graph import Graph
from central.main import PatchSpaceDaemon
from central.protocol import Connection
from central.registry import Registry
from modules.pipewire import declarations
from modules.primitives import types
from modules.primitives.engine import Engine
from modules.primitives.main import Primitives
from tests.test_module_host import DaemonSide


# ----------------------------------------------------------------------
# the arithmetic
# ----------------------------------------------------------------------

def _engine(nodes):
    engine = Engine()
    engine.add_nodes(nodes)
    return engine


def test_math_evaluates_and_settles():
    engine = _engine([
        {"instance_id": "n1", "type_id": "number", "config": {"value": 6}},
        {"instance_id": "n2", "type_id": "number", "config": {"value": 7}},
        {"instance_id": "m1", "type_id": "math", "config": {"op": "multiply"}},
        {"instance_id": "d1", "type_id": "display"},
    ])
    engine.apply_edge({"from_node": "n1", "to_node": "m1", "to_port": "a"}, "add")
    engine.apply_edge({"from_node": "n2", "to_node": "m1", "to_port": "b"}, "add")
    engine.apply_edge({"from_node": "m1", "to_node": "d1", "to_port": "in"}, "add")

    assert engine.values["m1"]["out"] == 42.0
    assert engine.values["d1"]["in"] == 42.0
    # A display passes the value through, so it can sit mid-chain.
    assert engine.values["d1"]["out"] == 42.0


def test_divide_by_zero_is_a_value_not_a_crash():
    engine = _engine([
        {"instance_id": "n1", "type_id": "number", "config": {"value": 1}},
        {"instance_id": "n2", "type_id": "number", "config": {"value": 0}},
        {"instance_id": "m1", "type_id": "math", "config": {"op": "divide"}},
    ])
    engine.apply_edge({"from_node": "n1", "to_node": "m1", "to_port": "a"}, "add")
    engine.apply_edge({"from_node": "n2", "to_node": "m1", "to_port": "b"}, "add")
    assert engine.values["m1"]["out"] == 0.0


def test_booleans_compare_and_combine():
    engine = _engine([
        {"instance_id": "a", "type_id": "number", "config": {"value": 3}},
        {"instance_id": "b", "type_id": "number", "config": {"value": 5}},
        {"instance_id": "cmp", "type_id": "compare", "config": {"op": "<"}},
        {"instance_id": "t", "type_id": "toggle", "config": {"on": True}},
        {"instance_id": "lg", "type_id": "logic", "config": {"op": "and"}},
        {"instance_id": "inv", "type_id": "invert"},
    ])
    engine.apply_edge({"from_node": "a", "to_node": "cmp", "to_port": "a"}, "add")
    engine.apply_edge({"from_node": "b", "to_node": "cmp", "to_port": "b"}, "add")
    engine.apply_edge({"from_node": "cmp", "to_node": "lg", "to_port": "a"}, "add")
    engine.apply_edge({"from_node": "t", "to_node": "lg", "to_port": "b"}, "add")
    engine.apply_edge({"from_node": "lg", "to_node": "inv", "to_port": "a"}, "add")

    assert engine.values["cmp"]["out"] is True      # 3 < 5
    assert engine.values["lg"]["out"] is True       # True and True
    assert engine.values["inv"]["out"] is False


def test_a_slider_is_clamped_and_a_control_reevaluates():
    engine = _engine([{"instance_id": "s1", "type_id": "slider",
                       "config": {"value": 3}}])
    assert engine.values["s1"]["out"] == 1.0
    engine.set_control("s1", "value", -2)
    assert engine.values["s1"]["out"] == 0.0
    engine.set_control("s1", "value", 0.25)
    assert engine.values["s1"]["out"] == 0.25
    # A control for a node this module does not run is refused, not a crash.
    assert engine.set_control("nope", "value", 1) is False


def test_a_cycle_settles_instead_of_spinning():
    """The user can wire a loop; evaluation must terminate."""
    engine = _engine([
        {"instance_id": "m1", "type_id": "math", "config": {"op": "add"}},
        {"instance_id": "m2", "type_id": "math", "config": {"op": "add"}},
    ])
    engine.apply_edge({"from_node": "m1", "to_node": "m2", "to_port": "a"}, "add")
    engine.apply_edge({"from_node": "m2", "to_node": "m1", "to_port": "a"}, "add")
    assert engine.values["m1"]["out"] == 0.0


def test_removing_a_node_takes_its_edges_with_it():
    engine = _engine([
        {"instance_id": "n1", "type_id": "number", "config": {"value": 2}},
        {"instance_id": "d1", "type_id": "display"},
    ])
    engine.apply_edge({"from_node": "n1", "to_node": "d1", "to_port": "in"}, "add")
    assert engine.values["d1"]["in"] == 2.0
    engine.remove_nodes(["n1"])
    assert engine.edges == []
    assert engine.values["d1"]["in"] == 0.0


# ----------------------------------------------------------------------
# the value sources (agents/SPACES.md §16)
# ----------------------------------------------------------------------

def _decl(type_id):
    return next(t for t in types.TYPES if t["type_id"] == type_id)


def test_the_sources_declare_an_output_of_the_kind_they_report():
    """§16: a `const` (number literal) and a `string` (text literal) beside
    the 0.0–1.0 `slider`, each with an output port of the kind it holds."""
    assert _decl("const")["ports"] == [
        {"name": "out", "kind": types.NUMBER, "direction": "out"}]
    assert _decl("string")["ports"] == [
        {"name": "out", "kind": types.TEXT, "direction": "out"}]
    slider = _decl("slider")
    assert slider["ports"] == [
        {"name": "out", "kind": types.NUMBER, "direction": "out"}]
    # It *is* 0..1: no range of its own to configure (the engine clamps), and
    # a display draws its slider widget at the 0..1 it stands for.
    assert [c["key"] for c in slider["controls"]] == ["value"]
    assert "min" not in slider["controls"][0]
    assert "max" not in slider["controls"][0]


def test_every_source_reports_what_it_holds():
    """Each one's value goes out through the module's own `values`, which is
    what the daemon relays (`report_values`)."""
    for type_id, held, reported in (
        ("const", 6, 6.0),
        ("const", "6", 6.0),
        ("string", "pattern", "pattern"),
        ("string", 12, "12"),
        ("slider", 2, 1.0),
        ("slider", -1, 0.0),
        ("slider", 0.25, 0.25),
    ):
        engine = _engine([{"instance_id": "n1", "type_id": type_id,
                           "config": {"value": held}}])
        assert engine.values["n1"] == {"out": reported}, (type_id, held)


def test_a_string_reaches_the_display_that_shows_it():
    engine = _engine([
        {"instance_id": "s1", "type_id": "string", "config": {"value": "pattern"}},
        {"instance_id": "d1", "type_id": "display"},
    ])
    engine.apply_edge({"from_node": "s1", "to_node": "d1", "to_port": "text"}, "add")
    assert engine.values["d1"]["text"] == "pattern"
    # Nothing numeric is wired, so the number it passes on is still its own
    # default — a text does not make the numeric chain lie.
    assert engine.values["d1"]["out"] == 0.0

    engine.apply_edge({"from_node": "s1", "to_node": "d1", "to_port": "text"}, "remove")
    assert "text" not in engine.values["d1"]


def test_an_operand_reads_the_control_its_port_is_named_for():
    """§14's one slot: the control the port is named for is the second
    source, and the wire wins over it while one is there."""
    engine = _engine([
        {"instance_id": "m1", "type_id": "math", "config": {"op": "add", "a": 2}},
        {"instance_id": "c1", "type_id": "const", "config": {"value": 40}},
    ])
    assert engine.values["m1"]["out"] == 2.0        # the control stands in
    engine.apply_edge({"from_node": "c1", "to_node": "m1", "to_port": "a"}, "add")
    assert engine.values["m1"]["out"] == 40.0       # the wire wins
    # Unwiring hands the control back to the daemon, which restores the value
    # it froze when the drive began (nothing to restore here: it was never
    # set, so the node's own default stands — see the socket test).
    engine.apply_edge({"from_node": "c1", "to_node": "m1", "to_port": "a"}, "remove")
    assert "a" not in engine.nodes["m1"]["config"]
    assert engine.values["m1"]["out"] == 0.0


def test_both_operands_of_every_operation_are_addressable():
    engine = _engine([
        {"instance_id": "l1", "type_id": "logic", "config": {"op": "and", "a": True}},
        {"instance_id": "i1", "type_id": "invert", "config": {"a": True}},
        {"instance_id": "k1", "type_id": "compare", "config": {"op": "==", "b": 1}},
        {"instance_id": "n1", "type_id": "number", "config": {"value": 1}},
    ])
    engine.apply_edge({"from_node": "n1", "to_node": "k1", "to_port": "a"}, "add")
    assert engine.values["l1"]["out"] is False      # True and (b unset: False)
    assert engine.values["i1"]["out"] is False      # not True
    assert engine.values["k1"]["out"] is True       # 1 == 1, b from its control


def test_the_new_inputs_are_named_for_a_control_of_the_same_name():
    """§14, held to by the module's own declarations: every type still
    validates with the daemon (`validate_type_decl`), and a port that drives
    a control is an input named for one."""
    for decl in types.TYPES:
        ok, message = api.validate_type_decl(decl)
        assert ok, message
        keys = {c["key"] for c in decl.get("controls", [])}
        for port in decl["ports"]:
            if port.get("drives_control"):
                assert port["name"] in keys
                assert port["direction"] == "in"


def test_the_menu_offers_every_type_the_module_declares():
    items = [item["type_id"] for group in types.MENU for item in group["items"]]
    declared = [t["type_id"] for t in types.TYPES]
    assert sorted(items) == sorted(declared)
    assert len(items) == len(set(items))
    assert {"const", "string"} <= set(items)


def test_a_string_output_pairs_with_the_text_controls_that_consume_it():
    """§16's "a string needs somewhere to go", answered honestly: the
    consumers that exist today are *other* modules' text controls.  A
    `string` node's output is a `text` port, the daemon accepts it on any
    `drives_control` text port, and the PipeWire catalog's `field` controls
    (`regex_classifier.pattern`, `app_classifier.app_key`, `sound.path`, the
    warp names…) are exactly that — so the relay carries this module's
    literal to that control."""
    reg = Registry()
    pipewire = reg.allocate_module_id("pipewire")
    reg.attach(pipewire, "pipewire", "1.0")
    assert reg.declare_types(pipewire, declarations.catalog())[0] == []
    primitives = reg.allocate_module_id("primitives")
    reg.attach(primitives, "primitives", "1.0")
    assert reg.declare_types(primitives, types.TYPES)[0] == []

    reg.place("s1", "string")
    reg.place("rx", "regex_classifier")
    assert reg.port_kind("s1", "out", "out") == "text"
    # The classifier declares only its `out`; `pattern` is the port S3
    # auto-declared for its `field` control, named for it.
    assert reg.port_kind("rx", "pattern", "in") == "text"
    assert reg.port_drives_control("rx", "pattern") == "pattern"
    ok, message = Graph().can_connect("s1", "rx", "pattern", "out",
                                      reg.port_kind, reg.port_cardinality)
    assert ok, message


# ----------------------------------------------------------------------
# the module, over a real socket
# ----------------------------------------------------------------------

@pytest.fixture()
def rig(tmp_path):
    side = DaemonSide(str(tmp_path / "ps.sock"))
    module = Primitives(socket_path=str(tmp_path / "ps.sock"))
    yield side, module
    module.host.close()
    side.stop()


def _pump_until(module, predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        module.host.pump(timeout=0.1)
        if predicate():
            return True
    return False


def test_the_daemon_creates_a_node_and_the_module_runs_it(rig):
    side, module = rig
    assert module.host.connect() is True
    assert side.registry.type_decl("math") is not None
    assert side.registry.type_decl("display") is not None

    # The daemon creates the node (the user added one in the canvas) and
    # asks the module to run it.
    assert side.handler.spawn_instance("m1", "math", config={"op": "multiply"})["status"] == "ok"
    assert _pump_until(module, lambda: "m1" in module.engine.nodes)
    assert side.registry.instance("m1").status == api.STATUS_ONLINE

    # A control from a display reaches it, and the values it computes reach
    # the daemon (so the canvas can show them).
    display = Connection()
    side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    assert side.handler.handle(api.node_control("m1", "op", "add"), display)["type"] == "ok"
    assert _pump_until(module, lambda: module.engine.nodes["m1"]["config"]["op"] == "add")
    assert _pump_until(
        module, lambda: side.registry.instance("m1").values.get("out") == 0.0
    )
    assert side.registry.instance("m1").values["out"] == 0.0

    # Removing it stops the module running it.
    assert side.handler.stop_instance("m1") is True
    assert _pump_until(module, lambda: "m1" not in module.engine.nodes)


def test_a_spawned_node_is_served_to_a_display_with_its_values(rig):
    side, module = rig
    module.host.connect()
    side.handler.spawn_instance("n1", "number", config={"value": 5})
    _pump_until(module, lambda: "n1" in module.engine.nodes)

    display = Connection()
    side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    snapshot = side.handler.handle(api.snapshot_request(), display)
    instance = next(i for i in snapshot["instances"] if i["instance_id"] == "n1")
    assert instance["type_id"] == "number"
    assert instance["status"] == api.STATUS_ONLINE
    assert instance["values"] == {"out": 5.0}


def test_adding_a_node_of_a_module_type_through_the_daemon():
    """`add_node` for a type the daemon does not model itself goes to the
    module that declares it — and shows up in get_nodes."""
    daemon = PatchSpaceDaemon()
    daemon.attach_modules()
    delivered = []
    conn = Connection(module_id="primitives", role=api.ROLE_MODULE,
                      deliver=delivered.append)
    daemon.registry.attach("primitives", "primitives", "1.0")
    daemon.registry.declare_types("primitives", [
        {"type_id": "number", "schema": 1,
         "ports": [{"name": "out", "kind": "number", "direction": "out"}],
         "display": {"name": "Number"}},
    ])
    daemon._protocol.register_module(conn)

    reply = daemon.handle_command({"command": "add_node", "node_type": "number",
                                   "node_id": "n1", "config": {"value": 3}})
    assert reply["status"] == "ok", reply
    assert [m["type"] for m in delivered] == ["node_spawn"]
    assert delivered[0]["nodes"][0]["instance_id"] == "n1"

    # The module announces it back (what the real one does on spawn).
    daemon.registry.announce("primitives", [
        {"instance_id": "n1", "type_id": "number", "config": {"value": 3}},
    ])
    daemon.registry.set_values("primitives", {"n1": {"out": 3.0}})
    nodes = daemon.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["n1"]["status"] == api.STATUS_ONLINE
    assert nodes["n1"]["ready"] is True
    assert nodes["n1"]["values"] == {"out": 3.0}

    # ...and removing it asks the module to stop.
    delivered.clear()
    assert daemon.handle_command({"command": "remove_node",
                                  "node_id": "n1"})["status"] == "ok"
    assert [m["type"] for m in delivered] == ["node_stop"]
    assert daemon.registry.instance("n1") is None


# ----------------------------------------------------------------------
# the sources, wired to what consumes them (agents/SPACES.md §16)
# ----------------------------------------------------------------------

def test_a_slider_wired_into_another_nodes_control_drives_it(rig):
    """The whole of §16 over a real socket: a source reports its value, the
    daemon relays it onto the control the wire landed on, and the module
    applies it — so the operand it drives is the operand that is computed."""
    side, module = rig
    assert module.host.connect() is True
    # One pump per spawn: `ModuleHost._call` reads whatever line is already
    # queued as the reply it waits for, so two `node_spawn`s sent back to
    # back can arrive in one read and the second is dropped (a pre-existing
    # hole in the host's reply read, see agents/NOTES.md — not this step's).
    assert side.handler.spawn_instance("s1", "slider", config={"value": 0.25})["status"] == "ok"
    assert _pump_until(module, lambda: "s1" in module.engine.nodes)
    assert side.handler.spawn_instance("m1", "math", config={"op": "add"})["status"] == "ok"
    assert _pump_until(module, lambda: "m1" in module.engine.nodes)
    assert _pump_until(module, lambda: side.registry.instance("s1").values.get("out") == 0.25)

    # `a` is math's operand *and* its control — one slot, the port's name.
    port = next(p for p in side.registry.type_decl("math").ports if p["name"] == "a")
    assert port["drives_control"] is True and port["kind"] == "number"
    assert side.registry.port_drives_control("m1", "a") == "a"

    display = Connection()
    side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    assert side.handler.handle(
        api.node_edge("s1", "m1", "a", "out"), display)["type"] == "ok"
    # The daemon relayed the source's value as a `control_set` for `a`, and
    # the module applied it exactly as if the user had set the control.
    assert _pump_until(module, lambda: module.engine.nodes["m1"]["config"].get("a") == 0.25)
    assert _pump_until(module, lambda: side.registry.instance("m1").values.get("out") == 0.25)
    assert side.registry.instance("m1").values["out"] == 0.25

    # Unwiring it puts the operand back to the node's own default: nothing
    # ever set this control, so the daemon restores nothing and an unfed
    # operand is 0.0 again.
    assert side.handler.handle(
        api.node_edge("s1", "m1", "a", "out", op="remove"), display)["type"] == "ok"
    assert _pump_until(module, lambda: "a" not in module.engine.nodes["m1"]["config"])
    assert _pump_until(module, lambda: side.registry.instance("m1").values.get("out") == 0.0)
    assert side.registry.instance("m1").values["out"] == 0.0


def test_a_control_the_user_set_survives_a_wire_being_plugged_and_pulled(rig):
    """`_release_drive` clears the slot a driver filled, so the daemon's
    restore — the value it froze when the drive began — has to arrive after
    the clear.  It does (`protocol._node_edge` forwards `edge_apply` and then
    binds the control edge), and this is that round trip end to end."""
    side, module = rig
    module.host.connect()
    assert side.handler.spawn_instance("m1", "math", config={"op": "add"})["status"] == "ok"
    assert _pump_until(module, lambda: "m1" in module.engine.nodes)
    assert side.handler.spawn_instance("s1", "slider", config={"value": 0.25})["status"] == "ok"
    assert _pump_until(module, lambda: "s1" in module.engine.nodes)

    display = Connection()
    side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    # The user sets the operand ...
    assert side.handler.handle(
        api.node_control("m1", "a", 5), display)["type"] == "ok"
    assert _pump_until(module, lambda: side.registry.instance("m1").values.get("out") == 5.0)
    # ... wires a source into it (the drive takes the slot over) ...
    assert side.handler.handle(
        api.node_edge("s1", "m1", "a", "out"), display)["type"] == "ok"
    assert _pump_until(module, lambda: side.registry.instance("m1").values.get("out") == 0.25)
    # ... and pulls the wire: what the user set is back, not the driven value
    # and not the node's blank default.
    assert side.handler.handle(
        api.node_edge("s1", "m1", "a", "out", op="remove"), display)["type"] == "ok"
    assert _pump_until(module, lambda: side.registry.instance("m1").values.get("out") == 5.0)
    assert module.engine.nodes["m1"]["config"]["a"] == 5


def test_a_string_wired_into_a_display_shows_the_text_over_the_socket(rig):
    """§16's text consumer in this module: the display's `text` input.  The
    kinds have to pair for the daemon to accept the wire at all."""
    side, module = rig
    module.host.connect()
    side.handler.spawn_instance("s1", "string", config={"value": "pattern"})
    assert _pump_until(module, lambda: "s1" in module.engine.nodes)
    side.handler.spawn_instance("d1", "display")
    assert _pump_until(module, lambda: "d1" in module.engine.nodes)

    display = Connection()
    side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    assert side.registry.port_kind("s1", "out", "out") == "text"
    assert side.registry.port_kind("d1", "text", "in") == "text"
    assert side.handler.handle(
        api.node_edge("s1", "d1", "text", "out"), display)["type"] == "ok"
    assert _pump_until(module, lambda: side.registry.instance("d1").values.get("text") == "pattern")
    assert side.registry.instance("d1").values == {"in": 0.0, "out": 0.0, "text": "pattern"}


def test_a_value_source_with_no_consumer_changes_nothing(rig):
    """The invariant §16 rests on: a value nothing is wired to costs
    nothing — it is reported (so a display can read it) and relayed nowhere.
    The slider here also shows the 0.0–1.0 clamp end to end: 2 is *reported
    as* 1.0."""
    side, module = rig
    module.host.connect()
    assert side.handler.spawn_instance("s1", "slider", config={"value": 2})["status"] == "ok"
    assert _pump_until(module, lambda: "s1" in module.engine.nodes)
    assert side.handler.spawn_instance("m1", "math", config={"op": "add"})["status"] == "ok"
    assert _pump_until(module, lambda: "m1" in module.engine.nodes)
    assert _pump_until(module, lambda: side.registry.instance("s1").values.get("out") == 1.0)

    # What it holds is still readable ...
    assert side.registry.instance("s1").values == {"out": 1.0}
    # ... and nothing consumes it: no wire leaves it, the relay sends
    # nothing, and no other node moved.
    assert side.handler.topology.edges_from("s1", "out") == []
    assert side.handler.relay_values({"s1": {"out": 1.0}}) == 0
    assert module.engine.nodes["m1"]["config"] == {"op": "add"}
    assert side.registry.instance("m1").values == {"out": 0.0}
    assert module.engine.values == {"s1": {"out": 1.0}, "m1": {"out": 0.0}}


def test_a_relayed_value_set_lands_where_the_engine_reads_its_input(rig):
    """`value_set` is the daemon's other spelling of the same relay — the
    one addressed at a raw port — and it lands in the port's own slot, so a
    relayed value is applied rather than dropped.  (Nothing emits it yet:
    S3 spells the relay `control_set` for a port that drives a control, so
    this drives the callback directly.)"""
    side, module = rig
    module.host.connect()
    module.engine.add_nodes([{"instance_id": "d1", "type_id": "display"}])
    module.apply_value(api.value_set("d1", "text", "pattern"))
    assert module.engine.nodes["d1"]["config"]["text"] == "pattern"
    assert module.engine.values["d1"]["text"] == "pattern"
    # A value for a node this module does not run is refused, not a crash.
    module.apply_value(api.value_set("nope", "text", "x"))
