"""Bundle wires, classifiers, filters and terminals.

Headless tests over the in-memory FakeGraph from test_pwnodes, plus the
port-kind/compatibility rules enforced by PatchSpace.add_edge.  No real
PipeWire is touched (the Bundle Output terminal's dummy is faked)."""

import pytest

from pwmatch import INTERNAL_MEDIA_CLASS
from pwnodes import (
    PatchSpace,
    InputNode,
    OutputNode,
    BackedNode,
    AllInputsNode,
    AllOutputsNode,
    AllAppsNode,
    RegexClassifierNode,
    MediaClassClassifierNode,
    AppClassifierNode,
    AppNameClassifierNode,
    FilterNode,
    TitleClassifierNode,
    BundleMergeNode,
    BundleSplitNode,
    BundleToAudioNode,
    BundleOutputNode,
)

from tests.test_pwnodes import FakeGraph


class SrcNode(InputNode):
    def __init__(self, node_id, name):
        super().__init__(node_id)
        self._name = name

    def source_filters(self):
        return [{"nodeName": self._name}]


class SinkNode(OutputNode):
    def __init__(self, node_id, name):
        super().__init__(node_id)
        self._name = name

    def sink_filters(self):
        return [{"name": self._name}]


class FakeBundleOutput(BundleOutputNode):
    """BundleOutputNode with a pre-resolved dummy (no real backing
    process); add_node's ensure_structural is a no-op."""

    def __init__(self, node_id, dummy_id):
        super().__init__(node_id, f"{node_id}_dummy")
        self._dummy_id = dummy_id

    def ensure_structural(self):
        pass

    def structural_ok(self):
        return True

    def sink_node_id(self):
        return self._dummy_id


def add_duplex(g, node_id, name, media_class=INTERNAL_MEDIA_CLASS):
    """A node with both playback (in) and monitor (out) ports, standing
    in for a null-audio-sink dummy in the FakeGraph."""
    g._nodes[node_id] = {
        "info": {"props": {"node.name": name, "media.class": media_class}}
    }
    out, inn = {}, {}
    for ch in ("FL", "FR"):
        pid = g._next_port
        g._next_port += 1
        g._ports[pid] = {"info": {"props": {
            "node.id": node_id, "port.direction": "out",
            "port.name": f"monitor_{ch}", "audio.channel": ch}}}
        out[ch] = pid
        pid2 = g._next_port
        g._next_port += 1
        g._ports[pid2] = {"info": {"props": {
            "node.id": node_id, "port.direction": "in",
            "port.name": f"playback_{ch}", "audio.channel": ch}}}
        inn[ch] = pid2
    return out, inn


def make_space(g):
    space = PatchSpace(g)
    space.mark_graph_loaded()
    return space


# ---------------------------------------------------------------------------
# port kinds / compatibility
# ---------------------------------------------------------------------------


def test_port_kinds():
    assert AllInputsNode("a").port_kind("out", "out") == "bundle"
    assert AllOutputsNode("a").port_kind("out", "out") == "bundle"
    f = FilterNode("f")
    assert f.port_kind("in", "in") == "bundle"
    assert f.port_kind("out", "out") == "bundle"
    assert f.port_kind("filter", "in") == "filter"
    assert RegexClassifierNode("c", "x").port_kind("out", "out") == "filter"
    assert RegexClassifierNode("c", "x").port_kind("out", "in") == "audio"


def test_add_edge_kind_compatibility():
    g = FakeGraph()
    g.add_source(1, "app")
    g.add_sink(2, "sink")
    space = PatchSpace(g)
    space.mark_graph_loaded()
    space.add_node(AllInputsNode("all"))
    space.add_node(FilterNode("f"))
    space.add_node(RegexClassifierNode("c", "x"))
    space.add_node(SinkNode("snk", "sink"))
    space.add_node(SrcNode("src", "app"))

    # bundle -> bundle and bundle <-> audio are fine.
    space.add_edge("all", "f")
    space.add_edge("f", "snk")
    space.add_edge("all", "src")  # bundle output -> audio input of a source? no-op path
    space.remove_edge("all->src")

    # filter only pairs with filter.
    with pytest.raises(ValueError):
        space.add_edge("snk", "f", to_port="filter")  # audio -> filter
    with pytest.raises(ValueError):
        space.add_edge("c", "snk")  # filter -> audio
    space.add_edge("c", "f", to_port="filter")


def _kept_ids(space, filter_id, ids):
    """Which of `ids` a Filter keeps - what it sums into its own dummy sink.

    The members no longer travel *on* from the node: they are mixed into the
    node's private sink and its output is that sink's monitor (see
    PatchSpace._filter_links), which a fake graph has no way to model.  So the
    filter's own decision is what these tests assert.
    """
    return space._apply_classifier(space.nodes[filter_id], "source", list(ids))


def test_all_inputs_filter_regex_routes_only_matching_source():
    g = FakeGraph()
    a = g.add_source(10, "alpha", app="alpha")
    b = g.add_source(11, "beta", app="beta")
    mic = g.add_source(12, "mic", media_class="Audio/Source")
    sink = g.add_sink(20, "sink1")
    s = make_space(g)
    s.add_node(AllInputsNode("all"))
    s.add_node(FilterNode("f"))
    s.add_node(RegexClassifierNode("c", "alpha"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("all", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "snk")
    # The classifier keeps alpha among everything All Inputs offers.
    assert _kept_ids(s, "f", [10, 11, 12]) == [10]


def test_title_classifier_selects_the_stream_by_title():
    """The title selector is a classifier like the others - it plugs into a
    Filter node's filter input and matches the stream's media.name."""
    g = FakeGraph()
    watched = g.add_source(10, "watched", app="firefox",
                           media_name="YouTube - a video")
    g.add_source(11, "other", app="firefox", media_name="Some other tab")
    # Same words in the *description*, different title: not a match.
    g.add_source(12, "titled_elsewhere", app="firefox",
                 media_name="Nothing here", description="YouTube")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(TitleClassifierNode("c", "youtube"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "snk")
    # Case-insensitive substring, on the title only.
    assert _kept_ids(s, "f", [10, 11, 12]) == [10]


def test_app_name_classifier_selects_the_app():
    g = FakeGraph()
    wanted = g.add_source(10, "wanted", app="Firefox")
    g.add_source(11, "other", app="Spotify")
    # Same words as the app name, but only in the *title*: not a match.
    g.add_source(12, "titled", app="mpv", media_name="Firefox")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(AppNameClassifierNode("c", "firefox"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "snk")
    assert _kept_ids(s, "f", [10, 11, 12]) == [10]


def test_app_classifier_takes_every_stream_the_app_created(monkeypatch):
    """Vesktop's audio comes from an Electron audio subprocess ("Chromium
    input") plus, say, a voice-engine stream; one Application value has to
    match both, where the Subprocess classifier would need each name."""
    import pwmatch

    monkeypatch.setattr(
        pwmatch, "_pid_app_scope",
        lambda pid: {11: "vesktop", 12: "vesktop"}.get(pid, ""),
    )
    g = FakeGraph()
    audio = g.add_source(10, "one", app="Chromium input", binary="electron", pid=11)
    voice = g.add_source(11, "two", app="WEBRTC VoiceEngine", binary="electron", pid=12)
    other = g.add_source(12, "three", app="LibreWolf", binary="librewolf", pid=13)
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(AppClassifierNode("c", "vesktop"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "snk")
    # One Application value keeps both of its subprocesses' streams.
    assert _kept_ids(s, "f", [10, 11, 12]) == [10, 11]


def test_filter_exclude_switch_flips_the_bundle_between_keep_and_drop():
    """One Filter node covers both senses: keep the members its classifiers
    match, or - switched to Exclude - everything else."""
    g = FakeGraph()
    watched = g.add_source(10, "watched", app="firefox",
                           media_name="YouTube - a video")
    other = g.add_source(11, "other", app="firefox",
                         media_name="Some other tab")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(TitleClassifierNode("c", "YouTube"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "snk")

    members = [10, 11]
    # Include (the default): the matching title is what the node feeds on.
    assert _kept_ids(s, "f", members) == [10]
    # Exclude: the same node drops exactly that member - and keeps the rest.
    s.nodes["f"].exclude = True
    assert _kept_ids(s, "f", members) == [11]


def test_filter_chain_intersects():
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f1"))
    s.add_node(FilterNode("f2"))
    s.add_node(RegexClassifierNode("c1", "alpha"))
    s.add_node(RegexClassifierNode("c2", "beta"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f1")
    s.add_edge("c1", "f1", to_port="filter")
    s.add_edge("f1", "f2")
    s.add_edge("c2", "f2", to_port="filter")
    s.add_edge("f2", "snk")
    # A second Filter narrows what the first one kept: alpha then beta is none.
    assert _kept_ids(s, "f1", [10, 11]) == [10]


def test_filter_chain_keeps_the_intersection():
    g = FakeGraph()
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f1"))
    s.add_node(FilterNode("f2"))
    s.add_node(MediaClassClassifierNode("c1", "Stream/Output/Audio"))
    s.add_node(RegexClassifierNode("c2", "beta"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f1")
    s.add_edge("c1", "f1", to_port="filter")
    s.add_edge("f1", "f2")
    s.add_edge("c2", "f2", to_port="filter")
    s.add_edge("f2", "snk")
    # Both nodes keep beta: the second Filter narrows what the first fed on.
    assert _kept_ids(s, "f1", [10, 11]) == [11]
    assert _kept_ids(s, "f2", [10, 11]) == [11]


def test_filter_ands_multiple_classifiers():
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(MediaClassClassifierNode("c1", "Stream/Output/Audio"))
    s.add_node(RegexClassifierNode("c2", "beta"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c1", "f", to_port="filter1")
    s.add_edge("c2", "f", to_port="filter2")
    s.add_edge("f", "snk")
    # Both classifiers have to match: only beta survives the AND.
    assert _kept_ids(s, "f", [10, 11]) == [11]


def test_filter_grows_an_input_per_classifier():
    g = FakeGraph()
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(RegexClassifierNode("c1", "a"))
    s.add_node(RegexClassifierNode("c2", "b"))
    assert s.filter_input_ports("f") == ["filter1"]
    s.add_edge("c1", "f", to_port="filter1")
    assert s.filter_input_ports("f") == ["filter1", "filter2"]
    s.add_edge("c2", "f", to_port="filter2")
    assert s.filter_input_ports("f") == ["filter1", "filter2", "filter3"]


def test_invert_classifier_complements():
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(RegexClassifierNode("c", "alpha", invert=True))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "snk")
    # The inverted classifier keeps everything *except* its match.
    assert _kept_ids(s, "f", [10, 11]) == [11]


def test_no_classifier_passes_bundle_through():
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("f", "snk")
    # Nothing wired to filter on: everything the bundle offers passes.
    assert _kept_ids(s, "f", [10, 11]) == [10, 11]


def test_bundle_to_audio_hands_on_through_its_own_sink():
    """The conversion point resolves to its *own* sink's monitor, not to the
    bundle's members: that is what makes the wire downstream stable, where
    changing the members used to re-point it (and re-initialise whatever it
    fed).  The members are summed into that sink instead."""
    g = FakeGraph()
    g.add_source(10, "alpha", app="alpha")
    g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(BundleToAudioNode("conv"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "conv")
    s.add_edge("conv", "snk")

    assert isinstance(s.nodes["conv"], BackedNode)
    assert s._resolve_sources("conv", "out", set()) == [
        {"nodeName": "bundle_audio_conv"}
    ]
    # The member still reaches the node's *input* side (the sink it sums into).
    assert s._resolve_sources("apps", "out", set()) == [
        {"mediaClassRegex": "^Stream/Output/Audio$"}
    ]


def test_excluding_a_stream_leaves_its_other_links_alone():
    """An Exclude drops a member from *that* node's own sink - it must not
    silence the app: the same stream may be routed by another part of the graph,
    or simply meant to keep playing.  (Taking its other links down was wrong:
    it dropped Vesktop's audio everywhere when only the chain past the filter
    was meant to lose it.)"""
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    sink = g.add_sink(20, "sink1")
    g.connect(alpha["FL"], sink["FL"])
    g.connect(alpha["FR"], sink["FR"])

    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f", exclude=True))
    s.add_node(AppNameClassifierNode("c", "alpha"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "f")
    s.add_edge("c", "f", to_port="filter1")
    s.add_edge("f", "snk")

    s.sync()
    # The node stops feeding it on...
    assert _kept_ids(s, "f", [10]) == []
    # ...and its own link, made elsewhere, is untouched.
    assert (alpha["FL"], sink["FL"]) in g.linked_pairs()


def test_bundle_merge_collects_multiple_inputs():
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(SrcNode("sa", "alpha"))
    s.add_node(SrcNode("sb", "beta"))
    s.add_node(BundleMergeNode("merge"))
    s.add_node(SinkNode("snk", "sink1"))
    # Several edges land on the one bus input.
    s.add_edge("sa", "merge")
    s.add_edge("sb", "merge")
    s.add_edge("merge", "snk")
    s.sync()
    links = g.linked_pairs()
    assert (alpha["FL"], sink["FL"]) in links
    assert (beta["FL"], sink["FL"]) in links


def test_bundle_merge_grows_an_input_per_connection():
    g = FakeGraph()
    g.add_source(10, "alpha", app="alpha")
    g.add_source(11, "beta", app="beta")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(SrcNode("sa", "alpha"))
    s.add_node(SrcNode("sb", "beta"))
    s.add_node(BundleMergeNode("merge"))
    # Starts with a single spare socket.
    assert s.bundle_input_ports("merge") == ["in1"]
    s.add_edge("sa", "merge", to_port="in1")
    # Plugging into the spare grows another one.
    assert s.bundle_input_ports("merge") == ["in1", "in2"]
    s.add_edge("sb", "merge", to_port="in2")
    assert s.bundle_input_ports("merge") == ["in1", "in2", "in3"]


def test_bundle_split_reports_members_and_routes_one_line():
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(BundleSplitNode("split"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "split")
    # The daemon reports one member per live source, keyed by node.name.
    members = s.bundle_members("split")
    ports = {m["port"] for m in members}
    assert ports == {"alpha", "beta"}
    assert all(m["label"] for m in members)

    # A single output line carries only that member.
    s.add_edge("split", "snk", from_port="alpha")
    s.sync()
    links = g.linked_pairs()
    assert (alpha["FL"], sink["FL"]) in links
    assert not any(o == beta["FL"] for o, _ in links)


def test_bundle_split_unknown_member_resolves_to_nothing():
    g = FakeGraph()
    g.add_source(10, "alpha", app="alpha")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(BundleSplitNode("split"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "split")
    s.add_edge("split", "snk", from_port="ghost")
    s.sync()
    assert g.linked_pairs() == set()


def test_bundle_side_reports_direction():
    s = PatchSpace(FakeGraph())
    s.mark_graph_loaded()
    s.add_node(AllInputsNode("i"))
    s.add_node(AllOutputsNode("o"))
    s.add_node(FilterNode("f"))
    s.add_node(RegexClassifierNode("c", "x"))
    s.add_edge("o", "f")
    s.add_edge("c", "f", to_port="filter")
    assert s.bundle_side("i") == "source"
    assert s.bundle_side("o") == "sink"
    assert s.bundle_side("f") == "sink"


def test_all_outputs_filter_and_bundle_output_routes_to_matched_sink():
    g = FakeGraph()
    src = g.add_source(10, "app", app="app")
    speaker = g.add_sink(20, "speaker")
    headphones = g.add_sink(21, "headphones")
    dummy_id = 30
    mon, play = add_duplex(g, dummy_id, "bundle_output_1")

    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(SrcNode("src", "app"))
    s.add_node(AllOutputsNode("outs"))
    s.add_node(FilterNode("f"))
    s.add_node(RegexClassifierNode("c", "speaker"))
    s.add_node(FakeBundleOutput("bo", dummy_id))
    s.add_edge("src", "bo")
    s.add_edge("outs", "f")
    s.add_edge("c", "f", to_port="filter")
    s.add_edge("f", "bo", to_port="bundle")
    s.sync()
    links = g.linked_pairs()
    # The source sums into the dummy...
    assert (src["FL"], play["FL"]) in links
    assert (src["FR"], play["FR"]) in links
    # ...and the dummy's monitor feeds only the matched sink.
    assert (mon["FL"], speaker["FL"]) in links
    assert (mon["FR"], speaker["FR"]) in links
    assert not any(i in (headphones["FL"], headphones["FR"]) for _, i in links)


def test_bundle_output_without_dummy_has_no_links():
    g = FakeGraph()
    g.add_source(10, "app", app="app")
    g.add_sink(20, "speaker")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(SrcNode("src", "app"))
    s.add_node(AllOutputsNode("outs"))
    s.add_node(FakeBundleOutput("bo", None))
    s.add_edge("src", "bo")
    s.add_edge("outs", "bo", to_port="bundle")
    s.sync()
    assert g.linked_pairs() == set()


# ---------------------------------------------------------------------------
# daemon registry / serialization
# ---------------------------------------------------------------------------


def test_daemon_registers_and_serializes_bundle_types():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    leaf_types = [
        "all_inputs",
        "all_outputs",
        "all_apps",
        "filter",
        "bundle",
        "bundle_split",
        "bundle_to_audio",
        "regex_classifier",
        "media_class_classifier",
        "description_classifier",
        "external_only_classifier",
    ]
    for node_type in leaf_types:
        resp = d.handle_command(
            {"command": "add_node", "node_type": node_type, "node_id": node_type}
        )
        assert resp["status"] == "ok", (node_type, resp)
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    for node_type in leaf_types:
        assert nodes[node_type]["type"] == node_type

    d.handle_command({
        "command": "add_node", "node_type": "regex_classifier",
        "node_id": "c1", "config": {"pattern": "Firefox", "invert": True},
    })
    c1 = d.handle_command({"command": "get_nodes"})["nodes"]["c1"]
    assert c1["pattern"] == "Firefox"
    assert c1["invert"] is True


def test_node_specs_declare_bundle_and_filter_ports():
    from gui import node_specs

    assert node_specs.port_kind("all_inputs", "out", "out") == "bundle"
    assert node_specs.port_kind("all_outputs", "out", "out") == "bundle"
    assert node_specs.port_kind("filter", "bundle", "in") == "bundle"
    assert node_specs.port_kind("filter", "out", "out") == "bundle"
    assert node_specs.port_kind("filter", "filter1", "in") == "filter"
    assert node_specs.port_kind("filter", "filter9", "in") == "filter"
    assert node_specs.port_kind("regex_classifier", "out", "out") == "filter"
    assert node_specs.port_kind("bundle_to_audio", "in", "in") == "bundle"
    assert node_specs.port_kind("bundle", "in1", "in") == "bundle"
    # Dynamic merge inputs (in2, ...) are bundles too.
    assert node_specs.port_kind("bundle", "in7", "in") == "bundle"
    assert node_specs.port_kind("bundle", "out", "out") == "bundle"
    assert node_specs.port_kind("bundle_split", "in", "in") == "bundle"
    # A Split Bundle's member outputs are ordinary audio.
    assert node_specs.port_kind("bundle_split", "alpha", "out") == "audio"
    assert node_specs.port_kind("bundle_output", "bundle", "in") == "bundle"
    # Every new type has a spec and a menu description.
    for node_type in (
        "all_inputs", "all_outputs", "all_apps", "filter", "bundle",
        "bundle_split", "bundle_to_audio", "bundle_output", "regex_classifier",
        "media_class_classifier", "description_classifier",
        "external_only_classifier",
    ):
        assert node_type in node_specs.NODE_TYPE_SPECS
        assert node_type in node_specs.NODE_DESCRIPTIONS


def test_node_specs_ports_compatible_matches_daemon_rules():
    from gui import node_specs

    # Bundle <-> audio either way.
    assert node_specs.ports_compatible("all_inputs", "out", "app_output", "in")
    assert node_specs.ports_compatible("app_input", "out", "filter", "in")
    # Filter only with filter.
    assert node_specs.ports_compatible("regex_classifier", "out", "filter", "filter")
    assert not node_specs.ports_compatible("regex_classifier", "out", "app_output", "in")
    # Boolean only with boolean.
    assert node_specs.ports_compatible("boolean_switch", "out", "gate", "ctrl")
    assert not node_specs.ports_compatible("all_inputs", "out", "gate", "ctrl")


def test_daemon_serializes_merge_bundle_inputs():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    d.handle_command({"command": "add_node", "node_type": "app_input",
                      "node_id": "src", "config": {"app_name": "x"}})
    d.handle_command({"command": "add_node", "node_type": "bundle",
                      "node_id": "m"})
    d.handle_command({"command": "add_edge", "from_node": "src",
                      "to_node": "m", "to_port": "in1"})
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["m"]["bundle_inputs"] == ["in1", "in2"]


def test_daemon_serializes_filter_inputs():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    d.handle_command({"command": "add_node", "node_type": "all_apps",
                      "node_id": "apps"})
    d.handle_command({"command": "add_node", "node_type": "filter",
                      "node_id": "f"})
    d.handle_command({"command": "add_node", "node_type": "regex_classifier",
                      "node_id": "c", "config": {"pattern": "x"}})
    d.handle_command({"command": "add_edge", "from_node": "c",
                      "to_node": "f", "to_port": "filter1"})
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["f"]["filter_inputs"] == ["filter1", "filter2"]


def test_daemon_switch_and_title_classifier_survive_set_node_property():
    """Both controls the GUI sends (the Filter node's Include/Exclude switch,
    the title classifier's box) go out as set_node_property, reach the daemon
    and are exported."""
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    d.handle_command({"command": "add_node", "node_type": "filter",
                      "node_id": "f"})
    d.handle_command({"command": "add_node", "node_type": "title_classifier",
                      "node_id": "c", "config": {"title": "YouTube"}})
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["f"]["exclude"] is False
    assert nodes["c"]["title"] == "YouTube"

    for node_id, prop, value, expected in (
        ("f", "exclude", True, True),
        ("c", "title", "some track", "some track"),
    ):
        res = d.handle_command({"command": "set_node_property", "node_id": node_id,
                                "property": prop, "value": value})
        assert res.get("status") != "error", res
        assert d.handle_command(
            {"command": "get_nodes"}
        )["nodes"][node_id][prop] == expected


def test_daemon_get_titles_lists_live_stream_titles():
    """The Title classifier's dropdown is built from these: the live streams'
    media.name, deduped and sorted - a device's media.name is left out, since
    it is a description rather than a title."""
    from main import PatchSpaceDaemon

    g = FakeGraph()
    g.add_source(10, "one", media_name="YouTube - a video")
    g.add_source(11, "two", media_name="YouTube - a video")   # same title
    g.add_source(12, "three", media_name="Spotify - a song")
    g.add_sink(20, "sink1", media_name="Built-in Audio Analog Stereo")
    d = PatchSpaceDaemon()
    d.graph = g
    resp = d.handle_command({"command": "get_titles"})
    assert resp["status"] == "ok"
    assert resp["titles"] == ["Spotify - a song", "YouTube - a video"]


def test_daemon_application_classifier_round_trips_its_name():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    d.handle_command({"command": "add_node", "node_type": "app_name_classifier",
                      "node_id": "c", "config": {"app_name": "Firefox"}})
    assert d.handle_command({"command": "get_nodes"})["nodes"]["c"]["app_name"] == "Firefox"
    res = d.handle_command({"command": "set_node_property", "node_id": "c",
                            "property": "app_name", "value": "Spotify"})
    assert res.get("status") != "error", res
    assert (d.handle_command({"command": "get_nodes"})["nodes"]["c"]["app_name"]
            == "Spotify")


def test_daemon_get_apps_lists_live_applications(monkeypatch):
    """The Application picker's list: app keys of the live streams, with
    Patch Space's own plumbing left out."""
    import pwmatch
    from main import PatchSpaceDaemon

    g = FakeGraph()
    g.add_source(10, "one", app="Chromium input", binary="electron", pid=11)
    g.add_source(11, "two", app="WEBRTC VoiceEngine", binary="electron", pid=12)
    g.add_source(12, "three", app="LibreWolf", binary="librewolf", pid=13)
    g.add_source(13, "keepalive", media_class="Stream/Output/Audio",
                 binary="pw-cat", pid=14)
    d = PatchSpaceDaemon()
    d.graph = g
    monkeypatch.setattr(
        pwmatch, "_pid_app_scope",
        lambda pid: {11: "vesktop", 12: "vesktop", 13: "librewolf"}.get(pid, ""),
    )
    # pw-cat is Patch Space's own plumbing, not an app.
    monkeypatch.setattr(
        pwmatch, "is_patchspace_owned",
        lambda props: props.get("application.process.binary") == "pw-cat",
    )
    resp = d.handle_command({"command": "get_apps"})
    assert resp["status"] == "ok"
    assert resp["apps"] == ["librewolf", "vesktop"]


def test_daemon_application_classifier_round_trips_its_key():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    d.handle_command({"command": "add_node", "node_type": "app_classifier",
                      "node_id": "c", "config": {"app_key": "vesktop"}})
    assert d.handle_command({"command": "get_nodes"})["nodes"]["c"]["app_key"] == "vesktop"
    res = d.handle_command({"command": "set_node_property", "node_id": "c",
                            "property": "app_key", "value": "discord"})
    assert res.get("status") != "error", res
    assert (d.handle_command({"command": "get_nodes"})["nodes"]["c"]["app_key"]
            == "discord")


def test_daemon_create_node_builds_bundle_output_without_starting_it():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    node = d._create_node("bundle_output", "bo", {})
    assert type(node).__name__ == "BundleOutputNode"
    assert node.backing_node_name


def test_daemon_load_migrates_legacy_source_leaf():
    from main import PatchSpaceDaemon

    d = PatchSpaceDaemon()
    config = {
        "nodes": {
            "old_in": {"type": "regex_input", "params": {"pattern": "Firefox"}},
        },
        "edges": [],
        "groups": [],
    }
    d._load_session(config, migrate=True)
    assert "old_in" not in d.space.nodes
    assert "all_inputs" in d.space.nodes
    assert any(
        type(n).__name__ == "RegexClassifierNode" for n in d.space.nodes.values()
    )
    assert any(type(n).__name__ == "FilterNode" for n in d.space.nodes.values())


def test_a_filter_hands_on_through_its_own_sink():
    """Like Bundle -> Audio: the Filter sums what it keeps into its own private
    sink and its output is that sink's monitor.  That is what makes an Exclude
    a change *inside* this chain - the wire downstream never moves and no other
    part of the graph is touched."""
    g = FakeGraph()
    g.add_source(10, "alpha", app="alpha")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("f"))
    s.add_edge("apps", "f")

    assert isinstance(s.nodes["f"], BackedNode)
    assert s._resolve_sources("f", "out", set()) == [{"nodeName": "filter_f"}]
    # Its own sink is what the kept members are summed into (the ``:sum``
    # bookkeeping entry exists as soon as the dummy resolves).
    assert set(s._filter_links(s.nodes["f"])) == {f"__internal__:f:sum"}


def test_a_split_cycle_does_not_recurse_for_ever():
    """A Split resolves through its upstream, which can be another Split.  The
    recursion used to run before the `seen` guard was initialised, so a *cycle*
    of them (the model allows one, even though the sync refuses to wire it)
    recursed until Python raised - and that call happens inside get_nodes,
    which the daemon serves holding its lock."""
    g = FakeGraph()
    g.add_source(10, "alpha")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(BundleSplitNode("split1"))
    s.add_node(BundleSplitNode("split2"))
    s.add_edge("split2", "split1")      # split1 resolves through split2 ...
    s.add_edge("split1", "split2")      # ... which resolves through split1

    assert s.bundle_members("split1") == []


def test_a_split_on_the_far_side_of_a_filter_sees_only_what_passed():
    """A Filter narrows a bundle and a Split downstream of it hands the lines
    on: the shape "on the other side of a filter node" that appeared not to
    work.  The split's members have to come from the *filter's* output (git
    only alpha), and the member must still route."""
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    sink = g.add_sink(20, "sink1")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("flt"))
    s.add_node(RegexClassifierNode("cls", "alpha"))
    s.add_node(BundleSplitNode("split"))
    s.add_node(SinkNode("snk", "sink1"))
    s.add_edge("apps", "flt")
    s.add_edge("cls", "flt", to_port="filter")
    s.add_edge("flt", "split")

    # The split offers a line per stream that *survived* the filter.
    assert {m["port"] for m in s.bundle_members("split")} == {"alpha"}

    s.add_edge("split", "snk", from_port="alpha")
    s.sync()
    links = g.linked_pairs()
    assert (alpha["FL"], sink["FL"]) in links
    assert not any(o == beta["FL"] for o, _ in links)


def _filter_links_with(exclude, classifier_key="vesktop"):
    """Build All Apps -> Filter(app classifier) -> its dummy sink and return
    (space, graph, sources, dummy ports, links) after a sync.

    The filter's *own* dummy is what the model sums its kept members into, so
    the graph has to carry it for the links to exist at all - the same reason
    the link-level behaviour had no test before."""
    g = FakeGraph()
    vesktop = g.add_source(10, "vesktop-stream", app="vesktop")
    other = g.add_source(11, "other-stream", app="librewolf")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("flt"))
    s.add_node(AppClassifierNode("cls", app_key=classifier_key))
    s.add_edge("apps", "flt")
    s.add_edge("cls", "flt", to_port="filter1")
    s.nodes["flt"].exclude = exclude
    dummy = g.add_sink(90, "filter_flt")
    # The sink id comes from the node's own backing registry, which needs a
    # spawned pw-cli to populate - the one piece a fake graph can't stand in
    # for.  Everything *downstream* of that (which members are summed, and the
    # channel pairs for them) is real.
    s.nodes["flt"].sink_node_id = lambda: 90
    s.sync()
    return s, g, vesktop, other, dummy, g.linked_pairs()


def test_a_filter_links_only_the_members_it_keeps():
    """Include mode keeps what matches, exclude mode keeps what doesn't - and
    the *links* have to say so.  What a Filter passes is its kept members summed
    into its own dummy sink, whose monitor is its output; the earlier tests
    asserted the decision, not the wiring, which is what the pipeline hears."""
    _s, _g, vesktop, other, dummy, links = _filter_links_with(exclude=False)

    assert (vesktop["FL"], dummy["FL"]) in links          # the match gets through
    assert not any(o == other["FL"] for o, _ in links)    # the rest does not


def test_an_excluding_filter_links_everything_but_the_match():
    """Exclude mode is the same pair the other way round: the members that
    *don't* match reach the output, and the matching one is dropped."""
    _s, _g, vesktop, other, dummy, links = _filter_links_with(exclude=True)

    assert (other["FL"], dummy["FL"]) in links
    assert not any(o == vesktop["FL"] for o, _ in links)


def test_a_filter_with_no_dummy_passes_nothing_at_all():
    """A Filter's output *is* its dummy sink's monitor, so with no dummy there
    is nothing to link and nothing gets through - however the switch is set.
    That is the state a vanished backing leaves behind, and why a filter can
    look healthy and be silently bypassing everything."""
    g = FakeGraph()
    g.add_source(10, "vesktop-stream", app="vesktop")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("flt"))
    s.add_node(AppClassifierNode("cls", app_key="vesktop"))
    s.add_edge("apps", "flt")
    s.add_edge("cls", "flt", to_port="filter1")

    assert s.nodes["flt"].sink_node_id() is None      # no backing has landed
    s.sync()
    assert g.linked_pairs() == set()            # nothing in, nothing out
    assert s._filter_links(s.nodes["flt"]) == {
        "__internal__:flt:sum": set()
    }


def test_a_filter_with_no_classifiers_passes_the_whole_bundle():
    """Nothing plugged into the filter inputs means the bundle passes through
    unchanged - the case that is easy to mistake for "the filter is broken"."""
    g = FakeGraph()
    alpha = g.add_source(10, "alpha", app="alpha")
    beta = g.add_source(11, "beta", app="beta")
    s = PatchSpace(g)
    s.mark_graph_loaded()
    s.add_node(AllAppsNode("apps"))
    s.add_node(FilterNode("flt"))
    s.add_edge("apps", "flt")
    dummy = g.add_sink(90, "filter_flt")
    s.nodes["flt"].sink_node_id = lambda: 90
    s.sync()

    links = g.linked_pairs()
    assert (alpha["FL"], dummy["FL"]) in links
    assert (beta["FL"], dummy["FL"]) in links
