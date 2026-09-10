"""The Sensitivity gate's gain-staging Volume nodes are daemon-owned
and hidden: the user never creates or wires them, get_nodes/export never
show them, and the user's edges are transparently routed through them.

These tests drive the daemon command layer directly (like
test_daemon_protocol), with the pw-cli process classes faked out so no
real PipeWire server is touched."""

import pytest

import pwnodes
from main import PatchBayDaemon


class FakeCli:
    instances = []

    def __init__(self, name, command=("pw-cli",), settle=0.0, **kw):
        self.name = name
        self.node_id = None
        self.alive = True
        self.owns_process = True
        self.set_params = []
        FakeCli.instances.append(self)

    def create(self, line):
        self.alive = True
        return True

    @property
    def is_alive(self):
        return self.alive

    def stuck(self, grace_s):
        return False

    def resolve(self, node_id):
        self.node_id = node_id

    def set_param(self, iface, body):
        self.set_params.append((iface, body))

    def destroy(self):
        self.alive = False


class FakeProc(FakeCli):
    def create(self, command):
        self.alive = True
        return True


@pytest.fixture(autouse=True)
def _fake_processes(monkeypatch):
    FakeCli.instances.clear()
    monkeypatch.setattr(pwnodes, "OwnedPwNode", FakeCli)
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)


def fresh_daemon():
    return PatchBayDaemon()


def _add(d, node_type, node_id, config=None):
    resp = d.handle_command(
        {
            "command": "add_node",
            "node_type": node_type,
            "node_id": node_id,
            "config": config or {},
        }
    )
    assert resp["status"] == "ok", resp
    return resp


def test_sensitivity_gets_hidden_bracketing_volume_nodes():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")

    pre, post = "__sens_pre__sens", "__sens_post__sens"
    assert pre in d.space.nodes
    assert post in d.space.nodes
    # They are not public, so the GUI never sees them...
    assert pre not in d.space.public_nodes
    assert post not in d.space.public_nodes
    assert set(d.handle_command({"command": "get_nodes"})["nodes"]) == {"sens"}
    # ...but the internal links that put them in the signal path exist.
    assert f"{pre}->sens" in d.space.edges
    assert f"sens->{post}" in d.space.edges
    # Pre-gain swings below/above unity; post is the reciprocal makeup
    # stage, so its own ceiling has to clear 1.0.
    assert d.space.nodes[pre].volume_min == pwnodes.SensitivityGateNode.PRE_GAIN_MIN
    assert d.space.nodes[pre].volume_max == pwnodes.SensitivityGateNode.PRE_GAIN_MAX
    assert d.space.nodes[post].volume_min == 0.0
    assert d.space.nodes[post].volume_max == pwnodes.SensitivityGateNode.POST_GAIN_MAX


def test_user_edges_route_through_hidden_nodes_and_stay_logical():
    d = fresh_daemon()
    _add(d, "regex_input", "src", {"pattern": ".*"})
    _add(d, "sensitivity_gate", "sens")
    _add(d, "description_output", "out", {"description": "speakers"})
    assert d.handle_command(
        {"command": "add_edge", "from_node": "src", "to_node": "sens"}
    )["status"] == "ok"
    assert d.handle_command(
        {"command": "add_edge", "from_node": "sens", "to_node": "out"}
    )["status"] == "ok"

    # Physically, the audio goes src -> pre -> sens -> post -> out ...
    assert "src->__sens_pre__sens" in d.space.edges
    assert "__sens_post__sens->out" in d.space.edges
    # ... but the GUI sees only the logical edges.
    edges = d.handle_command({"command": "get_nodes"})["edges"]
    assert set(edges) == {"src->sens", "sens->out"}
    assert edges["src->sens"]["from_node"] == "src"
    assert edges["sens->out"]["to_node"] == "out"


def test_removing_logical_edge_removes_routed_edge():
    d = fresh_daemon()
    _add(d, "regex_input", "src", {"pattern": ".*"})
    _add(d, "sensitivity_gate", "sens")
    d.handle_command(
        {"command": "add_edge", "from_node": "src", "to_node": "sens"}
    )

    resp = d.handle_command({"command": "remove_edge", "edge_id": "src->sens"})
    assert resp["status"] == "ok", resp
    assert "src->__sens_pre__sens" not in d.space.edges
    assert d.handle_command({"command": "get_nodes"})["edges"] == {}


def test_sensitivity_property_drives_hidden_gains_reciprocally():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    pre = d.space.nodes["__sens_pre__sens"]
    post = d.space.nodes["__sens_post__sens"]

    assert d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sens",
            "property": "sensitivity",
            "value": 1.0,
        }
    )["status"] == "ok"
    assert pre.volume == 1.0
    pre_gain = pre.volume_min + (pre.volume_max - pre.volume_min) * pre.volume
    post_gain = post.volume_min + (post.volume_max - post.volume_min) * post.volume
    assert pre_gain * post_gain == pytest.approx(1.0)

    assert d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sens",
            "property": "sensitivity",
            "value": 0.0,
        }
    )["status"] == "ok"
    assert pre.volume == 0.0
    pre_gain = pre.volume_min + (pre.volume_max - pre.volume_min) * pre.volume
    post_gain = post.volume_min + (post.volume_max - post.volume_min) * post.volume
    assert pre_gain * post_gain == pytest.approx(1.0)

    # Out-of-range values clamp rather than error.
    assert d.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sens",
            "property": "sensitivity",
            "value": 5.0,
        }
    )["status"] == "ok"
    assert d.space.nodes["sens"].sensitivity == 1.0


def test_removing_sensitivity_node_removes_hidden_children():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    assert d.handle_command({"command": "remove_node", "node_id": "sens"})[
        "status"
    ] == "ok"
    assert "__sens_pre__sens" not in d.space.nodes
    assert "__sens_post__sens" not in d.space.nodes


def test_renaming_sensitivity_gate_carries_hidden_children_and_edges():
    d = fresh_daemon()
    _add(d, "regex_input", "src", {"pattern": ".*"})
    _add(d, "sensitivity_gate", "sens")
    d.handle_command(
        {"command": "add_edge", "from_node": "src", "to_node": "sens"}
    )

    resp = d.handle_command(
        {
            "command": "rename_node",
            "old_node_id": "sens",
            "new_node_id": "sens2",
        }
    )
    assert resp["status"] == "ok", resp
    assert "__sens_pre__sens2" in d.space.nodes
    assert "__sens_post__sens2" in d.space.nodes
    assert "__sens_pre__sens" not in d.space.nodes
    edges = d.handle_command({"command": "get_nodes"})["edges"]
    assert set(edges) == {"src->sens2"}


def test_reset_clears_hidden_sensitivity_nodes():
    d = fresh_daemon()
    _add(d, "sensitivity_gate", "sens")
    d.handle_command({"command": "reset"})
    assert d.handle_command({"command": "get_nodes"})["nodes"] == {}
    assert not any(nid.startswith("__sens_") for nid in d.space.nodes)


def test_export_hides_hidden_nodes_and_round_trips_sensitivity():
    d1 = fresh_daemon()
    _add(d1, "regex_input", "src", {"pattern": ".*"})
    _add(d1, "sensitivity_gate", "sens", {"sensitivity": 0.5})
    d1.handle_command(
        {"command": "add_edge", "from_node": "src", "to_node": "sens"}
    )
    d1.handle_command(
        {
            "command": "set_node_property",
            "node_id": "sens",
            "property": "sensitivity",
            "value": 0.5,
        }
    )
    export = d1.handle_command({"command": "export_config"})["config"]
    assert set(export["nodes"]) == {"src", "sens"}
    assert export["nodes"]["sens"]["params"]["sensitivity"] == 0.5
    assert export["edges"] == [{"from": "src", "to": "sens"}]

    d2 = fresh_daemon()
    for node_id, node_cfg in export["nodes"].items():
        _add(d2, node_cfg["type"], node_id, node_cfg["params"])
    for edge in export["edges"]:
        d2.handle_command(
            {
                "command": "add_edge",
                "from_node": edge["from"],
                "to_node": edge["to"],
                "to_port": edge.get("to_port", "in"),
            }
        )
    nodes = d2.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["sens"]["sensitivity"] == 0.5
    assert d2.space.nodes["__sens_pre__sens"].volume == 0.5
