"""
pwproc.py

The low-level building blocks every "node" in the patchbay is made of.

This module deliberately contains *no* knowledge of the patch graph, of
matching, or of routing.  It is the ownership/supervision substrate:

  * ``OwnedPwNode``      - a real PipeWire node owned by a pw-cli client
                           session we keep running (create/destroy/set-param).
  * ``OwnedPwProcess``   - the same ownership contract for a long-lived
                           helper command (pw-cat, pw-loopback).
  * ``Backoff``          - per-key bounded exponential backoff used by the
                           supervisor so a node that keeps failing to come
                           up is retried periodically without being thrashed
                           every tick.

Why every helper has to be "owned"
----------------------------------
A node created via ``pw-cli create-node`` or a module loaded via
``pw-cli load-module`` is exported over the *client connection* that
created it.  When that pw-cli process exits, the server tears the object
down with it.  The same is true, less obviously, of ``pw-cat`` /
``pw-loopback``: they are plain clients, and their streams live only as
long as the client does.

That makes every real PipeWire object this project materialises the
child of a process.  Ownership is therefore the whole game:

  * create()   starts the owner and (for pw-cli) issues the command;
  * is_alive   tells the supervisor whether the owner is still up;
  * destroy()  asks the owner to tear the object down, escalating from
               an in-band request to SIGTERM to SIGKILL.

If the owner dies on its own (crash, OOM-kill, plugin segfault) the
objects die with it - the supervisor's job is to notice ``is_alive`` go
False and respawn.  ``owns_process`` stays True after a death so the
supervisor can tell "this backing used to own a process and now it is
gone" apart from a name-only placeholder that never owned one.

Phantom-creation guard
----------------------
A pw-cli session that is *still running* is not proof that the requested
object was created.  A ``load-module`` that fails (e.g. a filter-chain
whose LADSPA plugin path does not exist) makes pw-cli print
``Error: Could not load module`` to stderr and then carry on waiting for
the next command - process alive, no module, and a node that would never
resolve.  create() reads stderr for that marker and fails fast instead of
registering a phantom backing that the graph can never confirm.
"""

from __future__ import annotations

import logging
import os
import random
import select
import subprocess
import threading
import time as _time
from typing import Callable, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Owned processes
# ---------------------------------------------------------------------------


class OwnedPwNode:
    """A single real PipeWire node (or module), owned for as long as
    this object is alive.

    ``name`` is the node.name the real object is expected to carry; it is
    what the daemon matches node_created events against (``resolve()``)
    and what the graph-presence checks look up.  It does not have to be
    globally unique by itself, just unique enough for that lookup.
    """

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
        self._owns_process = False
        # Background readers that keep the child's stdout/stderr pipes
        # empty - see _start_drain.  Without them a chatty pw-cli/pw-cat
        # blocks on write() once its pipe buffer (64 KiB) fills, freezing
        # the module it owns ("the audio halts after a while").
        self._drain_threads: list = []
        # Set the moment the owning process is confirmed up (end of a
        # successful create()) - used by stuck() below, NOT by is_alive/
        # owns_process, which only ever look at the process itself.
        self._created_at: Optional[float] = None
        # Set when the graph id is resolved, so handle_node_removed can
        # tell a genuinely-destroyed object from a lagging removal event
        # for a recycled id (see PatchSpace.handle_node_removed).
        self._resolved_at: Optional[float] = None

    # -- health -------------------------------------------------------------

    @property
    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    @property
    def owns_process(self) -> bool:
        """True once this backing has ever started a real process.  Stays
        True after the process exits - it is a fact about what this
        backing *is*, pair it with ``is_alive`` to detect a death."""
        return self._owns_process

    def stuck(self, grace_s: float) -> bool:
        """True once this backing has had long enough to show up as a
        real object in the live graph and still hasn't - as distinct
        from ``dead`` (process exited).

        The owning process/module can stay perfectly alive forever
        while never actually producing the object it promised: a
        ``load-module`` call returns success the moment PipeWire
        accepts the module, which is *not* the same moment (and for
        some module types, not even the same success/failure outcome)
        as the module's own internal plugin actually instantiating and
        registering a node. create()'s phantom-creation guard only
        recognizes one specific failure string
        (``"Could not load module"``), which is what a filter-chain
        module prints when a LADSPA plugin *path* doesn't exist - it
        does not cover every module's failure mode (an LV2 plugin that
        fails to instantiate, or an SPA library like the WebRTC AEC
        plugin that isn't installed, can both leave pw-cli sitting
        there alive with nothing ever created). Without this check,
        such a backing looks identically healthy to a working one to
        structural_ok()/module_ok() forever, and nothing ever retries
        it."""
        return (
            self.owns_process
            and self.is_alive
            and self.node_id is None
            and self._created_at is not None
            and (_time.monotonic() - self._created_at) > grace_s
        )

    def resolve(self, node_id: int) -> None:
        """Record the real graph id once the graph confirms the object."""
        self.node_id = node_id
        self._resolved_at = _time.monotonic()

    # -- lifecycle ----------------------------------------------------------

    def create(self, pw_cli_line: str) -> bool:
        """Start the owning pw-cli session and issue one command line.

        Returns False (never raises) when the session cannot be started
        or the object was not actually created - see the phantom-guard
        note at the top of the module."""
        if self._proc is not None:
            return True  # already running

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
            stderr = proc.stderr.read() if proc.stderr else ""
            logger.warning(
                "pw-cli exited while creating %r (exit code %s): %s",
                self.name,
                proc.returncode,
                stderr.strip(),
            )
            return False

        if "Could not load module" in self._read_stderr(proc, 0.2):
            logger.warning(
                "Module load failed for %r (plugin missing? command rejected)",
                self.name,
            )
            proc.kill()
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
            return False

        self._start_drain(proc)
        self._proc = proc
        self._owns_process = True
        self._created_at = _time.monotonic()
        logger.info("Requested node %r, owner pw-cli running.", self.name)
        return True

    def set_param(self, iface: str, params_body: str) -> None:
        """Send a live ``set-param`` to this object, e.g.
        ``set_param("Props", '{ params = [ "Volume" 0.5 ] }')``.  No-op
        until the object id is known."""
        if self.node_id is None or self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(f"set-param {self.node_id} {iface} {params_body}\n")
            self._proc.stdin.flush()
        except OSError as exc:
            logger.warning("Failed to set-param on %r: %s", self.name, exc)

    def set_param_for(self, node_id: int, iface: str, params_body: str) -> None:
        """set-param addressed to an arbitrary object id owned by this
        connection (a module's sibling streams all live on the same
        pw-cli session)."""
        if self._proc is None or self._proc.stdin is None:
            return
        try:
            self._proc.stdin.write(f"set-param {node_id} {iface} {params_body}\n")
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

    # -- small helpers ------------------------------------------------------

    def _start_drain(self, proc: subprocess.Popen) -> None:
        """Keep the child's stdout/stderr pipes empty for its whole life.

        A pw-cli session that loads a module (filter-chain, echo-cancel)
        or a pw-cat/pw-loopback helper keeps logging to stdout/stderr; the
        daemon only reads those pipes once, at create time.  Once the pipe
        buffer fills the child blocks in write(), which freezes the module
        it owns and stops it answering the ``set-param``/``destroy`` lines
        we later write to its stdin.  Nothing needs the output past the
        create-time guards, so a pair of daemon threads just discards it
        until EOF (process exit / destroy)."""
        for pipe in (proc.stdout, proc.stderr):
            if pipe is None:
                continue
            thread = threading.Thread(
                target=self._drain_until_eof, args=(pipe,), daemon=True
            )
            thread.start()
            self._drain_threads.append(thread)

    @staticmethod
    def _drain_until_eof(pipe) -> None:
        try:
            while pipe.read(4096):
                pass
        except Exception:
            pass

    @staticmethod
    def _read_stderr(proc: subprocess.Popen, timeout: float) -> str:
        return OwnedPwNode._drain(proc.stderr, timeout)

    @staticmethod
    def _read_stdout(proc: subprocess.Popen, timeout: float) -> str:
        return OwnedPwNode._drain(proc.stdout, timeout)

    @staticmethod
    def _drain(pipe, timeout: float) -> str:
        """Non-blocking read of whatever a still-running subprocess has
        written to a pipe within ``timeout`` seconds."""
        if pipe is None:
            return ""
        fd = pipe.fileno()
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
    """Same ownership contract as OwnedPwNode but for a long-lived
    command launched as-is - ``pw-cat`` streaming silence into a sink,
    ``pw-loopback`` republishing a monitor.  There is no destroy <id>
    protocol; the process is simply terminated."""

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
            stderr = proc.stderr.read() if proc.stderr else ""
            logger.warning(
                "%r exited immediately (exit code %s): %s",
                self.name,
                proc.returncode,
                stderr.strip(),
            )
            return False

        self._start_drain(proc)
        self._proc = proc
        self._owns_process = True
        self._created_at = _time.monotonic()
        logger.info("Started helper process %r.", self.name)
        return True

    def destroy(self) -> None:
        proc, self._proc = self._proc, None
        if proc is None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=1)
        logger.info("Stopped helper process %r", self.name)
        self.node_id = None


# ---------------------------------------------------------------------------
# Retry backoff
# ---------------------------------------------------------------------------

# A "unit" can be any hashable key; nodes key their backoff on their id.
Key = str


class Backoff:
    """Bounded exponential backoff keyed by unit id.

    A key with no history is always ``ready()`` - so a node that just
    broke is retried on the very next supervision tick, exactly as it
    would be with no gate at all.  Only a key that keeps failing backs
    off (doubling from ``initial_s``, capped at ``max_s``, plus up to
    ``jitter_fraction`` of random spread so several failing nodes do not
    all retry in lockstep).  ``success()`` / ``forget()`` clear all
    state so a genuinely-fixed unit is retried immediately.
    """

    def __init__(
        self,
        initial_s: float = 1.0,
        max_s: float = 30.0,
        multiplier: float = 2.0,
        jitter_fraction: float = 0.2,
        clock=None,
    ):
        self._initial = initial_s
        self._max = max_s
        self._multiplier = multiplier
        self._jitter = jitter_fraction
        self._clock = clock or _time.monotonic
        # key -> (next_allowed_monotonic, last_backoff_used)
        self._state: dict[str, Tuple[float, float]] = {}

    def ready(self, key: Key) -> bool:
        entry = self._state.get(key)
        if entry is None:
            return True
        next_allowed, _ = entry
        return self._clock() >= next_allowed

    def next_attempt_in(self, key: Key) -> float:
        entry = self._state.get(key)
        if entry is None:
            return 0.0
        return max(0.0, entry[0] - self._clock())

    def record_failure(self, key: Key) -> None:
        _, prev = self._state.get(key, (0.0, self._initial))
        wait = prev if key in self._state else self._initial
        next_backoff = min(max(wait, 0.0) * self._multiplier, self._max)
        jitter = random.uniform(0.0, self._jitter) * wait
        delay = wait + jitter
        self._state[key] = (self._clock() + delay, next_backoff)

    def record_success(self, key: Key) -> None:
        self._state.pop(key, None)

    def forget(self, key: Key) -> None:
        self._state.pop(key, None)


# Re-exported under the old name so callers that only need the gate can
# keep reading code that names the concept directly.
RetryGate = Backoff


class Ticker:
    """A tiny periodic wake-up used by the daemon's supervision loop.

    A single thread with a fixed period is much easier to reason about
    (and stop) than a chain of self-rescheduling threading.Timers, and it
    is the only thread the supervisor needs.  ``wake()`` can be called
    from any thread to make the very next wait return early, which is
    what lets a mutating command nudge an urgent reconcile without
    waiting out a full period.
    """

    def __init__(self, period_s: float, fn: Callable[[], None]):
        self._period = period_s
        self._fn = fn
        self._stopped = threading.Event()
        self._event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def wake(self) -> None:
        """Make the current wait return immediately (called from other
        threads when urgent work is pending)."""
        self._event.set()

    def _loop(self) -> None:
        while not self._stopped.is_set():
            if self._event.wait(self._period):
                self._event.clear()
            try:
                self._fn()
            except Exception:
                logger.exception("supervision loop crashed")

    def stop(self) -> None:
        self._stopped.set()
        self._event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
