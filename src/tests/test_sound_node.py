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
