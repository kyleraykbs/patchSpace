"""The module API over the daemon: hello/role gating, the declare →
migrate → announce handshake, app queries, and module drop-off.

These exercise `PatchSpaceDaemon.handle_command` directly (the same way the
other daemon tests do) plus one real socketpair for the disconnect path.
Constructing a daemon starts nothing - no pw-dump, no server, no ticker.
"""

import json
import socket
import threading

from central import module_api as api
from central.main import PatchSpaceDaemon
from central.protocol import Connection

CAMERA_IN = {
    "type_id": "camera_in",
    "schema": 1,
    "capabilities": ["camera-source"],
    "ports": [{"name": "out", "kind": "camera", "direction": "out"}],
    "display": {"name": "Camera In"},
}


def fresh_daemon():
    return PatchSpaceDaemon()


def test_a_connection_must_greet_before_it_may_declare():
    daemon = fresh_daemon()
    conn = Connection()
    reply = daemon.handle_command(api.type_declare([CAMERA_IN]), conn)
    assert reply["type"] == "error"
    assert "hello" in reply["message"]


def test_hello_registers_a_module_and_greets_once():
    daemon = fresh_daemon()
    conn = Connection()
    reply = daemon.handle_command(api.hello(api.ROLE_MODULE, "pipewire", "1.0"), conn)
    assert reply["type"] == "hello_ok" and reply["proto"] == api.PROTO
    assert conn.module_id == "pipewire"
    assert daemon.registry.module("pipewire").connected is True

    again = daemon.handle_command(api.hello(api.ROLE_MODULE, "pipewire"), conn)
    assert again["type"] == "error" and "already greeted" in again["message"]


def test_the_legacy_protocol_still_works_on_an_ungreeted_connection():
    """The GUI speaks {"command": ...} and never greets: that path must be
    untouched while the module API lands beside it."""
    daemon = fresh_daemon()
    reply = daemon.handle_command({"command": "get_nodes"})
    assert isinstance(reply, dict)
    assert reply.get("status") == "ok"
    assert "nodes" in reply


def test_a_display_may_not_declare_types():
    daemon = fresh_daemon()
    conn = Connection()
    daemon.handle_command(api.hello(api.ROLE_DISPLAY, "patchspaceGui"), conn)
    reply = daemon.handle_command(api.type_declare([CAMERA_IN]), conn)
    assert reply["type"] == "error" and "may not" in reply["message"]


def test_declare_fans_out_one_migrate_per_persisted_instance():
    daemon = fresh_daemon()
    daemon.registry.place("cam_1", "camera_in",
                          config={"device": "/dev/video0", "schema_version": 1})
    conn = Connection()
    daemon.handle_command(api.hello(api.ROLE_MODULE, "pipewire", "1.0"), conn)

    replies = daemon.handle_command(api.type_declare([CAMERA_IN]), conn)
    assert isinstance(replies, list)
    # The fan-out comes first, one terminal reply last: a client reads until
    # it sees something that is not a migrate.
    assert replies[-1]["type"] == "declared"
    migrates = [r for r in replies if r["type"] == "migrate"]
    assert [m["instance_id"] for m in migrates] == ["cam_1"]
    assert migrates[0]["from_schema"] == 1
    # Migrating is what makes it online: the module keeps the id and the
    # config it wrote back.
    result = daemon.handle_command(
        api.migrate_result("cam_1", {"device": "/dev/video0", "schema_version": 1}), conn
    )
    assert result["type"] == "ok"
    announced = daemon.handle_command(
        api.node_announce([{"instance_id": "cam_1", "type_id": "camera_in"}]), conn
    )
    assert announced[0]["bound"] == ["cam_1"]
    assert daemon.registry.instance("cam_1").status == api.STATUS_ONLINE
    assert daemon.registry.instance("cam_1").config["device"] == "/dev/video0"


def test_an_app_queries_the_registry_without_a_module_role():
    daemon = fresh_daemon()
    module = Connection()
    daemon.handle_command(api.hello(api.ROLE_MODULE, "pipewire", "1.0"), module)
    daemon.handle_command(api.type_declare([CAMERA_IN]), module)
    daemon.handle_command(
        api.node_announce([{"instance_id": "cam_1", "type_id": "camera_in"}]), module
    )

    app = Connection()
    daemon.handle_command(api.hello(api.ROLE_APP, "game"), app)
    reply = daemon.handle_command(api.registry_query(capability="camera-source"), app)
    assert reply["type"] == "registry_reply"
    assert reply["types"][0]["type_id"] == "camera_in"
    assert [i["instance_id"] for i in reply["instances"]] == ["cam_1"]
    assert reply["modules"][0]["module_id"] == "pipewire"

    missing = daemon.handle_command(api.registry_query(type_id="nobody_makes_this"), app)
    assert missing["instances"] == [] and missing["types"] == []

    assert daemon.handle_command({"type": "registry_query"}, app)["type"] == "error"


def test_a_module_function_that_only_applies_to_modules_is_refused():
    daemon = fresh_daemon()
    app = Connection()
    daemon.handle_command(api.hello(api.ROLE_APP, "game"), app)
    for message in (
        api.node_announce([{"instance_id": "n", "type_id": "t"}]),
        api.node_remove(["n"]),
        api.migrate_result("n", {}),
    ):
        reply = daemon.handle_command(message, app)
        assert reply["type"] == "error", message


def test_a_dropped_module_connection_takes_its_nodes_offline():
    """A module process dying is what the detached status is for: the
    placements stay, the ids stay bound to the same module record."""
    import time

    daemon = fresh_daemon()
    daemon._running = True
    ours, theirs = socket.socketpair()

    def read_until(kind, deadline=5.0):
        buffer = ""
        end = time.monotonic() + deadline
        theirs.settimeout(deadline)
        while time.monotonic() < end:
            data = theirs.recv(65536)
            if not data:
                break
            buffer += data.decode()
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if not line.strip():
                    continue
                message = json.loads(line)
                if message.get("type") == kind:
                    return message
        raise AssertionError(f"no {kind!r} reply arrived")

    try:
        worker = threading.Thread(target=daemon._handle_client, args=(ours,), daemon=True)
        worker.start()
        theirs.sendall((json.dumps(api.hello(api.ROLE_MODULE, "pipewire", "1.0")) + "\n").encode())
        assert read_until("hello_ok")["proto"] == api.PROTO
        theirs.sendall((json.dumps(api.type_declare([CAMERA_IN])) + "\n").encode())
        assert read_until("declared")["module_id"] == "pipewire"
        theirs.sendall((json.dumps(api.node_announce(
            [{"instance_id": "cam_1", "type_id": "camera_in"}]
        )) + "\n").encode())
        assert read_until("announced")["created"] == ["cam_1"]
        # The announced node is live while the module is connected.
        assert daemon.registry.instance("cam_1").status == api.STATUS_ONLINE

        theirs.close()
        worker.join(timeout=5)
        assert daemon.registry.instance("cam_1").status == api.STATUS_OFFLINE_MODULE
    finally:
        ours.close()


def test_unknown_message_types_are_refused_not_ignored():
    daemon = fresh_daemon()
    conn = Connection()
    daemon.handle_command(api.hello(api.ROLE_APP, "game"), conn)
    reply = daemon.handle_command({"type": "summon_demon"}, conn)
    assert reply["type"] == "error" and "summon_demon" in reply["message"]
