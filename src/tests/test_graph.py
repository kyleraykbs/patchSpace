"""The generic graph: connection rules (the single authority), placement
bookkeeping, edge ids that match the legacy scheme, and persistence."""

import pytest

from central import graph as g
from modules.pipewire.pwnodes import PatchSpace


def _kinds(mapping):
    def kind_of(instance_id, port, direction):
        return mapping.get((instance_id, port, direction), "")
    return kind_of


def _cardinality(mapping):
    def cardinality_of(instance_id, port, direction):
        return mapping.get((instance_id, port), "many")
    return cardinality_of


@pytest.mark.parametrize(
    "from_kind,to_kind,expected",
    [
        ("audio", "audio", True),
        ("audio", "bundle", True),
        ("bundle", "audio", True),
        ("bundle", "bundle", True),
        ("boolean", "boolean", True),
        ("number", "number", True),
        ("text", "text", True),
        ("choice", "choice", True),
        ("audio", "boolean", False),
        ("boolean", "audio", False),
        ("number", "audio", False),
        # The value kinds pair only with themselves (§19), so a `choice`
        # port pairs with a `choice` port and nothing else.
        ("text", "choice", False),
        ("choice", "text", False),
        ("number", "text", False),
        ("choice", "boolean", False),
        # An undeclared kind pairs with nothing — not even itself: the
        # vocabulary is what a pair is judged against (§19).
        ("widget", "widget", False),
        ("widget", "audio", False),
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


def test_an_input_declared_one_takes_a_single_driver():
    """Cardinality is *declared* per port (S4, agents/SPACES.md §15): a
    port that declares ``one`` refuses the second driver."""
    graph = g.Graph()
    kinds = _kinds({
        ("btn1", "out", "out"): "impulse",
        ("btn2", "out", "out"): "impulse",
        ("play", "in", "in"): "impulse",
    })
    one = _cardinality({("play", "in"): "one"})

    graph.connect("btn1", "play", "in", "out", kinds, one)
    ok, message = graph.can_connect("btn2", "play", "in", "out", kinds, one)
    assert not ok and "already driven" in message


def test_a_port_declaring_many_takes_several_even_on_a_single_driver_kind():
    """The step's whole point: impulse is single-driver by default, and a
    port that *can* take several says so instead of being restricted by
    its kind's name."""
    graph = g.Graph()
    kinds = _kinds({
        ("btn1", "out", "out"): "impulse",
        ("btn2", "out", "out"): "impulse",
        ("space", "in", "in"): "impulse",
    })
    many = _cardinality({("space", "in"): "many"})

    graph.connect("btn1", "space", "in", "out", kinds, many)
    ok, message = graph.can_connect("btn2", "space", "in", "out", kinds, many)
    assert ok, message


def test_a_mixable_kind_can_still_be_declared_to_take_one():
    """The declaration is the authority in both directions: audio is
    mixable by default, so only a declaration restricts it."""
    graph = g.Graph()
    kinds = _kinds({
        ("a", "out", "out"): "audio",
        ("b", "out", "out"): "audio",
        ("mix", "in", "in"): "audio",
    })
    one = _cardinality({("mix", "in"): "one"})

    graph.connect("a", "mix", "in", "out", kinds, one)
    ok, message = graph.can_connect("b", "mix", "in", "out", kinds, one)
    assert not ok and "already driven" in message


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


def test_the_graph_holds_no_kind_list_of_its_own():
    """The pre-S4 `SINGLE_DRIVER_KINDS` is gone: how many drivers a port
    takes is the caller's answer (the registry's resolution), and without
    a resolver the graph restricts nothing but the kind mismatch."""
    assert not hasattr(g, "SINGLE_DRIVER_KINDS")
    graph = g.Graph()
    kinds = _kinds({
        ("b1", "out", "out"): "impulse",
        ("b2", "out", "out"): "impulse",
        ("btn", "in", "in"): "impulse",
    })
    graph.connect("b1", "btn", "in", "out", kinds)
    ok, message = graph.can_connect("b2", "btn", "in", "out", kinds)
    assert ok, message


def test_edges_from_finds_what_a_producer_feeds():
    """The consumer side of the value relay looks a producer's reported port
    up here (agents/SPACES.md §14), the mirror of `edges_into`."""
    graph = g.Graph()
    kinds = _kinds({
        ("n1", "out", "out"): "number",
        ("s1", "gain", "in"): "number",
        ("s2", "in", "in"): "number",
    })
    graph.connect("n1", "s1", "gain", "out", kinds)
    graph.connect("n1", "s2", "in", "out", kinds)

    assert [e.to_node for e in graph.edges_from("n1")] == ["s1", "s2"]
    assert [e.to_node for e in graph.edges_from("n1", "out")] == ["s1", "s2"]
    assert graph.edges_from("n1", "other") == []
    assert graph.edges_from("s1") == []
    assert graph.edges_from("nobody") == []


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
    graph.place("b", space="game", x=3.0, y=4.0, space_id="p1")
    kinds = _kinds({("a", "out", "out"): "audio", ("b", "in", "in"): "audio"})
    graph.connect("a", "b", "in", "out", kinds)

    restored = g.Graph()
    restored.load(graph.to_dict())
    assert set(restored.spaces) == {"main", "game"}
    assert restored.spaces["game"].label == "Game"
    assert restored.placement("a") == g.Placement("main", 1.5, 2.5, True, "")
    assert restored.placement("b") == g.Placement("game", 3.0, 4.0, False, "p1")
    assert restored.edges == graph.edges
