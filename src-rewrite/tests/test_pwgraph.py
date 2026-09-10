"""Unit tests for pwgraph link control (idempotent connect/disconnect)
and stale-object reaping, without touching a live PipeWire server."""

import types

import pytest

from pwgraph import LinkError, PipewireGraph


def fake_run(rc, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=rc, stdout=stdout, stderr=stderr)


def test_connect_existing_link_is_silent_noop(monkeypatch):
    g = PipewireGraph()
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(255, stderr="failed to link ports: File exists\n"),
    )
    assert g.connect(1, 2) is False  # no exception: already linked


def test_connect_missing_port_is_silent_noop(monkeypatch):
    g = PipewireGraph()
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(255, stderr="no such port 999"),
    )
    assert g.connect(999, 2) is False


def test_connect_genuine_failure_raises(monkeypatch):
    g = PipewireGraph()
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(1, stderr="port is of wrong direction"),
    )
    with pytest.raises(LinkError):
        g.connect(1, 2)


def test_disconnect_already_gone_is_silent(monkeypatch):
    g = PipewireGraph()
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(255, stderr="not linked"),
    )
    assert g.disconnect(1, 2) is False


def test_disconnect_success(monkeypatch):
    g = PipewireGraph()
    monkeypatch.setattr("pwgraph.subprocess.run", lambda *a, **k: fake_run(0))
    assert g.disconnect(1, 2) is True


def test_reap_stale_returns_count(monkeypatch):
    """reap_stale_for_names returns how much it cleaned up, and is a
    no-op when nothing is stale."""
    g = PipewireGraph()
    g._terminate_orphan_helpers = lambda markers: 1
    g._snapshot_stale = lambda markers: ({}, set())
    assert g.reap_stale_for_names(["patchbay_x"]) == 1
    assert g.reap_stale_for_names([]) == 0

    g._terminate_orphan_helpers = lambda markers: 0
    g._snapshot_stale = lambda markers: ({1: "patchbay_x"}, set())
    destroyed = []
    g._destroy_nodes = lambda nodes: destroyed.extend(nodes) or len(nodes)
    assert g.reap_stale_for_names(["patchbay_x"]) == 1
