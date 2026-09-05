"""
node_specs.py

Declarative registry of PatchSpace node types.

The original widget had the same "one entry per node type" knowledge
spread across four separate if/elif chains: socket counts, the inline
control drawn on the node body, the settings-dialog fields, and the
display label - and it was easy for a control (e.g. a slider) to be
drawn/hit-tested correctly but never actually wired to a
`client.send(...)` call, because nothing forced the drawing code and
the command-sending code to agree on which node types have a control
at all. Centralizing that here means there is exactly one place that
says "this node type has a volume control", and both the drawing code
and the command-sending code in PatchSpaceGraphWidget read it from
here instead of keeping their own parallel lists.

To add a new node type: add one entry to NODE_TYPE_SPECS (and, if the
daemon might report it as a raw Python class name instead of the
short key, one entry to CLASS_NAME_TO_TYPE).
"""

from __future__ import annotations

from typing import Dict, List, Optional

FIELD_LABELS = {
    "pattern": "Pattern:",
    "media_class": "Media Class:",
    "description": "Description:",
}

# Human-readable labels for the raw PipeWire media.class strings that
# media_class_input/media_class_output nodes filter on. Centralized
# here for the same reason as NODE_TYPE_SPECS above: one list that
# both the inline field popover and the settings-dialog dropdown in
# patchspace_widget.py read, instead of a free-text entry that showed
# the raw "Stream/Output/Audio"-style strings directly.
#
# This is the full set (used for *display* - see media_class_label()
# below, which has to turn any raw value into something readable
# regardless of which node it's on) but it is NOT the right choice
# list to offer in either direction's dropdown - see
# MEDIA_CLASS_INPUT_CHOICES / MEDIA_CLASS_OUTPUT_CHOICES for why.
MEDIA_CLASS_CHOICES = [
    ("Hardware Output", "Audio/Sink"),
    ("Hardware Input", "Audio/Source"),
    ("App Playback", "Stream/Output/Audio"),
    ("App Recording", "Stream/Input/Audio"),
]

# Choices for a media_class_input node's dropdown. media_class_input
# is a *source* filter (pwmatch.find_source_nodes), which only ever
# matches SOURCE_MEDIA_CLASSES - nodes with OUTPUT ports to pull audio
# from: a hardware capture device, an app's playback stream, or a
# virtual sink's monitor ports (loopback). "App Recording" is
# deliberately absent: a Stream/Input/Audio node has no output ports,
# so it could never actually match anything as a source.
MEDIA_CLASS_INPUT_CHOICES = [
    ("Hardware Input", "Audio/Source"),
    ("Hardware Output (loopback)", "Audio/Sink"),
    ("App Playback", "Stream/Output/Audio"),
]

# Choices for a media_class_output node's dropdown. media_class_output
# is a *target* filter (pwmatch.find_target_nodes) whose matches then
# get connected via their INPUT ports (port_groups_for_node(...,
# "in")) - so only nodes that have input ports belong here: a
# hardware playback device, or an app's recording/input stream.
# "Hardware Input" (a mic - output ports only) and "App Playback" (an
# app's own output ports) are deliberately absent: picking either
# here would produce a target with no input ports to connect into, so
# resolve_channel_pairs() would just never find any channel pairs.
MEDIA_CLASS_OUTPUT_CHOICES = [
    ("Hardware Output", "Audio/Sink"),
    ("App Recording", "Stream/Input/Audio"),
]


def media_class_choices_for(node_type: str) -> List[tuple]:
    """Which MEDIA_CLASS_CHOICES-style list to offer in a dropdown for
    this node type - see the two lists above for why they differ.
    Anything that isn't media_class_output falls back to the input
    list, since media_class_input is the only other caller today."""
    if node_type == "media_class_output":
        return MEDIA_CLASS_OUTPUT_CHOICES
    return MEDIA_CLASS_INPUT_CHOICES


def media_class_label(value: Optional[str]) -> str:
    """Human-readable label for a raw media.class value. Falls back to
    the raw string itself (or "(none)") for anything not in
    MEDIA_CLASS_CHOICES, so a value set outside the GUI - e.g. a
    custom media.class from apply_config.py - still displays as
    *something* instead of silently vanishing."""
    for label, raw in MEDIA_CLASS_CHOICES:
        if raw == value:
            return label
    return value or "(none)"


class NodeSpec:
    __slots__ = ("label", "inputs", "outputs", "control", "field")

    def __init__(
        self,
        label: str,
        inputs: List[str],
        outputs: List[str],
        control: Optional[str] = None,
        field: Optional[str] = None,
    ):
        self.label = label
        self.inputs = inputs
        self.outputs = outputs
        # None | "gate" | "volume" - which inline control (if any) is
        # drawn on the node body and wired to a daemon command.
        self.control = control
        # None | "pattern" | "media_class" | "description" - which
        # single string property (if any) is edited via an inline
        # text field + a settings-dialog row.
        self.field = field

    @property
    def has_extra_row(self) -> bool:
        """Whether this node type draws something in the bottom area
        that needs extra node height + a socket offset so ports don't
        overlap it."""
        return self.control is not None or self.field is not None


NODE_TYPE_SPECS: Dict[str, NodeSpec] = {
    "regex_input": NodeSpec("Regex In", [], ["out"], field="pattern"),
    "regex_output": NodeSpec("Regex Out", ["in"], [], field="pattern"),
    "media_class_input": NodeSpec("Media Class In", [], ["out"], field="media_class"),
    "media_class_output": NodeSpec("Media Class Out", ["in"], [], field="media_class"),
    "description_input": NodeSpec("Description In", [], ["out"], field="description"),
    "description_output": NodeSpec("Description Out", ["in"], [], field="description"),
    "splitter": NodeSpec("Splitter", ["in"], ["out"]),
    "gate": NodeSpec("Gate", ["in"], ["out"], control="gate"),
    "exclude_filter": NodeSpec("Exclude (Regex)", ["in"], ["out"], field="pattern"),
    "volume": NodeSpec("Volume", ["in"], ["out"], control="volume"),
}

NODE_TYPE_SPECS.update(
    {
        "device_input": NodeSpec("Hardware Input", [], ["out"]),
        "device_output": NodeSpec("Hardware Output", ["in"], []),
        "app_input": NodeSpec("App Input", [], ["out"]),
        "app_output": NodeSpec("App Output", ["in"], []),
    }
)

# Menu entries for the right-click "add node" popover and the add-node
# side panel, grouped into the categories the side panel shows as
# collapsible sections (patchspace_widget.build_add_node_panel()).
# "mute" isn't a real node type - _on_add_node()/add_node_at() map it
# to a real "volume" node whose id happens to be prefixed "mute_",
# which is how is_mute_node() tells a slider-style volume node from a
# checkbox-style one without needing any backend changes.
#
# ADD_NODE_MENU_ITEMS is the flat form (still used by the right-click
# popover, which has no notion of categories) - derived from
# ADD_NODE_CATEGORIES below so the two can't drift apart.
ADD_NODE_CATEGORIES = [
    (
        "Filters",
        [
            ("Regex Input", "regex_input"),
            ("Media Class Input", "media_class_input"),
            ("Description Input", "description_input"),
            ("Regex Output", "regex_output"),
            ("Media Class Output", "media_class_output"),
            ("Description Output", "description_output"),
        ],
    ),
    (
        "Processing",
        [
            ("Splitter", "splitter"),
            ("Gate (checkbox)", "gate"),
            ("Exclude Filter (regex)", "exclude_filter"),
            ("Volume (slider)", "volume"),
            ("Mute Switch (checkbox)", "mute"),
        ],
    ),
    (
        "Hardware & Apps",
        [
            ("Hardware Input", "device_input"),
            ("Hardware Output", "device_output"),
            ("App Input", "app_input"),
            ("App Output", "app_output"),
        ],
    ),
]

ADD_NODE_MENU_ITEMS = [
    item for _category, items in ADD_NODE_CATEGORIES for item in items
]

# The daemon is *supposed* to serialize node types via its own
# CLASS_TO_TYPE map (in main.py) into the short keys used above. When
# that doesn't happen (older daemon build, a node whose type() isn't
# literally the registered class, etc.) what arrives here instead is
# the raw backend class name, e.g. "VolumeProcessNode". Those strings
# don't match any key in NODE_TYPE_SPECS, so every lookup keyed on
# node["type"] would silently miss: no slider, no checkbox, no inline
# field, and the label would show the raw class name instead of
# something readable. normalize_node_type() defends against that
# unconditionally on the client side, independent of what the daemon
# actually sends.
CLASS_NAME_TO_TYPE = {
    "RegexInputNode": "regex_input",
    "RegexOutputNode": "regex_output",
    "MediaClassInputNode": "media_class_input",
    "MediaClassOutputNode": "media_class_output",
    "DescriptionInputNode": "description_input",
    "DescriptionOutputNode": "description_output",
    "SplitterNode": "splitter",
    "GateNode": "gate",
    "ExcludeFilterNode": "exclude_filter",
    "VolumeProcessNode": "volume",
}

CLASS_NAME_TO_TYPE.update(
    {
        "DeviceInputNode": "device_input",
        "DeviceOutputNode": "device_output",
        "AppInputNode": "app_input",
        "AppOutputNode": "app_output",
    }
)

_FALLBACK_SPEC = NodeSpec("Unknown", ["in"], ["out"])


def normalize_node_type(raw_type: str) -> str:
    """Map a backend class name to its canonical short key. A no-op
    passthrough if `raw_type` is already a canonical key, or is some
    genuinely unrecognized type (so it still shows up as itself rather
    than vanishing to None)."""
    return CLASS_NAME_TO_TYPE.get(raw_type, raw_type)


def spec_for(node_type: str) -> NodeSpec:
    """Look up a node's spec, falling back to a generic in/out node
    with no control so an unrecognized type still renders instead of
    raising."""
    return NODE_TYPE_SPECS.get(node_type, _FALLBACK_SPEC)


def is_mute_node(node_id) -> bool:
    """A "mute switch" is a plain 'volume' node created via the "Mute
    Switch" add-node entry, identified purely by its id prefix (no
    backend changes needed) and driven as 0.0/1.0 instead of a
    continuous slider."""
    return str(node_id).startswith("mute_")


def type_label(node_type: str, node_id) -> str:
    if node_type == "volume" and is_mute_node(node_id):
        return "Mute Switch"
    return spec_for(node_type).label
