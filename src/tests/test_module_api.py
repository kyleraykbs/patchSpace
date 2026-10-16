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


def test_a_port_may_declare_its_cardinality_and_merge():
    """S4's declaration (agents/SPACES.md §15): how many drivers a port
    takes, and how the receiving node combines them."""
    ok, message = api.validate_type_decl({
        "type_id": "space_in",
        "schema": 1,
        "ports": [
            {"name": "audio", "kind": "audio", "direction": "in",
             "cardinality": "many", "merge": "mix"},
            {"name": "out", "kind": "audio", "direction": "out"},
        ],
    })
    assert ok, message
    # Absent means the pre-S4 default, so an old declaration is still valid
    # (and JSON null reads as absent, not as a value).
    assert api.validate_type_decl({
        "type_id": "t",
        "ports": [{"name": "in", "kind": "boolean", "direction": "in",
                   "cardinality": None, "merge": None}],
    })[0]


@pytest.mark.parametrize("bad", ["", "One", "one ", "many ", "several", True, 1])
def test_an_unknown_cardinality_is_refused(bad):
    ok, message = api.validate_type_decl({
        "type_id": "t",
        "ports": [{"name": "in", "kind": "audio", "direction": "in",
                   "cardinality": bad}],
    })
    assert not ok and "cardinality" in message and "t.in" in message


@pytest.mark.parametrize("bad", ["", "mix ", "average", "first", "sums", 1])
def test_an_unknown_merge_is_refused(bad):
    ok, message = api.validate_type_decl({
        "type_id": "t",
        "ports": [{"name": "in", "kind": "audio", "direction": "in",
                   "merge": bad}],
    })
    assert not ok and "merge" in message and "t.in" in message


def test_the_declared_cardinality_vocabulary_is_closed():
    assert api.CARDINALITIES == ("one", "many")
    assert api.MERGES == ("mix", "sum", "last")
    # The pre-S4 rule, kept as the default for a port that declares nothing.
    assert api.default_cardinality("boolean") == "one"
    assert api.default_cardinality("impulse") == "one"
    assert api.default_cardinality("sound") == "one"
    assert api.default_cardinality("camera") == "one"
    assert api.default_cardinality("audio") == "many"
    assert api.default_cardinality("bundle") == "many"
    assert api.default_cardinality("number") == "many"
    assert api.default_cardinality("filter") == "many"


def test_the_value_kinds_are_port_kinds():
    """A control's value can be routed, so each of the four kinds has to be
    a kind a port may declare (agents/SPACES.md §14)."""
    assert api.VALUE_KINDS == ("number", "boolean", "text", "choice")
    for kind in api.VALUE_KINDS:
        assert kind in api.PORT_KINDS, kind


def test_a_value_control_declares_an_input_port_named_for_it():
    """The step's rule: every control that carries a value is also an input
    port on its node type, named for the control, so a producer can drive
    the parameter.  A widget that is not a value (a `setting`, a button, a
    readout) declares nothing."""
    auto = api.control_input_ports(
        [{"name": "out", "kind": "number", "direction": "out"}],
        [
            {"key": "value", "widget": "slider"},
            {"key": "on", "widget": "switch"},
            {"key": "note", "widget": "field"},
            {"key": "mode", "widget": "choice", "choices": ["a", "b"]},
            {"key": "label", "widget": "setting"},
            {"key": "impulse", "widget": "impulse"},
            {"key": "duration", "widget": "indicator"},
        ],
    )
    assert auto == [
        {"name": "value", "kind": "number", "direction": "in",
         "drives_control": True, "cardinality": "one"},
        {"name": "on", "kind": "boolean", "direction": "in",
         "drives_control": True, "cardinality": "one"},
        {"name": "note", "kind": "text", "direction": "in",
         "drives_control": True, "cardinality": "one"},
        {"name": "mode", "kind": "choice", "direction": "in",
         "drives_control": True, "cardinality": "one"},
    ]


def test_a_control_may_state_its_own_kind_and_the_widget_is_the_fallback():
    # A module declares the widget and never the value kind; a widget the
    # table does not know says "not a value", and a stated kind wins.
    assert api.control_value_kind({"key": "x", "widget": "slider"}) == "number"
    assert api.control_value_kind({"key": "x", "widget": "switch"}) == "boolean"
    assert api.control_value_kind({"key": "x", "widget": "field"}) == "text"
    assert api.control_value_kind({"key": "x", "widget": "choice"}) == "choice"
    assert api.control_value_kind({"key": "x", "widget": "volume"}) == "number"
    assert api.control_value_kind({"key": "x", "widget": "picker"}) == ""
    assert api.control_value_kind({"key": "x", "widget": "indicator"}) == ""
    assert api.control_value_kind({"key": "x"}) == ""
    assert api.control_value_kind({}) == ""
    assert api.control_value_kind(
        {"key": "x", "widget": "indicator", "kind": "text"}
    ) == "text"


def test_a_controls_port_never_shadows_a_declared_one_or_repeats_a_key():
    """A module's own declaration wins, and a key declared twice (the
    shipped catalog does this) contributes one port."""
    declared = [{"name": "gain", "kind": "number", "direction": "in",
                 "cardinality": "many"}]
    assert api.control_input_ports(
        declared,
        [{"key": "gain", "widget": "gain"}, {"key": "gain", "widget": "setting"}],
    ) == []
    # An *output* of the same name is not an input port: the control still
    # gets its own input port.
    assert api.control_input_ports(
        [{"name": "value", "kind": "number", "direction": "out"}],
        [{"key": "value", "widget": "slider"}],
    ) == [{"name": "value", "kind": "number", "direction": "in",
           "drives_control": True, "cardinality": "one"}]


def test_a_port_that_drives_a_control_must_name_a_control_it_declares():
    """The daemon declares these itself; a module may declare one too, and
    then it has to point where the relay can deliver."""
    controls = [{"key": "gain", "widget": "gain"}]
    ok, message = api.validate_type_decl({
        "type_id": "t", "schema": 1,
        "ports": [{"name": "gain", "kind": "number", "direction": "in",
                   "drives_control": True, "cardinality": "many"}],
        "controls": controls,
    })
    assert ok, message
    for port, needle in [
        ({"name": "gain", "kind": "number", "direction": "out",
          "drives_control": True}, "input"),
        ({"name": "ghost", "kind": "number", "direction": "in",
          "drives_control": True}, "named for"),
        ({"name": "gain", "kind": "number", "direction": "in",
          "drives_control": "yes"}, "true or false"),
    ]:
        ok, message = api.validate_type_decl({
            "type_id": "t", "schema": 1, "ports": [port], "controls": controls,
        })
        assert not ok, port
        assert needle in message, message


def test_an_unknown_control_kind_is_refused():
    ok, message = api.validate_type_decl({
        "type_id": "t", "schema": 1,
        "controls": [{"key": "colour", "kind": "colour"}],
    })
    assert not ok and "kind" in message


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
