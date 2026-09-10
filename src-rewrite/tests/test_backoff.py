"""Unit tests for the retry backoff gate in pwproc.Backoff."""

import pytest

from pwproc import Backoff


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def advance(self, seconds):
        self.now += seconds

    def __call__(self):
        return self.now


def make_clock(initial_s=1.0, max_s=30.0, multiplier=2.0, jitter=0.0, **kw):
    clock = Clock()
    gate = Backoff(
        initial_s=initial_s,
        max_s=max_s,
        multiplier=multiplier,
        jitter_fraction=jitter,
        clock=clock,
    )
    return gate, clock


def test_fresh_key_is_ready():
    gate, _ = make_clock()
    assert gate.ready("node1")


def test_failure_blocks_until_backoff_elapses():
    gate, clock = make_clock(initial_s=2.0)
    gate.record_failure("n")
    assert not gate.ready("n")
    clock.advance(2.0)
    assert gate.ready("n")


def test_success_clears_backoff_immediately():
    gate, clock = make_clock(initial_s=5.0)
    gate.record_failure("n")
    gate.record_success("n")
    assert gate.ready("n")


def test_repeated_failures_back_off_exponentially():
    gate, clock = make_clock(initial_s=1.0, max_s=8.0)
    waits = []
    for _ in range(4):
        gate.record_failure("n")
        waits.append(gate.next_attempt_in("n"))
        clock.advance(1000)  # let it become ready again
    # 1s, 2s, 4s, capped 8s
    assert waits[0] == pytest.approx(1.0)
    assert waits[1] == pytest.approx(2.0)
    assert waits[2] == pytest.approx(4.0)
    assert waits[3] == pytest.approx(8.0)


def test_backoff_is_capped():
    gate, clock = make_clock(initial_s=1.0, max_s=3.0)
    for _ in range(6):
        gate.record_failure("n")
        clock.advance(1000)
    # never exceeds the cap
    assert gate.next_attempt_in("n") <= 3.0 + 1e-6


def test_forget_resets_state():
    gate, clock = make_clock(initial_s=10.0)
    gate.record_failure("n")
    assert not gate.ready("n")
    gate.forget("n")
    assert gate.ready("n")


def test_keys_are_independent():
    gate, clock = make_clock(initial_s=100.0)
    gate.record_failure("a")
    assert not gate.ready("a")
    assert gate.ready("b")
