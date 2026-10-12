"""The daemon reports the deployment's canvas opacity with the graph.

The *client* side of this (resolving the flag/environment, and handing a
reported opacity to the window) lives with the client; what is asserted here
is the daemon's half: a module puts the preference in the daemon's
environment, and `get_nodes` reports it so a window the daemon did not
launch still learns it.
"""

import importlib

import pytest

import central.main as main_mod
from central.main import PatchSpaceDaemon


def test_the_daemon_reports_the_deployment_opacity(monkeypatch):
    monkeypatch.setattr(main_mod, "CANVAS_OPACITY", 0.85)
    payload = PatchSpaceDaemon()._cmd_get_nodes({})
    assert payload["canvas_opacity"] == 0.85


def test_an_unusable_daemon_opacity_is_no_preference(monkeypatch):
    monkeypatch.setenv("PATCHSPACE_CANVAS_OPACITY", "nonsense")
    reloaded = importlib.reload(main_mod)
    assert reloaded.CANVAS_OPACITY is None
    monkeypatch.setenv("PATCHSPACE_CANVAS_OPACITY", "2.5")
    reloaded = importlib.reload(reloaded)
    assert reloaded.CANVAS_OPACITY == 1.0
    monkeypatch.delenv("PATCHSPACE_CANVAS_OPACITY")
    importlib.reload(reloaded)
