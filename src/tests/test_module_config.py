"""Which built-in modules are enabled, and turning them on or off.

The config file is the source of truth; the daemon reads it and applies a
change live, so disabling a module takes its nodes offline without touching
the graph.
"""

import json
import os

import pytest

from central import module_config
from central.main import PatchSpaceDaemon
from central.module_config import ModuleConfig
from central.modules_cli import main as modules_cli_main


def test_a_missing_file_means_the_builtins_are_on(tmp_path):
    config = ModuleConfig(str(tmp_path / "modules.json"))
    assert config.enabled("pipewire") is True
    assert config.enabled("primitives") is True
    assert config.enabled("something-else") is False
    assert config.managed is False


def test_set_enabled_persists_and_reloads(tmp_path):
    path = str(tmp_path / "modules.json")
    config = ModuleConfig(path)
    assert config.set_enabled("primitives", False) is True
    assert os.path.exists(path)
    with open(path) as handle:
        assert json.load(handle) == {"modules": {"primitives": {"enable": False}}}

    again = ModuleConfig(path)
    assert again.enabled("primitives") is False
    assert again.enabled("pipewire") is True


def test_a_deployment_managed_file_is_read_but_not_written(tmp_path):
    path = tmp_path / "nix" / "modules.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"modules": {"primitives": {"enable": False}}}))
    # A store path is what the nix module points the daemon at.
    config = ModuleConfig(str(path))
    assert config.enabled("primitives") is False
    config.path = "/nix/store/abc-modules.json"
    config.managed = True
    assert config.set_enabled("primitives", True) is False


def test_a_broken_file_does_not_stop_the_daemon(tmp_path):
    path = tmp_path / "modules.json"
    path.write_text("{ this is not json")
    config = ModuleConfig(str(path))
    assert config.enabled("pipewire") is True


# ----------------------------------------------------------------------
# the daemon
# ----------------------------------------------------------------------

def test_the_daemon_reports_and_applies_the_module_set(tmp_path):
    daemon = PatchSpaceDaemon(
        modules_config_path=str(tmp_path / "modules.json")
    )
    assert daemon.attach_modules()["module_id"] == "pipewire"

    report = daemon.handle_command({"command": "modules"})
    assert report["status"] == "ok"
    rows = {m["name"]: m for m in report["modules"]}
    assert rows["pipewire"]["enabled"] is True and rows["pipewire"]["running"] is True
    assert rows["pipewire"]["kind"] == "inproc"
    assert rows["primitives"]["kind"] == "process"
    assert report["config_path"] == str(tmp_path / "modules.json")

    # Disabling the in-process provider takes its nodes offline and stops
    # running it; the graph and the placements stay.
    daemon.handle_command({"command": "add_node", "node_type": "gate",
                           "node_id": "g1", "config": {}})
    assert daemon.registry.instance("g1").status == "online"
    reply = daemon.handle_command({"command": "set_module", "name": "pipewire",
                                   "enable": False})
    assert reply["status"] == "ok", reply
    assert daemon._pw_module is None
    assert daemon.registry.instance("g1").status != "online"
    assert "g1" in daemon.space.nodes
    assert {m["name"]: m["enabled"] for m in reply["modules"]}["pipewire"] is False
    # ...and it is remembered.
    assert ModuleConfig(str(tmp_path / "modules.json")).enabled("pipewire") is False

    # Re-enabling brings the provider back and its nodes online.
    daemon.handle_command({"command": "set_module", "name": "pipewire",
                           "enable": True})
    daemon.sync_registry()
    assert daemon.registry.instance("g1").status == "online"


def test_disabling_a_process_module_removes_it_from_the_specs(tmp_path):
    daemon = PatchSpaceDaemon(
        modules_config_path=str(tmp_path / "modules.json")
    )
    daemon.attach_modules()
    # No socket bound in a test, so nothing was spawned; the *spec* set is
    # what the config drives.
    assert [s.module_id for s in daemon.module_specs] == ["primitives"]
    daemon.handle_command({"command": "set_module", "name": "primitives",
                           "enable": False})
    assert daemon.module_specs == []
    assert daemon.handle_command(
        {"command": "set_module", "name": "nope", "enable": True}
    )["status"] == "error"
    assert daemon.handle_command(
        {"command": "set_module", "name": "", "enable": True}
    )["status"] == "error"


def test_a_managed_config_refuses_a_live_change(tmp_path):
    daemon = PatchSpaceDaemon(
        modules_config_path=str(tmp_path / "modules.json")
    )
    daemon.module_config.managed = True  # as a store path would be
    reply = daemon.handle_command({"command": "set_module", "name": "primitives",
                                   "enable": False})
    assert reply["status"] == "error" and "deployment" in reply["message"]


# ----------------------------------------------------------------------
# the CLI
# ----------------------------------------------------------------------

def test_the_cli_reports_the_file_when_no_daemon_is_running(tmp_path, capsys):
    os.environ["PATCHSPACE_MODULES_CONFIG"] = str(tmp_path / "modules.json")
    try:
        assert modules_cli_main(["--socket", str(tmp_path / "none.sock")]) == 1
        out = capsys.readouterr().out
        assert "no daemon running" in out
        assert "primitives" in out and "enabled" in out
    finally:
        os.environ.pop("PATCHSPACE_MODULES_CONFIG", None)
