"""
Integration tests for every node type in the user's graph, each brought
up for real inside its *own* private PipeWire instance.

These are deliberately separate from the fast fake-graph suite: they
launch a private ``pipewire`` + ``wireplumber`` (isolated in a temp
runtime dir, never touching the user's real audio), build the real
``PipewireGraph`` against it, and drive the real node classes through
their structural/module bring-up and an actual audio round-trip.

They take a couple of minutes, so they are opt-in: the default
``pytest -q`` run skips them.  Run them with

    PATCHSPACE_RUN_INTEGRATION=1 python -m pytest -q tests/test_pipewire_integration.py

They are also skipped when pipewire, wireplumber or the DSP plugins
aren't available.

The graph under test is the one in ``~/patchspace_config.json``; the
node set and the edge/port cases below mirror it.
"""

import math
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import time
import wave

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "gui"))

import pwgraph  # noqa: E402
import pwmatch  # noqa: E402
import pwnodes  # noqa: E402

pytestmark = [
    pytest.mark.skipif(
        shutil.which("pipewire") is None or shutil.which("wireplumber") is None,
        reason="needs pipewire + wireplumber",
    ),
    pytest.mark.skipif(
        os.environ.get("PATCHSPACE_RUN_INTEGRATION") != "1",
        reason="slow live-PipeWire tests; set PATCHSPACE_RUN_INTEGRATION=1 to run",
    ),
]

# A headless server: no udev/ALSA/pulse, just the protocol, adapter and
# link factories plus the two dummy drivers the daemon's nodes need to be
# schedulable.  Same content as PipeWire's minimal.conf with the host
# integrations turned off, so it can never touch the real audio devices.
HEADLESS_CONF = """
context.properties = {
    core.daemon = true
    core.name = pipewire-0
    default.clock.rate = 48000
    settings.check-quantum = true
    settings.check-rate = true
}
context.spa-libs = {
    audio.convert.* = audioconvert/libspa-audioconvert
    audio.adapt     = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}
context.modules = [
    { name = libpipewire-module-rt
        args = { nice.level = -11 rt.prio = 88 }
        flags = [ ifexists nofail ] }
    { name = libpipewire-module-protocol-native }
    { name = libpipewire-module-profiler }
    { name = libpipewire-module-metadata }
    { name = libpipewire-module-spa-node-factory }
    { name = libpipewire-module-spa-device-factory }
    { name = libpipewire-module-client-node }
    { name = libpipewire-module-access args = { } flags = [ nofail ] }
    { name = libpipewire-module-adapter }
    { name = libpipewire-module-link-factory }
]
stream.properties = {
    adapter.auto-port-config = { mode = dsp }
}
context.objects = [
    { factory = metadata args = { metadata.name = default } }
    { factory = spa-node-factory
        args = { factory.name = support.node.driver node.name = Dummy-Driver
                 node.group = pipewire.dummy priority.driver = 20000 } }
    { factory = spa-node-factory
        args = { factory.name = support.node.driver node.name = Freewheel-Driver
                 priority.driver = 19000 node.group = pipewire.freewheel
                 node.freewheel = true } }
]
"""


def _devnull():
    return subprocess.DEVNULL


@pytest.fixture(scope="module")
def pw_instance():
    """A private pipewire + wireplumber in a temp runtime dir.  Sets the
    runtime env for the whole module so every subprocess the production
    code spawns (pw-dump/pw-cli/pw-link/pw-cat/pw-loopback) talks to the
    private instance, then restores it."""
    rt = tempfile.mkdtemp(prefix="patchspace-it-")
    conf = os.path.join(rt, "pipewire.conf")
    with open(conf, "w") as f:
        f.write(HEADLESS_CONF)
    env = dict(os.environ, PIPEWIRE_RUNTIME_DIR=rt, XDG_RUNTIME_DIR=rt)
    keys = ("PIPEWIRE_RUNTIME_DIR", "XDG_RUNTIME_DIR")
    saved = {k: os.environ.get(k) for k in keys}
    procs = []
    try:
        pw = subprocess.Popen(["pipewire", "-c", conf], env=env,
                              stdout=_devnull(), stderr=subprocess.PIPE)
        procs.append(pw)
        sock = os.path.join(rt, "pipewire-0")
        deadline = time.time() + 15
        while time.time() < deadline and not os.path.exists(sock):
            if pw.poll() is not None:
                pytest.skip("private pipewire exited during startup")
            time.sleep(0.1)
        if not os.path.exists(sock):
            pytest.skip("private pipewire socket never appeared")
        wp = subprocess.Popen(["wireplumber"], env=env,
                              stdout=_devnull(), stderr=_devnull())
        procs.append(wp)
        time.sleep(2)
        if wp.poll() is not None:
            pytest.skip("private wireplumber exited during startup")
        os.environ["PIPEWIRE_RUNTIME_DIR"] = rt
        os.environ["XDG_RUNTIME_DIR"] = rt
        yield rt
    finally:
        for p in reversed(procs):
            p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(rt, ignore_errors=True)


@pytest.fixture(scope="module")
def graph(pw_instance):
    g = pwgraph.PipewireGraph()
    g.start()
    deadline = time.time() + 10
    while not g._initial_dump_received and time.time() < deadline:
        time.sleep(0.05)
    if not g._initial_dump_received:
        g.stop()
        pytest.skip("no initial pw-dump snapshot from the private instance")
    yield g
    g.stop()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

# The backed node types that appear in the user's graph, with a factory
# that mirrors how main.py's registry constructs each one.
BACKED_TYPES = {
    "splitter": lambda i, b: pwnodes.SplitterNode(i, b),
    "volume": lambda i, b: pwnodes.VolumeProcessNode(i, b),
    "virtual_speaker": lambda i, b: pwnodes.VirtualSpeakerNode(i, b, "vs"),
    "virtual_mic": lambda i, b: pwnodes.VirtualMicNode(i, b, "vm"),
    "echo_cancel": lambda i, b: pwnodes.EchoCancelNode(i, b),
    "light_noise_cancel": lambda i, b: pwnodes.LightNoiseCancelNode(i, b),
    "noise_cancel": lambda i, b: pwnodes.NoiseCancelNode(i, b),
    "normalize": lambda i, b: pwnodes.NormalizeNode(i, b),
    "sensitivity_gate": lambda i, b: pwnodes.SensitivityGateNode(i, b),
}

EFFECT_TYPES = (
    "echo_cancel",
    "light_noise_cancel",
    "noise_cancel",
    "normalize",
    "sensitivity_gate",
)


def _ready(node):
    if not isinstance(node, pwnodes.BackedNode):
        return True
    return (
        node.structural_ok()
        and (not node.has_module() or node.module_ok())
        and all(b.node_id is not None for b in node.backings)
    )


def _diag(node):
    if not isinstance(node, pwnodes.BackedNode):
        return f"{type(node).__name__}: transparent"
    return (
        f"{type(node).__name__}: structural_ok={node.structural_ok()} "
        f"module_ok={node.module_ok() if node.has_module() else 'n/a'} "
        f"backings={[(b.name, b.node_id) for b in node.backings]}"
    )


def bring_up(space, node, timeout=25.0):
    if not isinstance(node, pwnodes.BackedNode):
        return True
    deadline = time.time() + timeout
    while time.time() < deadline:
        with space._lock:
            space._supervise_node(node)
        if _ready(node):
            return True
        time.sleep(0.1)
    return _ready(node)


def sync_until(space, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        with space._lock:
            space.sync_locked()
        if predicate():
            return True
        time.sleep(0.2)
    return predicate()


def new_space(graph):
    space = pwnodes.PatchSpace(graph)
    space.mark_graph_loaded()
    return space


# ---------------------------------------------------------------------------
# every node type comes up on its own
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("node_type", sorted(BACKED_TYPES))
def test_backed_node_type_comes_up(graph, node_type):
    space = new_space(graph)
    node = BACKED_TYPES[node_type](node_type, f"it_{node_type}")
    space.add_node(node)
    assert bring_up(space, node), _diag(node)
    assert node.structural_ok(), _diag(node)
    assert all(b.node_id is not None for b in node.backings), _diag(node)
    if node.has_module():
        assert node.module_ok(), _diag(node)


@pytest.mark.parametrize("node_type", EFFECT_TYPES)
def test_effect_sandwich_interior_wires(graph, node_type):
    """A filter-chain/echo-cancel effect's dummy -> module -> dummy
    interior must actually connect, not just exist."""
    space = new_space(graph)
    node = BACKED_TYPES[node_type](node_type, f"fx_{node_type}")
    space.add_node(node)
    assert bring_up(space, node), _diag(node)
    assert sync_until(space, lambda: space.node_internals_wired(node_type)), (
        f"{node_type} interior never wired: "
        f"{ {k: v.pairs for k, v in space._edge_links.items()} }"
    )


# ---------------------------------------------------------------------------
# the user's mic chain, end to end
# ---------------------------------------------------------------------------


def test_mic_chain_topology_wires(graph):
    """Build the mic chain from ~/patchspace_config.json (stand-ins for
    the real hardware endpoints) and assert every edge becomes a live
    link - including the tricky bits: echo-cancel's mic/probe ports, the
    inverse-switcher on/off selection, a legacy 'a' port, the boolean
    ctrl wiring, and the volume -> Mic Line tail."""
    space = new_space(graph)

    # The built-ins the aliases resolve to.
    builtin_mic = pwnodes.VirtualMicNode(
        "__builtin_mic__", "Patch Space Mic", "Patch Space Mic"
    )
    builtin_sink = pwnodes.VirtualSpeakerNode(
        "__builtin_sink__", "Patch Space", "Patch Space"
    )
    for builtin in (builtin_mic, builtin_sink):
        space.add_node(builtin, public=False)
        assert bring_up(space, builtin), _diag(builtin)

    # Mic source stand-in (the graph's device_input), and the nodes.
    src = pwnodes.SplitterNode("src", "it_src")
    ec_in = pwnodes.SplitterNode("ec_in", "it_ec_in")
    speaker = pwnodes.PatchSpaceDeviceNode("speaker")            # probe source
    echo = pwnodes.EchoCancelNode("echo", "it_echo")
    echo_toggle = pwnodes.InverseSwitcherNode("echo_toggle", output=1)
    legacy_toggle = pwnodes.InverseSwitcherNode("legacy_toggle", output=1)
    volume = pwnodes.VolumeProcessNode("boost", "it_boost")
    mic_line = pwnodes.PatchSpaceMicDeviceNode("mic_line")
    switch = pwnodes.BooleanSourceNode("force", output=1)

    for node in (src, ec_in, speaker, echo, echo_toggle, legacy_toggle,
                 volume, mic_line, switch):
        space.add_node(node)
    for node in (src, ec_in, echo, volume):
        assert bring_up(space, node), _diag(node)

    # The chain + every port case from the graph.
    space.add_edge("src", "ec_in")                              # splitter -> splitter
    space.add_edge("ec_in", "echo", to_port="mic")             # -> echo.mic
    space.add_edge("speaker", "echo", to_port="probe")         # -> echo.probe
    space.add_edge("echo", "echo_toggle", to_port="on")        # effect -> .on
    space.add_edge("ec_in", "echo_toggle", to_port="off")      # splitter -> .off
    space.add_edge("echo_toggle", "boost")                     # toggle -> volume
    space.add_edge("boost", "mic_line")                        # volume -> Mic Line
    space.add_edge("force", "echo_toggle", to_port="ctrl")     # boolean ctrl
    space.add_edge("echo", "legacy_toggle", to_port="a")       # legacy port name

    ok = sync_until(
        space,
        lambda: all(space.edge_wired(e) for e in space.edges),
    )
    assert ok, "edges not wired: " + repr(
        {e: space.edge_wired(e) for e in space.edges}
    )
    # And the effect's own interior connected too.
    assert sync_until(space, lambda: space.node_internals_wired("echo")), (
        "echo interior never wired"
    )


def test_boolean_control_network_wires(graph):
    """The graph's control plane: switches -> AND/OR/INVERT -> bool warp
    -> a switcher's ctrl.  Carries no audio, but must resolve end to
    end."""
    space = new_space(graph)

    noisew = pwnodes.BooleanSourceNode("noise_suppress", output=0)
    ai = pwnodes.BooleanSourceNode("ai", output=0)
    use_ai = pwnodes.BooleanAndNode("use_ai")
    invert = pwnodes.BooleanInvertNode("inv")
    use_basic = pwnodes.BooleanAndNode("use_basic")
    warp_in = pwnodes.BooleanWarpInNode("basic_in", "basic_noise_cancel")
    warp_out = pwnodes.BooleanWarpOutNode("basic_warp", "basic_noise_cancel")
    toggle = pwnodes.InverseSwitcherNode("basic_toggle", output=1)

    for node in (noisew, ai, use_ai, invert, use_basic, warp_in, warp_out, toggle):
        space.add_node(node)

    space.add_edge("noise_suppress", "use_ai", to_port="a")
    space.add_edge("ai", "use_ai", to_port="b")
    space.add_edge("ai", "inv")
    space.add_edge("noise_suppress", "use_basic", to_port="a")
    space.add_edge("inv", "use_basic", to_port="b")
    space.add_edge("use_basic", "basic_in")
    space.add_edge("basic_warp", "basic_toggle", to_port="ctrl")

    # Boolean edges never become PipeWire links; "wired" here means the
    # topology resolves without error and the ctrl input sees a value.
    with space._lock:
        space.sync_locked()
    assert toggle.bool_driven
    # noise_suppress=0 -> AND(0, NOT ai) = False.
    assert toggle.bool_state is False


# ---------------------------------------------------------------------------
# push real audio through each node
# ---------------------------------------------------------------------------

RATE = 48000
CHANNELS = 2
TONE_HZ = 1000.0
TONE_AMP = 0.6
TONE_SECONDS = 20.0
TONE_PEAK = int(TONE_AMP * 32767)  # ~19660


@pytest.fixture(scope="module")
def tone(pw_instance):
    path = os.path.join(pw_instance, "au_tone.wav")
    n = int(RATE * TONE_SECONDS)
    with wave.open(path, "wb") as w:
        w.setnchannels(CHANNELS)
        w.setsampwidth(2)
        w.setframerate(RATE)
        frames = bytearray()
        for i in range(n):
            v = int(TONE_AMP * 32767 * math.sin(2 * math.pi * TONE_HZ * i / RATE))
            for _ in range(CHANNELS):
                frames += struct.pack("<h", v)
        w.writeframes(bytes(frames))
    return path


def _peak(path):
    with wave.open(path, "rb") as w:
        data = w.readframes(w.getnframes())
    if len(data) < 2:
        return 0
    vals = struct.unpack("<%dh" % (len(data) // 2), data)
    return max(abs(v) for v in vals)


def _audio_node(node_type, nid, backing):
    """A node configured to pass a 1 kHz tone (gates opened, denoiser
    VAD forced open, sensitivity gate bypassing attenuation)."""
    if node_type == "noise_cancel":
        return pwnodes.NoiseCancelNode(nid, backing, vad_threshold=0.0)
    if node_type == "sensitivity_gate":
        return pwnodes.SensitivityGateNode(nid, backing, sensitivity=1.0, range_db=0.0)
    return BACKED_TYPES[node_type](nid, backing)


def _build_audio_chain(space, node_id):
    """src -> node -> dst, with both ends `VolumeProcessNode`s whose
    monitors give us a clean 'speaker out' to play into and record from.
    Returns the (src_id, dst_id) node ids."""
    src = pwnodes.VolumeProcessNode(f"asrc_{node_id}", f"au_{node_id}_src")
    dst = pwnodes.VolumeProcessNode(f"adst_{node_id}", f"au_{node_id}_dst")
    for n in (src, dst):
        space.add_node(n)
    space.add_edge(src.id, node_id)
    space.add_edge(node_id, dst.id)
    return src.id, dst.id


def _node_id(graph, name, timeout=4.0):
    end = time.time() + timeout
    while time.time() < end:
        nid = graph.node_id_by_name(name)
        if nid is not None:
            return nid
        time.sleep(0.05)
    return None


def _pairs_active(graph, pairs):
    want = {(p.get("info", {}).get("props", {}).get("link.output.port"),
             p.get("info", {}).get("props", {}).get("link.input.port"),
             p.get("info", {}).get("state"))
            for p in graph.all_objects().values()
            if p.get("type") == "PipeWire:Interface:Link"}
    for out_port, in_port in pairs:
        if (out_port, in_port, "active") not in want:
            return False
    return True


def _link_active(graph, src_name, dst_name, timeout=4.0):
    """Explicitly link src -> dst and wait until every channel pair is
    actually `active`, not merely present (a freshly-created link is
    `init` until the graph schedules it)."""
    end = time.time() + timeout
    pairs = set()
    while time.time() < end:
        src = _node_id(graph, src_name, 0.5)
        dst = _node_id(graph, dst_name, 0.5)
        if src is not None and dst is not None:
            pairs = pwmatch.resolve_channel_pairs(graph, src, dst, None)
            if pairs:
                for pair in pairs:
                    graph.connect(*pair)
                break
        time.sleep(0.05)
    if not pairs:
        return False
    end = time.time() + timeout
    while time.time() < end:
        if _pairs_active(graph, pairs):
            return True
        time.sleep(0.05)
    return False


def _measure_once(graph, play_backing, rec_backing, tone_path, rt, tag, seconds,
                  wait_pairs=None):
    rec = os.path.join(rt, f"au_rec_{tag}.wav")
    # Start playback first: a running client stream drives the graph, so
    # the node's own internal links come up before we attach the recorder.
    playp = subprocess.Popen(
        ["pw-cat", "--playback",
         "--properties", f'{{ node.name="au_play_{tag}" node.autoconnect=false }}',
         "--format", "s16", "--rate", str(RATE), "--channels", str(CHANNELS),
         tone_path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        assert _link_active(graph, f"au_play_{tag}", play_backing), "play link not active"
        if wait_pairs:
            end = time.time() + 6.0
            while time.time() < end and not _pairs_active(graph, wait_pairs):
                time.sleep(0.05)
        # Let the node's module actually start processing before we tap it.
        time.sleep(0.5)
        recp = subprocess.Popen(
            ["pw-cat", "--record",
             "--properties",
             f'{{ node.name="au_rec_{tag}" node.autoconnect=false }}',
             "--format", "s16", "--rate", str(RATE), "--channels", str(CHANNELS), rec],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            assert _link_active(graph, rec_backing, f"au_rec_{tag}"), "record link not active"
            time.sleep(seconds)
        finally:
            recp.terminate()
            recp.wait()
    finally:
        playp.terminate()
        playp.wait()
    return _peak(rec)


def _measure(graph, play_backing, rec_backing, tone_path, rt, tag, seconds=2.0,
             attempts=2, wait_pairs=None):
    """Play the tone into one named node and record another named node's
    monitor; returns the recorded peak amplitude.

    Routing is explicit and waited on until the links are *active* (both
    probe streams run with autoconnect off and are wired with pw-link), so
    the result never depends on WirePlumber's target policy.  A 0 result
    is retried - the private instance is shared across the module and a
    probe occasionally races a slow snapshot."""
    peak = 0
    for attempt in range(attempts):
        peak = _measure_once(
            graph, play_backing, rec_backing, tone_path, rt,
            f"{tag}_{attempt}", seconds, wait_pairs,
        )
        if peak > 0:
            break
    return peak


def _play_through(graph, node_id, tone_path, rt, seconds=2.0, wait_pairs=None):
    return _measure(
        graph, f"au_{node_id}_src", f"au_{node_id}_dst", tone_path, rt, node_id,
        seconds, wait_pairs=wait_pairs,
    )


def _wire_audio(space, timeout=15.0):
    return sync_until(
        space, lambda: all(space.edge_wired(e) for e in space.edges), timeout
    )


def _edge_pairs(space, edge_id):
    entry = space._edge_links.get(edge_id)
    return set(entry.pairs) if entry else set()


def _sync_until_pairs(space, edge_id, predicate, timeout=6.0):
    """Re-run sync_locked until `predicate(pairs)` holds (the graph
    snapshot lags a just-resolved node by a cycle)."""
    end = time.time() + timeout
    while time.time() < end:
        with space._lock:
            space.sync_locked()
        pairs = _edge_pairs(space, edge_id)
        if predicate(pairs):
            return pairs
        time.sleep(0.2)
    return _edge_pairs(space, edge_id)


def _wait_pairs_active(graph, pairs, timeout=6.0):
    end = time.time() + timeout
    while time.time() < end:
        if _pairs_active(graph, pairs):
            return True
        time.sleep(0.05)
    return _pairs_active(graph, pairs)


def _port_node(graph, port_id):
    for obj in graph.all_objects().values():
        if obj.get("type") == "PipeWire:Interface:Port" and obj.get("id") == port_id:
            return obj.get("info", {}).get("props", {}).get("node.id")
    return None


AUDIO_NODE_TYPES = {
    "splitter": 15000,
    "volume": 15000,
    "virtual_speaker": 15000,
    "virtual_mic": 15000,
    # AEC / Normalize reshape or attenuate an uncorrelated 1 kHz tone; the
    # point here is that signal still passes (silence would be <200).
    "echo_cancel": 500,
    "light_noise_cancel": 500,
    "normalize": 5000,
    "sensitivity_gate": 15000,
}


def _identity_backing(ident):
    return ident.get("nodeName") or ident.get("name")


@pytest.mark.parametrize("node_type", sorted(AUDIO_NODE_TYPES))
def test_audio_flows_through_node(graph, pw_instance, tone, node_type):
    """The real signal path, not just structure: play a 1 kHz tone into
    the node's own input socket and record its output socket."""
    space = new_space(graph)
    node = _audio_node(node_type, node_type, f"au_{node_type}")
    space.add_node(node)
    assert bring_up(space, node), _diag(node)
    if isinstance(node, pwnodes.VolumeProcessNode):
        node.set_volume(1.0)
        time.sleep(0.3)
        node.set_volume(1.0)  # re-push in case the first landed pre-activation
    with space._lock:
        space.sync_locked()
    if node.internal_links():
        assert sync_until(space, lambda: space.node_internals_wired(node_type), 15), (
            f"{node_type} interior never wired"
        )

    in_backing = _identity_backing(node.input_identity("in"))
    out_backing = _identity_backing(node.output_identity())
    peak = _measure(graph, in_backing, out_backing, tone, pw_instance, node_type)
    assert peak >= AUDIO_NODE_TYPES[node_type], (
        f"{node_type} passed no/too little audio: peak={peak}"
    )


def test_noise_cancel_attenuates_a_non_voiced_tone(graph, pw_instance, tone):
    """RNNoise is a speech denoiser, so a pure 1 kHz tone is (correctly)
    treated as noise and attenuated - unlike the other effects, asserting
    it passes a tone unchanged would be wrong.  This checks the module is
    genuinely running and actually doing its job."""
    space = new_space(graph)
    node = pwnodes.NoiseCancelNode("noise_cancel", "au_noise_cancel", vad_threshold=0.0)
    space.add_node(node)
    assert bring_up(space, node), _diag(node)
    assert node.module_ok(), _diag(node)
    with space._lock:
        space.sync_locked()
    assert sync_until(space, lambda: space.node_internals_wired("noise_cancel"), 15)

    in_backing = _identity_backing(node.input_identity("in"))
    out_backing = _identity_backing(node.output_identity())
    peak = _measure(graph, in_backing, out_backing, tone, pw_instance, "noise_cancel")
    # The raw tone peak is ~19660; a working denoiser drops it well below.
    assert peak < 10000, f"noise_cancel did not attenuate the tone (peak={peak})"


def test_volume_scales_then_mutes_audio(graph, pw_instance, tone):
    space = new_space(graph)
    node = pwnodes.VolumeProcessNode("vol", "au_vol")
    space.add_node(node)
    assert bring_up(space, node), _diag(node)
    with space._lock:
        space.sync_locked()
    backing = "au_vol"

    node.set_volume(1.0)
    full = _measure(graph, backing, backing, tone, pw_instance, "vol_full")
    assert full >= 15000

    # The slider is a Pulse-style volume: 0.5 maps to a cubic ~0.125 gain.
    node.set_volume(0.5)
    half = _measure(graph, backing, backing, tone, pw_instance, "vol_half")
    assert 0.05 * full <= half <= 0.25 * full, f"half={half} full={full}"

    node.set_volume(0.0)
    muted = _measure(graph, backing, backing, tone, pw_instance, "vol_mute")
    assert muted < 200, f"volume 0 still passed {muted}"


def test_gate_routes_audio_only_when_open(graph, pw_instance, tone):
    """A gate is transparent: open it and its downstream edge resolves to
    the source (an active link), close it and the link is torn down.  The
    routing is asserted at the link level because that is exactly what the
    gate controls; the closed phase also proves silence at the sink."""
    space = new_space(graph)
    gate = pwnodes.GateNode("gate", enabled=True)
    switch = pwnodes.BooleanSourceNode("gate_sw", output=1)
    space.add_node(gate)
    space.add_node(switch)
    src_id, dst_id = _build_audio_chain(space, "gate")
    space.add_edge("gate_sw", "gate", to_port="ctrl")
    assert bring_up(space, space.nodes[src_id]), _diag(space.nodes[src_id])
    assert bring_up(space, space.nodes[dst_id]), _diag(space.nodes[dst_id])
    assert _wire_audio(space)

    switch.output = 1
    pairs = _sync_until_pairs(space, "gate->adst_gate", bool)
    assert pairs, "open gate routed nothing downstream"
    assert _wait_pairs_active(graph, pairs), "open gate link never became active"

    switch.output = 0
    pairs = _sync_until_pairs(space, "gate->adst_gate", lambda p: not p)
    assert pairs == set(), "closed gate still wants a downstream link"
    assert _play_through(graph, "gate", tone, pw_instance) < 200


def test_inverse_switcher_selects_the_configured_input(graph, pw_instance, tone):
    """On/off selection must actually choose which source reaches the
    output: the downstream link is sourced from the selected input."""
    space = new_space(graph)
    on_src = pwnodes.VolumeProcessNode("on_src", "au_on_src")
    off_src = pwnodes.VolumeProcessNode("off_src", "au_off_src")
    toggle = pwnodes.InverseSwitcherNode("tog", output=1)
    dst = pwnodes.VolumeProcessNode("tog_dst", "au_tog_dst")
    for node in (on_src, off_src, toggle, dst):
        space.add_node(node)
    space.add_edge("on_src", "tog", to_port="on")
    space.add_edge("off_src", "tog", to_port="off")
    space.add_edge("tog", "tog_dst")
    assert bring_up(space, on_src), _diag(on_src)
    assert bring_up(space, off_src), _diag(off_src)
    assert bring_up(space, dst), _diag(dst)
    assert _wire_audio(space)

    on_node = graph.node_id_by_name("au_on_src")
    off_node = graph.node_id_by_name("au_off_src")

    # Select "on": the downstream link is fed by the on-source.
    pairs = _sync_until_pairs(
        space, "tog->tog_dst",
        lambda p: bool(p) and all(_port_node(graph, out) == on_node for out, _ in p),
    )
    assert pairs and all(_port_node(graph, out) == on_node for out, _ in pairs), (
        f"toggle selected the wrong input: {pairs} (on={on_node})"
    )

    # Select "off": the downstream link follows the off-source.
    toggle.output = 0
    pairs = _sync_until_pairs(
        space, "tog->tog_dst",
        lambda p: bool(p) and all(_port_node(graph, out) == off_node for out, _ in p),
    )
    assert pairs and all(_port_node(graph, out) == off_node for out, _ in pairs), (
        f"toggle did not switch inputs: {pairs} (off={off_node})"
    )

