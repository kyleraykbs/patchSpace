"""The daemon's module layer end to end: the PipeWire module attached to a
real `PatchSpaceDaemon` (no PipeWire processes - only control-plane node
types are created), nodes mirrored into the registry, the gray placement
that survives, and detaching the module.

The suite's conftest already redirects HOME/XDG/socket, so nothing here can
touch a live session; `start()` is never called.
"""

from central import module_api as api
from central.main import NODE_TYPE_REGISTRY, PatchSpaceDaemon
from central.protocol import Connection
from modules.pipewire import declarations


def fresh_daemon():
    return PatchSpaceDaemon()


def add_node(daemon, node_type, node_id, **config):
    reply = daemon.handle_command({
        "command": "add_node",
        "node_type": node_type,
        "node_id": node_id,
        "config": config,
    })
    assert reply.get("status") == "ok", reply
    return reply


def test_attaching_declares_the_catalog():
    daemon = fresh_daemon()
    summary = daemon.attach_modules()
    canonical = declarations.canonical_types(NODE_TYPE_REGISTRY)
    assert summary["module_id"] == "pipewire"
    assert summary["types"] == len(canonical)
    assert daemon.registry.type_decl("gate") is not None
    status = daemon.module_status()
    assert [m["module_id"] for m in status["modules"]] == ["pipewire"]
    assert len(status["types"]) == len(canonical)
    # The legacy aliases are the daemon's business, not a display's.
    assert daemon.registry.type_decl("patchbay_device") is None


def test_a_node_the_user_adds_is_mirrored_and_online():
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "gate", "g1", enabled=True)

    instance = daemon.registry.instance("g1")
    assert instance is not None, "add_node should sync the registry"
    assert instance.status == api.STATUS_ONLINE
    assert instance.type_id == "gate"
    assert daemon.topology.placement("g1") is not None
    # The declared ports are what the connection rules resolve against.
    assert daemon.registry.port_kind("g1", "audio", "in") == "audio"
    assert daemon.registry.port_kind("g1", "ctrl", "in") == "boolean"


def test_an_added_node_can_be_wired_through_the_new_authority():
    """`central/graph.py` validates against the declared ports — the same
    rules the model and the GUI enforce today, from one place."""
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "gate", "g1")
    add_node(daemon, "regex_input", "in1", pattern=".*")

    edge = daemon.topology.connect(
        "in1", "g1", "audio", "out",
        kind_of=daemon.registry.port_kind,
        cardinality_of=daemon.registry.port_cardinality,
    )
    assert edge.id == "in1->g1:audio"

    # A control signal may not drive the audio input.
    add_node(daemon, "boolean_switch", "b1")
    ok, message = daemon.topology.can_connect(
        "b1", "g1", "audio", "out",
        kind_of=daemon.registry.port_kind,
        cardinality_of=daemon.registry.port_cardinality,
    )
    assert not ok and "boolean" in message


def test_removing_a_node_forgets_it():
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "gate", "g1")
    assert daemon.registry.instance("g1").status == api.STATUS_ONLINE

    reply = daemon.handle_command({"command": "remove_node", "node_id": "g1"})
    assert reply.get("status") == "ok", reply
    assert daemon.registry.instance("g1") is None
    assert daemon.topology.placement("g1") is None


def test_a_gray_placement_survives_a_sync():
    """The load-bearing guarantee: a node placed for a module that is not
    there must not be swept by the next sync — it is the gray node the user
    keeps across reboots."""
    daemon = fresh_daemon()
    daemon.attach_modules()
    daemon.registry.place("ghost_1", "nobody_makes_this", config={"x": 1})

    daemon.sync_registry()

    instance = daemon.registry.instance("ghost_1")
    assert instance is not None
    assert instance.status == api.STATUS_UNBACKED
    assert instance.config == {"x": 1}


def test_detaching_the_module_offlines_its_nodes_and_keeps_the_graph():
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "gate", "g1")

    daemon.detach_modules()

    assert daemon.registry.instance("g1").status == api.STATUS_OFFLINE_MODULE
    assert daemon.topology.placement("g1") is not None
    assert "g1" in daemon.space.nodes, "the node itself is untouched"

    # Re-attaching (the module coming back) brings it online again.
    daemon.attach_modules()
    daemon.sync_registry()
    assert daemon.registry.instance("g1").status == api.STATUS_ONLINE


def test_a_display_gets_the_catalog_and_the_graph_snapshot():
    """What phase 2's GUI consumes: the type catalog once, then the graph
    (identity, status, position, edges) whenever it asks."""
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "gate", "g1")
    add_node(daemon, "regex_input", "in1", pattern=".*")
    daemon.topology.connect(
        "in1", "g1", "audio", "out",
        kind_of=daemon.registry.port_kind,
        cardinality_of=daemon.registry.port_cardinality,
    )

    display = Connection()
    assert daemon.handle_command(api.catalog_request(), display)["type"] == "error"
    daemon.handle_command(api.hello(api.ROLE_DISPLAY, "patchspaceGui"), display)

    catalog = daemon.handle_command(api.catalog_request(), display)
    assert catalog["type"] == "catalog_reply"
    assert len(catalog["types"]) == len(declarations.canonical_types(NODE_TYPE_REGISTRY))
    gate = next(t for t in catalog["types"] if t["type_id"] == "gate")
    assert any(p["name"] == "ctrl" and p["kind"] == "boolean" for p in gate["ports"])

    snapshot = daemon.handle_command(api.snapshot_request(), display)
    assert snapshot["type"] == "snapshot_reply"
    by_id = {i["instance_id"]: i for i in snapshot["instances"]}
    assert set(by_id) == {"g1", "in1"}
    assert by_id["g1"]["status"] == api.STATUS_ONLINE
    assert by_id["g1"]["type_id"] == "gate"
    placements = {p["instance_id"]: p for p in snapshot["placements"]}
    assert set(placements) == {"g1", "in1"}
    assert placements["g1"]["space"] == "main"
    assert [e["id"] for e in snapshot["edges"]] == ["in1->g1:audio"]
    assert [s["space_id"] for s in snapshot["spaces"]] == ["main"]


def test_the_snapshot_reports_a_gray_node_as_unbacked():
    daemon = fresh_daemon()
    daemon.attach_modules()
    daemon.registry.place("ghost_1", "nobody_makes_this")
    daemon.topology.place("ghost_1", x=40.0, y=10.0)

    display = Connection()
    daemon.handle_command(api.hello(api.ROLE_DISPLAY, "patchspaceGui"), display)
    snapshot = daemon.handle_command(api.snapshot_request(), display)

    ghost = next(i for i in snapshot["instances"] if i["instance_id"] == "ghost_1")
    assert ghost["status"] == api.STATUS_UNBACKED
    placement = next(p for p in snapshot["placements"] if p["instance_id"] == "ghost_1")
    assert (placement["x"], placement["y"]) == (40.0, 10.0)


def test_get_nodes_marks_nodes_the_registry_is_not_running():
    """What the canvas greys out: the registry's lifecycle status travels
    with every node, and a placement nothing runs is drawn too."""
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "gate", "g1")
    nodes = daemon.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["g1"]["status"] == api.STATUS_ONLINE

    # A placement nothing runs (a module that is not loaded): drawn, inert,
    # with the registry's own reason and its remembered position.
    daemon.registry.place("ghost_1", "nobody_makes_this")
    daemon.topology.place("ghost_1", x=12.0, y=34.0)
    nodes = daemon.handle_command({"command": "get_nodes"})["nodes"]
    ghost = nodes["ghost_1"]
    assert ghost["status"] == api.STATUS_UNBACKED
    assert "no module" in ghost["status_reason"]
    assert (ghost["x"], ghost["y"]) == (12.0, 34.0)
    assert ghost["ready"] is False

    # Detaching the module offlines the nodes it was running: they stay on
    # the canvas, washed out, and say why.
    daemon.detach_modules()
    nodes = daemon.handle_command({"command": "get_nodes"})["nodes"]
    assert nodes["g1"]["status"] == api.STATUS_OFFLINE_MODULE
    assert "pipewire" in nodes["g1"]["status_reason"]


def test_a_declared_many_port_takes_several_drivers_through_the_daemon():
    """The shipped opt-out, through the daemon's authority: `bool_space_in`
    is a *bus* of control signals - several sources may be offered, the
    first wired one wins (`BoolSpaceInNode.ALLOW_MULTIPLE_BOOLEAN`, which the
    model has always allowed) - so its input declares
    `cardinality: "many"` and the daemon accepts a second driver where the
    pre-S4 kind name refused it."""
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "bool_space_in", "in1")
    add_node(daemon, "boolean_switch", "b1")
    add_node(daemon, "boolean_switch", "b2")

    def wire(from_node, to_node):
        return daemon.topology.connect(
            from_node, to_node, "boolean", "out",
            kind_of=daemon.registry.port_kind,
            cardinality_of=daemon.registry.port_cardinality,
        )

    wire("b1", "in1")
    ok, message = daemon.topology.can_connect(
        "b2", "in1", "boolean", "out",
        kind_of=daemon.registry.port_kind,
        cardinality_of=daemon.registry.port_cardinality,
    )
    assert ok, message
    wire("b2", "in1")
    assert len(daemon.topology.edges_into("in1", "boolean")) == 2


def test_a_display_wires_a_second_driver_onto_a_bus_port():
    """The daemon's authority as a display reaches it: `node_edge` onto a
    boolean panel port takes the second source (its input declares `many`),
    while a `boolean` input that declares nothing still refuses one."""
    daemon = fresh_daemon()
    daemon.attach_modules()
    add_node(daemon, "bool_space_in", "in1")
    add_node(daemon, "boolean_switch", "b1")
    add_node(daemon, "boolean_switch", "b2")
    add_node(daemon, "gate", "g1")

    display = Connection()
    daemon.handle_command(api.hello(api.ROLE_DISPLAY, "patchspaceGui"), display)

    def edge(from_node, to_node, to_port):
        return daemon.handle_command(
            api.node_edge(from_node, to_node, to_port, "out"), display
        )

    assert edge("b1", "in1", "boolean")["type"] == "ok"
    assert edge("b2", "in1", "boolean")["type"] == "ok"

    assert edge("b1", "g1", "ctrl")["type"] == "ok"
    refused = edge("b2", "g1", "ctrl")
    assert refused["type"] == "error" and "already driven" in refused["message"]


def test_a_declared_merge_is_surfaced_where_it_is_consumed():
    """`merge` is part of the declaration and travels to whatever consumes
    it: the daemon relays the ports it was given, so a display sees how a
    receiving node combines several drivers (the daemon itself never
    merges - the module does)."""
    daemon = fresh_daemon()
    errors, _ = daemon.registry.declare_types("probe", [{
        "type_id": "mixer_probe",
        "schema": 1,
        "ports": [
            {"name": "in", "kind": "number", "direction": "in",
             "cardinality": "many", "merge": "sum"},
            {"name": "out", "kind": "number", "direction": "out"},
        ],
        "display": {"name": "Mixer Probe"},
    }])
    assert errors == []
    daemon.registry.place("probe_1", "mixer_probe")

    display = Connection()
    daemon.handle_command(api.hello(api.ROLE_DISPLAY, "patchspaceGui"), display)
    catalog = daemon.handle_command(api.catalog_request(), display)
    probe = next(t for t in catalog["types"] if t["type_id"] == "mixer_probe")
    port = next(p for p in probe["ports"] if p["name"] == "in")
    assert (port["cardinality"], port["merge"]) == ("many", "sum")
    assert daemon.registry.port_merge("probe_1", "in", "in") == "sum"
