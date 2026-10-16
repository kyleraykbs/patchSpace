"""
host.py — the client side of the module API: what a module *process* runs.

A module is a program that connects to the daemon's socket, greets as a
module, declares the types it provides, announces the instances it is
running, and answers what the daemon asks of it (set this control, realize
this edge, migrate this stored config).  This class owns that handshake,
the reconnect, and the dispatch, so a module supplies only its data and its
handlers.

The built-in PipeWire provider runs in-process through the same
`ProtocolHandler` (phase 1/2) — this is the identical protocol over a real
socket, which is what makes "move a provider out of the daemon" a transport
change rather than a rewrite (agents/PLAN.md phase 3).
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from central import module_api as api

logger = logging.getLogger(__name__)

#: Reconnect backoff bounds, seconds.
RETRY_MIN_S = 0.5
RETRY_MAX_S = 10.0


class ModuleHost:
    """A module's connection to the daemon."""

    def __init__(
        self,
        name: str,
        version: str = "1.0",
        *,
        socket_path: Optional[str] = None,
        declarations: Optional[List[Dict[str, Any]]] = None,
        menu: Optional[List[Dict[str, Any]]] = None,
        vocabulary: Optional[Dict[str, Any]] = None,
        aliases: Optional[Dict[str, str]] = None,
        space: Optional[Mapping[str, Any]] = None,
        migrate: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        on_control: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_edge: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_status: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_value: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_spawn: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_stop: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        if socket_path is None:
            from central.main import SOCKET_PATH  # late: only for a real run

            socket_path = SOCKET_PATH
        self.name = name
        self.version = version
        self.socket_path = socket_path
        self.declarations = list(declarations or [])
        self.menu = list(menu or [])
        self.vocabulary = dict(vocabulary or {})
        self.aliases = dict(aliases or {})
        #: The space this module declares on every attach (§18): the id path
        #: its nodes live in, declared so the daemon creates it.  None: the
        #: module names no space of its own, and its nodes land per their own
        #: id paths (the root, for a bare id).
        self.space = dict(space) if space else None
        self.migrate = migrate
        self.on_control = on_control
        self.on_edge = on_edge
        self.on_status = on_status
        self.on_value = on_value
        self.on_spawn = on_spawn
        self.on_stop = on_stop
        #: What the daemon said when it accepted us.
        self.module_id = ""
        self.daemon_version = ""
        #: Migrations the daemon asked for, keyed by instance id.
        self.migrations: Dict[str, Dict[str, Any]] = {}
        self._sock: Optional[socket.socket] = None
        self._buffer = b""
        self._lock = threading.Lock()
        self._running = False

    # -- connection ------------------------------------------------------

    def connect(self) -> bool:
        """Connect, greet, declare and announce.  Returns False if the
        daemon is not there (the caller decides whether to retry)."""
        self._sock = self._open()
        if self._sock is None:
            return False
        replies = self._call(api.hello(api.ROLE_MODULE, self.name, self.version))
        reply = replies[-1] if replies else {}
        if reply.get("type") != "hello_ok":
            logger.error("module %s was refused: %s", self.name, reply)
            self.close()
            return False
        self.daemon_version = str(reply.get("daemon") or "")
        self._call(api.type_declare(
            self.declarations, menu=self.menu, vocabulary=self.vocabulary,
            aliases=self.aliases,
        ))
        if self.space is not None:
            # On *every* attach, including a reconnect: the declaration is the
            # only thing that makes an app's space exist (§18).  The answer is
            # not fatal here (a claim the daemon refuses leaves the module's
            # nodes to land per their own id paths); a module that wants the
            # reason calls `declare_space` itself.
            self.declare_space(self.space)
        return True

    def _open(self) -> Optional[socket.socket]:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.connect(self.socket_path)
            return sock
        except OSError as exc:
            logger.info("module %s: daemon not reachable (%s)", self.name, exc)
            return None

    def close(self) -> None:
        with self._lock:
            self._running = False
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # -- what a module tells the daemon ----------------------------------

    def announce(self, nodes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        """Declare the instances this module is running."""
        return self._reply_of(api.node_announce(list(nodes)), "announced")

    def unannounce(self, instance_ids: Iterable[str]) -> Dict[str, Any]:
        return self._reply_of(api.node_remove(list(instance_ids)), "removed")

    def declare_space(self, space: Mapping[str, Any]) -> Dict[str, Any]:
        """Declare (or re-declare) the space this module owns
        (agents/SPACES.md §18).

        Sent on *every* attach — :meth:`connect` sends `self.space` if the
        module gave one, so a reconnect re-declares it without the module
        doing anything — which is how an app's space exists at all: it is
        never persisted, so the declaration is what makes it, and the same
        id every time is what makes it the same space.  Returns the daemon's
        answer: `space_declared` with the record, or `error` with its reason
        (a claim on a permanent, file-declared or another client's space)."""
        replies = self._call(api.space_declare(dict(space)))
        return replies[-1] if replies else {}

    def report_health(self, health: Mapping[str, str]) -> None:
        self._call(api.node_health(dict(health)))

    def report_values(self, values: Mapping[str, Mapping[str, Any]]) -> None:
        """Tell the daemon what the module's nodes currently compute."""
        self._call(api.node_values({k: dict(v) for k, v in values.items()}))

    # -- the daemon's side -----------------------------------------------

    def pump(self, timeout: float = 0.0) -> List[Dict[str, Any]]:
        """Handle whatever the daemon has sent.  Returns the messages
        handled (useful for tests and for embedding a module in a loop)."""
        handled: List[Dict[str, Any]] = []
        sock = self._sock
        if sock is None:
            return handled
        import select

        # Always poll first: a caller that asked for no wait must not block
        # on a socket the daemon has nothing to say on.
        ready, _, _ = select.select([sock], [], [], timeout)
        if not ready:
            return handled
        try:
            chunk = sock.recv(65536)
        except OSError:
            self.close()
            return handled
        if not chunk:
            self.close()
            return handled
        self._buffer += chunk
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError:
                logger.warning("module %s: bad JSON from the daemon", self.name)
                continue
            self._handle(message)
            handled.append(message)
        return handled

    def run_forever(self) -> None:
        """Serve until closed: pump, and reconnect with backoff if the
        daemon goes away (a module outlives one daemon restart)."""
        self._running = True
        backoff = RETRY_MIN_S
        while self._running:
            if self._sock is None:
                if not self.connect():
                    time.sleep(backoff)
                    backoff = min(backoff * 2, RETRY_MAX_S)
                    continue
                backoff = RETRY_MIN_S
            self.pump(timeout=1.0)

    # -- internals -------------------------------------------------------

    def _handle(self, message: Dict[str, Any]) -> None:
        kind = message.get("type")
        if kind == "migrate":
            self._answer_migration(message)
        elif kind == "control_set":
            if self.on_control is not None:
                self.on_control(message)
        elif kind == "edge_apply":
            if self.on_edge is not None:
                self.on_edge(message)
        elif kind == "status_change":
            if self.on_status is not None:
                self.on_status(message)
        elif kind == "value_set":
            if self.on_value is not None:
                self.on_value(message)
        elif kind == "node_spawn":
            if self.on_spawn is not None:
                self.on_spawn(message)
        elif kind == "node_stop":
            if self.on_stop is not None:
                self.on_stop(message)
        elif kind == "error":
            logger.error("module %s: daemon error: %s", self.name,
                         message.get("message"))
        elif kind in ("hello_ok", "declared", "announced", "removed", "ok"):
            pass
        else:
            logger.warning("module %s: ignoring %r", self.name, kind)

    def _answer_migration(self, message: Dict[str, Any]) -> None:
        instance_id = str(message.get("instance_id") or "")
        config = dict(message.get("config") or {})
        upgraded = config
        if self.migrate is not None:
            upgraded = self.migrate(config)
        self.migrations[instance_id] = dict(upgraded)
        self._call(api.migrate_result(instance_id, upgraded))

    def _reply_of(self, message: Dict[str, Any], wanted: str) -> Dict[str, Any]:
        for reply in self._call(message):
            if reply.get("type") == wanted:
                return reply
        return {}

    def _call(self, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Send one message and read its replies (one, or several for a
        type declaration's migration fan-out)."""
        sock = self._sock
        if sock is None:
            return []
        try:
            sock.sendall((json.dumps(message) + "\n").encode("utf-8"))
        except OSError:
            self.close()
            return []
        replies: List[Dict[str, Any]] = []
        # A declaration answers with `declared` plus one `migrate` per
        # persisted instance; the fan-out ends when a reply is not a
        # migrate.  A migration result is answered by a single `ok`.
        while True:
            line = self._read_line()
            if line is None:
                break
            replies.append(line)
            if line.get("type") == "migrate" and message.get("type") == "type_declare":
                self._answer_migration(line)
                continue
            break
        return replies

    def _read_line(self) -> Optional[Dict[str, Any]]:
        sock = self._sock
        if sock is None:
            return None
        while b"\n" not in self._buffer:
            try:
                chunk = sock.recv(65536)
            except OSError:
                self.close()
                return None
            if not chunk:
                self.close()
                return None
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        if not line.strip():
            return None
        try:
            return json.loads(line.decode("utf-8"))
        except json.JSONDecodeError:
            return None
