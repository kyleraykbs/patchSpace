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
    "device_label": "Name:",
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
    __slots__ = ("label", "inputs", "outputs", "control", "field", "settings")

    def __init__(
        self,
        label: str,
        inputs: List[str],
        outputs: List[str],
        control: Optional[str] = None,
        field: Optional[str] = None,
        settings: Optional[List[tuple]] = None,
    ):
        self.label = label
        self.inputs = inputs
        self.outputs = outputs
        # None | "gate" | "volume" | "wetdry" | "sensitivity" - which
        # inline control (if any) is drawn on the node body and wired
        # to a daemon command. "gate" is the on/off toggle, "volume"
        # the gain slider, "wetdry" the 0..1 dry/wet mix slider
        # (Reverb), "sensitivity" the Sensitivity Gate's 0..1
        # gain-staging slider (handed to the daemon, which drives the
        # hidden pre/post Volume nodes it owns - see that node's
        # comment below).
        self.control = control
        # None | "pattern" | "media_class" | "description" - which
        # single string property (if any) is edited via an inline
        # text field + a settings-dialog row.
        self.field = field
        # Optional list of settings-dialog-only rows, each either a
        # 3-tuple (attr, label, kind) or a 4-tuple (attr, label, kind,
        # extra) when the widget needs more than a label - kind is one
        # of:
        #   "text"   - free-text entry (extra unused)
        #   "bool"   - checkbox (extra unused)
        #   "number" - spin button; extra = {"min", "max", "step"}
        #   "choice" - dropdown; extra = {"choices": [(label, value), ...]}
        # These are arbitrary per-node daemon properties (see main.py's
        # set_node_property) - e.g. the echo-cancel module options or
        # Noise Cancel's VAD dial - that don't get an inline
        # field on the node body, only a settings row.
        self.settings = settings

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
    "switcher": NodeSpec("Switcher", ["in"], ["a", "b"], control="switcher"),
    "inverse_switcher": NodeSpec(
        "Inverse Switcher", ["a", "b"], ["out"], control="switcher"
    ),
    "exclude_filter": NodeSpec("Exclude (Regex)", ["in"], ["out"], field="pattern"),
    "volume": NodeSpec("Volume", ["in"], ["out"], control="volume"),
}

NODE_TYPE_SPECS.update(
    {
        "echo_cancel": NodeSpec(
            "Echo Cancel",
            ["mic", "probe"],
            ["out"],
            settings=[
                ("library_name", "AEC library:", "text"),
                ("aec_args", "AEC args:", "text"),
                ("monitor_mode", "Monitor mode (auto-cancel default sink)", "bool"),
            ],
        ),
        "noise_cancel": NodeSpec(
            "Noise Cancel",
            ["in"],
            ["out"],
            settings=[
                (
                    "vad_threshold",
                    "Sensitivity, RNNoise (0-100):",
                    "number",
                    {"min": 0, "max": 100, "step": 1},
                ),
                ("ladspa_plugin", "Override plugin path (blank = auto):", "text"),
                ("ladspa_label", "Override plugin label (blank = auto):", "text"),
            ],
        ),
        # control="sensitivity" is the Sensitivity Gate's 0..1 inline
        # slider. It is deliberately *not* wired to this node's own live
        # LADSPA threshold (that set-param path isn't reliable on every
        # build - see SensitivityGateNode's docstring). Instead the
        # daemon invisibly brackets every Sensitivity gate with two
        # hidden VolumeProcessNodes - a pre-gain and an equal-and-
        # opposite post-gain (main.py's _ensure_sensitivity_internals) -
        # and routes the user's edges through them. The pre gain swings
        # below unity (more aggressive gating) and above it (opens more
        # easily), with the post node's reciprocal keeping output
        # loudness constant. The slider just sends
        # set_node_property("sensitivity", <0..1>) and the daemon drives
        # both hidden nodes (main.py's _apply_sensitivity), so there is
        # nothing for the user to create or wire and the control works
        # the moment the node exists. `level` (this node's static
        # threshold, in dB terms via THRESHOLD_DB_AT_LEVEL_0/100) remains
        # a Settings-dialog-only value, same as Noise Cancel's
        # vad_threshold - the fixed point the gain staging pushes signal
        # across.
        "sensitivity_gate": NodeSpec(
            "Sensitivity",
            ["in"],
            ["out"],
            control="sensitivity",
            settings=[
                (
                    "level",
                    "Fixed gate threshold (0-100, not live-adjustable):",
                    "number",
                    {"min": 0, "max": 100, "step": 1},
                ),
                ("ladspa_plugin", "Override plugin path (blank = auto):", "text"),
                ("ladspa_label", "Override plugin label (blank = auto):", "text"),
            ],
        ),
        # Reverb's only real dial - the dry/wet mix - is drawn as an
        # inline slider on the node body (control="wetdry") rather than
        # buried in its Settings menu; see the Reverb wet_dry handler in
        # main.py's set_node_property.
        "reverb": NodeSpec("Reverb", ["in"], ["out"], control="wetdry"),
    }
)

NODE_TYPE_SPECS.update(
    {
        "device_input": NodeSpec("Hardware Input", [], ["out"]),
        "device_output": NodeSpec("Hardware Output", ["in"], []),
        "app_input": NodeSpec("App Playback", [], ["out"]),
        "app_output": NodeSpec("App Mic", ["in"], []),
        "patchbay_device": NodeSpec("PatchBay Device", ["in"], ["out"]),
        "patchbay_mic_device": NodeSpec("PatchBay Mic Device", ["in"], ["out"]),
        "virtual_speaker": NodeSpec(
            "Virtual Speaker", ["in"], ["out"], field="device_label"
        ),
        "virtual_mic": NodeSpec("Virtual Mic", ["in"], ["out"], field="device_label"),
    }
)

# Menu entries for the right-click "add node" popover and the add-node
# side panel, grouped into the categories the side panel shows as
# collapsible sections (patchspace_widget.build_add_node_panel()).
#
# (The old "Mute Switch" entry was removed - it was just a Gate with a
# checkbox skin. Nodes created before its removal still load fine: they
# are plain "volume" nodes whose id is prefixed "mute_", which is how
# is_mute_node() tells a slider-style volume node from a checkbox-style
# one without needing any backend changes. See is_mute_node below.)
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
            ("Switcher (A/B out)", "switcher"),
            ("Inverse Switcher (A/B in)", "inverse_switcher"),
            ("Exclude Filter (regex)", "exclude_filter"),
            ("Volume (slider)", "volume"),
        ],
    ),
    (
        "Effects",
        [
            ("Echo Cancel", "echo_cancel"),
            ("Noise Cancel", "noise_cancel"),
            ("Sensitivity Gate", "sensitivity_gate"),
            ("Reverb", "reverb"),
        ],
    ),
    (
        "Hardware & Apps",
        [
            ("Hardware Input", "device_input"),
            ("Hardware Output", "device_output"),
            ("App Playback", "app_input"),
            ("App Mic", "app_output"),
            ("PatchBay Device", "patchbay_device"),
            ("PatchBay Mic Device", "patchbay_mic_device"),
        ],
    ),
    (
        "Virtual Devices",
        [
            ("Virtual Speaker", "virtual_speaker"),
            ("Virtual Mic", "virtual_mic"),
        ],
    ),
]

ADD_NODE_MENU_ITEMS = [
    item for _category, items in ADD_NODE_CATEGORIES for item in items
]

# One stable border colour per node, picked from the running GTK theme
# (see render_utils.theme_color) rather than hashing the type name.
# Colour is assigned by the same category the add-node menu groups by,
# so every "Filters" node is the same accent, every "Effects" node the
# same, and so on - and it never changes between runs or machines the
# way the old process-salted hash() did.  A type the theme has no name
# for (or one added to NODE_TYPE_SPECS without a menu entry) falls back
# to the theme accent.
CATEGORY_COLOR_NAMES = {
    "Filters": "accent_color",
    "Processing": "success_color",
    "Effects": "warning_color",
    "Hardware & Apps": "error_color",
    "Virtual Devices": "destructive_color",
}

DEFAULT_NODE_COLOR_NAME = "accent_color"

NODE_TYPE_COLOR_NAMES = {
    node_type: CATEGORY_COLOR_NAMES.get(category, DEFAULT_NODE_COLOR_NAME)
    for category, items in ADD_NODE_CATEGORIES
    for _label, node_type in items
}


def color_name_for_node_type(node_type: str) -> str:
    """GTK theme colour name assigned to `node_type` - see
    CATEGORY_COLOR_NAMES above.  Falls back to the theme accent for an
    unknown type so it still gets a consistent colour rather than a
    random one."""
    return NODE_TYPE_COLOR_NAMES.get(node_type, DEFAULT_NODE_COLOR_NAME)

# Best-effort symbolic icon per add-node entry, used by the sidebar
# panel and the right-click "add node" popover. Purely cosmetic - a
# theme that lacks one of these just falls back to its own generic
# "missing icon" glyph, nothing else depends on this mapping. "mute"
# is included even though it isn't a real backend node type (see
# ADD_NODE_CATEGORIES above) because it's still a distinct entry in
# these menus with its own icon.
NODE_TYPE_ICONS: Dict[str, str] = {
    "regex_input": "edit-find-symbolic",
    "regex_output": "edit-find-symbolic",
    "media_class_input": "view-list-symbolic",
    "media_class_output": "view-list-symbolic",
    "description_input": "text-x-generic-symbolic",
    "description_output": "text-x-generic-symbolic",
    "splitter": "network-transmit-receive-symbolic",
    "gate": "view-reveal-symbolic",
    "switcher": "object-flip-horizontal-symbolic",
    "inverse_switcher": "object-flip-horizontal-symbolic",
    "sensitivity_gate": "microphone-sensitivity-high-symbolic",
    "exclude_filter": "action-unavailable-symbolic",
    "volume": "audio-volume-high-symbolic",
    "mute": "audio-volume-muted-symbolic",
    "echo_cancel": "audio-input-microphone-symbolic",
    "noise_cancel": "microphone-sensitivity-muted-symbolic",
    "sensitivity_gate": "microphone-sensitivity-high-symbolic",
    "reverb": "media-playlist-repeat-symbolic",
    "device_input": "audio-input-microphone-symbolic",
    "device_output": "audio-speakers-symbolic",
    "app_input": "application-x-executable-symbolic",
    "app_output": "application-x-executable-symbolic",
    "patchbay_device": "audio-speakers-symbolic",
    "patchbay_mic_device": "audio-input-microphone-symbolic",
    "virtual_speaker": "audio-speakers-symbolic",
    "virtual_mic": "audio-input-microphone-symbolic",
}

_DEFAULT_ADD_NODE_ICON = "list-add-symbolic"


def icon_for_add_node_type(node_type: str) -> str:
    """Icon name for an ADD_NODE_CATEGORIES/ADD_NODE_MENU_ITEMS entry
    (which, for "mute", is not a real backend node type - see
    NODE_TYPE_ICONS above). Falls back to the old generic plus-icon
    for anything not listed, so a future node type added to
    ADD_NODE_CATEGORIES without a matching icon entry still renders
    something instead of raising."""
    return NODE_TYPE_ICONS.get(node_type, _DEFAULT_ADD_NODE_ICON)


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
    "SwitcherNode": "switcher",
    "InverseSwitcherNode": "inverse_switcher",
    "ExcludeFilterNode": "exclude_filter",
    "VolumeProcessNode": "volume",
    "NoiseCancelNode": "noise_cancel",
    "SensitivityGateNode": "sensitivity_gate",
    "ReverbNode": "reverb",
    "EchoCancelNode": "echo_cancel",
}

CLASS_NAME_TO_TYPE.update(
    {
        "DeviceInputNode": "device_input",
        "DeviceOutputNode": "device_output",
        "AppInputNode": "app_input",
        "AppOutputNode": "app_output",
        "PatchBayDeviceNode": "patchbay_device",
        "PatchBayMicDeviceNode": "patchbay_mic_device",
        "VirtualSpeakerNode": "virtual_speaker",
        "VirtualMicNode": "virtual_mic",
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
