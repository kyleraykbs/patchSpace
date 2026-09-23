"""Impulse wires (Button -> Sound Effect).

An impulse is a momentary event, not a signal: a Button holds no state,
its wire never becomes a PipeWire link, and the daemon pushes it through
the graph on demand (PatchSpace.pulse) rather than resolving a value on
each sync.  These tests drive the daemon command layer directly, with the
pw-cli/pw-cat process classes faked out so no real PipeWire is touched.
"""

import pytest

import pwnodes
from main import PatchBayDaemon
from gui import node_specs
from session_repair import ERROR, repair, validate


class FakeCli:
    instances = []

    def __init__(self, name, command=("pw-cli",), settle=0.0, **kw):
        self.name = name
        self.node_id = None
        self.alive = True
        self.owns_process = True
        self.resolve_now = True
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
        pass

    def destroy(self):
        self.alive = False


class FakeProc(FakeCli):
    commands = []

    def create(self, command, quiet=False):
        FakeProc.commands.append(list(command))
        self.alive = True
        return True


@pytest.fixture(autouse=True)
def _fake_processes(monkeypatch):
    FakeCli.instances.clear()
    FakeProc.commands.clear()
    monkeypatch.setattr(pwnodes, "OwnedPwNode", FakeCli)
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)


def _ok(d, **cmd):
    resp = d.handle_command(cmd)
    assert resp["status"] == "ok", resp
    return resp


def _daemon_with_pair(path="/sounds/clang.wav"):
    """A Button wired to a Sound Effect, with `path` set."""
    d = PatchBayDaemon()
    _ok(d, command="add_node", node_type="button", node_id="btn")
    _ok(
        d,
        command="add_node",
        node_type="sound_effect",
        node_id="fx",
        config={"path": path},
    )
    _ok(
        d,
        command="add_edge",
        from_node="btn",
        to_node="fx",
        from_port="out",
        to_port="in",
    )
    return d, d.space.nodes["fx"]


def test_impulse_pairs_only_with_impulse():
    d = PatchBayDaemon()
    for node_id, node_type in (("btn", "button"), ("fx", "sound_effect"),
                               ("vol", "volume")):
        _ok(d, command="add_node", node_type=node_type, node_id=node_id)
    # An impulse output only accepts an impulse input...
    resp = d.handle_command(
        {
            "command": "add_edge",
            "from_node": "btn",
            "to_node": "vol",
            "from_port": "out",
            "to_port": "in",
        }
    )
    assert resp["status"] == "error"
    # ...an audio source cannot drive the sound effect's impulse input...
    resp = d.handle_command(
        {
            "command": "add_edge",
            "from_node": "vol",
            "to_node": "fx",
            "from_port": "out",
            "to_port": "in",
        }
    )
    assert resp["status"] == "error"
    # ...and the impulse input takes exactly one source (a single trigger,
    # like a boolean control input).
    _ok(d, command="add_edge", from_node="btn", to_node="fx",
        from_port="out", to_port="in")
    resp = d.handle_command(
        {
            "command": "add_edge",
            "from_node": "btn",
            "to_node": "fx",
            "from_port": "out",
            "to_port": "in",
        }
    )
    assert resp["status"] == "ok"  # same edge: idempotent
    _ok(d, command="add_node", node_type="button", node_id="btn2")
    resp = d.handle_command(
        {
            "command": "add_edge",
            "from_node": "btn2",
            "to_node": "fx",
            "from_port": "out",
            "to_port": "in",
        }
    )
    assert resp["status"] == "error" and "already driven" in resp["message"]


def test_impulse_edge_is_never_a_pipewire_link():
    d, _fx = _daemon_with_pair()
    d.space._graph_loaded = True
    d.space.sync()
    # Nothing about an impulse resolves into ports/links...
    assert d.space._edge_links.get("btn->fx") is None
    # ...which also means it is never reported as "still wiring".
    assert d.space.edge_wired("btn->fx") is True
    nodes = d.handle_command({"command": "get_nodes"})
    assert nodes["edges"]["btn->fx"]["wired"] is True


def test_pulse_plays_the_file_once_per_impulse():
    d, fx = _daemon_with_pair()
    resp = _ok(d, command="impulse", node_id="btn")
    assert resp["fired"] == ["fx"]
    assert len(FakeProc.commands) == 1
    command = FakeProc.commands[0]
    # The sound must land in the node's own dummy sink, targeted by name -
    # that is the stable socket its audio out (the sink's monitor) hangs
    # off, so no user edge is ever attached to a stream that exits.
    assert command[0] == "pw-cat" and "--playback" in command
    target = command[command.index("--target") + 1]
    assert target == fx.backing_node_name
    assert command[-1] == "/sounds/clang.wav"
    assert fx.playing == 1


def test_retrigger_restarts_by_default_and_stacks_when_asked():
    d, fx = _daemon_with_pair()
    # Default (overlap off): a second impulse must stop the first take
    # rather than layer it.
    _ok(d, command="impulse", node_id="btn")
    first = fx._players[0]
    _ok(d, command="impulse", node_id="btn")
    assert first.is_alive is False
    assert fx.playing == 1

    # With overlap on, every impulse keeps its own stream, and the count
    # the GUI shows is the number actually running.
    _ok(d, command="set_node_property", node_id="fx", property="overlap", value=True)
    _ok(d, command="impulse", node_id="btn")
    _ok(d, command="impulse", node_id="btn")
    assert fx.playing == 3
    nodes = _ok(d, command="get_nodes")["nodes"]
    assert nodes["fx"]["playing"] == 3
    assert nodes["fx"]["overlap"] is True


def test_a_finished_sound_is_not_a_dead_node():
    """The whole reason players stay out of `backings`: a playback child
    exits by itself when the file ends, and a naturally-dead backing is
    exactly what dead_backings()/health treat as a fault."""
    d, fx = _daemon_with_pair()
    _ok(d, command="impulse", node_id="btn")
    player = fx._players[0]
    player.alive = False  # the file ended while nobody was watching

    assert fx.dead_backings() == []
    assert d._node_health(fx) != "dead"
    assert fx.playing == 0
    # The supervision tick retires it, so the count can't drift.
    fx.refresh_live()
    assert fx._players == []
    assert fx not in [b for b in FakeCli.instances if b.name == player.name]


def test_deleting_a_sound_effect_stops_what_it_is_playing():
    d, fx = _daemon_with_pair()
    _ok(d, command="impulse", node_id="btn")
    player = fx._players[0]
    assert player.is_alive
    _ok(d, command="remove_node", node_id="fx")
    assert player.is_alive is False


def test_impulse_with_no_file_is_a_silent_noop():
    d, fx = _daemon_with_pair(path="")
    assert _ok(d, command="impulse", node_id="btn")["fired"] == ["fx"]
    assert FakeProc.commands == []
    assert fx.playing == 0


def test_impulse_command_rejects_a_non_button():
    d, _fx = _daemon_with_pair()
    resp = d.handle_command({"command": "impulse", "node_id": "fx"})
    assert resp["status"] == "error"


def test_sound_effect_config_round_trips():
    d, fx = _daemon_with_pair(path="/a/b.wav")
    _ok(d, command="set_node_property", node_id="fx", property="overlap", value=True)
    exported = d._build_export_config()
    params = exported["nodes"]["fx"]["params"]
    assert params["path"] == "/a/b.wav"
    assert params["overlap"] is True

    # Replaying that config onto a fresh daemon reproduces the node.
    d2 = PatchBayDaemon()
    _ok(
        d2,
        command="add_node",
        node_type="sound_effect",
        node_id="fx",
        config=params,
    )
    assert d2.space.nodes["fx"].path == "/a/b.wav"
    assert d2.space.nodes["fx"].overlap is True


def test_a_loaded_session_brings_an_impulse_chain_back(monkeypatch):
    """The impulse wire and the sound effect's settings must survive a
    session save/load - the load path is a different code path from
    add_node (it stages nodes and replays their params)."""
    d = PatchBayDaemon()
    # There is no real graph here: resolve every backing immediately so
    # the load's per-node bring-up doesn't sit out its timeout.
    monkeypatch.setattr(d.graph, "node_id_by_name", lambda name: 1)
    d._load_session(
        {
            "nodes": {
                "kick": {"type": "button", "params": {"label": "Kick"}},
                "boom": {
                    "type": "sound_effect",
                    "params": {"path": "/sounds/boom.wav", "overlap": True},
                },
            },
            "edges": [
                {"from": "kick", "to": "boom", "from_port": "out", "to_port": "in"}
            ],
            "groups": [],
        },
        migrate=True,
    )
    assert set(d.space.nodes) == {"kick", "boom"}
    fx = d.space.nodes["boom"]
    assert fx.path == "/sounds/boom.wav" and fx.overlap is True
    assert "kick->boom" in d.space.edges
    assert d.space.pulse("kick") == ["boom"]
    assert len(FakeProc.commands) == 1


def test_gui_mirrors_the_impulse_port_kind():
    assert node_specs.port_kind("button", "out", "out") == "impulse"
    assert node_specs.port_kind("sound_effect", "in", "in") == "impulse"
    assert node_specs.port_kind("sound_effect", "out", "out") == "audio"
    assert node_specs.ports_compatible("button", "out", "sound_effect", "in")
    assert not node_specs.ports_compatible("button", "out", "volume", "in")
    assert not node_specs.ports_compatible("splitter", "out", "sound_effect", "in")


def test_session_repair_accepts_an_impulse_wire():
    cfg = {
        "nodes": {
            "btn": {"type": "button", "params": {}},
            "fx": {"type": "sound_effect", "params": {"path": "/a/b.wav"}},
        },
        "edges": [
            {"from": "btn", "to": "fx", "from_port": "out", "to_port": "in"},
        ],
        "groups": [],
    }
    assert [i for i in validate(cfg) if i.severity == ERROR] == []
    # repair normalizes away the redundant default ports; the edge itself
    # must survive untouched.
    assert repair(cfg).config["edges"] == [{"from": "btn", "to": "fx"}]


def test_session_repair_flags_a_mismatched_impulse_wire():
    cfg = {
        "nodes": {
            "btn": {"type": "button", "params": {}},
            "vol": {"type": "volume", "params": {}},
        },
        "edges": [
            {"from": "btn", "to": "vol", "from_port": "out", "to_port": "in"},
        ],
        "groups": [],
    }
    assert any(i.code == "port-kind" for i in validate(cfg))
