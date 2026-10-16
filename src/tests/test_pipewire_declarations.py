"""The PipeWire module's type catalog must describe every node type the
daemon can construct, and describe it validly.

Phase 2 moved the data out of `gui/node_catalog.py` into
`modules/pipewire/catalog.json`: this is the guard that keeps the module's
declarations (the only thing a display draws from) in step with the node
classes the daemon can build.
"""

import json
import os

import pytest

from central import module_api as api
from central.main import NODE_TYPE_REGISTRY
from central.registry import Registry
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

    # The space I/O nodes are real registered types (they carry a
    # space's face ports) even though no add-node menu offers them.
    assert "space_in" in by_type and "space_out" in by_type
    # Their pre-rename ids (`panel_in`, ...) are `legacy_aliases`: a space
    # file or session written before S0 still resolves to the canonical type
    # (the id is a persistent contract), while what this module announces -
    # and what an export therefore writes - is the canonical one.
    aliases = declarations.legacy_aliases()
    assert aliases["panel_in"] == "space_in"
    assert aliases["panel_out"] == "space_out"
    assert aliases["bool_panel_in"] == "bool_space_in"
    assert aliases["bool_panel_out"] == "bool_space_out"
    assert "panel_in" not in by_type


# ----------------------------------------------------------------------
# S4: cardinality is declared, never assumed (agents/SPACES.md §15)
# ----------------------------------------------------------------------

#: The rule the deleted `central/graph.py` `SINGLE_DRIVER_KINDS` list named,
#: written out here so the shipped catalog is measured against the rule that
#: was in force *before* cardinality existed.  (`api.default_cardinality` is
#: the same rule resolved at runtime; the sweep below is the independent
#: statement of it, so a port that changes an accept/refuse decision without
#: declaring it fails here.)
PRE_S4_ONE_DRIVER_KINDS = frozenset({"boolean", "impulse", "sound", "camera"})


def _shipped_registry(catalog):
    """A registry holding the shipped catalog, so cardinality resolves
    through the daemon's own path (`Registry.port_cardinality`)."""
    reg = Registry()
    module_id = reg.allocate_module_id("pipewire")
    reg.attach(module_id, "pipewire", "1.0")
    errors, _ = reg.declare_types(module_id, catalog)
    assert errors == []
    return reg


def test_every_port_that_declares_nothing_resolves_as_it_did_before(catalog):
    """The sweep S4's safety rests on: a port that declares no cardinality
    keeps the pre-S4 rule exactly, for every port of every shipped type (no
    hand list of types), so no existing type changed behaviour."""
    reg = _shipped_registry(catalog)
    inspected = declared = 0
    for decl in catalog:
        type_id = decl["type_id"]
        instance_id = f"probe_{type_id}"
        reg.place(instance_id, type_id)
        for port in decl["ports"]:
            inspected += 1
            resolved = reg.port_cardinality(instance_id, port["name"],
                                            port["direction"])
            if "cardinality" in port or "merge" in port:
                declared += 1
                assert resolved == (port.get("cardinality")
                                    or api.CARDINALITY_MANY), (type_id, port)
                continue
            before = ("one" if port["kind"] in PRE_S4_ONE_DRIVER_KINDS
                      else "many")
            assert resolved == before, (type_id, port)
    assert inspected > 100, "the sweep must actually walk the catalog"
    assert declared, "the shipped catalog does declare the buses"


def test_only_the_space_port_buses_declare_a_cardinality(catalog):
    """`agents/SPACES.md` §15 ships the declaration where it matters and
    nowhere else: the four space port types, whose input is a bus today."""
    sites = sorted(
        (decl["type_id"], port["name"])
        for decl in catalog
        for port in decl["ports"]
        if "cardinality" in port or "merge" in port
    )
    assert sites == [
        ("bool_space_in", "boolean"),
        ("bool_space_out", "boolean"),
        ("space_in", "audio"),
        ("space_out", "audio"),
    ]


def test_the_space_ports_declare_what_their_node_classes_already_do(catalog):
    """Not a new opinion: each declaration states the bus its class already
    implements (`MIX_INPUTS`/`allows_multiple_inputs` for the audio ports,
    `ALLOW_MULTIPLE_BOOLEAN` for the boolean ones) - which is what the old
    docstrings claimed and the by-kind list denied."""
    declared = {
        decl["type_id"]: port
        for decl in catalog
        for port in decl["ports"]
        if port.get("cardinality")
    }
    assert set(declared) == {"space_in", "space_out",
                             "bool_space_in", "bool_space_out"}
    for type_id in ("space_in", "space_out"):
        assert declared[type_id]["cardinality"] == "many"
        assert NODE_TYPE_REGISTRY[type_id].MIX_INPUTS is True, type_id
    for type_id in ("bool_space_in", "bool_space_out"):
        assert declared[type_id]["cardinality"] == "many"
        assert NODE_TYPE_REGISTRY[type_id].ALLOW_MULTIPLE_BOOLEAN is True, type_id
    # ... and the daemon now gives the same answer the model always did.
    reg = _shipped_registry(catalog)
    for type_id in declared:
        reg.place(f"declared_{type_id}", type_id)
        port = declared[type_id]
        assert reg.port_cardinality(f"declared_{type_id}", port["name"],
                                    port["direction"]) == "many"
