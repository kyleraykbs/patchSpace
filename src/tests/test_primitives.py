"""The primitives module: its arithmetic, and the whole loop a user's node
takes — the daemon creates it, the module runs it, the display sees the
value."""

import time

import pytest

from central import module_api as api
from central.main import PatchSpaceDaemon
from central.protocol import Connection
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
