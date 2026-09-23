"""Tests for the GUI's daemon socket client: active connection
maintenance and reconnection.

``socket_client`` uses bare ``from constants import ...`` (it is a GUI
module run with the ``gui`` directory on the path), so put that
directory on ``sys.path`` before importing it."""

import os
import socket
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gui"))

from socket_client import PatchSpaceClient  # noqa: E402


class _FakeDaemon:
    """A tiny AF_UNIX server: accepts one connection, replies ``ok`` to
    every newline-delimited command, and can be stopped/restarted."""

    def __init__(self, path):
        self.path = path
        self._srv = None
        self._conn = None
        self._thread = None
        self._stop = threading.Event()

    def start(self):
        self._stop.clear()
        if os.path.exists(self.path):
            os.unlink(self.path)
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(1)
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        try:
            conn, _ = self._srv.accept()
        except OSError:
            return
        self._conn = conn
        with conn:
            buf = b""
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    if line.strip():
                        conn.sendall(b'{"status": "ok"}\n')

    def stop(self):
        self._stop.set()
        if self._conn is not None:
            # shutdown() sends FIN immediately, which is what lets the
            # peer's idle check observe EOF; close() alone can be
            # deferred while another thread is blocked in recv().
            try:
                self._conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._conn.close()
            except OSError:
                pass
            self._conn = None
        if self._srv is not None:
            try:
                self._srv.close()
            except OSError:
                pass
            self._srv = None
        if os.path.exists(self.path):
            try:
                os.unlink(self.path)
            except OSError:
                pass


def _wait(pred, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.02)
    return False


def test_client_actively_connects_without_a_command(tmp_path):
    """The client should dial the daemon on its own, not only when a
    command is queued."""
    path = str(tmp_path / "sock")
    daemon = _FakeDaemon(path)
    daemon.start()
    client = PatchSpaceClient(path)
    try:
        assert _wait(lambda: client.is_connected()), "client never connected"
    finally:
        client.stop()
        daemon.stop()


def test_client_notifies_and_reconnects_after_daemon_restart(tmp_path):
    path = str(tmp_path / "sock")
    daemon = _FakeDaemon(path)
    daemon.start()
    client = PatchSpaceClient(path)
    states = []
    client.on_connection_changed.append(states.append)
    try:
        assert _wait(lambda: client.is_connected())
        # Daemon goes away: the idle worker must notice and drop.
        daemon.stop()
        assert _wait(lambda: not client.is_connected()), "client never noticed the drop"
        assert states[-1] is False

        # Comes back: the idle worker must redial with no command sent.
        daemon2 = _FakeDaemon(path)
        daemon2.start()
        try:
            assert _wait(lambda: client.is_connected()), "client never reconnected"
            assert states[-1] is True
        finally:
            daemon2.stop()
    finally:
        client.stop()
        daemon.stop()


def test_command_round_trip_after_connect(tmp_path):
    path = str(tmp_path / "sock")
    daemon = _FakeDaemon(path)
    daemon.start()
    client = PatchSpaceClient(path)
    try:
        assert _wait(lambda: client.is_connected())
        client.send({"command": "ping"})
        assert _wait(lambda: client.get_responses()), "no response"
    finally:
        client.stop()
        daemon.stop()
