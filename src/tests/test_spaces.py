"""Unit tests for spaces.py: namespacing, LCA edge ownership, the space
tree loader, and migration from the legacy session/declarative shapes."""

import json
import os

from central import spaces
from central.spaces import (
    MODE_RO,
    MODE_RW,
    ROOT_ID,
    Space,
    edge_owner,
    is_descendant,
    lca,
    local_of,
    make_id,
    space_of,
    relative_id,
    split_id,
)


def test_namespacing_round_trip():
    assert make_id("", "mic") == "mic"
    assert make_id("kit", "mic") == "kit::mic"
    assert make_id("kit::eq", "band1") == "kit::eq::band1"

    assert split_id("mic") == ("", "mic")
    assert split_id("kit::mic") == ("kit", "mic")
    assert split_id("kit::eq::band1") == ("kit::eq", "band1")

    assert space_of("kit::eq::band1") == "kit::eq"
    assert local_of("kit::eq::band1") == "band1"
    assert relative_id("kit", "kit::eq::band1") == "eq::band1"
    assert relative_id("", "kit::mic") == "kit::mic"
    assert relative_id("kit::eq", "kit::fx::x") == "kit::fx::x"


def test_lca_and_edge_owner():
    assert lca("kit", "kit") == "kit"
    assert lca("kit::eq", "kit::fx") == "kit"
    assert lca("kit::eq", "other") == ""
    assert lca("", "kit") == ""

    # Same space -> that space owns the edge.
    assert edge_owner("kit::a", "kit::b") == "kit"
    # Different sub-spaces of kit -> kit owns it.
    assert edge_owner("kit::eq::a", "kit::fx::b") == "kit"
    # Different top-level spaces -> root owns it.
    assert edge_owner("kit::a", "other::b") == ""
    # Root-level node to a top-level space node -> root owns it.
    assert edge_owner("topnode", "kit::a") == ""


def test_is_descendant():
    assert is_descendant("kit::eq", "kit")
    assert is_descendant("kit::eq", "")
    assert is_descendant("kit", "kit")
    assert not is_descendant("kit", "kit::eq")
    assert not is_descendant("other", "kit")


def test_write_read_and_load_tree(tmp_path):
    rw = tmp_path / "rw"
    rw.mkdir()
    root = tmp_path / "root.json"

    # kit space with a nested eq space and one node each.
    (rw / "kit.json").write_text(json.dumps({
        "type": "space",
        "mode": "read-write",
        "label": "Kitchen",
        "color": "#123456",
        "placement": {"x": 100, "y": 50, "w": 500, "h": 300, "anchored": True},
        "config": {
            "nodes": {"mic": {"type": "splitter", "params": {"x": 10, "y": 20}}},
            "edges": [],
            "spaces": ["eq"],
        },
    }))
    (rw / "eq.json").write_text(json.dumps({
        "type": "space",
        "mode": "read-only",
        "label": "EQ",
        "config": {
            "nodes": {"band1": {"type": "splitter", "params": {}}},
            "edges": [{"from": "band1", "to": "band1"}],
        },
    }))
    root.write_text(json.dumps({
        "type": "space",
        "mode": "read-write",
        "label": "root",
        "config": {"nodes": {"top": {"type": "splitter", "params": {}}},
                   "edges": [], "spaces": ["kit"]},
    }))

    tree = spaces.load_tree(str(root), [str(rw)], {str(rw): True})
    assert set(tree) == {"", "kit", "kit::eq"}

    kit = tree["kit"]
    assert kit.parent == ROOT_ID
    assert kit.label == "Kitchen"
    assert kit.mode == MODE_RW
    assert kit.writable is True
    assert kit.x == 100 and kit.anchored is True
    assert kit.children == ["eq"]

    eq = tree["kit::eq"]
    assert eq.parent == "kit"
    assert eq.mode == MODE_RO
    assert list(eq.nodes) == ["band1"]


def test_readonly_dir_marks_space_unwritable(tmp_path):
    ro = tmp_path / "ro"
    ro.mkdir()
    root = tmp_path / "root.json"
    (ro / "fx.json").write_text(json.dumps({
        "type": "space",
        "config": {"nodes": {}, "edges": [], "spaces": []},
    }))
    root.write_text(json.dumps({
        "type": "space",
        "config": {"nodes": {}, "edges": [], "spaces": ["fx"]},
    }))
    tree = spaces.load_tree(str(root), [str(ro)], {str(ro): False})
    assert tree["fx"].writable is False


def test_write_file_round_trips_space(tmp_path):
    space = Space(
        id="kit",
        parent=ROOT_ID,
        label="Kitchen",
        color="#abcdef",
        mode=MODE_RW,
        x=10, y=20, w=300, h=200, anchored=True,
        config={"nodes": {"a": {"type": "splitter", "params": {}}},
                "edges": [], "spaces": [], "groups": []},
    )
    path = tmp_path / "kit.json"
    spaces.write_file(str(path), space)
    back = spaces.load_space(str(path), "kit", ROOT_ID, True)
    assert back.label == "Kitchen"
    assert back.x == 10 and back.y == 20 and back.w == 300 and back.anchored
    assert list(back.nodes) == ["a"]


def test_writing_a_space_keeps_the_previous_content_as_a_backup(tmp_path):
    """A space file *is* the user's graph and every autosave overwrites it
    in place, so the previous content has to survive one write as
    ``<file>.json.bak`` - it is what recovers an interrupted reload (or any
    bad write) instead of losing the whole graph."""
    path = tmp_path / "kit.json"
    first = Space(
        id="kit", parent=ROOT_ID, label="Kitchen", color="#abcdef",
        mode=MODE_RW,
        config={"nodes": {"a": {"type": "splitter", "params": {}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(str(path), first)
    original = path.read_text()

    second = Space(
        id="kit", parent=ROOT_ID, label="Kitchen", color="#abcdef",
        mode=MODE_RW,
        config={"nodes": {"a": {"type": "splitter", "params": {}},
                          "b": {"type": "volume", "params": {}}},
                "edges": [], "spaces": [], "groups": []},
    )
    spaces.write_file(str(path), second)

    backup = tmp_path / "kit.json.bak"
    assert backup.exists()
    assert backup.read_text() == original
    assert path.read_text() != original
    # The .bak is not a space file: the scanner must ignore it.
    assert not backup.name.endswith(spaces.SPACE_SUFFIX)


def test_migrate_legacy_session():
    raw = {
        "nodes": {"a": {"type": "splitter", "params": {}}},
        "edges": [{"from": "a", "to": "a"}],
        "groups": [{"id": "g", "label": "G", "color": "#fff", "nodes": ["a"]}],
    }
    space = spaces.migrate_legacy_session(raw)
    assert space.id == ROOT_ID and space.is_root
    assert list(space.nodes) == ["a"]
    assert space.edges == raw["edges"]
    assert space.groups == raw["groups"]


def test_migrate_declarative_file_modes():
    rw = spaces.migrate_declarative_file("kit", {
        "label": "Kitchen",
        "config": {"nodes": {"mic": {"type": "splitter", "params": {}}},
                   "edges": [], "groups": []},
    })
    assert rw.id == "kit" and rw.mode == MODE_RW

    ro = spaces.migrate_declarative_file("fx", {
        "readonly": True,
        "config": {"nodes": {}, "edges": [], "groups": []},
    })
    assert ro.mode == MODE_RO


def test_an_old_spelling_space_file_loads_and_is_resaved_new(tmp_path):
    """S0 data migration: a file written before the rename (`type: "panel"`,
    the child list under `panels`) still loads as a space, and the first save
    rewrites it under the new spelling (`type: "space"`, `spaces`).

    The other direction matters as much: the old marker must read as *a space
    file*, not as a legacy session - otherwise a pre-rename root would be
    rewritten as an empty migrated session and the graph would be gone."""
    rw = tmp_path / "rw"
    rw.mkdir()
    root = tmp_path / "root.json"
    (rw / "kit.json").write_text(json.dumps({
        "type": "panel",
        "mode": "read-write",
        "label": "Kitchen",
        "placement": {"x": 100, "y": 50, "w": 300, "h": 200,
                      "anchored": False},
        "config": {
            "nodes": {"pin": {"type": "panel_in",
                              "params": {"x": 10, "y": 20}}},
            "edges": [],
            "panels": [],
        },
    }))
    root.write_text(json.dumps({
        "type": "panel",
        "mode": "read-write",
        "label": "root",
        "config": {"nodes": {}, "edges": [], "panels": ["kit"], "groups": []},
    }))

    tree = spaces.load_tree(str(root), [str(rw)], {str(rw): True})
    assert set(tree) == {"", "kit"}                 # the child list was read
    assert tree[""].children == ["kit"]
    assert tree["kit"].x == 100 and tree["kit"].w == 300
    assert list(tree["kit"].nodes) == ["pin"]

    # Save it back: only the new spelling is ever written.
    spaces.write_file(str(root), tree[""])
    spaces.write_file(str(rw / "kit.json"), tree["kit"])

    root_raw = json.loads(root.read_text())
    # The `type` key carries the space's *kind* since S1: a file with the
    # pre-S1 marker ("panel") reads as a live space and saves as `live`.
    assert root_raw["type"] == spaces.LIVE
    assert root_raw["config"]["spaces"] == ["kit"]
    assert "panels" not in root_raw["config"]

    kit_raw = json.loads((rw / "kit.json").read_text())
    assert kit_raw["type"] == spaces.LIVE
    assert "panels" not in kit_raw["config"]
    # The port id itself is canonicalized by the daemon's node registry, not
    # by the file layer - so the file keeps what it read (see
    # test_spaces_daemon for the re-save that re-spells it).
    assert kit_raw["config"]["nodes"]["pin"]["type"] == "panel_in"

    # Both spellings in one dict: the new one wins.
    dual = {"config": {"nodes": {}, "edges": [],
                       "spaces": ["new"], "panels": ["old"]}}
    assert spaces.config_from_raw(dual)["spaces"] == ["new"]


# ---------------------------------------------------------------------------
# S1: the space's kind, its permanence, and the write filter
# ---------------------------------------------------------------------------


def _tree(*specs):
    return {s.id: s for s in specs}


def _space(space_id, parent=ROOT_ID, **kw):
    kw.setdefault("label", space_id or "root")
    kw.setdefault("color", spaces.DEFAULT_COLOR)
    return Space(id=space_id, parent=parent, **kw)


def test_a_space_record_round_trips_the_s1_fields(tmp_path):
    """`type`, `permanent`, `node_view` and `template` are part of the file
    schema: written always (except `template`, which is absent when there is
    none) and read back."""
    path = tmp_path / "inst.json"
    space = _space("kit", parent=ROOT_ID, type=spaces.LIVE, permanent=False,
                   node_view=True, template="tmpl")
    spaces.write_file(str(path), space)
    raw = json.loads(path.read_text())
    assert raw["type"] == spaces.LIVE
    assert raw["permanent"] is False
    assert raw["node_view"] is True
    assert raw["template"] == "tmpl"

    back = spaces.load_space(str(path), "kit", ROOT_ID, True)
    assert back.type == spaces.LIVE
    assert back.permanent is False
    assert back.node_view is True
    assert back.template == "tmpl"
    assert back.is_template is False

    tmpl = tmp_path / "tmpl.json"
    spaces.write_file(str(tmpl), _space("tmpl2", type=spaces.TEMPLATE))
    raw = json.loads(tmpl.read_text())
    assert raw["type"] == spaces.TEMPLATE
    assert "template" not in raw          # absent, never null
    loaded = spaces.load_space(str(tmpl), "tmpl2", ROOT_ID, True)
    assert loaded.is_template is True
    assert loaded.permanent is True       # a file that exists is the user's
    assert loaded.node_view is False


def test_every_spelling_of_a_space_file_still_reads_as_one():
    """`is_space_type` has to answer yes for the *kind* markers S1 writes and
    for both pre-S1 spellings: a root mistaken for a bare legacy session
    would be rewritten empty."""
    assert spaces.is_space_type({"type": spaces.LIVE})
    assert spaces.is_space_type({"type": spaces.TEMPLATE})
    assert spaces.is_space_type({"type": spaces.TYPE_SPACE})       # pre-S1
    assert spaces.is_space_type({"type": spaces.LEGACY_TYPE_SPACE})  # pre-S0
    assert not spaces.is_space_type({})                            # a session
    assert not spaces.is_space_type({"type": "something_else"})
    # `space`/`panel` are live spaces, not templates.
    assert spaces.type_from_raw({"type": spaces.TYPE_SPACE}) == spaces.LIVE
    assert spaces.type_from_raw({}) == spaces.LIVE
    assert spaces.type_from_raw({"type": spaces.TEMPLATE}) == spaces.TEMPLATE


def test_a_templates_interior_never_spawns():
    tree = _tree(
        _space(ROOT_ID, parent=None),
        _space("tmpl", type=spaces.TEMPLATE),
        _space("tmpl::inner", parent="tmpl"),
        _space("live"),
        _space("live::inner", parent="live"),
        _space("live::t", parent="live", type=spaces.TEMPLATE),
        _space("live::t::deep", parent="live::t"),
    )
    assert spaces.spawns("", tree)
    assert spaces.spawns("live", tree)
    assert spaces.spawns("live::inner", tree)
    assert not spaces.spawns("tmpl", tree)
    assert not spaces.spawns("tmpl::inner", tree)      # inside one: no
    assert not spaces.spawns("live::t", tree)
    assert not spaces.spawns("live::t::deep", tree)
    # An unknown space is not something to silently swallow.
    assert spaces.spawns("ghost", tree)


def test_persistence_is_filtered_per_space_outermost_first():
    tree = _tree(
        _space(ROOT_ID, parent=None),
        _space("live"),
        _space("live::inner", parent="live"),
        _space("app", permanent=False),
        _space("app::inner", parent="app"),          # permanent itself…
        _space("ghost"),                             # not in the tree: no
    )
    del tree["ghost"]
    assert spaces.persists("", tree)
    assert spaces.persists("live", tree)
    assert spaces.persists("live::inner", tree)
    assert not spaces.persists("app", tree)
    assert not spaces.persists("app::inner", tree)   # …but its parent isn't
    assert not spaces.persists("ghost", tree)


def test_pruning_drops_the_space_everything_in_it_and_references_to_it():
    """What must not be written: the space, its nodes, its interior edges,
    its placement (the parent's child reference) and any group reaching into
    it - while a permanent neighbour keeps all of its own."""
    app = _space("app", permanent=False,
                 config={"nodes": {"a": {"type": "regex_input", "params": {}}},
                         "edges": [], "spaces": ["inner"], "groups": []})
    inner = _space("app::inner", parent="app")
    keep = _space("keep",
                  config={"nodes": {"k": {"type": "regex_input", "params": {}}},
                          "edges": [], "spaces": ["app"], "groups": [
                              {"id": "g", "label": "G",
                               "nodes": ["k", "app::a"]}]})
    root = _space(ROOT_ID, parent=None,
                  config={"nodes": {}, "edges": [
                      {"from": "keep::k", "to": "app::a"},
                      {"from": "keep::k", "to": "keep::k"}],
                      "spaces": [{"name": "keep", "stem": "keep"},
                                 {"name": "app", "stem": "app"}],
                      "groups": []})
    tree = _tree(root, keep, app, inner)
    out = spaces.prune_unpersisted(tree)

    assert set(out) == {"", "keep"}
    assert out[""] is not root            # pruned, not mutated in place
    assert [spaces.child_name(c) for c in out[""].child_entries] == ["keep"]
    # The edge into the non-permanent space is gone; the interior one stays.
    assert out[""].config["edges"] == [{"from": "keep::k", "to": "keep::k"}]
    # A group reaching into it is dropped too (it would reference a node
    # that comes back nowhere).
    assert out["keep"].config["groups"] == []
    # The surviving space keeps its own child reference until it is pruned
    # for pointing at a dropped space.
    assert [spaces.child_name(c) for c in out["keep"].child_entries] == []
    # Nothing was mutated on the originals.
    assert spaces.child_name(keep.child_entries[0]) == "app"
    assert len(root.config["edges"]) == 2
    assert len(root.config["spaces"]) == 2
    assert keep.config["groups"]

    # Nothing to prune: the very same mapping comes back (no copying).
    whole = _tree(root, keep)
    assert spaces.prune_unpersisted(whole) is whole


def test_face_input_rules():
    """Audio is node-only; a value input must not be audio; a method must be
    one of the two; everything else is allowed."""
    assert spaces.face_input_problem("node", "audio", "") is None
    assert spaces.face_input_problem("value", "number", "field") is None
    assert spaces.face_input_problem("value", "choice", "edge") is None
    assert spaces.face_input_problem("value", "boolean", "") is None
    assert spaces.face_input_problem("node", "audio", "field") is not None
    assert spaces.face_input_problem("node", "audio", "edge") is not None
    assert spaces.face_input_problem("value", "audio", "") is not None
    assert spaces.face_input_problem("node", "colour", "") is not None
    assert spaces.face_input_problem("sideways", "number", "") is not None
    assert spaces.face_input_problem("node", "number", "wizard") is not None


def test_copy_config_does_not_alias_the_source():
    """An instantiated template's definition must not share the template's
    dictionaries: editing the instance would edit the definition.  A portal
    comes along (a copy of a space that shows another space shows the *same*
    space), still without aliasing."""
    source = {"nodes": {"a": {"type": "regex_input", "params": {"x": 1}}},
              "edges": [{"from": "a", "to": "a"}],
              "spaces": [{"name": "inner", "stem": "inner"}],
              "portals": [{"space_id": "other", "view": "node",
                           "x": 1.0, "y": 2.0}],
              "groups": [{"id": "g", "nodes": ["a"]}]}
    copied = spaces.copy_config(source)
    assert copied == source
    copied["nodes"]["a"]["params"]["x"] = 99
    copied["edges"].append({})
    copied["spaces"][0]["name"] = "other"
    copied["groups"][0]["nodes"].append("b")
    copied["portals"][0]["view"] = "inside"
    assert source["nodes"]["a"]["params"]["x"] == 1
    assert source["edges"] == [{"from": "a", "to": "a"}]
    assert source["spaces"][0]["name"] == "inner"
    assert source["groups"][0]["nodes"] == ["a"]
    assert source["portals"][0]["view"] == "node"


# --- portals (SPACES.md §13): placement by reference, never a copy ---------


def test_a_portal_round_trips_through_the_file(tmp_path):
    """`portals` is part of the space's own saved record: it is written with
    the space that *embeds* the other, and the space it names is not touched
    (its interior is not written here at all)."""
    path = tmp_path / "host.json"
    host = _space("host", config={
        "nodes": {"n": {"type": "regex_input", "params": {}}},
        "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit", "view": "node", "x": 12.5, "y": -3.0},
                    {"space_id": "kit::eq", "view": "inside", "x": 0, "y": 40}],
    })
    spaces.write_file(str(path), host)

    raw = json.load(open(path))
    assert raw["config"]["portals"] == [
        {"space_id": "kit", "view": "node", "x": 12.5, "y": -3.0},
        {"space_id": "kit::eq", "view": "inside", "x": 0.0, "y": 40.0},
    ]
    # Nothing about the target: the file is the *parent's* record.
    assert set(raw["config"]) == {"nodes", "edges", "portals"}

    back = spaces.load_space(str(path), "host", ROOT_ID, True, stem="host")
    assert back.portals == raw["config"]["portals"]
    assert back.portal_ids == ["kit", "kit::eq"]
    assert back.portal("kit")["view"] == "node"
    assert back.portal("nope") is None
    # A file with no `portals` key has none - the field's default.
    assert spaces.portals_from_raw({"nodes": {}}) == []


def test_a_portal_entry_is_normalized_and_defaults_to_inside(tmp_path):
    """A hand-written entry with no view reads as `inside` - the space's
    `node_view` is only the default a *new* embedding is created with - and
    an entry that names no space is dropped."""
    config = {"portals": [
        {"space_id": "kit"},
        {"space_id": "eq", "view": "sideways", "x": "3"},
        None,
        {"view": "node"},
    ]}
    assert spaces.portals_from_raw(config) == [
        {"space_id": "kit", "view": "inside", "x": 0.0, "y": 0.0},
        {"space_id": "eq", "view": "inside", "x": 3.0, "y": 0.0},
    ]
    assert spaces.portal_entry("kit", "nonsense")["view"] == "inside"
    assert spaces.PORTAL_KEY == "portals"


def test_embedded_in_is_derived_from_the_portals_that_remain():
    """`embedded_in` is the reverse view of every space's `portals`: one
    source of truth, so a removal on either side cannot disagree."""
    root = _space(ROOT_ID, parent=None, config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit", "view": "node", "x": 0, "y": 0}],
    })
    kit = _space("kit", config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit::eq", "view": "inside", "x": 1, "y": 1}],
    })
    eq = _space("kit::eq", parent="kit")
    tree = _tree(root, kit, eq)
    assert spaces.embedded_in("kit", tree) == [""]
    assert spaces.embedded_in("kit::eq", tree) == ["kit"]
    assert spaces.embedded_in("elsewhere", tree) == []
    assert spaces.portal_index(tree) == {"kit": [""], "kit::eq": ["kit"]}

    # The target side is not stored anywhere: it is exactly what the portals
    # say.  Drop the root's portal and the target's `embedded_in` is empty.
    root.config["portals"] = []
    assert spaces.embedded_in("kit", tree) == []

    # A space that names itself is ignored (never written, but a hand-edited
    # file must not make the UI claim a space is inside itself).
    eq.config["portals"] = [{"space_id": "kit::eq", "view": "node",
                             "x": 0, "y": 0}]
    assert spaces.embedded_in("kit::eq", tree) == ["kit"]


def test_embed_problem_refuses_every_cycle_and_type_violation():
    """The whole embed rule: both spaces must exist, the parent editable, the
    target live, no cycle (self or descendant) and no duplicate."""
    root = _space(ROOT_ID, parent=None)
    kit = _space("kit")
    inner = _space("kit::inner", parent="kit")
    other = _space("other")
    tmpl = _space("tmpl", type=spaces.TEMPLATE)
    decl = _space("decl", mode=spaces.MODE_RO)
    tree = _tree(root, kit, inner, other, tmpl, decl)

    # Allowed: a sibling, and a child space shown in its own parent.
    assert spaces.embed_problem("other", "kit", tree) is None
    assert spaces.embed_problem("kit::inner", "kit", tree) is None
    # A child may show a space *outside* its subtree.
    assert spaces.embed_problem("other", "kit::inner", tree) is None

    assert "no space" in spaces.embed_problem("nope", "kit", tree)
    assert "no space" in spaces.embed_problem("kit", "nope", tree)
    assert "template" in spaces.embed_problem("tmpl", "kit", tree)
    assert "itself" in spaces.embed_problem("kit", "kit", tree)
    assert "descendant" in spaces.embed_problem("kit", "kit::inner", tree)
    # The root contains every space: a portal to it is always a cycle.
    assert "descendant" in spaces.embed_problem("", "kit", tree)
    assert "read-only" in spaces.embed_problem("kit", "decl", tree)

    # A duplicate is refused rather than silently re-placed.
    kit.config["portals"] = [{"space_id": "other", "view": "node",
                              "x": 5, "y": 5}]
    assert "already embedded" in spaces.embed_problem("other", "kit", tree)
    # …but another parent may show the same space (that is the point).
    assert spaces.embed_problem("other", "kit::inner", tree) is None


def test_portal_cycle_and_move_cycle():
    assert spaces.portal_cycle("kit", "kit")
    assert spaces.portal_cycle("kit::inner", "kit")
    assert spaces.portal_cycle("kit", "")
    assert not spaces.portal_cycle("kit", "other")
    assert not spaces.portal_cycle("kit", "kit::inner")

    root = _space(ROOT_ID, parent=None)
    host = _space("host", config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit", "view": "node", "x": 0, "y": 0}],
    })
    kit = _space("kit")
    tree = _tree(root, host, kit)
    # Nesting the space that shows `kit` *into* `kit` would embed a space in
    # its own descendant - the move is refused (no id_map entry == unchanged).
    assert "cycle" in spaces.move_cycle(tree, {"host": "kit::host"})
    # A move somewhere else is fine, and so is moving the target.
    assert spaces.move_cycle(tree, {"host": "moved"}) is None
    assert spaces.move_cycle(tree, {"kit": "moved"}) is None


def test_pruning_keeps_a_portal_to_a_non_permanent_space():
    """A portal is the *parent's* placement: it is written with the parent
    even when the space it shows is written nowhere (its owner re-creates it
    - agents/SPACES.md §13)."""
    app = _space("app", permanent=False, config={
        "nodes": {"a": {"type": "regex_input", "params": {}}},
        "edges": [], "spaces": [], "groups": [],
    })
    host = _space("host", config={
        "nodes": {}, "edges": [], "spaces": ["app"], "groups": [],
        "portals": [{"space_id": "app", "view": "inside", "x": 7, "y": 7},
                    {"space_id": "ghost", "view": "node", "x": 0, "y": 0}],
    })
    root = _space(ROOT_ID, parent=None, config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "app", "view": "node", "x": 1, "y": 2}],
    })
    tree = _tree(root, host, app)
    out = spaces.prune_unpersisted(tree)

    assert set(out) == {"", "host"}                 # `app` is written nowhere
    assert out[""].portals == [{"space_id": "app", "view": "node",
                                "x": 1.0, "y": 2.0}]
    assert out["host"].portals == [
        {"space_id": "app", "view": "inside", "x": 7.0, "y": 7.0},
        {"space_id": "ghost", "view": "node", "x": 0.0, "y": 0.0},
    ]
    # The child reference to the non-permanent space *is* dropped: that is
    # containment, which only exists while the space does.
    assert out["host"].config["spaces"] == []
    # A portal is not a containment, so a portal to a space the file never
    # had is kept as it is (there is nothing to prune it against).
    assert out["host"] is not host                  # copied, not mutated
    assert len(host.portals) == 2


def test_drop_portals_to_removes_only_the_entries_that_name_them():
    root = _space(ROOT_ID, parent=None, config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit", "view": "node", "x": 0, "y": 0},
                    {"space_id": "kit::eq", "view": "inside", "x": 0, "y": 9},
                    {"space_id": "other", "view": "node", "x": 3, "y": 3}],
    })
    kit = _space("kit", config={
        "nodes": {}, "edges": [], "spaces": ["eq"], "groups": [],
        "portals": [{"space_id": "other", "view": "inside", "x": 1, "y": 1}],
    })
    eq = _space("kit::eq", parent="kit")
    other = _space("other")
    tree = _tree(root, kit, eq, other)

    dropped = spaces.drop_portals_to(tree, {"kit", "kit::eq"})
    assert sorted(dropped) == [("", "kit"), ("", "kit::eq")]
    assert [p["space_id"] for p in root.portals] == ["other"]
    # A portal *inside* a doomed space is not touched (it goes with it).
    assert [p["space_id"] for p in kit.portals] == ["other"]
    assert spaces.embedded_in("kit", tree) == []
    assert spaces.embedded_in("other", tree) == ["", "kit"]


def test_retarget_portals_follows_a_re_keyed_space():
    root = _space(ROOT_ID, parent=None, config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit", "view": "node", "x": 0, "y": 0},
                    {"space_id": "other", "view": "node", "x": 0, "y": 0}],
    })
    kit = _space("kit", config={
        "nodes": {}, "edges": [], "spaces": [], "groups": [],
        "portals": [{"space_id": "kit::eq", "view": "inside", "x": 2, "y": 2}],
    })
    eq = _space("kit::eq", parent="kit")
    other = _space("other")
    tree = _tree(root, kit, eq, other)

    id_map = {"kit": "nest::kit", "kit::eq": "nest::kit::eq"}
    assert spaces.retarget_portals(tree, id_map) == 2
    assert [p["space_id"] for p in root.portals] == ["nest::kit", "other"]
    assert [p["space_id"] for p in kit.portals] == ["nest::kit::eq"]
    # No change: nothing was re-keyed for it, so the list is left alone.
    assert root.portals[1]["view"] == "node"
    assert spaces.retarget_portals(tree, {"unrelated": "x"}) == 0
