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
"""

from __future__ import annotations

import json
import queue
import socket
import threading
from typing import Optional

from constants import SOCKET_PATH


class PatchBayClient:
    def __init__(self, path: str = SOCKET_PATH):
        self.path = path
        self.cmd_queue: "queue.Queue" = queue.Queue()
        self.resp_queue: "queue.Queue" = queue.Queue()
        self.running = True
        self._sock: Optional[socket.socket] = None
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    # ---------- connection management ----------

    def _ensure_connected(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(self.path)
        self._sock = sock
        return sock

    def _drop_connection(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # ---------- worker loop ----------

    def _loop(self) -> None:
        # Any bytes read past the newline that terminates one response
        # belong to the *next* response - the daemon replies once per
        # command, in order, on this same connection - so this buffer
        # has to persist across queue.get() iterations, not just
        # within a single command's read.
        buffer = b""
        while self.running:
            cmd = self.cmd_queue.get()
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
            except Exception as e:
                # Any failure (broken pipe, daemon restart, bad JSON,
                # ...) drops the connection so the next send() dials a
                # fresh one, and reports the error back to the caller
                # instead of silently swallowing it.
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
