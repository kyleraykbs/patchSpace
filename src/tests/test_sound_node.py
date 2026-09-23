"""The Sound node, and the *sound* signal kind it introduces.

A sound is a file of known length carried on its own port kind: no audio
crosses it, it is a reference (file + range) that a Sound Player fires and a
Clip node slices.  Its length is probed once per path, which is what lets the
timeline and the node's read-out talk about time in a file nobody has opened.

Needs no GTK; the duration test needs ffprobe (skipped without it)."""

import os
import pathlib
import shutil
import struct
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pwnodes
from tests.test_impulse import FakeCli, FakeProc
from pwnodes import SoundNode, probe_duration  # noqa: E402


def _write_wav(path, seconds=0.25, rate=8000):
    with wave.open(str(path), "wb") as fh:
        fh.setnchannels(1)
        fh.setsampwidth(2)
        fh.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(12000 * ((i % 50) - 25) / 25))
            for i in range(int(rate * seconds))
        )
        fh.writeframes(frames)


def test_a_sound_node_has_no_inputs_and_a_sound_output():
    node = SoundNode("s", "~/sounds/click.wav")
    assert node.path == "~/sounds/click.wav"
    assert node.port_kind("out", "out") == "sound"
    assert node.port_kind("in", "in") == "audio"  # nothing to plug into


def test_the_length_of_a_real_file_is_known(tmp_path):
    if not shutil.which("ffprobe"):
        pytest.skip("ffprobe not available")
    path = tmp_path / "blip.wav"
    _write_wav(path, seconds=0.25)
    duration = probe_duration(str(path))
    assert duration == pytest.approx(0.25, abs=0.05)
    # Cached, and an unreadable file just has no length.
    assert probe_duration(str(path)) == duration
    assert probe_duration(str(tmp_path / "missing.wav")) == 0.0
    assert probe_duration("") == 0.0
    # The node reports its file's length; a node with no file has none yet.
    assert SoundNode("s", str(path)).duration == pytest.approx(0.25, abs=0.05)
    assert SoundNode("s", "").duration == 0.0


def test_sound_ports_pair_only_with_sound_ports():
    from gui import node_specs

    assert node_specs.port_kind("sound", "out", "out") == "sound"
    assert node_specs.spec_for("sound").sound_outputs == {"out"}
    assert node_specs.spec_for("sound").inputs == []
    # A sound output only ever meets a sound input: the player's.
    player = node_specs.spec_for("sound_player")
    assert player.sound_inputs == {"sound"}
    assert node_specs.ports_compatible("sound", "out", "sound_player", "sound")
    assert not node_specs.ports_compatible("sound", "out", "sound_player", "in")
    assert not node_specs.ports_compatible("volume", "out", "sound_player", "sound")


def test_a_sound_edge_is_accepted_and_is_never_a_link():
    """The whole point of the pair: a Sound node plugs into a Sound Player's
    sound input, and that wire carries no audio (a real bug: the daemon's
    add_edge rejected the kind outright, so nothing could be plugged in)."""
    from tests.test_pwnodes import FakeGraph
    from pwnodes import PatchSpace, SoundNode, SoundPlayerNode

    g = FakeGraph()
    space = PatchSpace(g)
    space.mark_graph_loaded()
    space.add_node(SoundNode("snd", "~/sounds/boom.wav"))
    space.add_node(SoundPlayerNode("pl", "patchspace_pl"))
    space.add_edge("snd", "pl", to_port="sound")
    # A named port makes the edge id carry it (like a Filter's filter1).
    assert "snd->pl:sound" in space.edges
    # One sound per input, like the other single-driver control inputs.
    space.add_node(SoundNode("snd2", "~/other.wav"))
    with pytest.raises(ValueError):
        space.add_edge("snd2", "pl", to_port="sound")
    # A sound output never pairs with the impulse input.
    with pytest.raises(ValueError):
        space.add_edge("snd", "pl", to_port="in")
    space.sync()
    assert space._edge_links.get("snd->pl:sound") is None
    assert space.resolve_sound("pl") == {
        "path": "~/sounds/boom.wav", "start": 0.0, "end": None,
    }


def test_a_clip_narrows_the_sound_it_is_given():
    """A Clip is a range, so clips stack *intersecting* like the Filter
    node's filters do - and the range stays in the file's own time base."""
    from tests.test_pwnodes import FakeGraph
    from pwnodes import ClipNode, PatchSpace, SoundNode

    g = FakeGraph()
    space = PatchSpace(g)
    space.mark_graph_loaded()
    space.add_node(SoundNode("snd", "/sounds/long.wav"))
    space.add_node(ClipNode("c1", 10.0, 30.0))
    space.add_node(ClipNode("c2", 5.0, 20.0))
    space.add_node(ClipNode("c3", 0.0, 60.0))
    space.add_edge("snd", "c1", to_port="sound")
    space.add_edge("c1", "c2", to_port="sound")
    space.add_edge("c2", "c3", to_port="sound")
    # A clip's *input* is whatever reaches it: the raw sound for the first
    # one, c1's range (10..30) for the second.
    assert space.resolve_sound("c1", "sound") == {
        "path": "/sounds/long.wav", "start": 0.0, "end": None,
    }
    assert space.resolve_sound("c2", "sound") == {
        "path": "/sounds/long.wav", "start": 10.0, "end": 30.0,
    }
    # A clip's times are seconds into *what it is given*, so c2 asking for
    # 5..20 of c1's 10..30 is 15..30 of the file - and that is what the next
    # node sees.
    assert space.resolve_sound("c3", "sound") == {
        "path": "/sounds/long.wav", "start": 15.0, "end": 30.0,
    }


def test_a_waveform_is_reduced_to_peaks(tmp_path):
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    from pwnodes import PEAK_BUCKETS, probe_peaks

    path = tmp_path / "blip.wav"
    _write_wav(path, seconds=0.4)
    peaks = probe_peaks(str(path))
    assert 0 < len(peaks) <= PEAK_BUCKETS + 1
    # A loud tone must actually move the waveform, not read as silence.
    assert max(high for _low, high in peaks) > 0.1
    assert min(low for low, _high in peaks) < -0.1
    assert probe_peaks(str(tmp_path / "missing.wav")) == []
    assert probe_peaks("") == []


def test_a_daemon_clip_sees_the_sound_wired_into_it(tmp_path):
    """The whole chain, the way the GUI builds it: Sound -> Clip, then the
    timeline's payload and waveform.

    This is the regression that mattered: the daemon resolved the clip's input
    under the *old* port name, so the clip reported no source, drew a flat line
    and a player downstream had nothing to play - while the graph looked
    perfectly wired.  Wiring through the real port names is what pins it."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not available")
    from main import PatchSpaceDaemon
    from tests.test_pwnodes import FakeGraph

    path = tmp_path / "blip.wav"
    _write_wav(path, seconds=0.6)

    d = PatchSpaceDaemon()
    d.space.graph = FakeGraph()
    d.space.mark_graph_loaded()
    _ok = lambda **cmd: d.handle_command(cmd)  # noqa: E731
    assert _ok(command="add_node", node_type="sound", node_id="snd")["status"] == "ok"
    assert _ok(command="add_node", node_type="clip", node_id="clip")["status"] == "ok"
    assert _ok(command="add_node", node_type="sound_player", node_id="pl")["status"] == "ok"
    _ok(command="set_node_property", node_id="snd", property="path", value=str(path))
    assert _ok(command="add_edge", from_node="snd", to_node="clip",
               to_port="sound")["status"] == "ok"
    assert _ok(command="add_edge", from_node="clip", to_node="pl",
               to_port="sound")["status"] == "ok"

    # What the GUI is told about the clip: its source, and the file's length.
    node = _ok(command="get_nodes")["nodes"]["clip"]
    assert node["source_path"] == str(path)
    assert node["duration"] == pytest.approx(0.6, abs=0.05)

    # ...and the waveform it draws the timeline from.
    peaks = _ok(command="get_peaks", node_id="clip")
    assert peaks["status"] == "ok"
    assert peaks["path"] == str(path)
    assert len(peaks["peaks"]) > 10
    # A player wired behind the clip resolves the same sound, so it has
    # something to play when it fires.
    resolved = d.space.resolve_sound("pl")
    assert resolved["path"] == str(path)


def test_a_clips_times_are_settable_and_stick():
    """Dragging a Clip's handles (or typing into a box) has to survive the next
    poll.  The daemon didn't know the properties, so it answered an error, and
    the GUI's own value was replaced by the stale one on every refresh - the
    selection snapped straight back and the handles looked immovable."""
    from main import PatchSpaceDaemon
    from tests.test_pwnodes import FakeGraph

    d = PatchSpaceDaemon()
    d.space.graph = FakeGraph()
    d.space.mark_graph_loaded()
    assert d.handle_command(
        {"command": "add_node", "node_type": "clip", "node_id": "clip"}
    )["status"] == "ok"

    def set_prop(prop, value):
        return d.handle_command({"command": "set_node_property", "node_id": "clip",
                                 "property": prop, "value": value})

    def reported():
        node = d.handle_command({"command": "get_nodes"})["nodes"]["clip"]
        return node["start"], node["end"]

    assert set_prop("start", 2.5)["status"] == "ok"
    assert set_prop("end", 4.0)["status"] == "ok"
    assert reported() == (2.5, 4.0)
    # "To the end of the sound" is a real value, not an error.
    assert set_prop("end", None)["status"] == "ok"
    assert reported() == (2.5, None)
    # Garbage is refused rather than stored, and the old value survives.
    assert set_prop("start", "abc")["status"] == "error"
    assert reported() == (2.5, None)


class _FakeRecorder:
    """Just enough of OwnedPwProcess for a Recorder's take."""

    commands = []

    def __init__(self, name, command=("pw-cli",), settle=0.0, **kw):
        self.name = name
        self.alive = False

    def create(self, command, quiet=False):
        _FakeRecorder.commands.append(list(command))
        self.alive = True
        return True

    @property
    def is_alive(self):
        return self.alive

    def destroy(self):
        self.alive = False


def test_a_recorder_takes_an_audio_input_and_gives_a_sound(monkeypatch):
    """Record -> Stop, and the take is what the node's sound output points at,
    so a Clip or a Player behind it resolves it like any other sound."""
    from main import PatchSpaceDaemon
    from tests.test_pwnodes import FakeGraph

    _FakeRecorder.commands.clear()
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", _FakeRecorder)
    # A take reads the node's own sink: that link is now made explicitly,
    # because pw-cat's --target fell back to the *default source* (the
    # microphone) whenever it could not bind to the sink.
    linked = []
    monkeypatch.setattr(pwnodes, "_recorder_input_ports",
                        lambda name: [("_FL", f"{name}:input_FL")])
    monkeypatch.setattr(pwnodes, "_run_pw_link",
                        lambda out, inp: linked.append((out, inp)) or True)

    d = PatchSpaceDaemon()
    d.space.graph = FakeGraph()
    d.space.mark_graph_loaded()
    _ok = lambda **cmd: d.handle_command(cmd)  # noqa: E731
    assert _ok(command="add_node", node_type="recorder", node_id="rec")["status"] == "ok"

    node = d.space.nodes["rec"]
    assert node.port_kind("audio", "in") == "audio"
    assert node.port_kind("out", "out") == "sound"

    # Record: one pw-cat child, recording that node's own sink, to its take.
    resp = _ok(command="record", node_id="rec", recording=True)
    assert resp["recording"] is True
    assert node.take_path.endswith("rec.wav")
    assert _ok(command="get_nodes")["nodes"]["rec"]["recording"] is True
    command = _FakeRecorder.commands[0]
    assert command[0] == "pw-cat" and "--record" in command
    assert "--target" not in command            # nothing to silently fall back from
    assert any("autoconnect = false" in arg for arg in command)
    assert command[-1] == node.take_path
    assert linked == [(f"{node.backing_node_name}:monitor_FL",
                       f"{node.backing_node_name}_recording:input_FL")]

    # Stop: the child goes, and the play head reads idle again.
    resp = _ok(command="record", node_id="rec", recording=False)
    assert resp["recording"] is False
    assert node.recording is False
    assert _ok(command="get_nodes")["nodes"]["rec"]["recording"] is False

    # Its output is a sound, resolved the same way a Sound node's is: a player
    # wired to the recorder's *sound output* sees the take.
    assert _ok(command="add_node", node_type="sound_player",
               node_id="pl")["status"] == "ok"
    assert _ok(command="add_edge", from_node="rec", to_node="pl",
               to_port="sound")["status"] == "ok"
    assert d.space.resolve_sound("pl") == {
        "path": node.take_path, "start": 0.0, "end": None,
    }
    # Recording something that isn't a recorder is an error, not a crash.
    assert d.handle_command({"command": "record", "node_id": "nope"})["status"] == "error"


def test_recording_again_overwrites_the_take(tmp_path, monkeypatch):
    """One fixed file per node: Record clears it first, so a new take replaces
    the old one and the cached waveform/length can't be stale."""
    from main import PatchSpaceDaemon
    from tests.test_pwnodes import FakeGraph

    _FakeRecorder.commands.clear()
    monkeypatch.setattr(pwnodes, "OwnedPwProcess", _FakeRecorder)
    monkeypatch.setattr(pwnodes.RecorderNode, "RECORD_DIR", str(tmp_path))

    d = PatchSpaceDaemon()
    d.space.graph = FakeGraph()
    d.handle_command({"command": "add_node", "node_type": "recorder", "node_id": "rec"})
    node = d.space.nodes["rec"]

    take = pathlib.Path(node.take_path)
    take.write_bytes(b"old take")
    pwnodes._DURATION_CACHE[take.as_posix()] = 12.5
    pwnodes._PEAKS_CACHE[take.as_posix()] = [(0.0, 1.0)]

    d.handle_command({"command": "record", "node_id": "rec", "recording": True})
    assert not take.exists()                      # cleared before the new take
    d.handle_command({"command": "record", "node_id": "rec", "recording": False})
    # The caches for that path were dropped, or a re-record would report the
    # old shape (the path never changes).
    assert take.as_posix() not in pwnodes._DURATION_CACHE
    assert take.as_posix() not in pwnodes._PEAKS_CACHE


def test_a_recorders_export_is_json_serialisable(monkeypatch):
    """The whole point of the parameters: the session has to be writable.

    The recorder's take-controls were named `start`/`stop`, and `start` is a
    serialized parameter (the Clip's), so the export handed json.dump a bound
    method - the autosave raised, the supervision loop died with it, and every
    node sat "not connected" from then on.  One export check catches it."""
    import json
    from main import PatchSpaceDaemon
    from tests.test_pwnodes import FakeGraph

    d = PatchSpaceDaemon()
    d.space.graph = FakeGraph()
    d.space.mark_graph_loaded()
    d.handle_command({"command": "add_node", "node_type": "recorder", "node_id": "rec"})
    serialised = json.dumps(d._build_export_config())   # must not raise
    assert "rec" in serialised


def test_every_node_type_exports_to_json(monkeypatch):
    """The session is written as JSON, so anything a node exposes as a
    parameter has to survive json.dumps.  Walking the whole registry catches
    the whole class of bug (a method or a live object under a serialized
    name), not just the one that bit us."""
    import json

    import pwnodes as pn
    from main import NODE_TYPE_REGISTRY, PatchSpaceDaemon
    from tests.test_pwnodes import FakeGraph

    monkeypatch.setattr(pn, "OwnedPwNode", FakeCli)
    monkeypatch.setattr(pn, "OwnedPwProcess", FakeProc)

    for node_type in sorted(NODE_TYPE_REGISTRY):
        d = PatchSpaceDaemon()
        d.space.graph = FakeGraph()
        d.space.mark_graph_loaded()
        resp = d.handle_command({"command": "add_node", "node_type": node_type,
                                 "node_id": f"n_{node_type}"})
        if resp.get("status") != "ok":
            continue                      # needs a device/config it hasn't got
        json.dumps(d._build_export_config())    # must not raise


def test_source_rev_changes_when_a_take_overwrites_the_file(tmp_path):
    """The GUI's queue to re-ask for a waveform comes from the daemon's
    source_rev, so it has to move when a take rewrites the file - and must not
    move on its own, or every poll would re-ask for a few hundred peaks."""
    from main import PatchSpaceDaemon

    take = tmp_path / "take.wav"
    assert PatchSpaceDaemon._file_rev(str(take)) == ""      # no take yet

    take.write_bytes(b"first take")
    first = PatchSpaceDaemon._file_rev(str(take))
    assert first
    assert PatchSpaceDaemon._file_rev(str(take)) == first    # unchanged file

    take.write_bytes(b"a second, longer take")               # same path, new file
    assert PatchSpaceDaemon._file_rev(str(take)) != first


def test_a_take_reads_its_own_sink_and_fails_loudly_when_it_cannot(
        tmp_path, monkeypatch):
    """pw-cat with a --target it cannot bind to silently falls back to the
    *default source* and records the microphone - which is what the take used
    to do.  It now runs with autoconnect off and is linked to this node's
    monitor explicitly, and a take that cannot be linked reports failure
    rather than recording the wrong thing."""
    monkeypatch.setattr(pwnodes.RecorderNode, "RECORD_DIR", str(tmp_path))
    node = pwnodes.RecorderNode("rec", backing_node_name="rec_sink")
    commands = []

    class FakeProc:
        def __init__(self, *args, **kwargs):
            self.is_alive = True
        def create(self, command, quiet=False):
            commands.append(command)
            return True
        def destroy(self):
            self.is_alive = False

    monkeypatch.setattr(pwnodes, "OwnedPwProcess", FakeProc)
    monkeypatch.setattr(pwnodes, "_recorder_input_ports",
                        lambda name: [("_FL", f"{name}:input_FL")])

    linked = []
    monkeypatch.setattr(pwnodes, "_run_pw_link",
                        lambda out, inp: linked.append((out, inp)) or True)
    assert node.start_take() is True
    assert linked == [("rec_sink:monitor_FL", "rec_sink_recording:input_FL")]
    assert "--target" not in commands[-1]          # nothing to fall back from
    assert any("autoconnect = false" in a for a in commands[-1])
    node.stop_take()

    # No monitor to read: report failure instead of recording silence or
    # somebody else's stream.
    monkeypatch.setattr(pwnodes, "_run_pw_link", lambda out, inp: False)
    assert node.start_take() is False
    assert node.recording is False
    monkeypatch.setattr(pwnodes, "_recorder_input_ports", lambda name: [])
    assert node.start_take() is False
