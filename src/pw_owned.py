"""
pw_owned.py

One reusable helper for the pattern PatchSpace process nodes need
over and over: "run a real PipeWire node for as long as some object
in our graph should exist, by keeping the pw-cli session that created
it alive, and tear it down again on request."

Why this exists as its own class instead of being copy-pasted per
node type (which is how the original code had it, once for the
daemon's virtual sink and again, almost identically, for the volume
node): a node type that needs to materialize more than one real
PipeWire object (e.g. a future EQ node with a separate input and
output adapter) just creates more than one OwnedPwNode instead of
duplicating this lifecycle logic again. See patchSpace.BackedNode,
whose `backings` list is exactly zero-or-more of these.

IMPORTANT background (same reasoning as pwgraph.PipewireGraph's
virtual sink, repeated here because it's the reason this class looks
the way it does): a node created via `pw-cli create-node` or a module
loaded via `pw-cli load-module` is owned by the pw-cli client
connection that created it. If that pw-cli process exits, the server
tears the node down with it. So the process is kept RUNNING for the
object's whole lifetime and only terminated in destroy().
"""

from __future__ import annotations

import logging
import os
import re
import select
import subprocess
import time as _time
from typing import Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


class OwnedPwNode:
    """A single real PipeWire node, owned for as long as this object
    is alive. `name` is only used for logging and for resolving
    `node_id` by name once the real graph confirms it exists (see
    resolve()) - it does not need to be globally unique by itself,
    just unique enough for that lookup to find the right node."""

    def __init__(
        self,
        name: str,
        pw_cli_command: Sequence[str] = ("pw-cli",),
        settle: float = 0.3,
    ):
        self.name = name
        self.node_id: Optional[int] = None
        self._pw_cli_command = list(pw_cli_command)
        self._settle = settle
        self._proc: Optional[subprocess.Popen] = None

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def create(self, pw_cli_line: str) -> bool:
        if self._proc is not None:
            return True

        try:
            proc = subprocess.Popen(
                self._pw_cli_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            logger.warning("Failed to start pw-cli for %r: %s", self.name, exc)
            return False

        try:
            assert proc.stdin is not None
            proc.stdin.write(pw_cli_line + "\n")
            proc.stdin.flush()
        except OSError as exc:
            logger.warning("Failed to send command for %r: %s", self.name, exc)
            proc.kill()
            return False

        _time.sleep(self._settle)

        if proc.poll() is not None:
            stderr_data = proc.stderr.read() if proc.stderr else ""
            logger.warning(
                "pw-cli exited while creating %r (exit code %s): %s",
                self.name,
                proc.returncode,
                stderr_data.strip(),
            )
            return False

        self._proc = proc
        logger.info("Successfully requested node %r, pw-cli is running.", self.name)
        return True

    def resolve(self, node_id: int) -> None:
        """Record the real graph id for this object, once a
        node_created callback matches it up by name."""
        self.node_id = node_id

    def set_param(self, iface: str, params_body: str) -> None:
        """Send a live `set-param` to this node, e.g.
        set_param("Props", '{ params = [ "Volume" 0.5 ] }'). No-op
        until both the backing process and the resolved node id are
        available."""
        if self._proc is None or self._proc.stdin is None or self.node_id is None:
            return
        try:
            self._proc.stdin.write(f"set-param {self.node_id} {iface} {params_body}\n")
            self._proc.stdin.flush()
        except OSError as exc:
            logger.warning("Failed to set-param on %r: %s", self.name, exc)

    def destroy(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            if proc.stdin is not None:
                if self.node_id is not None:
                    proc.stdin.write(f"destroy {self.node_id}\n")
                proc.stdin.write("quit\n")
                proc.stdin.flush()
            proc.wait(timeout=2)
        except Exception:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)
        logger.info("Destroyed owned node %r", self.name)
        self.node_id = None

    @staticmethod
    def _read_available(proc: subprocess.Popen, timeout: float) -> str:
        """Non-blocking read of whatever a still-running subprocess
        has written to stdout within `timeout` seconds - same
        technique as pwgraph.PipewireGraph._read_available, needed for
        the same reason (proc.communicate() would block until exit,
        but this process is deliberately kept running)."""
        if proc.stdout is None:
            return ""
        fd = proc.stdout.fileno()
        deadline = _time.monotonic() + timeout
        chunks = []
        while True:
            remaining = deadline - _time.monotonic()
            if remaining <= 0:
                break
            ready, _, _ = select.select([fd], [], [], remaining)
            if not ready:
                break
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            chunks.append(chunk.decode("utf-8", errors="replace"))
        return "".join(chunks)


class OwnedPwProcess(OwnedPwNode):
    """
    Same ownership contract as OwnedPwNode (kept alive for as long as
    the object it backs should exist; killed in destroy()) but for a
    real PipeWire client that's brought up by simply running a
    long-lived command - e.g. `pw-cat` streaming silence into a sink -
    rather than by speaking pw-cli's interactive create-node/destroy
    protocol over stdin.

    Used for patchSpace.VirtualMicNode's keepalive stream: that
    process registers itself as a PipeWire node exactly like a
    pw-cli-created one does, but there's no "destroy <id>" command to
    send it - it just needs to be started and, later, terminated.
    resolve()/is_alive/name all behave identically to the base class;
    only create()/destroy() differ in how the process is launched and
    stopped.
    """

    def create(self, command: Sequence[str]) -> bool:
        if self._proc is not None:
            return True

        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            logger.warning("Failed to start %r: %s", self.name, exc)
            return False

        _time.sleep(self._settle)

        if proc.poll() is not None:
            stderr_data = proc.stderr.read() if proc.stderr else ""
            logger.warning(
                "%r exited immediately (exit code %s): %s",
                self.name,
                proc.returncode,
                stderr_data.strip(),
            )
            return False

        self._proc = proc
        logger.info("Started keepalive process %r.", self.name)
        return True

    def destroy(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        # No in-band "quit" command exists for this process the way
        # pw-cli has one - just ask it to terminate, same escalation
        # (terminate -> wait -> kill) as OwnedPwNode.destroy() uses
        # for its own timeout fallback.
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
        logger.info("Stopped keepalive process %r", self.name)
        self.node_id = None
