"""Declarative node files: namespacing, daemon export/list/rename/delete,
and the imperative/declarative split in the autosave config.

Leaf node types only, so nothing here touches a real PipeWire process."""

import json
import os

import declarative
from main import PatchBayDaemon


def _daemon(tmp_path):
    ro = tmp_path / "ro"
    rw = tmp_path / "rw"
    ro.mkdir()
    rw.mkdir()
    return PatchBayDaemon(declarative_ro=str(ro), declarative_rw=str(rw))


def _add(d, node_type, node_id, **config):
    resp = d.handle_command(
        {
            "command": "add_node",
            "node_type": node_type,
            "node_id": node_id,
            "config": config,
        }
    )
    assert resp["status"] == "ok", resp


# ----------------------------------------------------------------------
# pure module
# ----------------------------------------------------------------------


def test_namespace_and_split_round_trip():
    assert declarative.namespace_id("mix", "mic") == "mix::mic"
    assert declarative.split_namespaced("mix::mic") == ("mix", "mic")
    assert declarative.split_namespaced("plain") == (None, "plain")


def test_namespaced_qualifies_own_ids_only(tmp_path):
    path = str(tmp_path / "kit.json")
    raw = {
        "nodes": {"mic": {"type": "regex_input", "params": {"pattern": ".*"}}},
        "edges": [
            {"from": "mic", "to": "other::sink"},
            {"from": "mic", "to": "mic"},
        ],
    }
    ns = declarative.namespaced(path, raw)
    assert set(ns["nodes"]) == {"kit::mic"}
    assert ns["nodes"]["kit::mic"]["params"]["declarative"] is True
    # Same-file endpoints qualified; a foreign namespaced id left alone.
    assert ns["edges"][0]["from"] == "kit::mic"
    assert ns["edges"][0]["to"] == "other::sink"
    assert ns["edges"][0]["declarative"] is True
    assert ns["edges"][1]["to"] == "kit::mic"


def test_load_all_read_write_shadows_read_only(tmp_path):
    ro = tmp_path / "ro"
    rw = tmp_path / "rw"
    ro.mkdir()
    rw.mkdir()
    (ro / "kit.json").write_text(
        json.dumps(
            {
                "nodes": {
                    "mic": {"type": "regex_input", "params": {}},
                    "keep": {"type": "gate", "params": {}},
                }
            }
        )
    )
    (rw / "kit.json").write_text(
        json.dumps({"nodes": {"mic": {"type": "gate", "params": {}}}})
    )
    config = declarative.load_all(str(ro), str(rw))
    # The read-write file's "mic" wins; the read-only "keep" survives.
    assert config["nodes"]["kit::mic"]["type"] == "gate"
    assert config["nodes"]["kit::keep"]["type"] == "gate"


def test_snapshot_detects_add_and_remove(tmp_path):
    d = tmp_path / "d"
    d.mkdir()
    empty = declarative.snapshot([str(d), None])
    assert empty == {}
    (d / "a.json").write_text("{}")
    one = declarative.snapshot([str(d), None])
    assert list(one) == [str(d / "a.json")]
    os.remove(d / "a.json")
    assert declarative.snapshot([str(d), None]) == {}


# ----------------------------------------------------------------------
# daemon commands
# ----------------------------------------------------------------------


def test_export_declarative_grabs_internal_and_declarative_edges(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    _add(d, "gate", "g1", enabled=True)
    _add(d, "description_output", "out1", description="speakers")
    for edge in (("in1", "g1"), ("g1", "out1")):
        assert (
            d.handle_command(
                {"command": "add_edge", "from_node": edge[0], "to_node": edge[1]}
            )["status"]
            == "ok"
        )

    resp = d.handle_command(
        {
            "command": "export_declarative",
            "name": "kit",
            "node_ids": ["in1", "g1"],
        }
    )
    assert resp["status"] == "ok", resp
    path = resp["path"]
    assert path.endswith(os.path.join("rw", "kit.json"))

    with open(path) as f:
        written = json.load(f)
    # The config is wrapped in a metadata layer.
    assert written["label"] == "kit"
    assert "color" in written
    config = written["config"]
    assert set(config["nodes"]) == {"in1", "g1"}
    # in1->g1 internal, g1->out1 dropped (out1 is imperative).
    assert len(config["edges"]) == 1
    assert config["edges"][0] == {"from": "in1", "to": "g1"}

    # Export was applied immediately: the nodes now exist namespaced and
    # the imperative originals are gone (declaring is a move).
    assert "kit::in1" in d.space.nodes
    assert d.space.nodes["kit::in1"].declarative is True
    assert d.space.nodes["kit::g1"].declarative is True
    assert "in1" not in d.space.nodes
    assert "g1" not in d.space.nodes
    # The edge to the outside node survived, re-pointed at the copy.
    assert "kit::g1->out1" in d.space.edges


def test_export_declarative_include_imperative_edges(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    _add(d, "gate", "g1", enabled=True)
    _add(d, "description_output", "out1", description="speakers")
    for edge in (("in1", "g1"), ("g1", "out1")):
        d.handle_command(
            {"command": "add_edge", "from_node": edge[0], "to_node": edge[1]}
        )

    resp = d.handle_command(
        {
            "command": "export_declarative",
            "name": "kit",
            "node_ids": ["in1", "g1"],
            "include_imperative_edges": True,
        }
    )
    assert resp["status"] == "ok", resp
    with open(resp["path"]) as f:
        written = json.load(f)
    assert len(written["config"]["edges"]) == 2
    # The far end keeps its full (imperative) id.
    assert {"from": "g1", "to": "out1"} in written["config"]["edges"]


def test_create_declarative_writes_metadata_and_groups(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    _add(d, "gate", "g1", enabled=True)
    d.handle_command({"command": "add_edge", "from_node": "in1", "to_node": "g1"})
    d.handle_command(
        {
            "command": "add_group",
            "group_id": "grp",
            "label": "My Group",
            "color": "#ff0000",
            "nodes": ["in1", "g1"],
        }
    )

    resp = d.handle_command(
        {
            "command": "export_declarative",
            "name": "kit",
            "label": "Kitchen",
            "color": "#00ff00",
            "node_ids": ["in1", "g1"],
        }
    )
    assert resp["status"] == "ok", resp
    with open(resp["path"]) as f:
        written = json.load(f)
    assert written["label"] == "Kitchen"
    assert written["color"] == "#00ff00"
    groups = written["config"]["groups"]
    assert len(groups) == 1
    assert groups[0]["label"] == "My Group"
    assert set(groups[0]["nodes"]) == {"in1", "g1"}

    # After the move, the group survives namespaced in the live graph.
    group = next(iter(d.groups.values()))
    assert set(group["nodes"]) == {"kit::in1", "kit::g1"}


def test_add_and_remove_nodes_from_declared_group(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "a", pattern=".*")
    _add(d, "gate", "b", enabled=True)
    _add(d, "description_output", "out", description="speakers")
    for edge in (("a", "b"), ("b", "out")):
        d.handle_command(
            {"command": "add_edge", "from_node": edge[0], "to_node": edge[1]}
        )
    export = d.handle_command(
        {"command": "export_declarative", "name": "grp", "node_ids": ["a", "b"]}
    )
    path = export["path"]

    # Add the output node to the group.
    add = d.handle_command(
        {
            "command": "export_declarative",
            "mode": "add",
            "path": path,
            "node_ids": ["out"],
        }
    )
    assert add["status"] == "ok", add
    assert "grp::out" in d.space.nodes
    assert "out" not in d.space.nodes
    assert "grp::b->grp::out" in d.space.edges

    # Remove a node again: it returns to the imperative graph, and its
    # edge to the still-declared node is re-pointed.
    rem = d.handle_command(
        {
            "command": "export_declarative",
            "mode": "remove",
            "path": path,
            "node_ids": ["grp::a"],
        }
    )
    assert rem["status"] == "ok", rem
    assert "a" in d.space.nodes
    assert d.space.nodes["a"].declarative is False
    assert "grp::a" not in d.space.nodes
    assert "a->grp::b" in d.space.edges


def test_edit_declarative_label_color_and_rename(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "foo", pattern=".*")
    export = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["foo"]}
    )
    path = export["path"]

    resp = d.handle_command(
        {
            "command": "edit_declarative",
            "path": path,
            "new_name": "renamed",
            "label": "Nice",
            "color": "#abcdef",
        }
    )
    assert resp["status"] == "ok", resp
    assert resp["path"].endswith("renamed.json")
    assert "renamed::foo" in d.space.nodes
    listed = d.handle_command({"command": "list_declarative"})["files"]
    entry = next(f for f in listed if f["stem"] == "renamed")
    assert entry["label"] == "Nice"
    assert entry["color"] == "#abcdef"


def test_incremental_move_keeps_unrelated_nodes_live(tmp_path):
    """Add/remove must be a live ownership flip, not a full rebuild: a
    node that didn't change keeps the exact same object (and therefore
    its running backing)."""
    d = _daemon(tmp_path)
    _add(d, "regex_input", "a", pattern=".*")
    _add(d, "gate", "b", enabled=True)
    _add(d, "description_output", "out", description="speakers")
    for edge in (("a", "b"), ("b", "out")):
        d.handle_command(
            {"command": "add_edge", "from_node": edge[0], "to_node": edge[1]}
        )
    export = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["a", "b"]}
    )
    kept = d.space.nodes["kit::a"]

    add = d.handle_command(
        {
            "command": "export_declarative",
            "mode": "add",
            "path": export["path"],
            "node_ids": ["out"],
        }
    )
    assert add["status"] == "ok", add
    # `a` wasn't part of the change, so it was not rebuilt.
    assert d.space.nodes["kit::a"] is kept
    assert "kit::out" in d.space.nodes

    # A subsequent full reload lands on exactly the same graph, with no
    # duplicates (the live move wrote a consistent file).
    d.reload_declarative()
    assert "kit::a" in d.space.nodes
    assert "kit::b" in d.space.nodes
    assert "kit::out" in d.space.nodes
    assert "a" not in d.space.nodes
    assert "out" not in d.space.nodes


def test_own_write_does_not_retrigger_a_reload(tmp_path):
    """Writing a declarative file ourselves must not look like an
    external change to the mtime watcher, or the next tick tears the
    whole declarative graph down for nothing."""
    d = _daemon(tmp_path)
    _add(d, "regex_input", "foo", pattern=".*")
    d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["foo"]}
    )
    kept = d.space.nodes["kit::foo"]
    # A watcher poll right after our write must find nothing to do.
    d._declarative_poll_at = 0.0
    d._poll_declarative()
    assert d.space.nodes["kit::foo"] is kept
    assert d._declarative_mtimes == declarative.snapshot(d._declarative_dirs())


def test_readonly_declarative_rejects_edits(tmp_path):
    d = _daemon(tmp_path)
    (tmp_path / "rw" / "locked.json").write_text(
        json.dumps(
            {
                "label": "Locked",
                "readonly": True,
                "config": {
                    "nodes": {"x": {"type": "regex_input", "params": {}}},
                    "edges": [],
                },
            }
        )
    )
    d.reload_declarative()
    resp = d.handle_command(
        {
            "command": "export_declarative",
            "mode": "add",
            "path": str(tmp_path / "rw" / "locked.json"),
            "node_ids": ["locked::x"],
        }
    )
    assert resp["status"] == "error"
    # The list marks it non-writable.
    listed = d.handle_command({"command": "list_declarative"})["files"]
    entry = next(f for f in listed if f["stem"] == "locked")
    assert entry["readonly"] is True
    assert entry["writable"] is False
    assert entry["label"] == "Locked"


def test_declare_moves_nodes_out_of_imperative(tmp_path):
    """Exporting imperative nodes takes them over: the originals are
    deleted and edges to nodes outside the selection are re-pointed at
    the declarative copies, so declaring never duplicates a chain."""
    d = _daemon(tmp_path)
    _add(d, "regex_input", "foo", pattern=".*")
    _add(d, "gate", "bar", enabled=True)
    _add(d, "description_output", "out", description="speakers")
    for edge in (("foo", "bar"), ("bar", "out")):
        assert (
            d.handle_command(
                {"command": "add_edge", "from_node": edge[0], "to_node": edge[1]}
            )["status"]
            == "ok"
        )

    resp = d.handle_command(
        {"command": "export_declarative", "name": "decl", "node_ids": ["foo", "bar"]}
    )
    assert resp["status"] == "ok", resp

    # Originals are gone; the declarative copies own the ids now.
    assert "foo" not in d.space.nodes
    assert "bar" not in d.space.nodes
    assert "decl::foo" in d.space.nodes
    assert "decl::bar" in d.space.nodes
    # The outside node survives, and its edge was re-pointed.
    assert "out" in d.space.nodes
    assert "decl::foo->decl::bar" in d.space.edges
    assert "decl::bar->out" in d.space.edges
    assert resp.get("imperative_taken_over") == 2

    # The imperative autosave no longer carries the moved nodes...
    imperative = d._build_export_config(imperative_only=True)
    assert "foo" not in imperative["nodes"]
    assert "bar" not in imperative["nodes"]
    assert "out" in imperative["nodes"]
    # ...and the edge to the outside node is now an imperative edge that
    # references the declarative copy (so reboots keep it).
    assert any(
        e["from"] == "decl::bar" and e["to"] == "out" for e in imperative["edges"]
    )


def test_duplicate_imperative_node_is_reconciled_on_reload(tmp_path):
    """A graph saved before Declare moved nodes (imperative original and
    declarative copy both present) is cleaned up when declarative files
    are applied."""
    d = _daemon(tmp_path)
    # Simulate the pre-move state: an imperative node and a declarative
    # file that declares the same local id.
    (tmp_path / "rw" / "decl.json").write_text(
        json.dumps(
            {
                "nodes": {"foo": {"type": "regex_input", "params": {"pattern": ".*"}}},
                "edges": [],
            }
        )
    )
    _add(d, "regex_input", "foo", pattern=".*")
    assert d.space.nodes["foo"].declarative is False

    d.reload_declarative()
    assert "foo" not in d.space.nodes
    assert "decl::foo" in d.space.nodes
    assert d.space.nodes["decl::foo"].declarative is True


def test_imperative_autosave_excludes_declarative(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    _add(d, "gate", "g1", enabled=True)
    d.handle_command({"command": "add_edge", "from_node": "in1", "to_node": "g1"})
    d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["in1", "g1"]}
    )

    full = d._build_export_config()
    assert "kit::in1" in full["nodes"]

    # Declared nodes (and the moved-away originals) never appear in the
    # imperative autosave, and no declarative edge leaks in either.
    imperative = d._build_export_config(imperative_only=True)
    assert imperative["nodes"] == {}
    assert imperative["edges"] == []


def test_list_declarative_reports_files_and_nodes(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["in1"]}
    )

    resp = d.handle_command({"command": "list_declarative"})
    assert resp["status"] == "ok"
    assert resp["directories"]["readwrite"] == d.declarative_rw
    assert len(resp["files"]) == 1
    entry = resp["files"][0]
    assert entry["stem"] == "kit"
    assert entry["writable"] is True
    assert [n["local_id"] for n in entry["nodes"]] == ["in1"]


def test_rename_declarative_reprefixes_nodes(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    export = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["in1"]}
    )
    path = export["path"]

    resp = d.handle_command(
        {"command": "rename_declarative", "path": path, "new_name": "renamed"}
    )
    assert resp["status"] == "ok", resp
    assert resp["path"].endswith("renamed.json")
    assert not os.path.exists(path)
    assert "renamed::in1" in d.space.nodes
    assert "kit::in1" not in d.space.nodes


def test_delete_declarative_removes_nodes(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    export = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["in1"]}
    )
    assert "kit::in1" in d.space.nodes

    resp = d.handle_command({"command": "delete_declarative", "path": export["path"]})
    assert resp["status"] == "ok", resp
    assert not os.path.exists(export["path"])
    assert "kit::in1" not in d.space.nodes


def test_reload_declarative_preserves_imperative_edges(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "description_output", "out1", description="speakers")
    _add(d, "regex_input", "in1", pattern=".*")
    # Declare in1; then wire the imperative node to the declarative copy.
    d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["in1"]}
    )
    wired = d.handle_command(
        {"command": "add_edge", "from_node": "kit::in1", "to_node": "out1"}
    )
    assert wired["status"] == "ok", wired
    assert any(
        e.from_node == "kit::in1" and e.to_node == "out1"
        for e in d.space.edges.values()
    )

    # A reload drops and re-derives the declarative node but must not
    # lose the imperative edge the user put on it.
    d.reload_declarative()
    assert "kit::in1" in d.space.nodes
    assert any(
        e.from_node == "kit::in1" and e.to_node == "out1"
        for e in d.space.edges.values()
    )


def test_assert_defaults_retries_until_set_default_succeeds(tmp_path):
    """With force-default on, a failed wpctl set-default must not be
    remembered as done - the built-in sink can resolve a couple of ticks
    before WirePlumber will accept it, so the promotion keeps retrying."""
    d = _daemon(tmp_path)
    d._builtin_resolved_ids = lambda: (10, None)
    # Live default is something else, so a forced check should act.
    d._read_default_id = lambda token: 999

    calls = []
    d._set_default = lambda token, nid: (calls.append((token, nid)), False)[1]
    d._assert_defaults()
    d._default_check_at = 0.0
    d._assert_defaults()
    assert calls == [("@DEFAULT_AUDIO_SINK@", 10)] * 2
    assert d._set_default_sink_id is None

    d._set_default = lambda token, nid: True
    d._default_check_at = 0.0
    d._assert_defaults()
    assert d._set_default_sink_id == 10

    # Once the live default already is the builtin, a forced check is a
    # no-op (no wpctl set-default spam).
    d._read_default_id = lambda token: 10
    calls.clear()
    d._default_check_at = 0.0
    d._assert_defaults()
    assert calls == []


def test_force_default_off_leaves_the_default_alone(tmp_path):
    """With the force button off, the daemon only promotes a freshly
    resolved id once; it won't fight a user's later choice."""
    import types

    d = _daemon(tmp_path)
    d.builtin_sink = types.SimpleNamespace(force_default=False)
    d.builtin_mic = None
    d._builtin_resolved_ids = lambda: (10, None)
    d._read_default_id = lambda token: 999

    calls = []
    d._set_default = lambda token, nid: (calls.append((token, nid)), True)[1]
    d._default_check_at = 0.0
    d._assert_defaults()
    assert calls == [("@DEFAULT_AUDIO_SINK@", 10)]
    # Already promoted once; a later user change is left alone.
    d._default_check_at = 0.0
    d._assert_defaults()
    assert calls == [("@DEFAULT_AUDIO_SINK@", 10)]


def test_declare_installs_group_live_and_saves_it(tmp_path):
    """Selecting a group's nodes and declaring them saves the group
    (label/colour/membership) into the file and puts the declarative
    group on the canvas immediately, dropping the imperative twin."""
    d = _daemon(tmp_path)
    _add(d, "regex_input", "a", pattern=".*")
    _add(d, "gate", "b", enabled=True)
    d.handle_command(
        {
            "command": "add_group",
            "group_id": "grp",
            "label": "My Group",
            "color": "#abcdef",
            "nodes": ["a", "b"],
        }
    )
    export = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["a", "b"]}
    )
    assert export["status"] == "ok", export

    # Saved into the file, with the group's properties.
    with open(export["path"]) as f:
        written = json.load(f)
    groups = written["config"]["groups"]
    assert len(groups) == 1
    assert groups[0]["label"] == "My Group"
    assert groups[0]["color"] == "#abcdef"
    assert set(groups[0]["nodes"]) == {"a", "b"}

    # Live: the declarative group replaced the imperative one.
    assert "grp" not in d.groups
    installed = d.groups.get("kit::grp")
    assert installed is not None and installed["declarative"] is True
    assert set(installed["nodes"]) == {"kit::a", "kit::b"}


def test_imperative_group_deduped_by_exact_declarative_match(tmp_path):
    """An imperative group whose members are exactly a declarative
    group's members is deleted in favour of the declarative one, even
    when the ids/labels differ."""
    d = _daemon(tmp_path)
    _add(d, "regex_input", "foo", pattern=".*")
    (tmp_path / "rw" / "kit.json").write_text(
        json.dumps(
            {
                "label": "Kit",
                "color": "#123456",
                "config": {
                    "nodes": {"foo": {"type": "regex_input", "params": {}}},
                    "edges": [],
                    "groups": [
                        {
                            "id": "g",
                            "label": "Kit Group",
                            "color": "#123456",
                            "nodes": ["foo"],
                        }
                    ],
                },
            }
        )
    )
    d.handle_command(
        {
            "command": "add_group",
            "group_id": "imperative_grp",
            "label": "Imp",
            "color": "#ff0000",
            "nodes": ["foo"],
        }
    )
    assert "imperative_grp" in d.groups

    d.reload_declarative()

    assert "imperative_grp" not in d.groups
    assert "kit::g" in d.groups
    assert d.groups["kit::g"]["declarative"] is True
    assert set(d.groups["kit::g"]["nodes"]) == {"kit::foo"}


def test_apply_node_config_pushes_volume_through_setter(tmp_path):
    """The shared re-adopt helper must route volume through set_volume
    (not a raw setattr), so the add and load paths agree."""
    from pwnodes import VolumeProcessNode

    d = _daemon(tmp_path)
    node = VolumeProcessNode("v", "patchbay_v", initial_volume=0.5)
    node.volume = 1.0
    d.space.nodes["v"] = node
    d._apply_node_config(node, {"initial_volume": 0.25})
    assert node.volume == 0.25


def test_socket_is_live_distinguishes_live_and_stale(tmp_path):
    """A daemon must not unlink a live daemon's socket; it may reap a
    stale one left by a crash."""
    import socket as _socket

    from main import _socket_is_live

    path = str(tmp_path / "s.sock")
    assert _socket_is_live(path) is False

    server = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    try:
        assert _socket_is_live(path) is True
    finally:
        server.close()

    # A leftover socket file with nothing listening is stale, not live.
    leftover = str(tmp_path / "stale.sock")
    open(leftover, "w").close()
    assert _socket_is_live(leftover) is False


def test_get_nodes_tags_declarative_nodes_with_label_and_color(tmp_path):
    d = _daemon(tmp_path)
    (tmp_path / "rw" / "kit.json").write_text(
        json.dumps(
            {
                "label": "Kitchen",
                "color": "#123456",
                "config": {
                    "nodes": {"foo": {"type": "regex_input", "params": {}}},
                    "edges": [],
                },
            }
        )
    )
    d.reload_declarative()
    nodes = d.handle_command({"command": "get_nodes"})["nodes"]
    node = nodes["kit::foo"]
    assert node["declarative"] is True
    assert node["declarative_label"] == "Kitchen"
    assert node["declarative_color"] == "#123456"


def test_heavy_load_counter_tracks_overlapping_loads(tmp_path):
    """The GUI's loading overlay is driven by get_nodes' `loading`; a
    rebuild that nests a reload must not clear it early."""
    d = _daemon(tmp_path)
    assert d._startup_loading is False
    d._begin_heavy_load()
    d._begin_heavy_load()
    assert d.handle_command({"command": "get_nodes"})["loading"] is True
    d._end_heavy_load()
    assert d._startup_loading is True
    d._end_heavy_load()
    assert d._startup_loading is False


def test_export_requires_overwrite_for_existing(tmp_path):
    d = _daemon(tmp_path)
    _add(d, "regex_input", "foo", pattern=".*")
    first = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["foo"]}
    )
    assert first["status"] == "ok"
    # foo is now declarative; declaring it again needs an explicit
    # overwrite flag.
    _add(d, "gate", "bar", enabled=True)
    dup = d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["bar"]}
    )
    assert dup["status"] == "error"
    over = d.handle_command(
        {
            "command": "export_declarative",
            "name": "kit",
            "node_ids": ["bar"],
            "overwrite": True,
        }
    )
    assert over["status"] == "ok"
    assert "kit::bar" in d.space.nodes


def test_get_nodes_reports_startup_loading(tmp_path):
    """The GUI raises its loading overlay for the daemon's own start-up
    load off this flag, since it never issued that load itself."""
    d = _daemon(tmp_path)
    assert d.handle_command({"command": "get_nodes"})["loading"] is False
    d._startup_loading = True
    assert d.handle_command({"command": "get_nodes"})["loading"] is True


def test_declarative_backing_name_is_sanitized(tmp_path):
    """A namespaced id contains "::", which is illegal in a PipeWire
    node.name; the derived default backing must not leak it."""
    d = _daemon(tmp_path)
    node = d._create_node("reverb", "kit::r1", {})
    assert ":" not in node.backing_node_name
    assert node.backing_node_name == "patchbay_kit__r1"


def test_declarative_node_export_marker_not_in_params(tmp_path):
    """A full export must not bake the declarative flag into node params;
    provenance lives in the file/directory, not the session JSON."""
    d = _daemon(tmp_path)
    _add(d, "regex_input", "in1", pattern=".*")
    d.handle_command(
        {"command": "export_declarative", "name": "kit", "node_ids": ["in1"]}
    )
    config = d.handle_command({"command": "export_config"})["config"]
    assert "declarative" not in config["nodes"]["kit::in1"]["params"]
