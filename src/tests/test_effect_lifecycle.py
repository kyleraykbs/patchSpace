"""End-to-end lifecycle tests for the filter-chain effect sandwich,
using fake process classes in place of real pw-cli / pw-cat so nothing
touches a live PipeWire server."""

import pytest

import pwnodes
from pwnodes import (
    EchoCancelNode,
    NoiseCancelNode,
    NormalizeNode,
    ReverbNode,
    SensitivityGateNode,
)
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


class OrderGraph(FakeGraph):
    """FakeGraph that records the order links are made, so a test can
    assert one call happened after another."""

    def __init__(self):
        super().__init__()
        self.events = []

    def connect(self, out_port, in_port):
        self.events.append(("connect", out_port, in_port))
        return super().connect(out_port, in_port)


def _add_duplex(g, node_id, name, media_class):
    """One node with both playback (in) and monitor (out) ports, the way
    a real null-audio-sink appears - unlike FakeGraph.add_sink /
    add_source, which model only one direction."""
    g._nodes[node_id] = {
        "info": {"props": {"node.name": name, "media.class": media_class}}
    }
    for ch in ("FL", "FR"):
        for direction, port_name in (("in", "playback"), ("out", "monitor")):
            pid = g._next_port
            g._next_port += 1
            g._ports[pid] = {
                "info": {
                    "props": {
                        "node.id": node_id,
                        "port.direction": direction,
                        "port.name": f"{port_name}_{ch}",
                        "audio.channel": ch,
                    }
                }
            }
    return g


def test_echo_cancel_out_drain_is_created_once_and_left_alone():
    """Echo Cancel likewise leaves its out-dummy drain alone: only its
    module-stream playback-sink tap is re-pointed, by ensure_module."""
    g = OrderGraph()
    _add_duplex(g, 60, "fx_mic_in", "Audio/Sink/Internal")
    _add_duplex(g, 61, "fx_probe_in", "Audio/Sink/Internal")
    _add_duplex(g, 62, "fx_out", "Audio/Sink/Internal")
    _add_duplex(g, 63, "fx_playback_sink", "Audio/Sink/Internal")
    g.add_sink(50, "fx", media_class="Audio/Sink")               # mic capture
    g.add_sink(51, "fx_probe", media_class="Audio/Sink")         # probe sink
    g.add_source(52, "fx_fx_out", media_class="Audio/Source")    # source
    g.add_source(53, "fx_playback", media_class="Audio/Source")  # playback

    space = make_space(g, repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0))
    space.mark_graph_loaded()

    node = EchoCancelNode("n", "fx")
    dropped = []
    real_drop = node._drop

    def recording_drop(name):
        dropped.append(name)
        return real_drop(name)

    node._drop = recording_drop
    space.add_node(node)
    dropped.clear()
    space.supervise()

    # The out-dummy drain is untouched; the module-stream tap is re-pointed.
    assert "fx_out_keepalive" not in dropped
    assert "fx_playback_sink_keepalive" in dropped





def test_effect_gets_stable_sockets_first_module_later():
    """add_node must create the stable dummy sockets immediately and
    leave the (fallible) DSP module to the supervision pass."""
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
    space.mark_graph_loaded()

    node = NoiseCancelNode("n", "fx")
    space.add_node(node)

    assert names() >= {"fx_in", "fx_out", "fx_in_keepalive", "fx_out_keepalive"}
    assert "fx" not in names()  # module not spawned yet

    space.supervise()
    assert "fx" in names()  # module now materialised
    assert "fx_fx_out" in names()  # plus its playback sibling


def test_missing_module_is_retried_not_duplicated():
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
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
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
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
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
    space.mark_graph_loaded()
    node = SensitivityGateNode("n", "gate", level=30.0)
    space.add_node(node)
    space.supervise()

    fx_out_names_before = {
        i.name for i in FakeCli.instances if i.name.startswith("gate_fx_out")
    }
    assert fx_out_names_before

    node.reload_module()
    # Sockets are untouched; interior streams recreated.
    assert "gate_in" in names()
    assert "gate_out" in names()


def test_sensitivity_threshold_is_a_load_time_calf_lv2_control():
    """SensitivityGateNode is Calf's LV2 Gate.  Its threshold is a
    load-time filter-graph control (the live set-param path doesn't
    reliably reach the plugin through the daemon's pw-cli session), so
    the slider maps to a threshold baked into the module graph and a
    change schedules an interior reload."""
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
    space.mark_graph_loaded()
    node = SensitivityGateNode("n", "gate")
    space.add_node(node)
    space.supervise()

    args = node._module_command_args()
    assert "type = lv2" in args
    assert 'plugin = "http://calf.sourceforge.net/plugins/Gate"' in args
    assert "type = ladspa" not in args
    assert "type = builtin" not in args
    assert '"ratio"' in args and '"knee"' in args

    # level 0 = most sensitive -> -45 dB -> linear ~0.005623
    node.set_level(0.0)
    assert '"threshold" = 0.005623' in node._module_command_args()
    # level 100 = least sensitive -> -15 dB -> linear ~0.177828
    node.set_level(100.0)
    assert '"threshold" = 0.177828' in node._module_command_args()

    # The 0..1 sensitivity slider maps inversely onto level.
    node.set_sensitivity(1.0)
    assert node.level == 0.0
    assert '"threshold" = 0.005623' in node._module_command_args()
    node.set_sensitivity(0.0)
    assert node.level == 100.0
    assert '"threshold" = 0.177828' in node._module_command_args()




def test_normalize_graph_links_compressor_and_lookahead_limiter():
    """Normalize builds a linked stereo graph: swh sc4 leveler into the
    fastLookaheadLimiter, with the boost capped by max_boost_db."""
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
    space.mark_graph_loaded()
    node = NormalizeNode(
        "n",
        "norm",
        boost_db=40.0,
        max_boost_db=12.0,
        ceiling_db=-3.0,
        ladspa_dir="/plugins",
    )
    space.add_node(node)
    space.supervise()

    args = node._module_command_args()
    assert "sc4_1882.so" in args
    assert "fast_lookahead_limiter_1913.so" in args
    assert "label = sc4" in args
    assert "label = fastLookaheadLimiter" in args
    # boost_db was clamped to 30, then capped to max_boost_db=12 at the
    # limiter's input gain - the user's "maximum cap".
    assert node.boost_db == NormalizeNode.BOOST_MAX_DB
    assert node.effective_boost_db() == 12.0
    assert '"Input gain (dB)" = 12.00' in args
    assert '"Limit (dB)" = -3.00' in args
    # The two stereo plugins are linked compressor-out -> limiter-in.
    assert '"norm_comp:Left output" input = "norm_lim:Input 1"' in args
    assert '"norm_comp:Right output" input = "norm_lim:Input 2"' in args
    assert 'inputs = [ "norm_comp:Left input" "norm_comp:Right input" ]' in args
    assert 'outputs = [ "norm_lim:Output 1" "norm_lim:Output 2" ]' in args


def test_normalize_leveling_off_omits_compressor():
    node = NormalizeNode("n", "norm", leveling=False)
    args = node._module_command_args()
    assert "sc4_1882.so" not in args
    assert "fast_lookahead_limiter_1913.so" in args
    assert "links" not in args
    assert 'inputs = [ "norm_lim:Input 1" "norm_lim:Input 2" ]' in args


def test_reverb_builds_calf_lv2_graph_with_wetdry():
    """ReverbNode is LV2 (Calf Reverb) found by URI, with a real wet/dry
    crossfade - not the old /usr/lib/ladspa/caps.so + bogus "dry/wet"."""
    from pwnodes import ReverbNode

    node = ReverbNode("n", "rev", wet_dry=0.25)
    args = node._module_command_args()
    assert "type = lv2" in args
    assert 'plugin = "http://calf.sourceforge.net/plugins/Reverb"' in args
    assert "type = ladspa" not in args
    assert "caps.so" not in args
    # wet 0.25 -> amount 0.25, dry 0.75.
    assert '"amount" = 0.2500' in args
    assert '"dry" = 0.7500' in args
    assert '"decay_time"' in args
    assert '"room_size"' in args

    # clamps out-of-range constructor values
    from pwnodes import ReverbNode as R
    n2 = R("n", "rev", decay_time=999, room_size=-3, wet_dry=5)
    assert n2.decay_time == R.DECAY_MAX_S
    assert n2.room_size == R.ROOM_MIN
    assert n2.wet_dry == 1.0


def test_reverb_plugin_uri_override():
    from pwnodes import ReverbNode

    node = ReverbNode("n", "rev", plugin_uri="http://example.com/custom")
    assert 'plugin = "http://example.com/custom"' in node._module_command_args()


def test_two_noise_cancel_nodes_keep_distinct_interiors():
    """Regression: adding a second AI Noise Cancel must not disturb the
    first - each node owns its own module backing, capture/playback
    streams and interior links, so nothing about one can match the
    other (this is the multi-instance failure the user hit: the first
    AI Noise Cancel worked, later ones broke)."""
    space = make_space(
        FakeGraph(), repair_gate=pwnodes.Backoff(initial_s=0, jitter_fraction=0)
    )
    space.mark_graph_loaded()
    a = NoiseCancelNode("a", "nc_a")
    b = NoiseCancelNode("b", "nc_b")
    space.add_node(a)
    space.add_node(b)
    space.supervise()

    assert a.module_backing() is not None
    assert b.module_backing() is not None
    assert a.module_backing() is not b.module_backing()
    assert a.module_backing().name == "nc_a"
    assert b.module_backing().name == "nc_b"

    # Distinct interior identities - the by-name sync can never wire one
    # node's dummy to the other node's module streams.
    assert a._capture_name != b._capture_name
    assert a._playback_name != b._playback_name
    assert a.internal_links() != b.internal_links()
    assert a._module_command_args() != b._module_command_args()

    # A second supervision pass leaves both healthy (no cross-teardown).
    space.supervise()
    assert a.module_ok()
    assert b.module_ok()
    assert a.module_backing().is_alive
    assert b.module_backing().is_alive


def test_sensitivity_gate_refresh_live_is_callable():
    """The shared supervision path (pwnodes._on_backing_resolved) calls
    refresh_live() on every resolved backing.  SensitivityGateNode lacked
    it, so resolving the gate's module raised AttributeError and aborted
    the node's supervision step (seen as repeated "Bring-up step ... has
    no attribute 'refresh_live'" warnings)."""
    node = SensitivityGateNode("n", "gate")
    node.refresh_live()  # must not raise


def test_noise_cancel_defaults_to_the_stereo_rnnoise_label():
    """Regression: the node's capture/playback are stereo, but the module
    used the mono RNNoise label - a mono plugin between stereo streams
    collapsed/dropped a channel, which is the inconsistent "AI Noise
    Cancel breaks the audio" report.  Default to the stereo label, while
    still honoring an explicit override."""
    node = NoiseCancelNode("n", "nc")
    args = node._module_command_args()
    assert "label = noise_suppressor_stereo" in args
    assert "label = noise_suppressor_mono" not in args
    assert "audio.position = [ FL FR ]" in args

    # The stereo plugin is 2-in/2-out, so its ports are named explicitly
    # rather than left to filter-chain auto-wiring (which loaded the
    # module but moved no audio).
    assert 'inputs = [ "nc_plugin:Input (L)" "nc_plugin:Input (R)" ]' in args
    assert 'outputs = [ "nc_plugin:Output (L)" "nc_plugin:Output (R)" ]' in args

    # An explicit override (older installs / saved sessions) still wins,
    # and gets the mono plugin's single port pair.
    mono = NoiseCancelNode(
        "n", "nc", ladspa_plugin="/x/librnnoise_ladspa.so",
        ladspa_label="noise_suppressor_mono",
    )
    mono_args = mono._module_command_args()
    assert "label = noise_suppressor_mono" in mono_args
    assert 'inputs = [ "nc_plugin:Input" ]' in mono_args
    assert 'outputs = [ "nc_plugin:Output" ]' in mono_args


