"""The generic graph: connection rules (the single authority), placement
bookkeeping, edge ids that match the legacy scheme, and persistence."""

import pytest

from central import graph as g
from central.pwnodes import PatchSpace


def _kinds(mapping):
    def kind_of(instance_id, port, direction):
        return mapping.get((instance_id, port, direction), "")
    return kind_of


def _multi(mapping):
    def multi_of(instance_id, port):
        return mapping.get((instance_id, port), False)
    return multi_of


@pytest.mark.parametrize(
    "from_kind,to_kind,expected",
    [
        ("audio", "audio", True),
        ("audio", "bundle", True),
        ("bundle", "audio", True),
        ("bundle", "bundle", True),
        ("boolean", "boolean", True),
        ("number", "number", True),
        ("audio", "boolean", False),
        ("boolean", "audio", False),
        ("number", "audio", False),
        ("camera", "camera", True),
        ("camera", "audio", False),
        ("", "audio", False),
        ("audio", "", False),
    ],
)
def test_kinds_compatible(from_kind, to_kind, expected):
    assert g.kinds_compatible(from_kind, to_kind) is expected


@pytest.mark.parametrize(
    "from_node,to_node,to_port,from_port",
    [
        ("a", "b", "in", "out"),
        ("a", "b", "left", "out"),
        ("a", "b", "in", "right"),
        ("a", "b", "left", "right"),
    ],
)
def test_edge_ids_match_the_legacy_scheme(from_node, to_node, to_port, from_port):
    """Persisted edges carry these ids: the new authority must agree with
    PatchSpace._edge_id exactly, or phase 2 rewrites everyone's session."""
    assert (
        g.edge_id(from_node, to_node, to_port, from_port)
        == PatchSpace._edge_id(from_node, to_node, to_port, from_port)
    )


def test_the_common_edge_keeps_the_historical_form():
    assert g.edge_id("a", "b") == "a->b"
    assert g.edge_id("a", "b", "left") == "a->b:left"
    assert g.edge_id("a", "b", "in", "right") == "a->b@right"


def test_connect_refuses_self_wires_and_wrong_directions():
    graph = g.Graph()
    kinds = _kinds({("a", "out", "out"): "audio", ("a", "in", "in"): "audio"})
    ok, message = graph.can_connect("a", "a", "in", "out", kinds)
    assert not ok and "itself" in message
    ok, message = graph.can_connect("a", "a", "out", "in", kinds)
    assert not ok


def test_connect_refuses_a_kind_mismatch():
    graph = g.Graph()
    kinds = _kinds({
        ("btn", "out", "out"): "impulse",
        ("vol", "in", "in"): "audio",
        ("vol", "out", "out"): "audio",
    })
    ok, message = graph.can_connect("btn", "vol", "in", "out", kinds)
    assert not ok and "impulse" in message and "audio" in message
    ok, _ = graph.can_connect("vol", "btn", "in", "out", kinds)
    assert not ok


def test_single_driver_inputs_take_one_edge_but_multi_opt_out_takes_many():
    graph = g.Graph()
    kinds = _kinds({
        ("btn1", "out", "out"): "impulse",
        ("btn2", "out", "out"): "impulse",
        ("play", "in", "in"): "impulse",
        ("panel", "in", "in"): "impulse",
    })
    multi = _multi({("panel", "in"): True})

    graph.connect("btn1", "play", "in", "out", kinds, multi)
    ok, message = graph.can_connect("btn2", "play", "in", "out", kinds, multi)
    assert not ok and "already driven" in message

    graph.connect("btn1", "panel", "in", "out", kinds, multi)
    ok, _ = graph.can_connect("btn2", "panel", "in", "out", kinds, multi)
    assert ok, "a multi input accepts a second driver"


def test_audio_inputs_are_mixable():
    graph = g.Graph()
    kinds = _kinds({
        ("a", "out", "out"): "audio",
        ("b", "out", "out"): "audio",
        ("mix", "in", "in"): "audio",
    })
    graph.connect("a", "mix", "in", "out", kinds)
    ok, _ = graph.can_connect("b", "mix", "in", "out", kinds)
    assert ok, "audio sums; a second driver is the point"


def test_disconnect_by_id_and_by_endpoints():
    graph = g.Graph()
    kinds = _kinds({("a", "out", "out"): "audio", ("b", "in", "in"): "audio"})
    graph.connect("a", "b", "in", "out", kinds)
    assert graph.disconnect("a", "b", "in", "out") is True
    graph.connect("a", "b", "in", "out", kinds)
    assert graph.disconnect("a->b") is True
    assert graph.disconnect("a->b") is False


def test_forget_drops_the_placement_and_its_edges():
    graph = g.Graph()
    kinds = _kinds({
        ("a", "out", "out"): "audio",
        ("b", "in", "in"): "audio",
        ("b", "out", "out"): "audio",
        ("c", "in", "in"): "audio",
    })
    graph.place("b", x=10, y=20)
    graph.connect("a", "b", "in", "out", kinds)
    graph.connect("b", "c", "in", "out", kinds)
    assert sorted(graph.forget("b")) == ["a->b", "b->c"]
    assert graph.placement("b") is None
    assert graph.edges == {}


def test_removing_a_space_takes_its_nodes_and_edges_with_it():
    graph = g.Graph()
    graph.add_space("game", label="Game")
    kinds = _kinds({
        ("outside", "out", "out"): "audio",
        ("inside", "in", "in"): "audio",
    })
    graph.place("outside", space="main")
    graph.place("inside", space="game")
    graph.connect("outside", "inside", "in", "out", kinds)

    assert graph.remove_space("game") == ["inside"]
    assert graph.placement("inside") is None
    assert graph.edges == {}


def test_the_default_space_cannot_be_removed():
    graph = g.Graph()
    with pytest.raises(ValueError):
        graph.remove_space("main")


def test_persistence_round_trip():
    graph = g.Graph()
    graph.add_space("game", label="Game")
    graph.place("a", space="main", x=1.5, y=2.5, anchored=True)
    graph.place("b", space="game", x=3.0, y=4.0, panel="p1")
    kinds = _kinds({("a", "out", "out"): "audio", ("b", "in", "in"): "audio"})
    graph.connect("a", "b", "in", "out", kinds)

    restored = g.Graph()
    restored.load(graph.to_dict())
    assert set(restored.spaces) == {"main", "game"}
    assert restored.spaces["game"].label == "Game"
    assert restored.placement("a") == g.Placement("main", 1.5, 2.5, True, "")
    assert restored.placement("b") == g.Placement("game", 3.0, 4.0, False, "p1")
    assert restored.edges == graph.edges
