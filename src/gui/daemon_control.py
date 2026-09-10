"""
daemon_control.py

Lets the GUI own the patchbay daemon's lifecycle.

The GUI can run against a daemon someone else started (a terminal, a
system service, another GUI) or start its own background daemon.  The
rule is: if one is already running, adopt it and never touch its
lifetime; only a daemon *this* GUI spawned is shut down when the window
closes.  The hamburger menu also exposes explicit Start / Stop / Restart
actions.

Everything here talks to the daemon over the same Unix socket the client
uses, so "is it running?" is simply "can I connect?" - no pid files or
process scanning to go stale.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import time
from typing import List, Optional

from constants import SOCKET_PATH

logger = logging.getLogger(__name__)

# How long to wait for a freshly spawned daemon to publish its socket, and
# for a shutdown command to actually take the daemon down.
START_TIMEOUT_S = 15.0
STOP_TIMEOUT_S = 6.0


def is_daemon_running(timeout: float = 0.25) -> bool:
    """Whether a patchbay daemon is accepting connections right now."""
    if not os.path.exists(SOCKET_PATH):
        return False
    try:
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(timeout)
        probe.connect(SOCKET_PATH)
        probe.close()
        return True
    except OSError:
        return False


def daemon_command() -> List[str]:
    """The argv that launches the daemon.

    Order of preference: an explicit path baked in by the packaged GUI
    wrapper (``PATCHBAY_DAEMON``), a ``patchbay-daemon`` on ``PATH``, and
    finally running the sibling ``main.py`` with this interpreter - which
    is what happens when the GUI is run straight from a checkout (e.g.
    inside ``nix develop``)."""
    explicit = os.environ.get("PATCHBAY_DAEMON")
    if explicit:
        return shlex.split(explicit)
    found = shutil.which("patchbay-daemon")
    if found:
        return [found]
    here = os.path.dirname(os.path.abspath(__file__))
    main_py = os.path.normpath(os.path.join(here, os.pardir, "main.py"))
    return [sys.executable, main_py]


class DaemonManager:
    """Start/stop/restart the daemon and remember whether we own it."""

    def __init__(self) -> None:
        # The process we spawned, if any, and whether *we* are responsible
        # for its lifetime (only true when we actually started it).
        self.proc: Optional[subprocess.Popen] = None
        self.owned = False

    # -- queries ---------------------------------------------------------

    def is_running(self) -> bool:
        return is_daemon_running()

    # -- lifecycle -------------------------------------------------------

    def ensure_started(self) -> bool:
        """Adopt an existing daemon, or start one if none is running.

        Returns True if a daemon is available afterwards.  A daemon we
        start here is marked owned (killed on window close); an adopted
        one is left strictly alone."""
        if self.is_running():
            logger.info("Adopted an already-running PatchBay daemon")
            return True
        return self.start()

    def start(self) -> bool:
        if self.is_running():
            return True
        cmd = daemon_command()
        logger.info("Starting PatchBay daemon: %s", " ".join(cmd))
        try:
            self.proc = subprocess.Popen(cmd)
        except OSError as exc:
            logger.error("Could not start the daemon: %s", exc)
            return False
        self.owned = True
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if self.is_running():
                logger.info("PatchBay daemon is up (pid %s)", self.proc.pid)
                return True
            if self.proc.poll() is not None:
                logger.error(
                    "PatchBay daemon exited during startup (code %s)",
                    self.proc.returncode,
                )
                self.proc = None
                self.owned = False
                return False
            time.sleep(0.1)
        logger.error("PatchBay daemon did not come up within %.0fs", START_TIMEOUT_S)
        return False

    def stop(self) -> bool:
        """Stop the running daemon (whoever started it) via its socket,
        falling back to killing the process we own if the socket is
        already gone."""
        if self.is_running():
            self._request_shutdown()
            self._wait_stopped()
        proc, self.proc = self.proc, None
        if self.owned and proc is not None and proc.poll() is None:
            self._terminate(proc)
        self.owned = False
        return True

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def shutdown_owned(self) -> None:
        """Called on window close: shut the daemon down only if this GUI
        started it.  An adopted daemon is deliberately left running."""
        if self.owned:
            self.stop()

    # -- helpers ---------------------------------------------------------

    def _request_shutdown(self) -> None:
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            sock.connect(SOCKET_PATH)
            sock.sendall(b'{"command": "shutdown"}\n')
            # Wait for the reply so we know the daemon accepted it before
            # the socket goes away.
            sock.recv(4096)
            sock.close()
        except OSError as exc:
            logger.debug("Shutdown request failed (daemon already gone?): %s", exc)

    def _wait_stopped(self) -> bool:
        deadline = time.monotonic() + STOP_TIMEOUT_S
        while time.monotonic() < deadline:
            if not self.is_running():
                return True
            time.sleep(0.05)
        logger.warning("Daemon still answering %.0fs after shutdown request", STOP_TIMEOUT_S)
        return False

    @staticmethod
    def _terminate(proc: subprocess.Popen) -> None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
