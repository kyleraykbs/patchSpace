"""
module_supervisor.py — running built-in modules as child processes.

A module is a program that speaks the module API (see `modules/host.py`):
it connects to the daemon's socket, greets as a module, declares what it
provides and announces what it runs.  The supervisor starts the enabled
built-ins, keeps them alive (restart with backoff, and give up on one that
keeps dying immediately), and stops them with the daemon.

What it deliberately does **not** do is decide what a module provides: a
module declares its own types and the registry serves what it says.  The
supervisor only owns the process.

External modules (another program, another repo) attach over the socket
themselves and are never spawned by the daemon — they are not in its specs.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

#: Backoff between restarts, seconds, and how many *fast* failures in a row
#: before the supervisor gives up on a module (a module that cannot start
#: must not spin).
RETRY_MIN_S = 0.5
RETRY_MAX_S = 30.0
GIVE_UP_AFTER = 5
FAST_EXIT_S = 5.0


@dataclass
class ModuleSpec:
    """One module the daemon may run."""

    module_id: str
    argv: Sequence[str]
    enabled: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_id": self.module_id,
            "argv": list(self.argv),
            "enabled": self.enabled,
        }


@dataclass
class ModuleProcess:
    spec: ModuleSpec
    proc: Optional[subprocess.Popen] = None
    restarts: int = 0
    fast_failures: int = 0
    last_exit: Optional[int] = None
    next_retry_at: float = 0.0
    gave_up: bool = False
    started_at: float = 0.0

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_id": self.spec.module_id,
            "pid": self.proc.pid if self.alive and self.proc else None,
            "alive": self.alive,
            "restarts": self.restarts,
            "last_exit": self.last_exit,
            "gave_up": self.gave_up,
        }


class ModuleSupervisor:
    """Runs the enabled built-in modules and keeps them running."""

    def __init__(self, specs: Sequence[ModuleSpec], socket_path: str,
                 log_dir: str = "") -> None:
        self.socket_path = socket_path
        self.log_dir = log_dir
        self.modules: List[ModuleProcess] = [
            ModuleProcess(spec=spec) for spec in specs if spec.enabled
        ]
        self._stopping = False

    # -- lifecycle -------------------------------------------------------

    def start_all(self) -> None:
        self._stopping = False
        for module in self.modules:
            self._spawn(module)

    def stop_all(self, timeout: float = 5.0) -> None:
        """Ask every module to stop, then make sure it did."""
        self._stopping = True
        for module in self.modules:
            proc = module.proc
            if proc is None or proc.poll() is not None:
                continue
            try:
                proc.terminate()
            except OSError:
                continue
        deadline = time.monotonic() + timeout
        for module in self.modules:
            proc = module.proc
            if proc is None:
                continue
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                logger.warning("module %s ignored terminate; killing it",
                               module.spec.module_id)
                try:
                    proc.kill()
                    proc.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    pass

    def poll(self) -> None:
        """One supervision tick: restart anything that died."""
        if self._stopping:
            return
        now = time.monotonic()
        for module in self.modules:
            proc = module.proc
            if proc is not None and proc.poll() is None:
                continue
            if proc is not None:
                module.last_exit = proc.returncode
                if now - module.started_at < FAST_EXIT_S:
                    module.fast_failures += 1
                else:
                    module.fast_failures = 0
            # A spawn that never produced a process (`proc is None`) already
            # counted its failure in _spawn.
            if module.fast_failures >= GIVE_UP_AFTER:
                if not module.gave_up:
                    module.gave_up = True
                    logger.error(
                        "module %s failed %s times; not restarting it",
                        module.spec.module_id, module.fast_failures,
                    )
                continue
            if now < module.next_retry_at:
                continue
            module.restarts += 1
            module.next_retry_at = now + min(
                RETRY_MIN_S * (2 ** (module.restarts - 1)), RETRY_MAX_S
            )
            logger.warning("module %s exited (%s); restarting it (try %d)",
                           module.spec.module_id, module.last_exit,
                           module.restarts)
            self._spawn(module)

    def status(self) -> List[Dict[str, Any]]:
        return [module.to_dict() for module in self.modules]

    # -- internals -------------------------------------------------------

    def _spawn(self, module: ModuleProcess) -> None:
        env = dict(os.environ)
        env["PATCHSPACE_SOCKET"] = self.socket_path
        stdout = subprocess.DEVNULL
        stderr = subprocess.STDOUT
        handle = None
        if self.log_dir:
            try:
                os.makedirs(self.log_dir, exist_ok=True)
                path = os.path.join(self.log_dir, f"{module.spec.module_id}.log")
                handle = open(path, "ab", buffering=0)
                stdout = handle
            except OSError:
                handle = None
                stdout = subprocess.DEVNULL
        try:
            module.proc = subprocess.Popen(
                list(module.spec.argv), env=env, stdout=stdout, stderr=stderr,
                start_new_session=True,
            )
            module.started_at = time.monotonic()
            logger.info("started module %s (pid %s)",
                        module.spec.module_id, module.proc.pid)
        except OSError as exc:
            logger.error("could not start module %s: %s",
                         module.spec.module_id, exc)
            module.last_exit = -1
            module.fast_failures += 1
            module.next_retry_at = time.monotonic() + RETRY_MIN_S
        finally:
            if handle is not None:
                handle.close()
