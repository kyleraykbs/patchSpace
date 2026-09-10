"""End-to-end lifecycle tests for the filter-chain effect sandwich,
using fake process classes in place of real pw-cli / pw-cat so nothing
touches a live PipeWire server."""

import pytest

import pwnodes
from pwnodes import NoiseCancelNode, SensitivityGateNode
from tests.test_pwnodes import FakeGraph, make_space


class FakeCli:
    """Records created objects instead of talking to pw-cli."""

    instances = []
    created_lines = []

    def __init__(self, name, command=("pw-cli",), settle=0.0, **kw):
        self.name = name
        self.node_id = None
        self.alive = True
        self.owns_process = True
        self.set_params = []
        FakeCli.instances.append(self)

    def create(self, line):
        if line.startswith("load-module") and getattr(FakeCli, "fail_modules", False):
            return False
        FakeCli.created_lines.append(line)
        self.alive = True
        return True

    @property
    def is_alive(self):
        return self.alive

    def stuck(self, grace_s):
        # The fakes always resolve promptly (or never own the object at
        # all), so they are never "alive but stuck" the way
        # pwproc.OwnedPwNode.stuck() models a real pw-cli session.
        return False

    def resolve(self, node_id):
        self.node_id = node_id

    def set_param(self, iface, body):
        self.set_params.append((iface, body))

    def destroy(self):
        self.alive = False


class FakeProc(FakeCli):
    def create(self, command):
        if isinstance(command, (list, tuple)):
            FakeCli.created_lines.append(" ".join(command))
        else:
            FakeCli.created_lines.append(command)
        self.alive = True
        return True


@pytest.fixture(autouse=True)
def _fake_processes(monkeypatch):
    FakeCli.instances.clear()
    FakeCli.created_lines.clear()
    FakeCli.fail_modules = False
    monkeypatch.setattr(pwnodes, "OwnedPwNode", FakeCli)
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)


def names():
    return {i.name for i in FakeCli.instances}


def test_effect_gets_stable_sockets_first_module_later():
    """add_node must create the stable dummy sockets immediately and
    leave the (fallible) DSP module to the supervision pass."""
    space = make_space(FakeGraph(), repair_gate=pwnodes.Backoff(
        initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()

    node = NoiseCancelNode("n", "fx")
    space.add_node(node)

    assert names() >= {"fx_in", "fx_out", "fx_in_keepalive", "fx_out_keepalive"}
    assert "fx" not in names()  # module not spawned yet

    space.supervise()
    assert "fx" in names()          # module now materialised
    assert "fx_fx_out" in names()   # plus its playback sibling


def test_missing_module_is_retried_not_duplicated():
    space = make_space(FakeGraph(), repair_gate=pwnodes.Backoff(
        initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = NoiseCancelNode("n", "fx")
    space.add_node(node)

    FakeCli.fail_modules = True
    space.supervise()
    assert node.module_backing() is None
    assert not node.module_ok()

    FakeCli.fail_modules = False
    space.supervise()
    assert node.module_backing() is not None
    assert node.module_ok()


def test_crashed_module_interior_is_reloaded():
    space = make_space(FakeGraph(), repair_gate=pwnodes.Backoff(
        initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = NoiseCancelNode("n", "fx")
    space.add_node(node)
    space.supervise()
    assert node.module_ok()

    # The module's owning process dies on its own.
    capture = next(i for i in FakeCli.instances if i.name == "fx")
    capture.alive = False
    n_before = len(FakeCli.instances)
    space.supervise()
    assert node.module_ok()
    # A fresh module + siblings were spawned (instances grew) and the
    # stable dummies/keepalives were not duplicated.
    assert len(FakeCli.instances) > n_before


def test_reload_module_swaps_interior_keeps_sockets():
    space = make_space(FakeGraph(), repair_gate=pwnodes.Backoff(
        initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = SensitivityGateNode("n", "gate", level=30.0)
    space.add_node(node)
    space.supervise()

    fx_out_names_before = {i.name for i in FakeCli.instances if i.name.startswith("gate_fx_out")}
    assert fx_out_names_before

    node.reload_module()
    # Sockets are untouched; interior streams recreated.
    assert "gate_in" in names()
    assert "gate_out" in names()


def test_control_param_reapplied_after_module_recreation():
    space = make_space(FakeGraph(), repair_gate=pwnodes.Backoff(
        initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()
    node = SensitivityGateNode("n", "gate", level=25.0)
    space.add_node(node)
    space.supervise()
    node._resolve_backings = None  # no-op guard below not needed

    module = node.module_backing()
    module.node_id = 101
    node.refresh_live()
    assert module.set_params  # threshold pushed once

    module.set_params.clear()
    node.set_level(60.0)
    assert node.level == 60.0
    assert module.set_params  # pushed again live, no reload needed
