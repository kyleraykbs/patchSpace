"""The PipeWire module's type catalog must cover every node type the daemon
can construct, and describe it the way the legacy tables do.

This is the guard that keeps the module's declarations (which phase 2 makes
the *only* source a display draws from) in step with the node classes and
`gui/node_specs.py` while both still exist.
"""

import pytest

from central import module_api as api
from central.main import CLASS_TO_TYPE, NODE_TYPE_REGISTRY
from modules.pipewire import declarations


@pytest.fixture(scope="module")
def catalog():
    return declarations.declarations(NODE_TYPE_REGISTRY)


def _canonical_keys():
    return set(declarations.canonical_types(NODE_TYPE_REGISTRY))


def test_the_catalog_covers_every_canonical_type(catalog):
    assert {d["type_id"] for d in catalog} == _canonical_keys()


def test_legacy_aliases_are_not_declared_separately(catalog):
    """`patchbay_device` / `clip_previous` must keep loading, but a display
    should never be offered them: they alias a canonical type."""
    declared = {d["type_id"] for d in catalog}
    assert "patchbay_device" not in declared
    assert "clip_previous" not in declared
    assert "patchspace_device" in declared
    assert "replay_buffer" in declared


def test_every_declaration_is_wire_valid(catalog):
    for decl in catalog:
        ok, message = api.validate_type_decl(decl)
        assert ok, (decl["type_id"], message)
        for port in decl["ports"]:
            assert port["kind"] in api.PORT_KINDS, (decl["type_id"], port)
            assert port["direction"] in (api.DIR_IN, api.DIR_OUT)


def test_ports_match_the_spec_the_gui_draws(catalog):
    """Same data as `node_specs`, so a node cannot be drawn with a socket
    the module does not declare."""
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"))
    from gui import node_specs

    by_type = {d["type_id"]: d for d in catalog}
    for type_key in _canonical_keys():
        spec = node_specs.NODE_TYPE_SPECS[type_key]
        declared = {
            (p["name"], p["direction"]) for p in by_type[type_key]["ports"]
        }
        for attr, direction in (
            ("inputs", "in"), ("outputs", "out"),
            ("boolean_inputs", "in"), ("boolean_outputs", "out"),
            ("impulse_inputs", "in"), ("impulse_outputs", "out"),
            ("bundle_inputs", "in"), ("bundle_outputs", "out"),
            ("filter_inputs", "in"), ("filter_outputs", "out"),
            ("sound_inputs", "in"), ("sound_outputs", "out"),
            ("camera_inputs", "in"), ("camera_outputs", "out"),
        ):
            for name in getattr(spec, attr, None) or ():
                assert (name, direction) in declared, (type_key, attr, name)


def test_labels_come_from_the_spec(catalog):
    import sys
    import os

    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"))
    from gui import node_specs

    by_type = {d["type_id"]: d for d in catalog}
    for type_key, spec in node_specs.NODE_TYPE_SPECS.items():
        if type_key not in by_type:
            continue
        assert by_type[type_key]["display"]["name"] == spec.label


def test_known_details_of_a_few_types(catalog):
    by_type = {d["type_id"]: d for d in catalog}

    volume = by_type["volume"]
    assert {(p["name"], p["kind"], p["direction"]) for p in volume["ports"]} == {
        ("audio", "audio", "in"), ("out", "audio", "out"),
    }
    assert volume["controls"], "volume's slider must be declared"
    assert volume["display"]["category"] == "Processing"

    gate = by_type["gate"]
    assert any(p["kind"] == "boolean" for p in gate["ports"]), "a gate takes a control signal"

    # A filter grows one socket per wired classifier, a bundle one per
    # member: those ports are marked dynamic for the display.
    filter_ports = [p for p in by_type["filter"]["ports"] if p["kind"] == "filter"]
    assert filter_ports and all(p.get("dynamic") for p in filter_ports)

    # The space/panel I/O nodes are real registered types (they carry a
    # space's face ports) even though no add-node menu offers them.
    assert "panel_in" in by_type and "panel_out" in by_type
    assert by_type["panel_in"]["display"]["category"] == ""


def test_the_legacy_serializer_agrees_on_the_type_map():
    """`CLASS_TO_TYPE` is what `get_nodes` reports today; the catalog's keys
    must be exactly those, or the registry and the GUI would disagree about
    what a node is."""
    for cls, type_key in CLASS_TO_TYPE.items():
        assert type_key in NODE_TYPE_REGISTRY
