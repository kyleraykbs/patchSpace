"""
node_catalog.py — what the daemon's modules provide, as the GUI sees it.

The canvas used to carry its own copy of every node type (`node_catalog.py`).
Now the daemon serves the catalog (`module_api.catalog_reply`) and the client
installs it here; every lookup the canvas makes — ports, kinds, labels,
colours, icons, the add-node menu, the settings rows — reads the installed
catalog, so a module the GUI has never heard of renders correctly.

An *empty* catalog is a valid state (the daemon has not answered yet): an
unknown type renders as a generic in/out node — the same shape the old
fallback spec had — and the add-node menu is empty rather than wrong.

`NodeSpec` keeps the attribute names the drawing code already reads, so the
canvas did not have to change; only where the data comes from did.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

#: The installed catalog, filled by `install()`.  Mutated in place so a
#: caller holding a reference (the widget imports `NODE_TYPE_SPECS` at
#: import time) sees the installed types.
NODE_TYPE_SPECS: Dict[str, "NodeSpec"] = {}

#: (category, [(label, type_id)]) for the add-node panel, in menu order.
ADD_NODE_CATEGORIES: List[Tuple[str, List[Tuple[str, str]]]] = []
ADD_NODE_MENU_ITEMS: List[Tuple[str, str]] = []

CLASS_NAME_TO_TYPE: Dict[str, str] = {}
FIELD_LABELS: Dict[str, str] = {}

_MEDIA_CLASS_CHOICES: List[Tuple[str, str]] = []
_MEDIA_CLASS_INPUT_CHOICES: List[Tuple[str, str]] = []
_MEDIA_CLASS_OUTPUT_CHOICES: List[Tuple[str, str]] = []

_DEFAULT_ADD_NODE_ICON = "list-add-symbolic"
DEFAULT_NODE_COLOR_NAME = "accent_color"


class NodeSpec:
    """One node type, as announced by its module.

    Port *names* keep their declared order (`inputs`/`outputs`) because the
    canvas lays sockets out in it; the per-kind sets answer "what does this
    port carry".
    """

    __slots__ = (
        "label", "inputs", "outputs", "control", "field", "settings",
        "boolean_inputs", "boolean_outputs", "impulse_inputs", "impulse_outputs",
        "bundle_inputs", "bundle_outputs", "filter_inputs", "filter_outputs",
        "sound_inputs", "sound_outputs", "camera_inputs", "camera_outputs",
        "socket_labels", "label_lone_output", "description",
        "setting_tooltips", "toggle", "field_choices", "indicator", "picker",
        "category", "color", "icon", "class_name", "dynamic_ports",
    )

    def __init__(self, label: str = "Unknown", inputs: Optional[List[str]] = None,
                 outputs: Optional[List[str]] = None) -> None:
        self.label = label
        self.inputs = list(inputs or [])
        self.outputs = list(outputs or [])
        self.control: Optional[str] = None
        self.field: Optional[str] = None
        self.settings: List[Any] = []
        self.boolean_inputs: Set[str] = set()
        self.boolean_outputs: Set[str] = set()
        self.impulse_inputs: Set[str] = set()
        self.impulse_outputs: Set[str] = set()
        self.bundle_inputs: Set[str] = set()
        self.bundle_outputs: Set[str] = set()
        self.filter_inputs: Set[str] = set()
        self.filter_outputs: Set[str] = set()
        self.sound_inputs: Set[str] = set()
        self.sound_outputs: Set[str] = set()
        self.camera_inputs: Set[str] = set()
        self.camera_outputs: Set[str] = set()
        self.socket_labels = True
        self.label_lone_output = False
        self.description = ""
        self.setting_tooltips: Dict[str, str] = {}
        self.toggle: Optional[Tuple[str, str]] = None
        self.field_choices = False
        self.indicator: Optional[str] = None
        self.picker = False
        self.category = ""
        self.color = DEFAULT_NODE_COLOR_NAME
        self.icon = _DEFAULT_ADD_NODE_ICON
        self.class_name = ""
        #: (name, direction) pairs the module grows on demand.
        self.dynamic_ports: Set[Tuple[str, str]] = set()

    @property
    def has_extra_row(self) -> bool:
        """Whether the node body draws something in its bottom area, which
        needs extra height and a socket offset so ports do not overlap it."""
        return (
            self.control is not None
            or self.field is not None
            or self.toggle is not None
        )

    @classmethod
    def from_wire(cls, entry: Dict[str, Any]) -> "NodeSpec":
        display = entry.get("display") or {}
        extra = entry.get("spec") or {}
        spec = cls(
            label=display.get("name") or entry.get("type_id", ""),
            inputs=extra.get("inputs") or [],
            outputs=extra.get("outputs") or [],
        )
        spec.description = display.get("description") or ""
        spec.category = display.get("category") or ""
        spec.color = display.get("color") or DEFAULT_NODE_COLOR_NAME
        spec.icon = display.get("icon") or _DEFAULT_ADD_NODE_ICON
        spec.class_name = entry.get("class_name") or ""
        spec.socket_labels = bool(extra.get("socket_labels", True))
        spec.label_lone_output = bool(extra.get("label_lone_output", False))
        spec.field_choices = bool(extra.get("field_choices", False))
        toggle = extra.get("toggle")
        spec.toggle = tuple(toggle) if toggle else None
        spec.settings = [tuple(row) for row in (extra.get("settings") or [])]
        spec.setting_tooltips = dict(extra.get("setting_tooltips") or {})

        for port in entry.get("ports") or []:
            name = str(port.get("name") or "")
            kind = str(port.get("kind") or "audio")
            direction = str(port.get("direction") or "in")
            if port.get("dynamic"):
                spec.dynamic_ports.add((name, direction))
            bucket = getattr(spec, f"{kind}_{'inputs' if direction == 'in' else 'outputs'}", None)
            if bucket is not None:
                bucket.add(name)
            # A port the module declared but did not list in the ordered
            # inputs/outputs (a dynamic family) still needs a layout slot.
            ordered = spec.inputs if direction == "in" else spec.outputs
            if name not in ordered:
                ordered.append(name)

        for control in entry.get("controls") or []:
            key = str(control.get("key") or "")
            widget = str(control.get("widget") or "")
            if widget == "field":
                spec.field = key
            elif widget == "toggle":
                spec.toggle = spec.toggle or (key, key)
            elif widget == "picker":
                spec.picker = True
            elif widget == "indicator":
                spec.indicator = key
            elif widget == "setting":
                if not any(row and row[0] == key for row in spec.settings):
                    spec.settings.append((key, key))
            elif widget:
                spec.control = key
        return spec


_FALLBACK_SPEC = NodeSpec("Unknown", ["audio"], ["out"])


def install(types: List[Dict[str, Any]], menu: Optional[List[Dict[str, Any]]] = None,
            vocabulary: Optional[Dict[str, Any]] = None,
            aliases: Optional[Dict[str, str]] = None) -> None:
    """Replace the catalog with what the daemon just sent.

    Called with the `catalog_reply` payload; safe to call again on
    reconnect (the spec dict is updated in place, so the widget's imported
    reference stays valid).
    """
    specs: Dict[str, NodeSpec] = {}
    for entry in types or []:
        if not entry.get("type_id"):
            continue
        specs[str(entry["type_id"])] = NodeSpec.from_wire(entry)
    NODE_TYPE_SPECS.clear()
    NODE_TYPE_SPECS.update(specs)

    CLASS_NAME_TO_TYPE.clear()
    for type_id, spec in specs.items():
        if spec.class_name:
            CLASS_NAME_TO_TYPE[spec.class_name] = type_id
    # Pre-rename keys the module still answers to: an old panel or session
    # renders as its canonical type instead of falling back to the generic
    # spec.
    for legacy, canonical in (aliases or {}).items():
        if canonical in specs:
            NODE_TYPE_SPECS[legacy] = specs[canonical]

    ADD_NODE_CATEGORIES.clear()
    for group in menu or []:
        items = [(item.get("label") or item.get("type_id"), item["type_id"])
                 for item in group.get("items") or [] if item.get("type_id")]
        ADD_NODE_CATEGORIES.append((group.get("category", ""), items))
    ADD_NODE_MENU_ITEMS[:] = [item for _cat, items in ADD_NODE_CATEGORIES for item in items]

    vocabulary = vocabulary or {}
    FIELD_LABELS.clear()
    FIELD_LABELS.update(vocabulary.get("field_labels") or {})
    for target, key in ((_MEDIA_CLASS_CHOICES, "media_class_choices"),
                        (_MEDIA_CLASS_INPUT_CHOICES, "media_class_input_choices"),
                        (_MEDIA_CLASS_OUTPUT_CHOICES, "media_class_output_choices")):
        target[:] = [tuple(choice) for choice in vocabulary.get(key) or []]


def clear() -> None:
    """Forget the catalog (a fresh connection has not answered yet)."""
    install([], [], {}, {})


# ----------------------------------------------------------------------
# lookups (the surface the canvas and the settings dialog read)
# ----------------------------------------------------------------------

def spec_for(node_type: str) -> NodeSpec:
    """A node's spec, falling back to a generic in/out node so an unknown
    type still renders instead of raising."""
    return NODE_TYPE_SPECS.get(node_type, _FALLBACK_SPEC)


def normalize_node_type(raw_type: str) -> str:
    """A daemon class name to its canonical type key; a no-op for a key or
    a genuinely unknown type, so it shows up as itself."""
    return CLASS_NAME_TO_TYPE.get(raw_type, raw_type)


def port_kind(node_type: str, port: str, direction: str) -> str:
    """What one port of `node_type` carries.  Shared by socket/edge colouring
    and edge-drop validation, so the GUI cannot offer a connection the daemon
    would reject."""
    spec = spec_for(node_type)
    if direction == "in" and port == "in" and port not in spec.inputs:
        # Legacy name: a node's first input used to be called "in".
        port = spec.inputs[0] if spec.inputs else "in"
    if direction == "in" and node_type == "bundle":
        # A Merge Bundle's inputs are dynamic (in1, in2, ...).
        return "bundle"
    if direction == "in" and node_type == "filter" and port.startswith("filter"):
        return "filter"
    ordered = (
        ("boolean", spec.boolean_inputs), ("impulse", spec.impulse_inputs),
        ("filter", spec.filter_inputs), ("sound", spec.sound_inputs),
        ("camera", spec.camera_inputs), ("bundle", spec.bundle_inputs),
    ) if direction == "in" else (
        ("boolean", spec.boolean_outputs), ("impulse", spec.impulse_outputs),
        ("filter", spec.filter_outputs), ("sound", spec.sound_outputs),
        ("camera", spec.camera_outputs), ("bundle", spec.bundle_outputs),
    )
    for kind, ports in ordered:
        if port in ports:
            return kind
    return "audio"


def ports_compatible(from_type: str, from_port: str, to_type: str,
                     to_port: str) -> bool:
    """Whether two ports may be connected.  Mirrors the daemon's authority
    (`central/graph.kinds_compatible`): audio and bundle mix either way,
    every other kind pairs only with itself."""
    from_kind = port_kind(from_type, from_port, "out")
    to_kind = port_kind(to_type, to_port, "in")
    if from_kind in ("audio", "bundle") and to_kind in ("audio", "bundle"):
        return True
    return from_kind == to_kind


def is_mute_node(node_id) -> bool:
    """A "mute switch" is a plain volume node whose id carries the prefix —
    no backend difference, only which control it draws."""
    return str(node_id).startswith("mute_")


def type_label(node_type: str, node_id) -> str:
    if node_type == "volume" and is_mute_node(node_id):
        return "Mute Switch"
    return spec_for(node_type).label


def description_for(node_type: str) -> str:
    """One-line "what this node is" for tooltips; empty for unknown types."""
    return spec_for(node_type).description


def setting_tooltip(node_type: str, attr: str, label: str) -> str:
    """Hover text for one Settings row, falling back to its own label so
    every row has something."""
    return spec_for(node_type).setting_tooltips.get(attr, label)


def icon_for_add_node_type(node_type: str) -> str:
    return spec_for(node_type).icon or _DEFAULT_ADD_NODE_ICON


def color_name_for_node_type(node_type: str) -> str:
    """The GTK theme colour name for a type (assigned by the module, per
    category), falling back to the theme accent."""
    return spec_for(node_type).color or DEFAULT_NODE_COLOR_NAME


def media_class_choices_for(node_type: str) -> List[Tuple[str, str]]:
    """Which media.class choices to offer in this node type's dropdown."""
    if node_type == "media_class_output":
        return _MEDIA_CLASS_OUTPUT_CHOICES
    if node_type == "media_class_classifier":
        return _MEDIA_CLASS_CHOICES
    return _MEDIA_CLASS_INPUT_CHOICES


def media_class_label(value: Optional[str]) -> str:
    """A human-readable label for a raw media.class value; the raw string
    (or "(none)") for anything not in the table."""
    for label, raw in _MEDIA_CLASS_CHOICES:
        if raw == value:
            return label
    return value or "(none)"
