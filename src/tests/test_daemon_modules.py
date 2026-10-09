"""The daemon's module layer end to end: the PipeWire module attached to a
real `PatchSpaceDaemon` (no PipeWire processes - only control-plane node
types are created), nodes mirrored into the registry, the gray placement
that survives, and detaching the module.

The suite's conftest already redirects HOME/XDG/socket, so nothing here can
touch a live session; `start()` is never called.
"""

from central import module_api as api
from central.main import NODE_TYPE_REGISTRY, PatchSpaceDaemon
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
        multi_of=daemon.registry.port_multi,
    )
    assert edge.id == "in1->g1:audio"

    # A control signal may not drive the audio input.
    add_node(daemon, "boolean_switch", "b1")
    ok, message = daemon.topology.can_connect(
        "b1", "g1", "audio", "out",
        kind_of=daemon.registry.port_kind,
        multi_of=daemon.registry.port_multi,
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
