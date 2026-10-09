"""The module API is a wire contract: roles, message shapes, validation.

Phase 3 sends these same dicts between processes, so the tests assert both
the validation verdicts and that the shapes survive a JSON round trip.
"""

import json

import pytest

from central import module_api as api


def test_every_role_is_accepted():
    for role in api.ROLES:
        ok, message = api.validate_hello(api.hello(role, "pipewire", "1.2"))
        assert ok, (role, message)


def test_hello_rejects_an_unknown_role():
    ok, message = api.validate_hello({"role": "plugin", "name": "x", "proto": 1})
    assert not ok and "role" in message


def test_hello_needs_a_name_and_a_sane_protocol():
    assert not api.validate_hello({"role": "module", "name": "  "})[0]
    assert not api.validate_hello({"role": "module", "name": "x", "proto": 0})[0]
    assert not api.validate_hello({"role": "module", "name": "x", "proto": "1"})[0]


def test_type_declaration_needs_id_schema_ports_and_controls():
    assert not api.validate_type_decl({"schema": 1})[0]
    assert not api.validate_type_decl({"type_id": "t", "schema": 0})[0]
    assert not api.validate_type_decl(
        {"type_id": "t", "ports": [{"name": "in", "kind": "audio"}]}
    )[0]  # no direction
    assert not api.validate_type_decl(
        {"type_id": "t", "ports": [{"name": "in", "kind": "teleport", "direction": "in"}]}
    )[0]
    assert not api.validate_type_decl(
        {"type_id": "t", "controls": [{"widget": "slider"}]}
    )[0]


def test_a_complete_declaration_passes():
    ok, message = api.validate_type_decl({
        "type_id": "virtual_camera",
        "schema": 2,
        "ports": [
            {"name": "in", "kind": "camera", "direction": "in"},
            {"name": "out", "kind": "camera", "direction": "out", "multi": True},
        ],
        "controls": [{"key": "output_device", "widget": "field"}],
        "display": {"name": "Virtual Cam"},
        "capabilities": ["virtual-camera"],
    })
    assert ok, message


def test_node_announce_validation():
    assert not api.validate_node_announce({"type_id": "t"})[0]
    assert not api.validate_node_announce({"instance_id": "n", "type_id": ""})[0]
    assert not api.validate_node_announce(
        {"instance_id": "n", "type_id": "t", "requires": "other"}
    )[0]
    ok, message = api.validate_node_announce(
        {"instance_id": "n", "type_id": "t", "requires": ["other"]}
    )
    assert ok, message


def test_messages_are_json_stable():
    messages = [
        api.hello(api.ROLE_MODULE, "pipewire", "1.0"),
        api.hello_ok(daemon="patchspaced"),
        api.type_declare([{"type_id": "t", "schema": 1}]),
        api.node_announce([{"instance_id": "n", "type_id": "t"}]),
        api.node_remove(["n"]),
        api.node_health({"n": api.HEALTH_OK}),
        api.migrate_result("n", {"schema_version": 2}),
        api.edge_apply({"id": "a->b"}, "add"),
        api.control_set("n", "volume", 0.5),
        api.status_change({"n": api.STATUS_UNBACKED}, reason="no module"),
        api.migrate("n", "t", {"schema_version": 1}, 1, 2),
        api.value_set("n", "out", 3),
        api.registry_query(capability="virtual-camera"),
        api.registry_reply([], [], []),
        api.error("no such type", ref="t"),
    ]
    for message in messages:
        assert json.loads(json.dumps(message)) == message


def test_registry_query_carries_only_what_was_asked():
    assert api.registry_query(capability="virtual-camera") == {
        "type": "registry_query",
        "capability": "virtual-camera",
    }
    assert api.registry_query(type_id="volume") == {
        "type": "registry_query",
        "type_id": "volume",
    }


@pytest.mark.parametrize("status", api.STATUSES)
def test_status_constants_are_distinct(status):
    assert api.STATUSES.count(status) == 1
