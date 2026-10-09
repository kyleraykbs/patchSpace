"""The client-side catalog: the GUI's lookups must answer exactly what the
daemon's modules declare, and degrade sanely with no catalog at all."""

import pytest

import node_catalog
from modules.pipewire import declarations


@pytest.fixture()
def installed():
    """Install the real payload the daemon would serve."""
    node_catalog.install(declarations.catalog(), declarations.menu(),
                         declarations.vocabulary(), declarations.legacy_aliases())
    yield node_catalog
    node_catalog.clear()


def test_the_canvas_import_surface_answers_from_the_wire(installed):
    spec = installed.spec_for("gate")
    assert spec.label == "Gate"
    assert spec.control == "fallback_onoff"
    assert spec.boolean_inputs == {"ctrl"}
    assert spec.inputs == ["audio", "ctrl"], "socket order is the declared order"
    assert spec.outputs == ["out"]
    assert spec.has_extra_row is True

    assert installed.port_kind("gate", "ctrl", "in") == "boolean"
    assert installed.port_kind("gate", "audio", "in") == "audio"
    assert installed.port_kind("gate", "out", "out") == "audio"
    # The legacy name for a node's first input still resolves.
    assert installed.port_kind("volume", "in", "in") == "audio"
    # A bundle merge's dynamic inputs are all bundle sockets.
    assert installed.port_kind("bundle", "in7", "in") == "bundle"
    assert installed.port_kind("filter", "filter3", "in") == "filter"


def test_pairing_follows_the_daemons_authority(installed):
    assert installed.ports_compatible("boolean_switch", "out", "gate", "ctrl") is True
    # Audio may not drive a control signal, and a control signal may not
    # drive an audio input.
    assert installed.ports_compatible("volume", "out", "gate", "ctrl") is False
    assert installed.ports_compatible("boolean_switch", "out", "volume", "audio") is False
    assert installed.ports_compatible("all_inputs", "out", "filter", "bundle") is True
    assert installed.ports_compatible("all_inputs", "out", "volume", "audio") is True
    assert installed.ports_compatible("button", "out", "gate", "ctrl") is False


def test_labels_colours_and_icons(installed):
    assert installed.type_label("volume", "mute_1") == "Mute Switch"
    assert installed.type_label("volume", "vol_1") == "Volume"
    assert installed.is_mute_node("mute_1") is True
    assert installed.is_mute_node("vol_1") is False
    assert installed.color_name_for_node_type("gate") == "success_color"
    assert installed.icon_for_add_node_type("gate")
    assert installed.description_for("gate").startswith("Passes or blocks")


def test_the_add_node_menu_keeps_its_order(installed):
    categories = [name for name, _items in installed.ADD_NODE_CATEGORIES]
    assert categories[0] == "Bundles"
    assert "Processing" in categories
    processing = dict(installed.ADD_NODE_CATEGORIES)["Processing"]
    assert ("Volume (slider)", "volume") in processing
    assert installed.ADD_NODE_MENU_ITEMS
    assert installed.ADD_NODE_MENU_ITEMS == [
        item for _cat, items in installed.ADD_NODE_CATEGORIES for item in items
    ]


def test_the_vocabulary_tables_come_from_the_module(installed):
    assert installed.FIELD_LABELS["pattern"] == "Pattern:"
    assert installed.media_class_label("Audio/Sink") == "Hardware Output"
    assert installed.media_class_label("Nonsense/Class") == "Nonsense/Class"
    assert installed.media_class_label("") == "(none)"
    assert installed.media_class_choices_for("media_class_output")
    assert installed.media_class_choices_for("media_class_classifier")


def test_settings_rows_and_tooltips_survive_the_wire(installed):
    spec = installed.spec_for("title_classifier")
    assert ("invert", "Invert (exclude matches)", "bool") in spec.settings
    assert spec.field == "title"
    assert spec.field_choices is True
    assert installed.setting_tooltip("reverb", "decay_time", "Decay") != "Decay"


def test_class_names_normalise_to_type_keys(installed):
    assert installed.normalize_node_type("VolumeProcessNode") == "volume"
    assert installed.normalize_node_type("volume") == "volume"
    assert installed.normalize_node_type("SomethingElse") == "SomethingElse"


def test_the_legacy_aliases_still_render(installed):
    assert installed.spec_for("patchbay_device") is installed.spec_for("patchspace_device")
    assert installed.spec_for("clip_previous") is installed.spec_for("replay_buffer")


def test_an_empty_catalog_degrades_to_the_generic_spec():
    node_catalog.clear()
    try:
        spec = node_catalog.spec_for("gate")
        assert spec.label == "Unknown"
        assert spec.inputs == ["audio"] and spec.outputs == ["out"]
        assert node_catalog.port_kind("gate", "out", "out") == "audio"
        assert node_catalog.ADD_NODE_CATEGORIES == []
    finally:
        node_catalog.clear()


def test_reinstalling_keeps_the_dict_the_widget_holds(installed):
    """The widget imports `NODE_TYPE_SPECS` once; a reconnect must update
    that same object, not replace it."""
    before = installed.NODE_TYPE_SPECS
    installed.install(declarations.catalog(), declarations.menu(),
                      declarations.vocabulary())
    assert installed.NODE_TYPE_SPECS is before
    assert "gate" in before
