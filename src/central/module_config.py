"""
module_config.py — which built-in modules are enabled.

One file, `~/.config/patchspace/modules.json`::

    {"modules": {"pipewire": {"enable": true},
                 "primitives": {"enable": true}}}

It is the single source of truth for the set (agents/PLAN.md phase 6): a
deployment writes it (the nix module points the daemon at a store copy with
`--modules-config`), the CLI edits it and asks the daemon to apply live, and
the daemon reads it at start.  No precedence games — one file, read once at
start and whenever the CLI changes it.

A module that is not listed is enabled if it is a *built-in the daemon
ships* (they are useful by default) and absent otherwise.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

#: The built-in modules the daemon knows how to run, and how.  `inproc` is
#: the provider that still lives inside the daemon (the PipeWire one, until
#: phase 3's extraction); `process` is a program the supervisor runs.
BUILTINS: Dict[str, Dict[str, Any]] = {
    "pipewire": {"kind": "inproc", "argv": []},
    "primitives": {"kind": "process", "argv": ["-m", "modules.primitives.main"]},
}


def default_path() -> str:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "patchspace", "modules.json")


class ModuleConfig:
    """The enabled set, and where it came from."""

    def __init__(self, path: Optional[str] = None) -> None:
        self.path = path or os.environ.get("PATCHSPACE_MODULES_CONFIG") or default_path()
        #: True when the file is written by a deployment (a store path):
        #: the CLI then reports "managed" instead of editing it.
        self.managed = self.path.startswith("/nix/store/")
        self._states: Dict[str, bool] = {}
        self.load()

    # -- reading ---------------------------------------------------------

    def load(self) -> None:
        states: Dict[str, bool] = {}
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except FileNotFoundError:
            payload = {}
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("ignoring %s: %s", self.path, exc)
            payload = {}
        for name, entry in (payload.get("modules") or {}).items():
            if isinstance(entry, dict):
                states[str(name)] = bool(entry.get("enable", True))
            else:
                states[str(name)] = bool(entry)
        self._states = states

    def enabled(self, name: str) -> bool:
        if name in self._states:
            return self._states[name]
        # Not mentioned: a built-in the daemon ships is on by default.
        return name in BUILTINS

    def states(self) -> Dict[str, bool]:
        return dict(self._states)

    def names(self) -> List[str]:
        known = dict.fromkeys(list(BUILTINS) + list(self._states))
        return sorted(known)

    # -- writing ---------------------------------------------------------

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Persist one module's state.  Refuses on a deployment-managed
        file (the CLI reports that instead of fighting Nix)."""
        if self.managed:
            return False
        self._states[name] = bool(enabled)
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(self.path, "w", encoding="utf-8") as handle:
                json.dump(
                    {"modules": {n: {"enable": e} for n, e in sorted(self._states.items())}},
                    handle, indent=2,
                )
                handle.write("\n")
            return True
        except OSError as exc:
            logger.error("could not write %s: %s", self.path, exc)
            return False
