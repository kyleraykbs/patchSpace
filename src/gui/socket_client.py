"""
socket_client.py

Unix-socket client for talking to the Patch Space daemon (main.py).

Keeps a single persistent connection open for the lifetime of the
client, instead of dialing a fresh AF_UNIX connection for every
command. The original implementation opened+closed a socket per
message, which meant something like dragging a volume slider (many
set_volume commands in a couple of seconds) was opening and tearing
down a socket dozens of times a second for no reason. Commands are
still processed one at a time, in order, off a background thread, so
callers on the GTK main loop never block.

The connection is *actively* maintained: the worker wakes on a short
interval even when no commands are queued, notices a daemon that went
away, and keeps redialing until the socket comes back.  Callers can
observe the state via ``is_connected()`` or register a callback with
``on_connection_changed`` (invoked from the worker thread, so a GTK
caller must marshal it with ``GLib.idle_add``).
"""

from __future__ import annotations

import json
import logging
import collections
import queue
import select
import socket
import threading
from typing import Callable, List, Optional

from constants import SOCKET_PATH

logger = logging.getLogger(__name__)

# How often the idle worker re-checks the connection (and tries to dial
# back in when the daemon isn't there).  Short enough that the GUI reads
# as "reconnected" almost immediately after a daemon restart, long enough
# that a missing daemon isn't a busy loop.
RECONNECT_INTERVAL_S = 0.5


class CommandQueue:
    """The client's outgoing commands, newest-state-wins.

    A plain queue let commands pile up behind a slow daemon - the poll alone
    enqueues one every 400ms, a slider drag one per motion - so every later
    click waited behind the backlog ("the longer the program is open, the
    longer an interaction takes to send"): nothing ever went *wrong*, the queue
    simply grew without limit and the daemon could not outrun it.

    A *state* command is replaced in place here, which keeps its position in the
    order - a layout save that arrived before a click still goes out before it.
    Actions are never coalesced: an impulse, a record, a panel-file delete all
    matter, and the log read is *incremental*, so coalescing it would drop
    lines.
    """

    #: What makes two commands "the same question": the fields whose values have
    #: to match for the older one to be pointless.  () means the command name
    #: alone is enough (a poll asks for the whole state; a catalogue is a
    #: catalogue).  Anything not listed is never coalesced.
    COALESCING = {
        # Polls and catalogues: only the newest answer can be wanted.
        "get_nodes": (),
        "get_apps": (),
        "get_applications": (),
        "get_titles": (),
        "get_hardware_devices": (),
        # Per-node reads.
        "get_peaks": ("node_id",),
        "get_device_profiles": ("node_id",),
        # Per-node state writes: a drag sends one per motion.
        "set_node_layout": ("node_id",),
        "set_panel_layout": ("panel_id",),
        "set_node_property": ("node_id", "property"),
        "set_volume": ("node_id",),
        "set_volume_range": ("node_id",),
        "set_gate": ("node_id",),
    }

    def __init__(self) -> None:
        self._keys: "collections.deque" = collections.deque()
        self._commands: dict = {}
        self._cond = threading.Condition()

    def _key(self, cmd):
        """The coalescing key for ``cmd``, or None when it never coalesces."""
        if not isinstance(cmd, dict):
            return None
        fields = self.COALESCING.get(cmd.get("command"))
        if fields is None:
            return None
        return (cmd["command"],) + tuple(str(cmd.get(f)) for f in fields)

    def put(self, cmd) -> None:
        key = self._key(cmd)
        with self._cond:
            if key is not None and key in self._commands:
                # Superseded, but it keeps its turn: the replacement goes where
                # the command it replaces already was.
                self._commands[key] = cmd
            else:
                if key is not None:
                    self._commands[key] = cmd
                else:
                    # Actions need a key of their own to sit in the order.
                    key = object()
                    self._commands[key] = cmd
                self._keys.append(key)
            self._cond.notify()

    def get(self, timeout=None):
        with self._cond:
            if not self._keys:
                if not self._cond.wait(timeout):
                    raise queue.Empty
            if not self._keys:
                raise queue.Empty
            key = self._keys.popleft()
            return self._commands.pop(key)


class PatchSpaceClient:
    def __init__(self, path: str = SOCKET_PATH):
        self.path = path
        self.cmd_queue = CommandQueue()
        self.resp_queue: "queue.Queue" = queue.Queue()
        self.running = True
        self.connected = False
        # Callables invoked as ``cb(connected: bool)`` whenever the
        # connection state flips.  Fired from the worker thread.
        self.on_connection_changed: List[Callable[[bool], None]] = []
        self._sock: Optional[socket.socket] = None
        self._state_lock = threading.Lock()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---------- connection management ----------

    def is_connected(self) -> bool:
        return self.connected

    def _set_connected(self, connected: bool) -> None:
        with self._state_lock:
            if connected == self.connected:
                return
            self.connected = connected
        logger.info(
            "Daemon connection %s", "established" if connected else "lost"
        )
        for cb in list(self.on_connection_changed):
            try:
                cb(connected)
            except Exception:
                logger.exception("connection-changed callback failed")

    def _ensure_connected(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.connect(self.path)
        except OSError:
            sock.close()
            raise
        self._sock = sock
        self._set_connected(True)
        return sock

    def _drop_connection(self) -> None:
        with self._state_lock:
            sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._set_connected(False)

    def _check_alive(self) -> None:
        """Detect a daemon that closed the connection while we were idle
        (no command in flight).  A readable socket with a zero-byte peek
        is EOF."""
        sock = self._sock
        if sock is None:
            return
        try:
            readable, _, _ = select.select([sock], [], [], 0)
            if readable and sock.recv(1, socket.MSG_PEEK) == b"":
                self._drop_connection()
        except OSError:
            self._drop_connection()

    def _reconnect_tick(self) -> None:
        """One idle wake-up: keep a dead connection detected and keep
        trying to dial back in."""
        if self._sock is None:
            try:
                self._ensure_connected()
            except (FileNotFoundError, ConnectionError, OSError):
                self._drop_connection()
            return
        self._check_alive()

    # ---------- worker loop ----------

    def _loop(self) -> None:
        # Any bytes read past the newline that terminates one response
        # belong to the *next* response - the daemon replies once per
        # command, in order, on this same connection - so this buffer
        # has to persist across queue.get() iterations, not just
        # within a single command's read.
        buffer = b""
        while self.running:
            try:
                cmd = self.cmd_queue.get(timeout=RECONNECT_INTERVAL_S)
            except queue.Empty:
                # No command waiting: this is the active-reconnect tick.
                self._reconnect_tick()
                continue
            if cmd is None:
                break
            try:
                sock = self._ensure_connected()
                sock.sendall((json.dumps(cmd) + "\n").encode())
                while b"\n" not in buffer:
                    chunk = sock.recv(4096)
                    if not chunk:
                        raise ConnectionError("daemon closed the connection")
                    buffer += chunk
                line, buffer = buffer.split(b"\n", 1)
                self.resp_queue.put(json.loads(line.decode()))
            except (FileNotFoundError, ConnectionError, OSError):
                # The daemon isn't there (no socket file, refused, reset,
                # or it closed on us).  Drop the connection so the idle
                # reconnect tick (and the next send) redials; the badge
                # tracks is_connected().
                self._drop_connection()
                buffer = b""
            except Exception as e:
                # A genuine failure (bad JSON, a bug in the command, ...)
                # still gets reported instead of silently swallowed.
                self._drop_connection()
                buffer = b""
                self.resp_queue.put({"status": "error", "message": str(e)})

    # ---------- public API ----------

    def send(self, cmd: dict) -> None:
        self.cmd_queue.put(cmd)

    def get_responses(self) -> list:
        responses = []
        while True:
            try:
                responses.append(self.resp_queue.get_nowait())
            except queue.Empty:
                break
        return responses

    def stop(self) -> None:
        self.running = False
        self.cmd_queue.put(None)
        self.thread.join(timeout=1)
        self._drop_connection()
