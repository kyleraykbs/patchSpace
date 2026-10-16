"""An app's own space: `space_declare` (agents/SPACES.md §18).

The spec's opening promise is that an app can create its own space and know
it can place nodes there.  Two halves are tested here:

* the **rule**, at the daemon-side handler with a space tree and a registry
  (create, idempotence, the three refusals, what an announcement does with a
  space id nobody declared, what a client going away gives up);
* the **promise, end to end**: a real module process (`ModuleHost`, the
  client side of the module API) meets the *real* daemon over a real socket,
  declares its space on attach, runs two nodes in it and wires them, and the
  payload a display receives shows that space with both nodes — while a save
  writes nothing of it to disk and a second attach re-declares the same
  space, with nothing persisted in between.
"""

import json
import os
import socket
import threading
import time

import pytest

import central.main as main_mod
from central import module_api as api
from central import spaces
from central.graph import Graph
from central.main import PatchSpaceDaemon
from central.protocol import Connection, ProtocolHandler
from central.registry import Registry
from modules.host import ModuleHost

#: The app's two node types: a number source and a number sink, so the two
#: nodes it runs can be wired to each other.
SOURCE = {
    "type_id": "app_source",
    "schema": 1,
    "ports": [{"name": "out", "kind": "number", "direction": "out"}],
    "controls": [{"key": "value", "widget": "slider", "label": "Value"}],
    "display": {"name": "App Source"},
}

SINK = {
    "type_id": "app_sink",
    "schema": 1,
    "ports": [{"name": "in", "kind": "number", "direction": "in"}],
    "display": {"name": "App Sink"},
}

#: What the app declares on every attach.
DECLARATION = {"space_id": "mycelion", "label": "Mycelion", "color": "@violet"}


@pytest.fixture(autouse=True)
def _no_legacy_migration(monkeypatch, tmp_path):
    """Loading a tree adopts whatever the pre-rename space directories hold
    (`_migrate_legacy_paths`), which would copy the *machine's* session into a
    temp dir.  These tests are about what the daemon writes for one app's
    space, so the source paths are pointed at nothing."""
    for name in ("LEGACY_SPACE_DIR", "LEGACY_OLD_SPELLING_SPACE_DIR",
                 "LEGACY_SESSION_CACHE"):
        monkeypatch.setattr(main_mod, name, str(tmp_path / ("none-" + name)))


# ---------------------------------------------------------------------------
# the rule: the handler, a registry and a space tree
# ---------------------------------------------------------------------------


class Harness:
    """The daemon-side handler with a space tree — no socket, no PipeWire."""

    def __init__(self):
        self.tree = {}
        self.registry = Registry()
        self.handler = ProtocolHandler(
            self.registry, Graph(), spaces=lambda: self.tree
        )
        self.mycelion = self.attach_module("mycelion")
        self.display = Connection(role=api.ROLE_DISPLAY, name="patchspaceGui")

    def attach_module(self, name):
        conn = Connection(role=api.ROLE_MODULE, module_id=name, name=name)
        self.registry.attach(name, name, "1.0")
        self.handler.register_module(conn)
        return conn

    def declare(self, conn, **space):
        return self.handler.handle(api.space_declare(space), conn)

    def announce(self, conn, nodes):
        return self.handler.handle(api.node_announce(nodes), conn)

    def snapshot(self):
        return self.handler.handle(api.snapshot_request(), self.display)

    def record(self, space_id):
        return next(s for s in self.snapshot()["spaces"] if s["id"] == space_id)


def test_a_declaration_creates_the_space_it_names():
    rig = Harness()
    reply = rig.declare(rig.mycelion, **DECLARATION)
    assert reply["type"] == "space_declared"
    record = reply["space"]
    assert record["id"] == "mycelion"
    assert record["label"] == "Mycelion"
    assert record["color"] == "@violet"
    # App-owned: an app cannot make its space persist, and the payload says
    # whose it is (the client id, and the name that id is known by).
    assert record["permanent"] is False
    assert record["declared_by"] == "mycelion"
    assert record["declared_by_name"] == "mycelion"
    assert record["nodes"] == []
    assert rig.tree["mycelion"].permanent is False
    # and the space is in what a display is served
    assert rig.record("mycelion")["declared_by"] == "mycelion"


def test_declaring_again_updates_in_place_and_never_duplicates():
    rig = Harness()
    first = rig.declare(rig.mycelion, **DECLARATION)["space"]
    again = rig.declare(
        rig.mycelion, space_id="mycelion", color="@green", node_view=True
    )["space"]
    assert again["id"] == first["id"] == "mycelion"
    assert list(rig.tree) == ["mycelion"]
    # What the declaration states changes; what it leaves out is kept.
    assert again["color"] == "@green"
    assert again["node_view"] is True
    assert again["label"] == "Mycelion"
    assert again["permanent"] is False
    assert again["declared_by"] == "mycelion"
    # A third, identical declaration is still the same space.
    third = rig.declare(rig.mycelion, **DECLARATION)["space"]
    assert third["id"] == "mycelion"
    assert list(rig.tree) == ["mycelion"]


def test_a_declaration_cannot_claim_a_permanent_space():
    rig = Harness()
    rig.tree["Kit"] = spaces.Space(
        id="Kit", parent="", label="Kitchen", color="@blue", permanent=True,
        config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    )
    reply = rig.declare(rig.mycelion, space_id="Kit", label="Mine now")
    assert reply["type"] == "error"
    assert "permanent" in reply["message"]
    # nothing changed
    assert rig.tree["Kit"].label == "Kitchen"
    assert rig.tree["Kit"].declared_by == ""
    assert rig.tree["Kit"].permanent is True


def test_a_declaration_cannot_claim_a_file_declared_space():
    rig = Harness()
    rig.tree["nix"] = spaces.Space(
        id="nix", parent="", label="From Nix", color="", mode=spaces.MODE_RO,
        permanent=False, config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    )
    reply = rig.declare(rig.mycelion, space_id="nix")
    assert reply["type"] == "error"
    assert "read-only" in reply["message"]
    assert rig.tree["nix"].declared_by == ""


def test_a_declaration_cannot_claim_another_clients_space():
    rig = Harness()
    other = rig.attach_module("otherapp")
    rig.declare(other, space_id="shared", label="Other")
    reply = rig.declare(rig.mycelion, space_id="shared", label="Mine")
    assert reply["type"] == "error"
    assert "owned by 'otherapp'" in reply["message"]
    assert rig.tree["shared"].label == "Other"
    assert rig.tree["shared"].declared_by == "otherapp"
    # ...and the owner re-declaring it still works (that is not a takeover).
    assert rig.declare(other, space_id="shared", label="Other")["type"] == "space_declared"
    # The ordinary edit paths stay allowed: an app may *edit* it by name.
    rig.tree["shared"].label = "Edited by name"
    assert rig.tree["shared"].label == "Edited by name"


def test_the_id_path_is_the_nesting_a_declaration_asserts():
    rig = Harness()
    rig.declare(rig.mycelion, space_id="hub", label="Hub")
    nested = rig.declare(rig.mycelion, space_id="hub::inner", parent="hub")
    assert nested["type"] == "space_declared"
    assert nested["space"]["id"] == "hub::inner"
    assert nested["space"]["parent"] == "hub"
    # A declaration whose stated parent contradicts the id path is refused
    # rather than relocated: the app names its nodes with the same id path.
    wrong = rig.declare(rig.mycelion, space_id="hub::other", parent="elsewhere")
    assert wrong["type"] == "error"
    assert "is in 'hub'" in wrong["message"]
    assert "hub::other" not in rig.tree


def test_a_malformed_declaration_is_refused_as_a_message():
    rig = Harness()
    assert "needs a space_id" in rig.declare(rig.mycelion)["message"]
    # The root is not an id anybody declares: the declaration is refused by
    # the id-path rule itself, with the reason a client should hear.
    assert "root space" in rig.declare(rig.mycelion, space_id="")["message"]
    assert "root space" in spaces.space_id_problem(spaces.ROOT_ID)
    bad_id = rig.declare(rig.mycelion, space_id="my app")
    assert bad_id["type"] == "error"
    assert "label" in bad_id["message"]
    unknown = rig.declare(rig.mycelion, space_id="x", permanent=True)
    assert "unknown space declaration field" in unknown["message"]
    assert rig.tree == {}


def test_only_a_module_may_declare():
    """A declaration is the app's own space for the nodes it runs, and a
    module is the client that runs nodes: a display creates spaces the user's
    way (`create_space`, permanent), and an `app` connection has no client id
    to own anything with."""
    rig = Harness()
    assert rig.declare(rig.display, **DECLARATION)["message"] == (
        "display connections may not do that"
    )
    assert rig.declare(Connection(), **DECLARATION)["message"] == "send hello first"
    assert rig.tree == {}


def test_a_handler_with_no_spaces_refuses_a_declaration():
    handler = ProtocolHandler(Registry(), Graph())
    conn = Connection(role=api.ROLE_MODULE, module_id="m", name="m")
    reply = handler.handle(api.space_declare(DECLARATION), conn)
    assert reply["type"] == "error"
    assert "no spaces" in reply["message"]


def test_the_nodes_an_app_announces_into_its_space_land_there():
    rig = Harness()
    rig.declare(rig.mycelion, **DECLARATION)
    rig.announce(rig.mycelion, [
        {"instance_id": "mycelion::src", "type_id": "app_source",
         "space": "mycelion"},
        {"instance_id": "mycelion::sink", "type_id": "app_sink",
         "space": "mycelion"},
    ])
    record = rig.record("mycelion")
    assert record["nodes"] == ["mycelion::sink", "mycelion::src"]
    snapshot = rig.snapshot()
    assert {i["space"] for i in snapshot["instances"]} == {"mycelion"}


def test_an_announcement_makes_the_space_it_names():
    """The pre-S8 module path: an app that just names a space gets it, as its
    own, without a round trip — that is what keeps every existing module
    working, and it is the same promise `space_declare` makes."""
    rig = Harness()
    rig.announce(rig.mycelion, [
        {"instance_id": "solo", "type_id": "app_source", "space": "hub"},
    ])
    assert rig.tree["hub"].declared_by == "mycelion"
    assert rig.tree["hub"].permanent is False
    assert rig.record("hub")["nodes"] == ["solo"]
    # A node whose own id path says where it lives is in that space, whatever
    # else the payload names...
    rig.announce(rig.mycelion, [
        {"instance_id": "mycelion::a", "type_id": "app_source", "space": "main"},
    ])
    assert rig.tree["mycelion"].declared_by == "mycelion"
    assert rig.record("mycelion")["nodes"] == ["mycelion::a"]
    # ...and naming nothing, or only the default routing region, is the root:
    # no space record is invented for `main`.
    rig.announce(rig.mycelion, [
        {"instance_id": "bare", "type_id": "app_source"},
        {"instance_id": "region", "type_id": "app_source", "space": "main"},
    ])
    assert sorted(rig.tree) == ["hub", "mycelion"]


def test_announcing_into_another_clients_space_does_not_claim_it():
    rig = Harness()
    other = rig.attach_module("otherapp")
    rig.declare(other, space_id="shared", label="Other's")
    rig.announce(rig.mycelion, [
        {"instance_id": "shared::a", "type_id": "app_source", "space": "shared"},
    ])
    assert rig.tree["shared"].declared_by == "otherapp"
    assert rig.record("shared")["nodes"] == ["shared::a"]


def test_a_client_going_away_releases_its_space_but_keeps_it():
    rig = Harness()
    rig.declare(rig.mycelion, **DECLARATION)
    rig.announce(rig.mycelion, [
        {"instance_id": "mycelion::a", "type_id": "app_source",
         "space": "mycelion"},
    ])
    rig.registry.detach("mycelion")
    assert rig.handler.release_spaces("mycelion") == ["mycelion"]
    record = rig.record("mycelion")
    assert record["declared_by"] == ""
    assert record["declared_by_name"] == ""
    # The record stays (its nodes are still there, offline), and the app that
    # comes back re-declares the same space.
    assert record["permanent"] is False
    assert record["nodes"] == ["mycelion::a"]
    assert rig.tree["mycelion"].label == "Mycelion"
    again = rig.attach_module("mycelion")
    assert rig.declare(again, **DECLARATION)["space"]["id"] == "mycelion"


def test_a_released_space_may_be_claimed_by_another_client():
    """Ownership belongs to a connection: a client that is gone owns nothing,
    so the id is not blocked for the whole session (the record cannot outlive
    it anyway — nothing of a declared space is persisted)."""
    rig = Harness()
    rig.declare(rig.mycelion, space_id="shared", label="Mycelion's")
    rig.registry.detach("mycelion")
    rig.handler.release_spaces("mycelion")
    other = rig.attach_module("otherapp")
    reply = rig.declare(other, space_id="shared", label="Other's now")
    assert reply["type"] == "space_declared"
    assert reply["space"]["declared_by"] == "otherapp"


# ---------------------------------------------------------------------------
# the promise: a module process, the real daemon, real sockets
# ---------------------------------------------------------------------------


def test_the_user_taking_a_declared_space_makes_it_theirs(tmp_path):
    """`permanent` is the user's to turn on (§18): doing it takes the space
    into the saved session — which needs a file, since an app's space never
    had one — ends the app's claim (the id is not declarable any more), and
    survives a restart."""
    spaces_dir = tmp_path / "spaces"
    spaces_dir.mkdir()
    root = tmp_path / "root.json"
    daemon = _daemon(spaces_dir, root)
    kit = daemon.handle_command(
        {"command": "create_space", "name": "Kit", "label": "Kitchen"}
    )["space_id"]

    # the app declares its space (over the handler, as it does on attach)
    conn = Connection(role=api.ROLE_MODULE, module_id="mycelion", name="mycelion")
    daemon.registry.attach("mycelion", "mycelion", "1.0")
    assert daemon._protocol.handle(
        api.space_declare(DECLARATION), conn
    )["type"] == "space_declared"

    taken = daemon.handle_command({
        "command": "set_space_meta", "space_id": "mycelion", "permanent": True,
    })
    assert taken["status"] == "ok", taken
    record = daemon.spaces["mycelion"]
    assert record.permanent is True
    assert record.declared_by == ""
    assert record.path and os.path.basename(record.path) == "mycelion.json"

    # it is written now, and reachable from the root on the next start
    again = _daemon(spaces_dir, root)
    assert "mycelion" in again.spaces
    assert again.spaces["mycelion"].permanent is True
    assert again.spaces["mycelion"].declared_by == ""
    assert kit in again.spaces

    # ...and the app cannot claim it back: that is what the flag means.
    refused = daemon._protocol.handle(api.space_declare(DECLARATION), conn)
    assert refused["type"] == "error"
    assert "permanent" in refused["message"]


class DaemonSide:
    """The *real* daemon behind a real AF_UNIX socket, with the daemon's own
    client loop: what a module process actually meets."""

    def __init__(self, daemon, path):
        self.daemon = daemon
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(path)
        self.server.listen(8)
        self._running = True
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while self._running:
            try:
                sock, _ = self.server.accept()
            except OSError:
                return
            threading.Thread(target=self._client, args=(sock,), daemon=True).start()

    def _client(self, sock):
        conn = Connection(socket=sock)
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
                    reply = self.daemon.handle_command(
                        json.loads(line.decode("utf-8")), conn
                    )
                    for message in reply if isinstance(reply, list) else [reply]:
                        sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        finally:
            # The daemon's own client loop, verbatim: a module socket dying
            # takes its nodes offline and gives up its space claim.
            if conn.module_id:
                self.daemon._protocol.unregister_module(conn)
                self.daemon._protocol.release_spaces(conn.module_id)
                self.daemon.registry.detach(conn.module_id)

    def stop(self):
        self._running = False
        try:
            self.server.close()
        except OSError:
            pass


class Display:
    """A display role over a real socket: what the GUI (or a headless
    display) does — greet once, then ask for the graph and wire nodes."""

    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(path)
        self.buffer = b""
        assert self.call(api.hello(api.ROLE_DISPLAY, "patchspaceGui"))["type"] == "hello_ok"

    def call(self, message):
        self.sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        while b"\n" not in self.buffer:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise AssertionError("the daemon closed the connection")
            self.buffer += chunk
        line, self.buffer = self.buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def _daemon(spaces_dir, root) -> PatchSpaceDaemon:
    """A daemon that has loaded its spaces (the startup path), with no
    PipeWire process and no socket of its own."""
    daemon = PatchSpaceDaemon(
        space_dirs=[(str(spaces_dir), True)], root_space_path=str(root)
    )
    daemon._install_spaces(daemon._load_spaces_tree())
    return daemon


def _until(predicate, timeout: float = 3.0) -> bool:
    """Wait for a background client thread to finish what it does on
    disconnect (the daemon's loop runs in one)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_an_app_declares_announces_wires_and_the_payload_shows_it(tmp_path):
    spaces_dir = tmp_path / "spaces"
    spaces_dir.mkdir()
    root = tmp_path / "root.json"
    socket_path = str(tmp_path / "ps.sock")

    daemon = _daemon(spaces_dir, root)
    side = DaemonSide(daemon, socket_path)
    try:
        # A permanent space the *user* made (the GUI's own path), to portal
        # the app's space into.
        created = daemon.handle_command(
            {"command": "create_space", "name": "Kit", "label": "Kitchen"}
        )
        assert created["status"] == "ok", created
        kit = created["space_id"]

        host = ModuleHost(
            "mycelion", "1.0", socket_path=socket_path,
            declarations=[SOURCE, SINK], space=DECLARATION,
        )
        assert host.connect() is True

        # The declaration went out with the attach: the space exists before
        # any node does.
        declared = daemon.spaces["mycelion"]
        assert (declared.label, declared.color, declared.permanent) == (
            "Mycelion", "@violet", False,
        )
        assert declared.declared_by == "mycelion"

        announced = host.announce([
            {"instance_id": "mycelion::src", "type_id": "app_source",
             "space": "mycelion"},
            {"instance_id": "mycelion::sink", "type_id": "app_sink",
             "space": "mycelion"},
        ])
        assert sorted(announced["created"]) == ["mycelion::sink", "mycelion::src"]

        display = Display(socket_path)
        wired = display.call(
            api.node_edge("mycelion::src", "mycelion::sink", "in", "out")
        )
        assert wired["type"] == "ok", wired

        snapshot = display.call(api.snapshot_request())
        record = next(s for s in snapshot["spaces"] if s["id"] == "mycelion")
        assert record["permanent"] is False
        assert record["declared_by"] == "mycelion"
        assert record["declared_by_name"] == "mycelion"
        assert record["nodes"] == ["mycelion::sink", "mycelion::src"]
        assert {i["instance_id"]: i["space"] for i in snapshot["instances"]} == {
            "mycelion::src": "mycelion", "mycelion::sink": "mycelion",
        }
        assert [e["id"] for e in snapshot["edges"]] == ["mycelion::src->mycelion::sink"]

        # The GUI's payload says the same thing about the same space.
        nodes = daemon.handle_command({"command": "get_nodes"})
        gui_record = next(s for s in nodes["spaces"] if s["id"] == "mycelion")
        assert gui_record["declared_by"] == "mycelion"
        assert gui_record["declared_by_name"] == "mycelion"
        assert gui_record["permanent"] is False

        # A save writes nothing of it...
        daemon._write_spaces()
        assert not (spaces_dir / "mycelion.json").exists()
        assert "mycelion" not in root.read_text()
        assert "mycelion" not in (tmp_path / "placement_diffs.json").read_text()

        # ...while a portal from a permanent space to it *is* saved, in the
        # permanent space's own file (agents/SPACES.md §13).
        embedded = daemon.handle_command({
            "command": "embed_space", "space_id": "mycelion",
            "parent_space": kit, "x": 10, "y": 20,
        })
        assert embedded["status"] == "ok", embedded
        assert embedded["embedded_in"] == [kit]
        kit_raw = json.loads((spaces_dir / f"{kit}.json").read_text())
        assert kit_raw["config"]["portals"] == [
            {"space_id": "mycelion", "view": "inside", "x": 10.0, "y": 20.0}
        ]
        assert not (spaces_dir / "mycelion.json").exists()

        # The app goes away: its claim goes with the connection, the record
        # and its (offline) nodes stay.
        host.close()
        assert _until(lambda: daemon.spaces["mycelion"].declared_by == "")
        assert daemon.spaces["mycelion"].label == "Mycelion"
        assert daemon.spaces["mycelion"].permanent is False
        display.close()
    finally:
        side.stop()

    # A restart: nothing of the app's space came back (it was never written),
    # and the app's next attach re-declares the same space, idempotently.
    again = _daemon(spaces_dir, root)
    assert "mycelion" not in again.spaces
    side2 = DaemonSide(again, str(tmp_path / "ps2.sock"))
    try:
        host2 = ModuleHost(
            "mycelion", "1.0", socket_path=str(tmp_path / "ps2.sock"),
            declarations=[SOURCE, SINK], space=DECLARATION,
        )
        assert host2.connect() is True
        back = again.spaces["mycelion"]
        assert (back.id, back.label, back.color, back.permanent, back.declared_by) == (
            "mycelion", "Mycelion", "@violet", False, "mycelion",
        )
        # The user's own space came back the way it was written, portal and
        # all: the app's space is the only thing that had to be re-declared.
        assert kit in again.spaces
        assert again.spaces[kit].portal_ids == ["mycelion"]
        host2.close()
    finally:
        side2.stop()