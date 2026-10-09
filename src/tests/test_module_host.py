"""The module API over a real socket, from a module process's side.

`ModuleHost` is what an out-of-process module runs.  These tests put the
*real* daemon-side handler (`ProtocolHandler` + `Registry`) behind a real
AF_UNIX socket and drive a host against it, so the handshake, the migration
fan-out, the announce/status path and the daemon's pushes are all exercised
across a process boundary (in-process objects, one real socket).
"""

import json
import socket
import threading
import time

import pytest

from central import module_api as api
from central.graph import Graph
from central.protocol import Connection, ProtocolHandler
from central.registry import Registry
from modules.host import ModuleHost

CAMERA_IN = {
    "type_id": "camera_in",
    "schema": 1,
    "capabilities": ["camera-source"],
    "ports": [{"name": "out", "kind": "camera", "direction": "out"}],
    "display": {"name": "Camera In"},
}


class DaemonSide:
    """A daemon with no PipeWire: the real handler on a real socket."""

    def __init__(self, path: str):
        self.registry = Registry()
        self.handler = ProtocolHandler(self.registry, Graph())
        self.connections = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(4)
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while self._running:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(sock,), daemon=True).start()

    def _client(self, sock: socket.socket) -> None:
        conn = Connection(socket=sock)
        self.connections.append(conn)
        buffer = b""
        try:
            while self._running:
                try:
                    chunk = sock.recv(65536)
                except OSError:
                    break
                if not chunk:
                    break
                buffer += chunk
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    if not line.strip():
                        continue
                    reply = self.handler.handle(json.loads(line.decode("utf-8")), conn)
                    for message in reply if isinstance(reply, list) else [reply]:
                        sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        finally:
            # Same as the daemon's own client loop: a module socket dying
            # takes its nodes offline (the placements stay, so a reconnect
            # binds them again).
            if conn.module_id:
                self.registry.detach(conn.module_id)

    def push(self, message: dict) -> None:
        """Send a daemon -> module message to the last module that attached."""
        self.connections[-1].socket.sendall((json.dumps(message) + "\n").encode())

    def stop(self) -> None:
        self._running = False
        try:
            self.server.close()
        except OSError:
            pass


@pytest.fixture()
def daemon_side(tmp_path):
    side = DaemonSide(str(tmp_path / "ps.sock"))
    yield side
    side.stop()


@pytest.fixture()
def socket_path(tmp_path):
    return str(tmp_path / "ps.sock")


def test_a_module_host_attaches_declares_and_announces(daemon_side, socket_path):
    host = ModuleHost("testmod", "2.0", socket_path=socket_path,
                      declarations=[CAMERA_IN])
    assert host.connect() is True
    assert daemon_side.registry.type_decl("camera_in") is not None
    assert daemon_side.registry.module("testmod").connected is True

    summary = host.announce([{"instance_id": "cam_1", "type_id": "camera_in"}])
    assert summary["created"] == ["cam_1"]
    assert daemon_side.registry.instance("cam_1").status == api.STATUS_ONLINE

    host.unannounce(["cam_1"])
    assert daemon_side.registry.instance("cam_1").status == api.STATUS_OFFLINE_MODULE
    host.close()


def test_the_migration_fan_out_reaches_the_host(daemon_side, socket_path):
    """A persisted instance of a declared type is handed to the module on
    attach, and the module's upgraded config is what the daemon stores."""
    daemon_side.registry.place("cam_1", "camera_in",
                               config={"device": "/dev/video0", "schema_version": 1})
    seen = []

    def migrate(config):
        seen.append(dict(config))
        return {"device": config.get("device"), "schema_version": 2}

    host = ModuleHost("testmod", "2.0", socket_path=socket_path,
                      declarations=[CAMERA_IN], migrate=migrate)
    assert host.connect() is True
    assert seen == [{"device": "/dev/video0", "schema_version": 1}]
    assert host.migrations["cam_1"] == {"device": "/dev/video0", "schema_version": 2}
    assert daemon_side.registry.instance("cam_1").config["schema_version"] == 2

    # Announcing the same id adopts the placement (the id survives).
    summary = host.announce([{"instance_id": "cam_1", "type_id": "camera_in"}])
    assert summary["bound"] == ["cam_1"]
    assert daemon_side.registry.instance("cam_1").status == api.STATUS_ONLINE
    host.close()


def test_the_daemon_pushes_and_the_host_hands_it_over(daemon_side, socket_path):
    controls, edges, statuses = [], [], []
    host = ModuleHost(
        "testmod", "2.0", socket_path=socket_path, declarations=[CAMERA_IN],
        on_control=controls.append, on_edge=edges.append, on_status=statuses.append,
    )
    assert host.connect() is True
    host.announce([{"instance_id": "cam_1", "type_id": "camera_in"}])

    daemon_side.push(api.control_set("cam_1", "output_device", "/dev/video2"))
    daemon_side.push(api.edge_apply({"id": "cam_1->vcam"}, "add"))
    daemon_side.push(api.status_change({"cam_1": api.STATUS_ONLINE}))

    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and not (controls and edges and statuses):
        host.pump(timeout=0.2)

    assert controls[-1]["key"] == "output_device"
    assert controls[-1]["value"] == "/dev/video2"
    assert edges[-1]["edge"]["id"] == "cam_1->vcam"
    assert statuses[-1]["instances"] == {"cam_1": api.STATUS_ONLINE}
    host.close()


def test_a_host_with_no_daemon_reports_not_connected(tmp_path):
    host = ModuleHost("testmod", socket_path=str(tmp_path / "missing.sock"),
                      declarations=[CAMERA_IN])
    assert host.connect() is False
    assert host.connected is False


def test_the_daemon_serves_the_catalog_it_was_told(daemon_side, socket_path):
    """What a module declares is what a display is served — including the
    menu and vocabulary the module owns."""
    host = ModuleHost(
        "testmod", socket_path=socket_path, declarations=[CAMERA_IN],
        menu=[{"category": "Cameras", "items": [{"type_id": "camera_in",
                                                 "label": "Camera In"}]}],
        vocabulary={"field_labels": {"device": "Device:"}},
        aliases={"old_cam": "camera_in"},
    )
    assert host.connect() is True

    display = Connection()
    daemon_side.handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    reply = daemon_side.handler.handle(api.catalog_request(), display)
    assert [t["type_id"] for t in reply["types"]] == ["camera_in"]
    assert reply["menu"][0]["category"] == "Cameras"
    assert reply["vocabulary"]["field_labels"] == {"device": "Device:"}
    assert reply["aliases"] == {"old_cam": "camera_in"}
    host.close()
