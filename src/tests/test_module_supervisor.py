"""The daemon running a module as a child process, end to end.

A real child process speaks the module API over a real socket to the real
daemon-side handler: it attaches, declares, announces, is restarted when it
dies, and is stopped with the daemon.  The child is a throwaway program in
the test (not a shipped stub) — the point is the supervisor's lifecycle and
the wire, not the module's own logic.
"""

import json
import os
import sys
import time

import pytest

from central.module_supervisor import ModuleSpec, ModuleSupervisor
from tests.test_module_host import DaemonSide

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A module program: connect, declare one type, announce one instance, then
#: serve until killed.  Exits non-zero if the daemon is not there.
CHILD_CODE = r"""
import sys
sys.path.insert(0, {src!r})
from modules.host import ModuleHost

TYPE = {{
    "type_id": "test_node", "schema": 1,
    "ports": [{{"name": "out", "kind": "number", "direction": "out"}}],
    "display": {{"name": "Test Node"}},
}}

def main():
    host = ModuleHost("childmod", "1.0", declarations=[TYPE])
    if not host.connect():
        return 3
    host.announce([{{"instance_id": "child_1", "type_id": "test_node"}}])
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
def daemon_side(tmp_path):
    side = DaemonSide(str(tmp_path / "ps.sock"))
    yield side
    side.stop()


def _spec(socket_path):
    return ModuleSpec("childmod", [sys.executable, "-c",
                                   CHILD_CODE.format(src=SRC)])


def test_a_spawned_module_attaches_over_the_socket(daemon_side, tmp_path):
    """`number` is a declared kind: a module may announce such ports."""
    supervisor = ModuleSupervisor([_spec(str(tmp_path / "ps.sock"))],
                                 socket_path=str(tmp_path / "ps.sock"),
                                 log_dir=str(tmp_path / "logs"))
    try:
        supervisor.start_all()
        assert _wait_for(
            lambda: daemon_side.registry.module("childmod") is not None
            and daemon_side.registry.instance("child_1") is not None
        ), supervisor.status()
        assert daemon_side.registry.instance("child_1").status == "online"
        assert daemon_side.registry.type_decl("test_node") is not None

        status = supervisor.status()[0]
        assert status["alive"] is True and status["pid"]
        assert os.path.exists(str(tmp_path / "logs" / "childmod.log"))
    finally:
        supervisor.stop_all()


def test_a_dead_module_is_restarted_and_rebinds_its_nodes(daemon_side, tmp_path):
    socket_path = str(tmp_path / "ps.sock")
    supervisor = ModuleSupervisor([_spec(socket_path)], socket_path=socket_path)
    try:
        supervisor.start_all()
        assert _wait_for(lambda: daemon_side.registry.instance("child_1") is not None)
        first_pid = supervisor.status()[0]["pid"]

        # The module dies: its nodes go offline, and the supervisor brings it
        # back.  The module keeps its id, so its placement binds again.
        supervisor.modules[0].proc.kill()
        assert _wait_for(
            lambda: daemon_side.registry.instance("child_1").status
            == "offline_module"
        )

        supervisor.modules[0].next_retry_at = 0.0  # no need to wait the backoff
        supervisor.poll()
        assert _wait_for(
            lambda: daemon_side.registry.instance("child_1").status == "online"
        )
        assert supervisor.status()[0]["pid"] != first_pid
        assert supervisor.status()[0]["restarts"] == 1
    finally:
        supervisor.stop_all()


def test_a_module_that_cannot_start_is_not_restarted_forever(daemon_side, tmp_path):
    """A module whose program does not exist fails fast; after a few tries the
    supervisor gives up instead of spinning."""
    socket_path = str(tmp_path / "ps.sock")
    supervisor = ModuleSupervisor(
        [ModuleSpec("broken", ["/nonexistent/module-program"])],
        socket_path=socket_path,
    )
    try:
        supervisor.start_all()
        for _ in range(6):
            supervisor.modules[0].next_retry_at = 0.0
            supervisor.poll()
        status = supervisor.status()[0]
        assert status["gave_up"] is True
        assert status["alive"] is False
    finally:
        supervisor.stop_all()


def test_stopping_the_daemon_stops_its_modules(daemon_side, tmp_path):
    socket_path = str(tmp_path / "ps.sock")
    supervisor = ModuleSupervisor([_spec(socket_path)], socket_path=socket_path)
    supervisor.start_all()
    assert _wait_for(lambda: supervisor.status()[0]["alive"])
    proc = supervisor.modules[0].proc

    supervisor.stop_all()
    assert proc.poll() is not None
    assert supervisor.status()[0]["alive"] is False
    # A stopped supervisor does not resurrect anything.
    supervisor.poll()
    assert supervisor.status()[0]["alive"] is False


def test_disabled_modules_are_not_spawned(tmp_path):
    supervisor = ModuleSupervisor(
        [ModuleSpec("off", [sys.executable, "-c", "import time; time.sleep(30)"],
                    enabled=False)],
        socket_path=str(tmp_path / "ps.sock"),
    )
    assert supervisor.modules == []
