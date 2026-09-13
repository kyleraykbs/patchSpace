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
    "warp_name": "Warp name:",
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
    __slots__ = (
        "label",
        "inputs",
        "outputs",
        "control",
        "field",
        "settings",
        "boolean_inputs",
        "boolean_outputs",
        "socket_labels",
        "description",
        "setting_tooltips",
    )

    def __init__(
        self,
        label: str,
        inputs: List[str],
        outputs: List[str],
        control: Optional[str] = None,
        field: Optional[str] = None,
        settings: Optional[List[tuple]] = None,
        boolean_inputs: Optional[List[str]] = None,
        boolean_outputs: Optional[List[str]] = None,
        socket_labels: bool = True,
        description: str = "",
        setting_tooltips: Optional[Dict[str, str]] = None,
    ):
        self.label = label
        self.inputs = inputs
        self.outputs = outputs
        # One-line "what this node is for", shown as a hover tooltip on the
        # add-node side panel and (after the normal GTK hover delay) on a
        # node in the canvas.  Populated per type in NODE_DESCRIPTIONS below.
        self.description = description
        # Optional per-Settings-row hover text, keyed by the daemon
        # property name (the first element of each `settings` tuple) - see
        # SETTING_TOOLTIPS below.  A row with no entry falls back to its
        # own label text so nothing is ever tooltip-less.
        self.setting_tooltips = dict(setting_tooltips or {})
        # Whether a multi-socket side draws each port's name next to its
        # circle.  True for the nodes whose ports carry distinct meaning
        # (Echo Cancel's "mic"/"probe", the Switcher's "on"/"off"); False
        # for the symmetric boolean logic gates, whose two inputs are
        # interchangeable so "a"/"b" labels are just noise.
        self.socket_labels = socket_labels
        # Which of inputs/outputs carry a *boolean control signal*
        # rather than audio (gray sockets, gray edges, never a PipeWire
        # link).  Every port not listed here is audio.
        self.boolean_inputs = set(boolean_inputs or ())
        self.boolean_outputs = set(boolean_outputs or ())
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
    # Gate and the two switches are bool-controlled: a boolean signal on
    # the gray "ctrl" input drives them.  When nothing is wired there
    # they show an on/off fallback button instead (control
    # "fallback_onoff"), which the widget hides as soon as ctrl is
    # connected.  Their two channels are "on"/"off".
    "gate": NodeSpec(
        "Gate",
        ["in", "ctrl"],
        ["out"],
        control="fallback_onoff",
        boolean_inputs=["ctrl"],
    ),
    "switcher": NodeSpec(
        "Switcher",
        ["in", "ctrl"],
        ["on", "off"],
        control="fallback_onoff",
        boolean_inputs=["ctrl"],
    ),
    "inverse_switcher": NodeSpec(
        "Inv. Switcher",
        ["on", "off", "ctrl"],
        ["out"],
        control="fallback_onoff",
        boolean_inputs=["ctrl"],
    ),
    "exclude_filter": NodeSpec("Exclude (Regex)", ["in"], ["out"], field="pattern"),
    "volume": NodeSpec("Volume", ["in"], ["out"], control="volume"),
    # Boolean control-signal nodes (gray ports/edges; never a PipeWire
    # link). The On/Off source's button flips a boolean output; the
    # splitter fans one boolean out to two; Invert negates its single input;
    # AND/OR combine their two inputs (an unwired input is ignored, so a
    # gate with one input wired passes that value through).
    "boolean_switch": NodeSpec(
        "On/Off",
        [],
        ["out"],
        control="boolean",
        boolean_outputs=["out"],
    ),
    "boolean_splitter": NodeSpec(
        "Bool Splitter",
        ["in"],
        ["out1", "out2"],
        boolean_inputs=["in"],
        boolean_outputs=["out1", "out2"],
    ),
    "boolean_invert": NodeSpec(
        "Invert",
        ["in"],
        ["out"],
        boolean_inputs=["in"],
        boolean_outputs=["out"],
    ),
    "boolean_and": NodeSpec(
        "AND",
        ["a", "b"],
        ["out"],
        boolean_inputs=["a", "b"],
        boolean_outputs=["out"],
        socket_labels=False,
    ),
    "boolean_or": NodeSpec(
        "OR",
        ["a", "b"],
        ["out"],
        boolean_inputs=["a", "b"],
        boolean_outputs=["out"],
        socket_labels=False,
    ),
    "boolean_xor": NodeSpec(
        "XOR",
        ["a", "b"],
        ["out"],
        boolean_inputs=["a", "b"],
        boolean_outputs=["out"],
        socket_labels=False,
    ),
    # Warps: named logical aliases. A Warp In publishes whatever is
    # plugged into it under `warp_name`; a Warp Out resolves to the
    # matching publisher(s). Audio warps mix multiple publishers;
    # boolean warps are a separate namespace (first match wins). The
    # inline `warp_name` text box is the pairing key.
    "warp_in": NodeSpec("Warp In", ["in"], [], field="warp_name"),
    "warp_out": NodeSpec("Warp Out", [], ["out"], field="warp_name"),
    "bool_warp_in": NodeSpec(
        "Bool Warp In",
        ["in"],
        [],
        field="warp_name",
        boolean_inputs=["in"],
    ),
    "bool_warp_out": NodeSpec(
        "Bool Warp Out",
        [],
        ["out"],
        field="warp_name",
        boolean_outputs=["out"],
    ),
    # Panel ports: pass-through proxy nodes placed at a panel's left
    # (inputs) / right (outputs) edge.  Audio ones are transparent; boolean
    # ones carry a boolean control signal.
    "panel_in": NodeSpec(
        "Panel In", ["in"], ["out"],
        settings=[("description", "Description:", "text")],
    ),
    "panel_out": NodeSpec(
        "Panel Out", ["in"], ["out"],
        settings=[("description", "Description:", "text")],
    ),
    "bool_panel_in": NodeSpec(
        "Bool Panel In",
        ["in"],
        ["out"],
        boolean_inputs=["in"],
        boolean_outputs=["out"],
        settings=[
            ("description", "Description:", "text"),
            ("default_state", "Default state (when unconnected):", "bool"),
        ],
    ),
    "bool_panel_out": NodeSpec(
        "Bool Panel Out",
        ["in"],
        ["out"],
        boolean_inputs=["in"],
        boolean_outputs=["out"],
        settings=[("description", "Description:", "text")],
    ),
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
            "AI Noise Cancel",
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
        # Same backing module as Echo Cancel (libpipewire-module-echo-
        # cancel, its dummies, keepalives and interior streams), but the
        # "probe" input is deliberately not surfaced: it reads as a plain
        # one-in/one-out noise suppressor. The probe stream still exists
        # privately inside the module - see LightNoiseCancelNode.
        "light_noise_cancel": NodeSpec(
            "Light Noise Cancel",
            ["mic"],
            ["out"],
            settings=[
                ("library_name", "AEC library:", "text"),
                ("aec_args", "AEC args:", "text"),
                ("monitor_mode", "Monitor mode (auto-capture default sink)", "bool"),
            ],
        ),
        # control="sensitivity" is the Sensitivity Gate's 0..1 inline
        # slider; the Settings dialog edits the *same* `sensitivity` value
        # (not the raw 0..100 `level`, which the daemon derives from it),
        # so the two always agree.  The daemon invisibly brackets every
        # Sensitivity gate with two hidden VolumeProcessNodes - a pre-gain
        # and an equal-and-opposite post-gain (main.py's
        # _ensure_sensitivity_internals) - and routes the user's edges
        # through them; they are unity pass-throughs now that the gate
        # itself is Calf LV2.  The slider just sends
        # set_node_property("sensitivity", <0..1>) and the daemon bakes
        # the matching threshold into the module graph.  The remaining
        # Calf Gate controls (ratio/attack/release/knee/makeup) are
        # load-time filter-graph values too - each change schedules the
        # same debounced interior reload (see SensitivityGateNode's
        # docstring).
        "sensitivity_gate": NodeSpec(
            "Sensitivity",
            ["in"],
            ["out"],
            control="sensitivity",
            settings=[
                (
                    "sensitivity",
                    "Sensitivity (0-1, 1 = most sensitive):",
                    "number",
                    {"min": 0, "max": 1, "step": 0.01},
                ),
                (
                    "ratio",
                    "Gate ratio (1-20):",
                    "number",
                    {"min": 1, "max": 20, "step": 0.5},
                ),
                (
                    "attack_ms",
                    "Attack (ms):",
                    "number",
                    {"min": 0, "max": 200, "step": 1},
                ),
                (
                    "release_ms",
                    "Release / decay (ms):",
                    "number",
                    {"min": 0, "max": 2000, "step": 10},
                ),
                (
                    "knee_db",
                    "Knee (dB):",
                    "number",
                    {"min": 0, "max": 12, "step": 0.5},
                ),
                (
                    "makeup",
                    "Makeup gain:",
                    "number",
                    {"min": 0, "max": 10, "step": 0.1},
                ),
                (
                    "range_db",
                    "Closed-gate level (dB, -96 = silence):",
                    "number",
                    {"min": -96, "max": 0, "step": 1},
                ),
                (
                    "lv2_uri",
                    "LV2 gate URI (blank = Calf Gate):",
                    "text",
                ),
            ],
        ),
        # Reverb's only real dial - the dry/wet mix - is drawn as an
        # inline slider on the node body (control="wetdry") rather than
        # buried in its Settings menu; see the Reverb wet_dry handler in
        # main.py's set_node_property.
        "reverb": NodeSpec(
            "Reverb",
            ["in"],
            ["out"],
            control="wetdry",
            # Calf Reverb (LV2). The inline slider is wet/dry; the rest
            # is reverb character, all load-time filter-graph values.
            settings=[
                (
                    "decay_time",
                    "Decay time (s):",
                    "number",
                    {"min": 0.4, "max": 15, "step": 0.1},
                ),
                (
                    "room_size",
                    "Room size (0-5):",
                    "number",
                    {"min": 0, "max": 5, "step": 0.1},
                ),
                (
                    "diffusion",
                    "Diffusion (0-1):",
                    "number",
                    {"min": 0, "max": 1, "step": 0.05},
                ),
                (
                    "hf_damp",
                    "High-freq damping (Hz):",
                    "number",
                    {"min": 2000, "max": 20000, "step": 100},
                ),
                (
                    "predelay",
                    "Pre-delay (ms):",
                    "number",
                    {"min": 0, "max": 500, "step": 1},
                ),
                (
                    "plugin_uri",
                    "LV2 plugin URI (blank = Calf Reverb):",
                    "text",
                ),
            ],
        ),
        # Loudness normalization: a lookahead-limiter sandwich that can
        # lift quiet/far-from-mic speech without blasting on resume (see
        # NormalizeNode). The inline slider is `boost_db`; the caps and
        # compressor tuning live in the Settings gear. All of them are
        # load-time filter-graph values, so a change schedules an
        # interior-only reload (debounced) rather than a live set-param.
        "normalize": NodeSpec(
            "Normalize",
            ["in"],
            ["out"],
            control="gain",
            settings=[
                (
                    "boost_db",
                    "Boost (dB):",
                    "number",
                    {"min": 0, "max": 30, "step": 0.5},
                ),
                (
                    "max_boost_db",
                    "Maximum boost cap (dB):",
                    "number",
                    {"min": 0, "max": 30, "step": 0.5},
                ),
                (
                    "ceiling_db",
                    "Output ceiling (dB):",
                    "number",
                    {"min": -20, "max": 0, "step": 0.5},
                ),
                ("leveling", "Leveling compressor (sc4)", "bool"),
                (
                    "threshold_db",
                    "Compressor threshold (dB):",
                    "number",
                    {"min": -60, "max": 0, "step": 1},
                ),
                (
                    "ratio",
                    "Compressor ratio (1:n):",
                    "number",
                    {"min": 1, "max": 20, "step": 0.5},
                ),
                (
                    "attack_ms",
                    "Compressor attack (ms):",
                    "number",
                    {"min": 0.1, "max": 200, "step": 1},
                ),
                (
                    "release_ms",
                    "Compressor release (ms):",
                    "number",
                    {"min": 10, "max": 2000, "step": 10},
                ),
                (
                    "knee_db",
                    "Compressor knee (dB):",
                    "number",
                    {"min": 0, "max": 24, "step": 1},
                ),
                (
                    "limiter_release_s",
                    "Limiter release (s):",
                    "number",
                    {"min": 0.01, "max": 5, "step": 0.05},
                ),
                ("ladspa_dir", "LADSPA dir override (blank = auto):", "text"),
            ],
        ),
    }
)

NODE_TYPE_SPECS.update(
    {
        "device_input": NodeSpec("Hardware Input", [], ["out"]),
        "device_output": NodeSpec("Hardware Output", ["in"], []),
        "app_input": NodeSpec("App Playback", [], ["out"]),
        "app_output": NodeSpec("App Mic", ["in"], []),
        "patchbay_device": NodeSpec("Speaker Line", ["in"], ["out"]),
        "patchbay_mic_device": NodeSpec("Mic Line", ["in"], ["out"]),
        "virtual_speaker": NodeSpec(
            "Virtual Speaker", ["in"], ["out"], field="device_label"
        ),
        "virtual_mic": NodeSpec("Virtual Mic", ["in"], ["out"], field="device_label"),
    }
)

# ---------------------------------------------------------------------------
# Descriptions + settings tooltips
# ---------------------------------------------------------------------------
# Kept as separate tables rather than repeating `description=` in every
# NodeSpec(...) literal above, so adding a description for a new node type
# is a one-line change here and the spec table stays scannable.

NODE_DESCRIPTIONS: Dict[str, str] = {
    # Filters
    "regex_input": "Audio sources whose node name matches a regular "
    "expression (e.g. every app playback stream).",
    "regex_output": "A destination picked by matching a regular expression "
    "against node names.",
    "media_class_input": "Audio sources selected by their PipeWire media "
    "class (hardware inputs, sink monitors, app playback).",
    "media_class_output": "Destinations selected by their PipeWire media "
    "class (hardware outputs, app recording streams).",
    "description_input": "Audio sources selected by a text match on their "
    "description.",
    "description_output": "A destination selected by a text match on its "
    "description.",
    # Processing
    "splitter": "Passes audio straight through; useful as a named junction "
    "that several edges can share.",
    "gate": "Passes or blocks audio, driven by a boolean control signal "
    "(or its own on/off button when nothing is wired to ctrl).",
    "switcher": "Routes its input to the On or Off output, chosen by a "
    "boolean control signal.",
    "inverse_switcher": "Takes two inputs (On/Off) and routes the selected "
    "one to its output.",
    "exclude_filter": "Passes audio through while excluding sources that "
    "match a regular expression from the mix.",
    "volume": "Adjusts the level of whatever passes through it.",
    # Boolean control plane
    "boolean_switch": "A manual On/Off control signal. Wire it into gates "
    "and switches to drive them.",
    "boolean_splitter": "Fans one boolean signal out to two destinations.",
    "boolean_invert": "Inverts a boolean signal (NOT).",
    "boolean_and": "True only when every wired input is true.",
    "boolean_or": "True when any wired input is true.",
    "boolean_xor": "True when exactly one wired input is true (odd parity).",
    "warp_in": "Publishes whatever is plugged into it under a name, so a "
    "Warp Out elsewhere can pull it in.",
    "warp_out": "Pulls in everything published under a name (mixes multiple "
    "publishers).",
    "bool_warp_in": "Publishes a boolean control signal under a name.",
    "bool_warp_out": "Reads the boolean control signal published under a "
    "name.",
    "panel_in": "A panel input port: wire audio in from outside; inside the "
    "panel it acts as a source.",
    "panel_out": "A panel output port: wire audio to it from inside the "
    "panel; outside it acts as a source.",
    "bool_panel_in": "A panel boolean input port (control signal from "
    "outside to inside).",
    "bool_panel_out": "A panel boolean output port (control signal from "
    "inside to outside).",
    # Effects
    "echo_cancel": "Removes speaker echo from a microphone using a probe/"
    "reference feed (WebRTC acoustic echo cancellation).",
    "noise_cancel": "AI (RNNoise) denoiser for a microphone signal.",
    "light_noise_cancel": "Mic-only denoiser using the same engine as Echo "
    "Cancel, with the probe input hidden.",
    "sensitivity_gate": "A voice-activity gate that opens on speech and "
    "closes on silence.",
    "reverb": "Adds reverb; the inline slider sets the dry/wet mix.",
    "normalize": "Loudness normalization with a lookahead limiter, so quiet "
    "speech can be lifted without blasting when it resumes.",
    # Hardware & apps
    "device_input": "A hardware capture device (microphone, interface "
    "input, ...).",
    "device_output": "A hardware playback device (speakers, headphones, "
    "...).",
    "app_input": "An application's audio output (playback stream).",
    "app_output": "An application's recording input.",
    "patchbay_device": "A line to the built-in Patch Space speaker device.",
    "patchbay_mic_device": "A line to the built-in Patch Space microphone "
    "device.",
    "virtual_speaker": "A named virtual speaker other applications can "
    "play into.",
    "virtual_mic": "A named virtual microphone other applications can "
    "record from.",
}

# Per-Settings-row hover text, keyed by node type then daemon property name.
# Rows without an entry fall back to their label text (see
# show_settings_dialog), so every row has a tooltip.
SETTING_TOOLTIPS: Dict[str, Dict[str, str]] = {
    "echo_cancel": {
        "library_name": "The AEC implementation to load. 'aec/libspa-aec-"
        "webrtc' is the WebRTC echo canceller.",
        "aec_args": "Extra options passed to the AEC library, e.g. "
        "'analog_gain_control=0 digital_gain_control=1'.",
        "monitor_mode": "Automatically use the default sink as the echo "
        "reference instead of the probe input.",
    },
    "noise_cancel": {
        "vad_threshold": "How confident RNNoise must be that a frame is "
        "speech before passing it through. Higher = more aggressive.",
        "ladspa_plugin": "Override the librnnoise_ladspa.so path. Blank = "
        "find it automatically.",
        "ladspa_label": "Override the RNNoise plugin label. Blank = auto "
        "(uses the stereo variant).",
    },
    "light_noise_cancel": {
        "library_name": "The AEC implementation to load.",
        "aec_args": "Extra options passed to the AEC library; use these to "
        "bias it toward noise suppression.",
        "monitor_mode": "Automatically use the default sink as the "
        "reference instead of a probe feed.",
    },
    "sensitivity_gate": {
        "sensitivity": "0..1: how easily sound opens the gate (1 = most "
        "sensitive).",
        "ratio": "How hard the gate attenuates below the threshold.",
        "attack_ms": "How quickly the gate opens when speech starts.",
        "release_ms": "How long before the gate closes after speech stops.",
        "knee_db": "Softness of the transition around the threshold.",
        "makeup": "Gain added back after gating.",
        "range_db": "Level the gate drops to when closed (-96 dB = "
        "silence).",
        "lv2_uri": "Override the LV2 gate plugin URI. Blank = Calf Gate.",
    },
    "reverb": {
        "decay_time": "How long the reverb tail lasts.",
        "room_size": "Apparent size of the simulated room.",
        "diffusion": "Density/smoothness of the reverb tail.",
        "hf_damp": "Frequency above which the tail is damped.",
        "predelay": "Delay before the reverb starts, in milliseconds.",
        "plugin_uri": "Override the LV2 plugin URI. Blank = Calf Reverb.",
    },
    "normalize": {
        "boost_db": "Input gain applied before the limiter.",
        "max_boost_db": "Ceiling that caps the boost.",
        "ceiling_db": "Maximum output level (the brickwall limit).",
        "leveling": "Enable the sc4 leveling compressor ahead of the "
        "limiter.",
        "threshold_db": "Compressor threshold: below this the compressor "
        "does nothing.",
        "ratio": "Compressor ratio.",
        "attack_ms": "Compressor attack time.",
        "release_ms": "Compressor release time.",
        "knee_db": "Softness of the compressor knee.",
        "limiter_release_s": "Limiter release time in seconds.",
        "ladspa_dir": "Directory holding the swh LADSPA plugins. Blank = "
        "find them automatically.",
    },
}

for _type, _desc in NODE_DESCRIPTIONS.items():
    if _type in NODE_TYPE_SPECS:
        NODE_TYPE_SPECS[_type].description = _desc
for _type, _tips in SETTING_TOOLTIPS.items():
    if _type in NODE_TYPE_SPECS:
        NODE_TYPE_SPECS[_type].setting_tooltips = dict(_tips)


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
            ("Gate (bool)", "gate"),
            ("Switcher (On/Off out)", "switcher"),
            ("Inverse Switcher (On/Off in)", "inverse_switcher"),
            ("Exclude Filter (regex)", "exclude_filter"),
            ("Volume (slider)", "volume"),
        ],
    ),
    (
        "Boolean",
        [
            ("On/Off", "boolean_switch"),
            ("Invert", "boolean_invert"),
            ("AND", "boolean_and"),
            ("OR", "boolean_or"),
            ("XOR", "boolean_xor"),
            ("Bool Splitter", "boolean_splitter"),
        ],
    ),
    (
        "Warp",
        [
            ("Warp In", "warp_in"),
            ("Warp Out", "warp_out"),
            ("Bool Warp In", "bool_warp_in"),
            ("Bool Warp Out", "bool_warp_out"),
        ],
    ),
    (
        "Effects",
        [
            ("Echo Cancel", "echo_cancel"),
            ("AI Noise Cancel", "noise_cancel"),
            ("Light Noise Cancel", "light_noise_cancel"),
            ("Sensitivity Gate", "sensitivity_gate"),
            ("Reverb", "reverb"),
            ("Normalize", "normalize"),
        ],
    ),
    (
        "Hardware & Apps",
        [
            ("Hardware Input", "device_input"),
            ("Hardware Output", "device_output"),
            ("App Playback", "app_input"),
            ("App Mic", "app_output"),
            ("Speaker Line", "patchbay_device"),
            ("Mic Line", "patchbay_mic_device"),
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
    "Boolean": "dim_label_color",
    "Warp": "accent_color",
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
    "boolean_switch": "object-select-symbolic",
    "boolean_splitter": "network-transmit-receive-symbolic",
    "boolean_invert": "action-unavailable-symbolic",
    "boolean_and": "checkbox-checked-symbolic",
    "boolean_or": "list-add-symbolic",
    "boolean_xor": "checkbox-mixed-symbolic",
    "warp_in": "insert-link-symbolic",
    "warp_out": "insert-link-symbolic",
    "bool_warp_in": "insert-link-symbolic",
    "bool_warp_out": "insert-link-symbolic",
    "volume": "audio-volume-high-symbolic",
    "mute": "audio-volume-muted-symbolic",
    "echo_cancel": "audio-input-microphone-symbolic",
    "noise_cancel": "microphone-sensitivity-muted-symbolic",
    "light_noise_cancel": "microphone-sensitivity-low-symbolic",
    "sensitivity_gate": "microphone-sensitivity-high-symbolic",
    "reverb": "media-playlist-repeat-symbolic",
    "normalize": "audio-volume-high-symbolic",
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
    "BooleanSourceNode": "boolean_switch",
    "BooleanSplitterNode": "boolean_splitter",
    "BooleanInvertNode": "boolean_invert",
    "BooleanAndNode": "boolean_and",
    "BooleanOrNode": "boolean_or",
    "BooleanXorNode": "boolean_xor",
    "WarpInNode": "warp_in",
    "WarpOutNode": "warp_out",
    "BooleanWarpInNode": "bool_warp_in",
    "BooleanWarpOutNode": "bool_warp_out",
    "PanelInNode": "panel_in",
    "PanelOutNode": "panel_out",
    "BoolPanelInNode": "bool_panel_in",
    "BoolPanelOutNode": "bool_panel_out",
    "VolumeProcessNode": "volume",
    "NoiseCancelNode": "noise_cancel",
    "SensitivityGateNode": "sensitivity_gate",
    "ReverbNode": "reverb",
    "NormalizeNode": "normalize",
    "EchoCancelNode": "echo_cancel",
    "LightNoiseCancelNode": "light_noise_cancel",
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


def port_kind(node_type: str, port: str, direction: str) -> str:
    """"audio" or "boolean" for one of `node_type`'s ports.  Shared by
    socket/edge coloring and by edge-drop validation so the GUI can't
    create a boolean-to-audio connection the daemon would reject."""
    spec = spec_for(node_type)
    kinds = spec.boolean_inputs if direction == "in" else spec.boolean_outputs
    return "boolean" if port in kinds else "audio"


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


def description_for(node_type: str) -> str:
    """One-line "what this node is" for tooltips; empty for unknown types."""
    return spec_for(node_type).description


def setting_tooltip(node_type: str, attr: str, label: str) -> str:
    """Hover text for one Settings row, falling back to its label so every
    row has something."""
    return spec_for(node_type).setting_tooltips.get(attr, label)
