"""
pw_supervise.py

One small, dependency-free helper: a per-key bounded-backoff gate for
main.py's periodic backing-repair loop (_repair_dead_backings, run
every safety-sync tick - see main.py).

Why this exists: without it, a backing that keeps failing to (re)create
- most commonly a NoiseCancelNode/SensitivityGateNode whose configured
LADSPA plugin simply isn't installed - gets a fresh repair attempt
(a real pw-cli spawn) on every single tick, forever. That is wasted
work and log noise with no upside: the plugin path isn't going to
start existing between one 2-second tick and the next. RetryGate
turns "retry every tick, unconditionally" into "retry immediately the
first time, then back off while it keeps failing, capped so it is
still retried periodically" - the same eventual-recovery guarantee
(a fixed config is picked up on the very next attempt after the fix,
since record_success()/forget() clear all backoff state immediately),
just without the thrash while it's still broken.

This intentionally knows nothing about PatchSpace, BackedNode, or
pw-cli - it is pure bookkeeping over a dict of keys to next-allowed
times, so any repair loop (not just today's effect-backing one) can
reuse it without adding a dependency on this module elsewhere.
"""

from __future__ import annotations

import time
from typing import Dict, Tuple


class RetryGate:
    """Bounded exponential backoff per key.

    A key with no history is always `ready()` - so a backing that just
    broke is repaired on the very next tick, exactly as before this
    existed. Only a key that keeps failing backs off (initial_backoff_s,
    doubling by default, capped at max_backoff_s) so it is still
    retried periodically rather than never again.
    """

    def __init__(
        self,
        initial_backoff_s: float = 2.0,
        max_backoff_s: float = 30.0,
        multiplier: float = 2.0,
    ):
        self._initial = initial_backoff_s
        self._max = max_backoff_s
        self._multiplier = multiplier
        # key -> (next_allowed_monotonic_time, backoff_s_used_last)
        self._state: Dict[str, Tuple[float, float]] = {}

    def ready(self, key: str) -> bool:
        """True if `key` has no recorded failure, or its backoff has
        elapsed. Does not itself record anything - call record_failure/
        record_success after the attempt this permits."""
        entry = self._state.get(key)
        if entry is None:
            return True
        next_allowed, _ = entry
        return time.monotonic() >= next_allowed

    def record_failure(self, key: str) -> None:
        """Attempt for `key` failed again - push its next allowed
        attempt out by the current backoff, then grow the backoff
        (capped) for next time."""
        _, prev_backoff = self._state.get(key, (0.0, self._initial))
        wait = prev_backoff if key in self._state else self._initial
        next_backoff = min(wait * self._multiplier, self._max)
        self._state[key] = (time.monotonic() + wait, next_backoff)

    def record_success(self, key: str) -> None:
        """Attempt for `key` succeeded - clear its backoff entirely so
        a *future*, unrelated failure starts fresh at initial_backoff_s
        rather than inheriting whatever backoff had built up."""
        self._state.pop(key, None)

    def forget(self, key: str) -> None:
        """Drop any backoff state for `key` outright - e.g. when the
        node it belongs to is removed or renamed, so a later, unrelated
        node reusing the same id string doesn't inherit stale backoff."""
        self._state.pop(key, None)
