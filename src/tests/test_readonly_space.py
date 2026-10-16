"""S10: a declarative space's interior is uneditable (agents/SPACES.md §20).

The rule is the daemon's, and every refusal here names what it refused and
leaves the graph exactly as it was: deleting a declared node, renaming it,
changing its properties or its stored position, and (un)wiring an edge whose
*both* endpoints are declared.  What stays allowed is the other half of the
same sentence: an edge that crosses the boundary in either direction
("plug things into it"), a declarative space as a portal target, a space
instantiated *from* a read-only template, and every edit in a read-write
space.

Leaf node types only, so no PipeWire starts.
"""

import os
from pathlib import Path

from central import module_api as api
from central import spaces
from central.graph import Graph
from central.main import PatchSpaceDaemon
from central.protocol import Connection, ProtocolHandler
from central.registry import Registry


def _daemon(tmp_path):
    pdir = tmp_path / "spaces"
    pdir.mkdir()
    root = tmp_path / "root.json"
    d = PatchSpaceDaemon(
        space_dirs=[(str(pdir), True)],
        root_space_path=str(root),
    )
    return d, str(root), str(pdir)


def _space(space_id, pdir, *, mode, nodes, edges, label="", type=spaces.LIVE):
    space = spaces.Space(
        id=space_id, parent="", label=label or space_id, color="#abc", mode=mode,
        type=type,
        path=os.path.join(pdir, space_id + ".json"), writable=True,
        config={"nodes": nodes, "edges": edges, "spaces": [], "groups": []},
    )
    spaces.write_file(space.path, space)
    return space


def _filter_node(pattern, x=10, y=10):
    return {"type": "regex_input", "params": {"x": x, "y": y, "pattern": pattern}}


def _sink_node(pattern, x=20, y=10):
    return {"type": "regex_output", "params": {"x": x, "y": y, "pattern": pattern}}


def _declarative_and_writable(tmp_path):
    """A declarative space with an interior and an edge of its own, a
    read-write space with the same, a node outside both, and the imperative
    edge that crosses the boundary into the declarative space."""
    d, root, pdir = _daemon(tmp_path)
    _space(
        "frozen", pdir, mode=spaces.MODE_RO, label="Frozen",
        # `b` is already wired from `a` (declared wiring); `b2` is free, so a
        # *new* edge between two declared nodes has somewhere to go.
        nodes={"a": _filter_node("a"), "b": _sink_node("b"),
               "b2": _sink_node("b2", y=40)},
        edges=[{"from": "a", "to": "b"}],
    )
    _space(
        "kit", pdir, mode=spaces.MODE_RW, label="Kitchen",
        nodes={"a": _filter_node("c"), "b": _sink_node("d")},
        edges=[{"from": "a", "to": "b"}],
    )
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode=spaces.MODE_RW,
        writable=True,
        config={"nodes": {"out": _sink_node("o", x=500)},
                "edges": [{"from": "frozen::a", "to": "out"}],
                "spaces": ["frozen", "kit"], "groups": []},
    ))
    d._install_spaces(d._load_spaces_tree())
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)
    assert d.spaces["frozen"].is_readonly
    assert (d.space.nodes["frozen::a"].pattern, d.space.nodes["frozen::b"].pattern) == ("a", "b")
    return d, root, pdir


def _edge_id(d, from_node, to_node):
    """The daemon's stored id for the edge between two nodes."""
    found = [
        eid for eid, edge in d.space.edges.items()
        if edge.from_node == from_node and edge.to_node == to_node
    ]
    assert len(found) == 1, (from_node, to_node, sorted(d.space.edges))
    return found[0]


# ---------------------------------------------------------------------------
# the refusals
# ---------------------------------------------------------------------------


def test_deleting_a_declared_node_is_refused(tmp_path):
    """`remove_node` on a node the declaring file writes down."""
    d, root, pdir = _declarative_and_writable(tmp_path)
    before = Path(os.path.join(pdir, "frozen.json")).read_text()

    resp = d._cmd_remove_node({"node_id": "frozen::a"})

    assert resp["status"] == "error", resp
    assert "space 'frozen' is read-only" in resp["message"]
    assert "node 'frozen::a'" in resp["message"]
    assert "deleted" in resp["message"]
    assert resp["node_id"] == "frozen::a"
    assert "frozen::a" in d.space.nodes
    assert Path(os.path.join(pdir, "frozen.json")).read_text() == before


def test_renaming_a_declared_node_is_refused(tmp_path):
    """`rename_node` on a declared node, and a rename that would re-home one
    *into* the declarative space (the reparent the move refusal refuses)."""
    d, root, pdir = _declarative_and_writable(tmp_path)

    resp = d._cmd_rename_node(
        {"old_node_id": "frozen::a", "new_node_id": "frozen::renamed"}
    )
    assert resp["status"] == "error", resp
    assert "space 'frozen' is read-only" in resp["message"]
    assert "node 'frozen::a'" in resp["message"]
    assert "renamed" in resp["message"]
    assert "frozen::a" in d.space.nodes and "frozen::renamed" not in d.space.nodes

    resp = d._cmd_rename_node(
        {"old_node_id": "out", "new_node_id": "frozen::out"}
    )
    assert resp["status"] == "error", resp
    assert "out" in resp["message"] and "frozen::out" in resp["message"]
    assert "space 'frozen' is read-only" in resp["message"]
    assert "out" in d.space.nodes and "frozen::out" not in d.space.nodes


def test_changing_a_declared_nodes_property_is_refused(tmp_path):
    """`set_node_property` - the node's own values are the file's."""
    d, root, pdir = _declarative_and_writable(tmp_path)

    resp = d._cmd_set_node_property(
        {"node_id": "frozen::a", "property": "pattern", "value": "changed"}
    )

    assert resp["status"] == "error", resp
    assert "space 'frozen' is read-only" in resp["message"]
    assert "node 'frozen::a'" in resp["message"]
    assert "'pattern'" in resp["message"]
    assert "changed" in resp["message"]
    assert d.space.nodes["frozen::a"].pattern == "a"


def test_changing_a_declared_nodes_position_is_refused(tmp_path):
    """`set_node_layout` - a declared node's stored position is the file's.
    The GUI sends the whole canvas in one command, so the editable entries in
    the same map still apply."""
    d, root, pdir = _declarative_and_writable(tmp_path)
    before = (d.space.nodes["frozen::a"].x, d.space.nodes["frozen::a"].y)

    resp = d._cmd_set_node_layout({"layout": {
        "frozen::a": {"x": 999.0, "y": 999.0, "anchored": True},
        "kit::a": {"x": 111.0, "y": 222.0, "anchored": False},
        "out": {"x": 333.0, "y": 444.0, "anchored": False},
    }})

    assert resp["status"] == "ok", resp
    refused = resp["refused"]
    assert len(refused) == 1
    assert "space 'frozen' is read-only" in refused[0]
    assert "node 'frozen::a'" in refused[0] and "position" in refused[0]
    assert (d.space.nodes["frozen::a"].x, d.space.nodes["frozen::a"].y) == before
    # ...and the read-write entries in the same command were applied.
    assert (d.space.nodes["kit::a"].x, d.space.nodes["kit::a"].y) == (111.0, 222.0)
    assert (d.space.nodes["out"].x, d.space.nodes["out"].y) == (333.0, 444.0)


def test_wiring_two_declared_nodes_is_refused(tmp_path):
    """`add_edge` when *both* endpoints are declared: the declared wiring is
    the file's."""
    d, root, pdir = _declarative_and_writable(tmp_path)

    resp = d._cmd_add_edge(
        {"from_node": "frozen::a", "to_node": "frozen::b2",
         "to_port": "in", "from_port": "out"}
    )

    assert resp["status"] == "error", resp
    assert "space 'frozen' is read-only" in resp["message"]
    assert "'frozen::a' -> 'frozen::b2'" in resp["message"]
    assert "wired" in resp["message"]
    assert not [
        e for e in d.space.edges.values()
        if e.from_node == "frozen::a" and e.to_node == "frozen::b2"
    ]


def test_unwiring_two_declared_nodes_is_refused(tmp_path):
    """`remove_edge` on an edge the declaring file writes down."""
    d, root, pdir = _declarative_and_writable(tmp_path)
    declared = _edge_id(d, "frozen::a", "frozen::b")

    resp = d._cmd_remove_edge({"edge_id": declared})

    assert resp["status"] == "error", resp
    assert "space 'frozen' is read-only" in resp["message"]
    assert "'frozen::a' -> 'frozen::b'" in resp["message"]
    assert "unwired" in resp["message"]
    assert declared in d.space.edges


def test_the_declarative_space_that_owns_the_edge_is_named(tmp_path):
    """Two declarative spaces at once: the refusal names both, because both
    endpoints are declared and neither space is the only one that owns the
    wiring."""
    d, root, pdir = _daemon(tmp_path)
    _space("one", pdir, mode=spaces.MODE_RO, nodes={"a": _filter_node("a")},
           edges=[])
    _space("two", pdir, mode=spaces.MODE_RO, nodes={"b": _sink_node("b")},
           edges=[])
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode=spaces.MODE_RW,
        writable=True,
        config={"nodes": {}, "edges": [],
                "spaces": ["one", "two"], "groups": []},
    ))
    d._install_spaces(d._load_spaces_tree())
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)

    resp = d._cmd_add_edge(
        {"from_node": "one::a", "to_node": "two::b", "to_port": "in",
         "from_port": "out"}
    )
    assert resp["status"] == "error", resp
    assert "spaces 'one' and 'two' are read-only" in resp["message"]


# ---------------------------------------------------------------------------
# what stays allowed
# ---------------------------------------------------------------------------


def test_an_edge_crossing_the_boundary_is_allowed_both_ways(tmp_path):
    """Only *both* endpoints being declared is refused: an edge between a
    declared node and one outside is "plug things into it", in either
    direction, and comes out again as usual."""
    d, root, pdir = _declarative_and_writable(tmp_path)

    # The cross-space edge the root's own file declares survived the load: it
    # is imperative (no file declares it as wiring *of* the declarative
    # space), which is what the reload path deliberately preserves.
    assert _edge_id(d, "frozen::a", "out")

    # Outside -> declared.
    resp = d._cmd_add_edge(
        {"from_node": "kit::a", "to_node": "frozen::b2", "to_port": "in",
         "from_port": "out"}
    )
    assert resp["status"] == "ok", resp
    into = _edge_id(d, "kit::a", "frozen::b2")
    assert d._cmd_remove_edge({"edge_id": into})["status"] == "ok", "removal refused"
    assert into not in d.space.edges

    # Declared -> outside.
    resp = d._cmd_add_edge(
        {"from_node": "frozen::a", "to_node": "kit::b", "to_port": "in",
         "from_port": "out"}
    )
    assert resp["status"] == "ok", resp
    outof = _edge_id(d, "frozen::a", "kit::b")
    assert d._cmd_remove_edge({"edge_id": outof})["status"] == "ok", "removal refused"
    assert outof not in d.space.edges


def test_a_declarative_space_is_still_a_portal_target(tmp_path):
    """Embedding is not editing: a portal *to* a declarative space is legal,
    and the space's own portal stays whatever its file says."""
    d, root, pdir = _declarative_and_writable(tmp_path)

    resp = d._cmd_embed_space({"space_id": "frozen", "parent_space": "", "x": 5, "y": 6})

    assert resp["status"] == "ok", resp
    assert resp["view"] in (spaces.VIEW_INSIDE, spaces.VIEW_NODE)
    assert d.spaces[""].portal("frozen") is not None


def test_a_readonly_template_can_still_be_instantiated(tmp_path):
    """Copying *out* of a declarative template is not editing it: the drop
    materializes a new live space and the template keeps its file."""
    d, root, pdir = _daemon(tmp_path)
    _space("kitro", pdir, mode=spaces.MODE_RO, type=spaces.TEMPLATE,
           nodes={"a": _filter_node("tmpl")}, edges=[])
    spaces.write_file(root, spaces.Space(
        id="", parent=None, label="root", color="#fff", mode=spaces.MODE_RW,
        writable=True,
        config={"nodes": {}, "edges": [], "spaces": ["kitro"], "groups": []},
    ))
    d._install_spaces(d._load_spaces_tree())
    d._load_session(d._flatten_spaces(d.spaces), declarative=True)
    assert d.spaces["kitro"].is_readonly and d.spaces["kitro"].is_template

    resp = d._cmd_instantiate_template({"template_id": "kitro", "parent_space": ""})

    assert resp["status"] == "ok", resp
    new_id = resp["space_id"]
    assert d.spaces[new_id].is_template is False
    assert d.spaces[new_id].template == "kitro"
    assert "a" in d.spaces[new_id].nodes
    assert d.spaces["kitro"].is_readonly


def test_a_read_write_space_is_unaffected_in_every_one_of_them(tmp_path):
    """The rule is about a *declarative* space: the same six operations on a
    read-write space's nodes all work exactly as they did."""
    d, root, pdir = _declarative_and_writable(tmp_path)

    assert d._cmd_set_node_property(
        {"node_id": "kit::a", "property": "pattern", "value": "edited"}
    )["status"] == "ok"
    assert d.space.nodes["kit::a"].pattern == "edited"

    resp = d._cmd_set_node_layout(
        {"layout": {"kit::a": {"x": 7.0, "y": 8.0, "anchored": True}}}
    )
    assert resp["status"] == "ok" and not resp.get("refused"), resp
    assert (d.space.nodes["kit::a"].x, d.space.nodes["kit::a"].y) == (7.0, 8.0)

    assert d._cmd_rename_node(
        {"old_node_id": "kit::a", "new_node_id": "kit::renamed"}
    )["status"] == "ok"
    assert "kit::renamed" in d.space.nodes

    # Its declared-from-file edge is still wiring the display may change:
    # remove it, then wire it again.
    declared = _edge_id(d, "kit::renamed", "kit::b")
    assert d._cmd_remove_edge({"edge_id": declared})["status"] == "ok"
    resp = d._cmd_add_edge(
        {"from_node": "kit::renamed", "to_node": "kit::b", "to_port": "in",
         "from_port": "out"}
    )
    assert resp["status"] == "ok", resp

    assert d._cmd_remove_node({"node_id": "kit::renamed"})["status"] == "ok"
    assert "kit::renamed" not in d.space.nodes


# ---------------------------------------------------------------------------
# the module-API edge report is refused by the same rule
# ---------------------------------------------------------------------------

PASSTHRU = {
    "type_id": "passthru",
    "schema": 1,
    "ports": [
        {"name": "in", "kind": "audio", "direction": "in"},
        {"name": "out", "kind": "audio", "direction": "out"},
    ],
}


def _protocol_side():
    """The daemon's own module-API handler with a space tree, three instances
    a module owns (two inside the declarative space, one outside), and a
    display to report an edge."""
    tree = {
        "": spaces.Space(id="", parent=None, label="root", color="#fff",
                         mode=spaces.MODE_RW, writable=True, config={}),
        "frozen": spaces.Space(id="frozen", parent="", label="Frozen",
                               color="#abc", mode=spaces.MODE_RO, writable=True,
                               config={"nodes": {}, "edges": []}),
    }
    registry = Registry()
    topology = Graph()
    handler = ProtocolHandler(registry, topology, spaces=lambda: tree)
    registry.attach("mod", "mod", "1.0")
    assert registry.declare_types("mod", [PASSTHRU])[0] == []
    registry.announce("mod", [
        {"instance_id": "frozen::a", "type_id": "passthru"},
        {"instance_id": "frozen::b", "type_id": "passthru"},
        {"instance_id": "out", "type_id": "passthru"},
    ])
    display = Connection(role=api.ROLE_DISPLAY, name="gui")
    handler.handle(api.hello(api.ROLE_DISPLAY, "gui"), display)
    return handler, display


def test_the_module_api_refuses_the_same_declared_edges():
    """A display or a module reporting an edge over the module API is refused
    exactly where the `add_edge`/`remove_edge` commands are - the refusal is
    equally true for both, and neither path records anything on the way."""
    handler, display = _protocol_side()

    resp = handler.handle(
        api.node_edge("frozen::a", "frozen::b", "in", "out"), display
    )
    assert resp["type"] == "error", resp
    assert "space 'frozen' is read-only" in resp["message"]
    assert "'frozen::a' -> 'frozen::b'" in resp["message"]
    assert "wired" in resp["message"]
    assert "frozen::a->frozen::b" not in handler.topology.edges

    # Removing the same declared wiring is refused too, before anything is
    # looked up.
    resp = handler.handle(
        api.node_edge("frozen::a", "frozen::b", "in", "out", "remove"), display
    )
    assert resp["type"] == "error", resp
    assert "unwired" in resp["message"]

    # The crossing edge - either direction - is accepted, and comes out again.
    assert handler.handle(
        api.node_edge("out", "frozen::b", "in", "out"), display
    )["type"] == "ok"
    assert handler.handle(
        api.node_edge("out", "frozen::b", "in", "out", "remove"), display
    )["type"] == "ok"
    assert handler.handle(
        api.node_edge("frozen::a", "out", "in", "out"), display
    )["type"] == "ok"
    assert handler.handle(
        api.node_edge("frozen::a", "out", "in", "out", "remove"), display
    )["type"] == "ok"


def test_the_rule_is_the_same_one_the_portal_refusal_uses():
    """One notion of declared: the space-level refusal for a portal and the
    interior refusal for a node are the same sentence about the same flag."""
    tree = {"frozen": spaces.Space(
        id="frozen", parent="", label="Frozen", color="#abc",
        mode=spaces.MODE_RO, writable=True, config={})}

    assert spaces.readonly_node_problem(tree, "frozen::a", "deleted") == (
        "space 'frozen' is read-only: node 'frozen::a' is declared by its "
        "file and cannot be deleted at runtime"
    )
    assert spaces.readonly_portal_problem("frozen") == (
        "space 'frozen' is read-only: its portals are declared by its file "
        "and cannot be changed at runtime"
    )
    # Read-write, unknown space, and a node outside the tree: no refusal.
    rw = {"kit": spaces.Space(id="kit", parent="", label="Kitchen",
                              color="#123", mode=spaces.MODE_RW, writable=True,
                              config={})}
    assert spaces.readonly_node_problem(rw, "kit::a", "deleted") is None
    assert spaces.readonly_node_problem(tree, "a", "deleted") is None
    assert spaces.readonly_node_problem(tree, "gone::a", "deleted") is None
    assert spaces.readonly_edge_problem(tree, "frozen::a", "kit::b", "wired") is None
    assert spaces.readonly_edge_problem(tree, "a", "b", "wired") is None