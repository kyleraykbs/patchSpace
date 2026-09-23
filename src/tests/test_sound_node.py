"""The Sound node, and the *sound* signal kind it introduces.

A sound is a file of known length carried on its own port kind: no audio
crosses it, it is a reference (file + range) that a Sound Player fires and a
Clip node slices.  Its length is probed once per path, which is what lets the
timeline and the node's read-out talk about time in a file nobody has opened.

Needs no GTK; the duration test needs ffprobe (skipped without it)."""

import os
import shutil
import struct
import sys
import wave

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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
    space.add_edge("snd", "c1", to_port="in")
    space.add_edge("c1", "c2", to_port="in")
    space.add_edge("c2", "c3", to_port="in")
    # A clip's *input* is whatever reaches it: the raw sound for the first
    # one, c1's range (10..30) for the second.
    assert space.resolve_sound("c1", "in") == {
        "path": "/sounds/long.wav", "start": 0.0, "end": None,
    }
    assert space.resolve_sound("c2", "in") == {
        "path": "/sounds/long.wav", "start": 10.0, "end": 30.0,
    }
    # A clip's times are seconds into *what it is given*, so c2 asking for
    # 5..20 of c1's 10..30 is 15..30 of the file - and that is what the next
    # node sees.
    assert space.resolve_sound("c3", "in") == {
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
