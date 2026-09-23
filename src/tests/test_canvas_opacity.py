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
