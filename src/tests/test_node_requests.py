"""A display can wire and control a node it does not own.

The daemon routes the request to whichever module owns the instance, so a
display never has to know which module that is — or dial it.  The in-process
provider and an out-of-process one are reached the same way (`Connection.send`
is a callable for one and a socket write for the other).
"""

import json
import os
import sys
import time

import pytest

from central import module_api as api
from central.main import PatchSpaceDaemon
from central.module_supervisor import ModuleSpec, ModuleSupervisor
from central.protocol import Connection
from tests.test_module_host import DaemonSide

NUMBER_OUT = {
    "type_id": "number_source",
    "schema": 1,
    "ports": [{"name": "out", "kind": "number", "direction": "out"}],
    "controls": [{"key": "value", "widget": "slider"}],
    "display": {"name": "Number"},
}

NUMBER_IN = {
    "type_id": "number_sink",
    "schema": 1,
    "ports": [{"name": "in", "kind": "number", "direction": "in"}],
    "display": {"name": "Display"},
}


def _daemon_with_a_module():
    """A daemon with the in-process provider attached (the cheapest module
    that owns real nodes)."""
    daemon = PatchSpaceDaemon()
    daemon.attach_modules()
    daemon.handle_command({"command": "add_node", "node_type": "gate",
                           "node_id": "g1", "config": {}})
    return daemon


def _display(daemon):
    conn = Connection()
    daemon.handle_command(api.hello(api.ROLE_DISPLAY, "patchspaceGui"), conn)
    return conn


def test_a_display_control_reaches_the_module_that_owns_the_node():
    daemon = _daemon_with_a_module()
    display = _display(daemon)

    reply = daemon.handle_command(
        api.node_control("g1", "enabled", False), display
    )
    assert reply["type"] == "ok" and reply["ref"] == "node_control"
    # It reached the provider as a control_set (the module's own log).
    assert daemon._pw_module.control_log[-1]["instance_id"] == "g1"
    assert daemon._pw_module.control_log[-1]["key"] == "enabled"
    assert daemon._pw_module.control_log[-1]["value"] is False


def test_a_control_for_a_node_nothing_runs_is_refused_with_the_reason():
    daemon = _daemon_with_a_module()
    display = _display(daemon)
    daemon.registry.place("ghost_1", "nobody_makes_this")

    reply = daemon.handle_command(api.node_control("ghost_1", "value", 1), display)
    assert reply["type"] == "error"
    assert "not running" in reply["message"] and "no module" in reply["message"]

    assert daemon.handle_command(
        api.node_control("nope", "value", 1), display
    )["type"] == "error"
    assert daemon.handle_command(
        api.node_control("g1", "", 1), display
    )["type"] == "error"


def test_a_display_edge_is_validated_recorded_and_forwarded():
    daemon = _daemon_with_a_module()
    daemon.handle_command({"command": "add_node", "node_type": "volume",
                           "node_id": "v1", "config": {}})
    display = _display(daemon)

    reply = daemon.handle_command(
        api.node_edge("v1", "g1", "audio", "out"), display
    )
    assert reply["type"] == "ok", reply
    assert reply["edge"]["id"] == "v1->g1:audio"
    assert "v1->g1:audio" in daemon.topology.edges
    assert daemon._pw_module.edge_log[-1]["op"] == "add"
    assert daemon._pw_module.edge_log[-1]["edge"]["id"] == "v1->g1:audio"

    # An incompatible edge is refused by the one authority, and nothing is
    # recorded or forwarded.
    before = len(daemon._pw_module.edge_log)
    bad = daemon.handle_command(api.node_edge("v1", "g1", "ctrl", "out"), display)
    assert bad["type"] == "error" and "audio" in bad["message"]
    assert len(daemon._pw_module.edge_log) == before

    # ...and a remove is forwarded as well.
    removed = daemon.handle_command(
        api.node_edge("v1", "g1", "audio", "out", op="remove"), display
    )
    assert removed["type"] == "ok"
    assert "v1->g1:audio" not in daemon.topology.edges
    assert daemon._pw_module.edge_log[-1]["op"] == "remove"


def test_the_requests_need_a_greeting():
    daemon = _daemon_with_a_module()
    fresh = Connection()
    assert daemon.handle_command(api.node_control("g1", "enabled", True),
                                 fresh)["type"] == "error"
    assert daemon.handle_command(api.node_edge("v1", "g1"), fresh)["type"] == "error"


# ----------------------------------------------------------------------
# the same path with a module in its own process
# ----------------------------------------------------------------------

CHILD_CODE = r"""
import sys
sys.path.insert(0, {src!r})
from modules.host import ModuleHost

TYPES = [
    {{"type_id": "number_source", "schema": 1,
      "ports": [{{"name": "out", "kind": "number", "direction": "out"}}],
      "display": {{"name": "Number"}}}},
    {{"type_id": "number_sink", "schema": 1,
      "ports": [{{"name": "in", "kind": "number", "direction": "in"}}],
      "display": {{"name": "Display"}}}},
]

def main():
    host = ModuleHost("childmod", "1.0", declarations=TYPES,
                      on_control=lambda m: print("CONTROL", m["key"], m["value"], flush=True),
                      on_edge=lambda m: print("EDGE", m["op"], m["edge"]["id"], flush=True))
    if not host.connect():
        return 3
    host.announce([{{"instance_id": "src_1", "type_id": "number_source"}},
                   {{"instance_id": "sink_1", "type_id": "number_sink"}}])
    host.run_forever()
    return 0

sys.exit(main())
"""


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def external(tmp_path):
    """A daemon-less rig: the real handler on a real socket, plus a spawned
    module process whose output goes to a log the test can read."""
    side = DaemonSide(str(tmp_path / "ps.sock"))
    supervisor = ModuleSupervisor(
        [ModuleSpec("childmod", [sys.executable, "-c",
                                 CHILD_CODE.format(src=SRC_DIR)])],
        socket_path=str(tmp_path / "ps.sock"),
        log_dir=str(tmp_path / "logs"),
    )
    yield side, supervisor, str(tmp_path / "logs" / "childmod.log")
    supervisor.stop_all()
    side.stop()


def test_a_control_and_an_edge_reach_a_module_in_another_process(external):
    side, supervisor, log_path = external
    supervisor.start_all()
    assert _wait_for(lambda: side.registry.instance("sink_1") is not None
                     and side.registry.instance("src_1") is not None)

    display = Connection()
    side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    assert side.handler.handle(
        api.node_control("src_1", "value", 0.75), display
    )["type"] == "ok"
    reply = side.handler.handle(
        api.node_edge("src_1", "sink_1", "in", "out"), display
    )
    assert reply["type"] == "ok", reply
    assert reply["edge"]["id"] == "src_1->sink_1"

    # The module process saw both: it prints them, and the supervisor's log
    # file is how a test reads a child's output.
    assert _wait_for(
        lambda: b"CONTROL value 0.75" in open(log_path, "rb").read()
    ), open(log_path, "rb").read()
    assert b"EDGE add src_1->sink_1" in open(log_path, "rb").read()


SRC_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
