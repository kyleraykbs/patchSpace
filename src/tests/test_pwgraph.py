"""Unit tests for pwgraph link control (idempotent connect/disconnect)
and stale-object reaping, without touching a live PipeWire server."""

import json
import types

import pytest

from pwgraph import LinkError, PipewireGraph, _name_is_backing_of


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


def test_connect_timeout_raises_link_error(monkeypatch):
    """A wedged pw-link must not block the daemon forever: it is bounded
    and surfaces as a LinkError (which callers already handle)."""
    import subprocess

    g = PipewireGraph()

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1)

    monkeypatch.setattr("pwgraph.subprocess.run", boom)
    with pytest.raises(LinkError):
        g.connect(1, 2)


def test_disconnect_timeout_raises_link_error(monkeypatch):
    import subprocess

    g = PipewireGraph()

    def boom(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=1)

    monkeypatch.setattr("pwgraph.subprocess.run", boom)
    with pytest.raises(LinkError):
        g.disconnect(1, 2)


def test_reap_stale_returns_count(monkeypatch):
    """reap_stale_for_names returns how much it cleaned up, and is a
    no-op when nothing is stale."""
    g = PipewireGraph()
    g._terminate_orphan_helpers = lambda markers: 1
    g._snapshot_stale = lambda *a, **k: ({}, set())
    assert g.reap_stale_for_names(["patchbay_x"]) == 1
    assert g.reap_stale_for_names([]) == 0

    g._terminate_orphan_helpers = lambda markers: 0
    g._snapshot_stale = lambda *a, **k: ({1: "patchbay_x"}, set())
    destroyed = []
    g._destroy_nodes = lambda nodes: destroyed.extend(nodes) or len(nodes)
    assert g.reap_stale_for_names(["patchbay_x"]) == 1


def test_backing_marker_matches_only_exact_or_underscore_siblings():
    """A backing marker covers its own ``_``-suffixed siblings but must
    NOT cover a different node whose name merely starts with it.  A bare
    prefix match here let adding one effect reap a live sibling
    (``..._1`` matching ``..._10``) and kill its audio."""
    assert _name_is_backing_of("nc", "nc")
    assert _name_is_backing_of("nc_in", "nc")
    assert _name_is_backing_of("nc_fx_out", "nc")
    assert _name_is_backing_of("nc_in_keepalive", "nc")
    # Different nodes that merely share a textual prefix:
    assert not _name_is_backing_of("nc10", "nc")
    assert not _name_is_backing_of("nc_10", "nc_1")
    assert not _name_is_backing_of("nc_10_in", "nc_1")
    assert not _name_is_backing_of("nc_1x", "nc_1")


def test_live_names_matching_is_sibling_scoped():
    g = PipewireGraph()
    g.nodes = lambda: {
        1: {"info": {"props": {"node.name": "nc_1"}}},
        2: {"info": {"props": {"node.name": "nc_1_in"}}},
        3: {"info": {"props": {"node.name": "nc_10"}}},
        4: {"info": {"props": {"node.name": "nc_10_in"}}},
    }
    assert set(g._live_names_matching(["nc_1"])) == {"nc_1", "nc_1_in"}


def test_snapshot_stale_is_sibling_scoped(monkeypatch):
    dump = [
        {
            "id": 1,
            "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": "nc_1"}},
        },
        {
            "id": 2,
            "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": "nc_10"}},
        },
        {
            "id": 3,
            "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": "nc_10_in"}},
        },
    ]
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(0, stdout=json.dumps(dump)),
    )
    found, pids = PipewireGraph()._snapshot_stale(["nc_1"])
    assert set(found.values()) == {"nc_1"}
    assert pids == set()


# The startup crash-recovery sweep does NOT pass exact backing names: it
# passes the daemon's `_OWNED_PREFIXES` (main.py), which are *prefixes*
# ending in "_" ("noise_cancel_node_", "echo_cancel_node_", "patchbay_",
# ...).  A new AI Noise Cancel is only safe to add once those stale
# objects are reaped, so the marker matcher must keep treating a
# trailing-underscore marker as a prefix - scoping it with the
# exact-or-"_"-sibling backing rule instead makes every owned node
# invisible to the sweep.  The stale echo/noise-cancel module and its
# dummies then linger under the same names, and plugging in the fresh
# node links against them and takes the whole chain silent.

_STARTUP_MARKER = "noise_cancel_node_"
_STALE_NOISE_CANCEL_NODES = {
    11: "noise_cancel_node_1699999999999_1",
    12: "noise_cancel_node_1699999999999_1_in",
    13: "noise_cancel_node_1699999999999_1_out",
    14: "noise_cancel_node_1699999999999_1_fx_out",
    15: "noise_cancel_node_1699999999999_1_in_keepalive",
}


def test_snapshot_stale_matches_owned_prefix_markers(monkeypatch):
    dump = [
        {
            "id": node_id,
            "type": "PipeWire:Interface:Node",
            "info": {"props": {"node.name": name}},
        }
        for node_id, name in _STALE_NOISE_CANCEL_NODES.items()
    ]
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(0, stdout=json.dumps(dump)),
    )
    found, _pids = PipewireGraph()._snapshot_stale([_STARTUP_MARKER])
    assert set(found.values()) == set(_STALE_NOISE_CANCEL_NODES.values())


def test_live_names_matching_matches_owned_prefix_markers():
    g = PipewireGraph()
    g.nodes = lambda: {
        node_id: {"info": {"props": {"node.name": name}}}
        for node_id, name in _STALE_NOISE_CANCEL_NODES.items()
    }
    assert set(g._live_names_matching([_STARTUP_MARKER])) == set(
        _STALE_NOISE_CANCEL_NODES.values()
    )


def _node(node_id, name, media_class):
    return {
        "id": node_id,
        "type": "PipeWire:Interface:Node",
        "info": {"props": {"node.name": name, "media.class": media_class}},
    }


# An effect whose backing was NOT the GUI's "<type>_node_<id>" pattern -
# an older/imported/test config using a bare "fx" - is invisible to the
# prefix sweep.  Its dead dummies and keepalives stay wired into the
# default devices and keep the graph wedged.  The owned sweep derives the
# backing from the reserved Internal media class / *_keepalive names, so
# the startup sweep can reap it regardless of what it was called.

_ORPHAN_DUMP = [
    _node(1, "fx", "Audio/Sink"),               # module capture
    _node(2, "fx_fx_out", "Audio/Source"),      # module playback
    _node(3, "fx_in", "Audio/Sink/Internal"),   # dummy
    _node(4, "fx_out", "Audio/Sink/Internal"),  # dummy
    _node(5, "fx_in_keepalive", "Stream/Output/Audio"),
    _node(6, "fx_out_keepalive", "Stream/Input/Audio"),
    _node(7, "Firefox", "Stream/Output/Audio"),  # must NOT be swept
    _node(8, "fxbox", "Stream/Output/Audio"),    # prefix, but not ours
]


def test_owned_sweep_reaps_arbitrary_backing_plumbing(monkeypatch):
    monkeypatch.setattr(
        "pwgraph.subprocess.run",
        lambda *a, **k: fake_run(0, stdout=json.dumps(_ORPHAN_DUMP)),
    )
    found, _pids = PipewireGraph()._snapshot_stale([], owned_sweep=True)
    assert set(found.values()) == {
        "fx",
        "fx_fx_out",
        "fx_in",
        "fx_out",
        "fx_in_keepalive",
        "fx_out_keepalive",
    }


def test_targeted_reap_matches_only_its_backing_family():
    # A targeted reap (default, owned_sweep off) matches only the marker
    # and its "_"-siblings - never the whole graph's owned plumbing.
    g = PipewireGraph()
    g.nodes = lambda: {
        node["id"]: {"info": node["info"]} for node in _ORPHAN_DUMP
    }
    assert set(g._live_names_matching(["fx"])) == {
        "fx",
        "fx_fx_out",
        "fx_in",
        "fx_out",
        "fx_in_keepalive",
        "fx_out_keepalive",
    }
    # A mere textual prefix ("fxbox") is not part of the "fx" family.
    assert "fxbox" not in g._live_names_matching(["fx"])

