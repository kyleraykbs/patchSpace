"""The GUI's canvas opacity: `--canvas-opacity` wins, then the
`PATCHSPACE_CANVAS_OPACITY` the NixOS/home-manager module sets (from stylix),
then the built-in default.

Skipped without GTK: the entrypoint imports Gtk/Adw at module level."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"
))


def _resolve():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from patchspace_gui import CANVAS_OPACITY_ENV, _resolve_canvas_opacity
    return CANVAS_OPACITY_ENV, _resolve_canvas_opacity


def test_the_flag_wins_over_the_environment():
    env_var, resolve = _resolve()
    argv, value = resolve(
        ["patchspace", "--canvas-opacity", "0.5"],
        environ={env_var: "0.9"},
    )
    assert value == "0.5"
    assert argv == ["patchspace"]


def test_the_environment_is_used_when_there_is_no_flag():
    env_var, resolve = _resolve()
    argv, value = resolve(["patchspace"], environ={env_var: "0.85"})
    assert value == "0.85"
    assert argv == ["patchspace"]


def test_a_bad_environment_value_is_ignored_not_fatal():
    """It comes from configuration (the module validates the range, but a
    hand-set variable can be anything); drawing an opaque canvas beats
    refusing to start."""
    env_var, resolve = _resolve()
    _argv, value = resolve(["patchspace"], environ={env_var: "not-a-number"})
    assert value is None


def test_no_flag_and_no_environment_means_the_default():
    _env_var, resolve = _resolve()
    _argv, value = resolve(["patchspace"], environ={})
    assert value is None


def test_the_daemon_reports_the_deployment_opacity(monkeypatch):
    """The module puts it in the *daemon's* environment (a session variable
    only reaches a session at login, so a launcher-started window would miss
    it), and the daemon reports it with the graph: that is how the client
    learns a preference it was not launched with."""
    from main import PatchSpaceDaemon
    import main as main_mod

    monkeypatch.setattr(main_mod, "CANVAS_OPACITY", 0.85)
    payload = PatchSpaceDaemon()._cmd_get_nodes({})
    assert payload["canvas_opacity"] == 0.85


def test_an_unusable_daemon_opacity_is_no_preference(monkeypatch):
    import importlib
    import main as main_mod

    monkeypatch.setenv("PATCHSPACE_CANVAS_OPACITY", "nonsense")
    reloaded = importlib.reload(main_mod)
    assert reloaded.CANVAS_OPACITY is None
    monkeypatch.setenv("PATCHSPACE_CANVAS_OPACITY", "2.5")
    reloaded = importlib.reload(reloaded)
    assert reloaded.CANVAS_OPACITY == 1.0
    monkeypatch.delenv("PATCHSPACE_CANVAS_OPACITY")
    importlib.reload(reloaded)


def test_the_widget_hands_the_reported_opacity_to_its_window():
    from test_panel_physics import _widget

    w = _widget()
    seen = []
    w.on_canvas_opacity.append(seen.append)
    assert w.canvas_opacity_from_daemon is None

    w.update_from_daemon({
        "nodes": {}, "edges": {}, "groups": {}, "panels": [], "canvas_opacity": 0.85,
    })
    assert seen == [0.85]
    assert w.canvas_opacity_from_daemon == 0.85

    # Only on change: the GUI polls this payload every tick.
    w.update_from_daemon({
        "nodes": {}, "edges": {}, "groups": {}, "panels": [], "canvas_opacity": 0.85,
    })
    assert seen == [0.85]
