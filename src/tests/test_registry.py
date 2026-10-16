"""The registry: module attach/detach, type declarations, placements (the
gray nodes), the migration handoff, and the status model."""

from central import module_api as api
from central.registry import Registry

CAMERA_IN = {
    "type_id": "camera_in",
    "schema": 1,
    "capabilities": ["camera-source"],
    "ports": [{"name": "out", "kind": "camera", "direction": "out"}],
    "display": {"name": "Camera In"},
}

VIRTUAL_CAM = {
    "type_id": "virtual_camera",
    "schema": 2,
    "capabilities": ["virtual-camera"],
    "ports": [
        {"name": "in", "kind": "camera", "direction": "in"},
        {"name": "out", "kind": "camera", "direction": "out", "multi": True},
    ],
    "display": {"name": "Virtual Cam"},
}


def _attached(reg: Registry, name: str = "pipewire") -> str:
    module_id = reg.allocate_module_id(name)
    reg.attach(module_id, name, "1.0")
    return module_id


def test_a_placement_with_no_provider_is_gray():
    reg = Registry()
    reg.place("mystery_1", "nobody_makes_this", space="main")
    instance = reg.instance("mystery_1")
    assert instance.status == api.STATUS_UNBACKED
    assert "no module" in instance.reason


def test_attach_declare_announce_binds_the_placement():
    reg = Registry()
    module_id = _attached(reg)
    reg.place("cam_1", "camera_in", config={"device": "/dev/video0", "schema_version": 1})

    errors, migrations = reg.declare_types(module_id, [CAMERA_IN])
    assert errors == []
    assert [m["instance_id"] for m in migrations] == ["cam_1"]
    assert migrations[0]["from_schema"] == 1 and migrations[0]["to_schema"] == 1
    # Declared but not announced: the module is attached, the node is not
    # running yet - offline, not a gray stranger.
    assert reg.instance("cam_1").status == api.STATUS_OFFLINE_MODULE

    bound, created, errors = reg.announce(module_id, [
        {"instance_id": "cam_1", "type_id": "camera_in",
         "config": {"device": "/dev/video0", "schema_version": 1}},
    ])
    assert (bound, created, errors) == (["cam_1"], [], [])
    assert reg.instance("cam_1").status == api.STATUS_ONLINE


def test_a_new_schema_is_reported_for_migration():
    reg = Registry()
    module_id = _attached(reg)
    reg.place("vcam_1", "virtual_camera", config={"schema_version": 1})
    errors, migrations = reg.declare_types(module_id, [VIRTUAL_CAM])
    assert errors == []
    assert migrations[0]["from_schema"] == 1
    assert migrations[0]["to_schema"] == 2


def test_a_type_the_module_no_longer_declares_is_not_migrated():
    reg = Registry()
    module_id = _attached(reg)
    reg.place("cam_1", "camera_in")
    errors, migrations = reg.declare_types(module_id, [VIRTUAL_CAM])
    assert errors == []
    assert migrations == []
    assert reg.instance("cam_1").status == api.STATUS_UNBACKED


def test_detach_offlines_and_reattach_recovers():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN])
    reg.announce(module_id, [{"instance_id": "cam_1", "type_id": "camera_in"}])
    assert reg.instance("cam_1").status == api.STATUS_ONLINE

    changed = reg.detach(module_id)
    assert "cam_1" in changed
    instance = reg.instance("cam_1")
    assert instance.status == api.STATUS_OFFLINE_MODULE
    assert "pipewire" in instance.reason

    reg.attach(module_id, "pipewire", "1.1")
    reg.declare_types(module_id, [CAMERA_IN])
    reg.announce(module_id, [{"instance_id": "cam_1", "type_id": "camera_in"}])
    assert reg.instance("cam_1").status == api.STATUS_ONLINE


def test_a_disabled_module_reads_offline_not_gray():
    """A type whose owner is known from persisted state but whose module is
    not attached: offline_module, never unbacked."""
    reg = Registry()
    reg.set_type_owner("camera_in", "pipewire")
    reg.place("cam_1", "camera_in")
    instance = reg.instance("cam_1")
    assert instance.status == api.STATUS_OFFLINE_MODULE
    assert "pipewire" in instance.reason


def test_offline_dependency_propagates_through_requires():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN, VIRTUAL_CAM])
    reg.announce(module_id, [
        {"instance_id": "cam_1", "type_id": "camera_in"},
        {"instance_id": "vcam_1", "type_id": "virtual_camera",
         "requires": ["cam_1"]},
        {"instance_id": "writer_1", "type_id": "virtual_camera",
         "requires": ["vcam_1"]},
        {"instance_id": "solo_1", "type_id": "camera_in"},
    ])
    assert reg.instance("cam_1").status == api.STATUS_ONLINE

    reg.unannounce(module_id, ["cam_1"])
    assert reg.instance("cam_1").status == api.STATUS_OFFLINE_MODULE
    assert reg.instance("vcam_1").status == api.STATUS_OFFLINE_DEP
    assert reg.instance("writer_1").status == api.STATUS_OFFLINE_DEP
    assert "cam_1" in reg.instance("vcam_1").reason
    # An unrelated node is untouched by someone else's outage.
    assert reg.instance("solo_1").status == api.STATUS_ONLINE


def test_a_requires_cycle_terminates_and_stays_online():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN])
    reg.announce(module_id, [
        {"instance_id": "a", "type_id": "camera_in", "requires": ["b"]},
        {"instance_id": "b", "type_id": "camera_in", "requires": ["a"]},
    ])
    assert reg.instance("a").status == api.STATUS_ONLINE
    assert reg.instance("b").status == api.STATUS_ONLINE


def test_a_cycle_with_one_offline_member_terminates():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN])
    reg.announce(module_id, [
        {"instance_id": "a", "type_id": "camera_in", "requires": ["b"]},
        {"instance_id": "b", "type_id": "nobody_makes_this", "requires": ["a"]},
    ])
    assert reg.instance("b").status == api.STATUS_UNBACKED
    assert reg.instance("a").status == api.STATUS_OFFLINE_DEP


def test_unannounce_keeps_the_placement_and_its_config():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN])
    reg.announce(module_id, [
        {"instance_id": "cam_1", "type_id": "camera_in", "config": {"device": "/dev/video0"}},
    ])
    assert reg.unannounce(module_id, ["cam_1"]) == ["cam_1"]
    instance = reg.instance("cam_1")
    assert instance.status == api.STATUS_OFFLINE_MODULE
    assert instance.config == {"device": "/dev/video0"}


def test_two_connected_modules_cannot_claim_one_type():
    reg = Registry()
    first = _attached(reg, "pipewire")
    second = _attached(reg, "primitives")
    assert reg.declare_types(first, [CAMERA_IN])[0] == []
    errors, _ = reg.declare_types(second, [CAMERA_IN])
    assert errors and "already declared" in errors[0]


def test_capability_and_type_queries_follow_the_connection():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN, VIRTUAL_CAM])

    reply = reg.capability_query("virtual-camera")
    assert reply["available"] is True
    assert [t["type_id"] for t in reply["types"]] == ["virtual_camera"]
    assert reg.capability_query("time-machine")["available"] is False

    assert reg.type_query("camera_in")["available"] is True
    assert reg.type_query("nobody_makes_this")["available"] is False

    reg.detach(module_id)
    assert reg.capability_query("virtual-camera")["available"] is False
    assert reg.type_query("camera_in")["available"] is False


def test_port_introspection_answers_the_graph_rules():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [VIRTUAL_CAM])
    reg.place("vcam_1", "virtual_camera")

    assert reg.port_kind("vcam_1", "in", "in") == "camera"
    assert reg.port_kind("vcam_1", "out", "out") == "camera"
    assert reg.port_kind("vcam_1", "out", "in") == ""
    assert reg.port_kind("nobody", "out", "out") == ""
    assert reg.port_multi("vcam_1", "out") is True
    assert reg.port_multi("vcam_1", "in") is False
    # S4: cardinality resolves through the declaration — the legacy
    # `multi: true` is a declaration of "many", a camera input that declares
    # nothing keeps the pre-S4 rule, and the merge declaration is surfaced.
    assert reg.port_cardinality("vcam_1", "in", "in") == "one"
    assert reg.port_cardinality("vcam_1", "out", "out") == "many"
    assert reg.port_cardinality("nobody", "in", "in") == "many"
    assert reg.port_merge("vcam_1", "in", "in") == ""


def test_port_cardinality_reads_the_declaration_first():
    """The declaration wins over the kind's default, in both directions,
    and a declared merge is itself a declaration of several drivers."""
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [{
        "type_id": "declared_ports",
        "schema": 1,
        "ports": [
            {"name": "one_audio", "kind": "audio", "direction": "in",
             "cardinality": "one"},
            {"name": "many_boolean", "kind": "boolean", "direction": "in",
             "cardinality": "many"},
            {"name": "merged", "kind": "number", "direction": "in",
             "merge": "sum"},
            {"name": "plain", "kind": "boolean", "direction": "in"},
        ],
    }])
    reg.place("declared_1", "declared_ports")

    assert reg.port_cardinality("declared_1", "one_audio", "in") == "one"
    assert reg.port_cardinality("declared_1", "many_boolean", "in") == "many"
    assert reg.port_cardinality("declared_1", "merged", "in") == "many"
    assert reg.port_cardinality("declared_1", "plain", "in") == "one"
    assert reg.port_merge("declared_1", "merged", "in") == "sum"
    assert reg.port_merge("declared_1", "plain", "in") == ""
    # The declaration travels in the catalog a display installs, unchanged.
    ports = reg.type_query("declared_ports")["type"]["ports"]
    assert [p.get("merge") for p in ports] == [None, None, "sum", None]
    assert [p.get("cardinality") for p in ports] == ["one", "many", None, None]


def test_snapshot_is_serialisable_and_complete():
    reg = Registry()
    module_id = _attached(reg)
    reg.declare_types(module_id, [CAMERA_IN])
    reg.announce(module_id, [{"instance_id": "cam_1", "type_id": "camera_in"}])
    snapshot = reg.snapshot()
    assert [m["module_id"] for m in snapshot["modules"]] == [module_id]
    assert [t["type_id"] for t in snapshot["types"]] == ["camera_in"]
    assert snapshot["instances"][0]["status"] == api.STATUS_ONLINE


# ----------------------------------------------------------------------
# a node's parameter as an input port (agents/SPACES.md §14)
# ----------------------------------------------------------------------

CONTROL_SINK = {
    "type_id": "control_sink",
    "schema": 1,
    "ports": [{"name": "in", "kind": "number", "direction": "in"}],
    "controls": [
        {"key": "gain", "widget": "gain"},
        {"key": "gain", "widget": "setting"},
        {"key": "note", "widget": "field"},
        {"key": "mode", "widget": "choice", "choices": ["a", "b"]},
        {"key": "label", "widget": "setting"},
    ],
    "display": {"name": "Control Sink"},
}

SETTINGS_ONLY = {
    "type_id": "settings_only",
    "schema": 1,
    "ports": [{"name": "out", "kind": "audio", "direction": "out"}],
    "controls": [{"key": "description", "widget": "setting"}],
    "display": {"name": "Settings Only"},
}


def _control_reg():
    reg = Registry()
    module_id = _attached(reg)
    errors, _ = reg.declare_types(module_id, [CONTROL_SINK, SETTINGS_ONLY])
    assert errors == []
    reg.announce(module_id, [
        {"instance_id": "s1", "type_id": "control_sink",
         "config": {"gain": 0.25, "note": "hi"}},
    ])
    return reg, module_id


def test_a_value_control_is_an_input_port_in_the_type_a_display_reads():
    """The auto-declared port appears wherever a type's ports are asked for
    — the catalog/type_query the display installs, and the graph rules — so
    there is one declaration, not two."""
    reg, _ = _control_reg()
    ports = reg.type_query("control_sink")["type"]["ports"]
    assert {(p["name"], p.get("kind"), p["direction"]) for p in ports} == {
        ("in", "number", "in"),
        ("gain", "number", "in"),
        ("note", "text", "in"),
        ("mode", "choice", "in"),
    }
    gain = next(p for p in ports if p["name"] == "gain")
    assert gain == {"name": "gain", "kind": "number", "direction": "in",
                    "drives_control": True, "cardinality": "one"}
    # ...and it is what the daemon's side of the rules resolves.
    assert reg.port_kind("s1", "gain", "in") == "number"
    assert reg.port_drives_control("s1", "gain") == "gain"
    assert reg.port_cardinality("s1", "gain", "in") == "one"
    assert reg.port_drives_control("s1", "note") == "note"
    # An ordinary port drives nothing, and neither does an output.
    assert reg.port_drives_control("s1", "in") == ""
    assert reg.port_drives_control("s1", "out") == ""


def test_a_type_that_declares_only_settings_gains_nothing():
    """`setting` is a widget, not a value: an old-style declaration keeps
    exactly the ports it declared (nothing here breaks an existing type)."""
    reg, _ = _control_reg()
    assert reg.type_query("settings_only")["type"]["ports"] == [
        {"name": "out", "kind": "audio", "direction": "out"},
    ]


def test_a_controls_local_value_is_captured_and_the_user_overrides_it():
    """The stored value the unwire goes back to: what the control held when
    it was first driven, updated whenever the user sets it — and nothing at
    all for a control nothing has ever set."""
    reg, _ = _control_reg()
    # Nothing stored yet, and no drive has captured anything.
    assert reg.local_control_value("s1", "gain") == (False, None)

    # Driving it captures the config's value: that is what it held.
    assert reg.capture_control("s1", "gain") == (True, 0.25)
    assert reg.local_control_value("s1", "gain") == (True, 0.25)
    # A second capture (another driver on a `many` port) does not move it.
    assert reg.capture_control("s1", "gain") == (True, 0.25)

    # The user setting the control is the other writer, and it is recorded
    # in the instance's config too (what a re-attaching module is handed).
    assert reg.set_control_value("s1", "gain", 0.9) is True
    assert reg.local_control_value("s1", "gain") == (True, 0.9)
    assert reg.instance("s1").config["gain"] == 0.9

    # A control with no config entry has no stored value: the daemon says so
    # instead of inventing one.
    assert reg.capture_control("s1", "mode") == (False, None)

    # Forgetting the instance forgets its controls with it.
    reg.forget_instance("s1")
    assert reg.local_control_value("s1", "gain") == (False, None)
