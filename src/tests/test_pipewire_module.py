"""The in-process PipeWire module: the attach handshake, the migration
handoff, announcing the inventory, and what the daemon asks of it after."""

from central import module_api as api
from central.main import NODE_TYPE_REGISTRY
from central.protocol import ProtocolHandler
from central.registry import Registry
from modules.pipewire import declarations
from modules.pipewire.module import PipewireModule


class Harness:
    """A daemon-less module host: the registry, the handler and the module,
    with an inventory the test controls."""

    def __init__(self, nodes=None, migrate_config=None):
        self.registry = Registry()
        self.handler = ProtocolHandler(self.registry)
        self.nodes = list(nodes or [])
        self.module = PipewireModule(
            self.handler,
            type_map=NODE_TYPE_REGISTRY,
            inventory=lambda: list(self.nodes),
            migrate_config=migrate_config,
        )

    def node(self, instance_id, type_id, **kw):
        node = {"instance_id": instance_id, "type_id": type_id}
        node.update(kw)
        self.nodes.append(node)
        return node


def _volume_node(instance_id, **kw):
    return {"instance_id": instance_id, "type_id": "volume", **kw}


def test_attach_declares_the_catalog_and_announces():
    harness = Harness()
    summary = harness.module.attach()
    assert summary["module_id"] == "pipewire"
    assert summary["types"] == len(declarations.canonical_types(NODE_TYPE_REGISTRY))
    assert harness.registry.module("pipewire").connected is True
    assert harness.registry.type_decl("volume") is not None
    # Nothing is in the inventory yet, so nothing is announced.
    assert summary["bound"] == [] and summary["created"] == []


def test_announcing_the_inventory_brings_nodes_online():
    harness = Harness(nodes=[_volume_node("vol_1", space="main")])
    summary = harness.module.attach()
    assert summary["created"] == ["vol_1"]
    assert harness.registry.instance("vol_1").status == api.STATUS_ONLINE
    assert harness.registry.port_kind("vol_1", "audio", "in") == "audio"
    assert harness.registry.port_kind("vol_1", "out", "out") == "audio"


def test_a_persisted_node_is_migrated_then_bound():
    """The handoff that matters: the placement exists before the module
    does, the module upgrades the stored config and keeps the id."""
    seen = []

    def migrate_config(config):
        seen.append(dict(config))
        return {"label": config.get("label"), "schema_version": 2}, ["bumped"]

    harness = Harness(migrate_config=migrate_config)
    harness.registry.place("vol_1", "volume",
                           config={"label": "mine", "schema_version": 1})
    harness.node("vol_1", "volume", config={"label": "mine", "schema_version": 2})

    summary = harness.module.attach()
    assert summary["migrated"] == 1
    assert seen == [{"label": "mine", "schema_version": 1}]
    assert summary["bound"] == ["vol_1"]
    instance = harness.registry.instance("vol_1")
    assert instance.status == api.STATUS_ONLINE
    assert instance.config == {"label": "mine", "schema_version": 2}


def test_a_placement_for_a_type_the_module_lacks_stays_gray():
    harness = Harness()
    harness.registry.place("mystery_1", "nobody_makes_this")
    harness.module.attach()
    assert harness.registry.instance("mystery_1").status == api.STATUS_UNBACKED


def test_a_placement_the_module_declares_but_does_not_run_is_offline():
    harness = Harness()
    harness.registry.place("vol_1", "volume")
    harness.module.attach()
    instance = harness.registry.instance("vol_1")
    assert instance.status == api.STATUS_OFFLINE_MODULE
    assert "pipewire" in instance.reason


def test_detach_offlines_everything_it_owns():
    harness = Harness(nodes=[_volume_node("vol_1")])
    harness.module.attach()
    harness.module.detach()
    assert harness.registry.instance("vol_1").status == api.STATUS_OFFLINE_MODULE

    # ...and a re-attach (the module restarting) binds it again.
    harness.module.attach()
    assert harness.registry.instance("vol_1").status == api.STATUS_ONLINE


def test_the_daemon_can_ask_the_module_to_change_a_control():
    harness = Harness(nodes=[_volume_node("vol_1", config={"initial_volume": 0.5})])
    harness.module.attach()

    harness.module.apply(api.control_set("vol_1", "initial_volume", 0.9))
    assert harness.module.control_log[-1]["value"] == 0.9
    assert harness.module.configs["vol_1"]["initial_volume"] == 0.9

    harness.module.apply(api.edge_apply({"id": "vol_1->speaker"}, "add"))
    assert harness.module.edge_log[-1]["edge"]["id"] == "vol_1->speaker"

    harness.module.apply(api.status_change({"vol_1": api.STATUS_ONLINE}))
    assert harness.module.last_status == {"vol_1": api.STATUS_ONLINE}


def test_an_asked_for_control_of_an_unknown_node_is_ignored_not_crashed():
    harness = Harness()
    harness.module.attach()
    harness.module.apply(api.control_set("ghost", "initial_volume", 1.0))
    assert harness.module.control_log[-1]["instance_id"] == "ghost"
    assert "ghost" not in harness.module.configs


def test_a_new_node_appearing_later_is_announced_as_created():
    harness = Harness(nodes=[_volume_node("vol_1")])
    harness.module.attach()
    harness.node("vol_2", "volume")
    summary = harness.module.announce()
    assert summary["created"] == ["vol_2"]
    assert summary["bound"] == ["vol_1"]
    assert harness.registry.instance("vol_2").status == api.STATUS_ONLINE
