"""
socket_client.py

Unix-socket client for talking to the PatchBay daemon (main.py).

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


class PatchBayClient:
    def __init__(self, path: str = SOCKET_PATH):
        self.path = path
        self.cmd_queue: "queue.Queue" = queue.Queue()
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
