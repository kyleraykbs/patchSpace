"""The PipeWire module's type catalog must describe every node type the
daemon can construct, and describe it validly.

Phase 2 moved the data out of `gui/node_specs.py` into
`modules/pipewire/catalog.json`: this is the guard that keeps the module's
declarations (the only thing a display draws from) in step with the node
classes the daemon can build.
"""

import json
import os

import pytest

from central import module_api as api
from central.main import NODE_TYPE_REGISTRY
from modules.pipewire import declarations


@pytest.fixture(scope="module")
def catalog():
    return declarations.catalog()


def _canonical_keys():
    return set(declarations.canonical_types(NODE_TYPE_REGISTRY))


def test_the_catalog_file_is_packaged_data():
    """It travels with the module (the flake copies `src`), so a display
    always has it without a GUI-side table."""
    assert os.path.isfile(declarations.CATALOG_PATH)
    with open(declarations.CATALOG_PATH, encoding="utf-8") as handle:
        payload = json.load(handle)
    assert payload["module"] == "pipewire"
    assert payload["types"]


def test_the_catalog_covers_every_canonical_type(catalog):
    assert {d["type_id"] for d in catalog} == _canonical_keys()
    assert declarations.missing_types(NODE_TYPE_REGISTRY) == []


def test_the_announced_set_is_the_canonical_set():
    """`declarations(type_map)` filters the catalog to what the daemon can
    build, and the legacy aliases are not separately declared."""
    announced = declarations.declarations(NODE_TYPE_REGISTRY)
    declared = {d["type_id"] for d in announced}
    assert declared == _canonical_keys()
    assert "patchbay_device" not in declared
    assert "clip_previous" not in declared


def test_every_declaration_is_wire_valid(catalog):
    for decl in catalog:
        ok, message = api.validate_type_decl(decl)
        assert ok, (decl["type_id"], message)
        for port in decl["ports"]:
            assert port["kind"] in api.PORT_KINDS, (decl["type_id"], port)
            assert port["direction"] in (api.DIR_IN, api.DIR_OUT)


def test_every_type_has_something_to_draw(catalog):
    for decl in catalog:
        display = decl["display"]
        assert display["name"], decl["type_id"]
        assert "category" in display and "color" in display


def test_known_details_of_a_few_types(catalog):
    by_type = {d["type_id"]: d for d in catalog}

    volume = by_type["volume"]
    assert {(p["name"], p["kind"], p["direction"]) for p in volume["ports"]} == {
        ("audio", "audio", "in"), ("out", "audio", "out"),
    }
    assert volume["controls"], "volume's slider must be declared"
    assert volume["display"]["category"] == "Processing"

    gate = by_type["gate"]
    kinds = {(p["name"], p["kind"]) for p in gate["ports"]}
    assert ("ctrl", "boolean") in kinds, "a gate takes a control signal"
    assert ("audio", "audio") in kinds, "and audio through"

    # A filter grows one socket per wired classifier, a bundle one per
    # member: those ports are marked dynamic for the display.
    filter_ports = [p for p in by_type["filter"]["ports"] if p["kind"] == "filter"]
    assert filter_ports and all(p.get("dynamic") for p in filter_ports)

    # The space/panel I/O nodes are real registered types (they carry a
    # space's face ports) even though no add-node menu offers them.
    assert "panel_in" in by_type and "panel_out" in by_type
