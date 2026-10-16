"""
types.py — the primitives module's node types.

Values, wired (agents/SPACES.md §16).  The **sources** — `number`/`const`
(a number), `slider` (0.0–1.0), `toggle` (a boolean), `string` (a text) —
each have an output port and report what they hold, so a producer's value is
routable wherever its wire lands.  The **operations** — `math`, `compare`,
`logic`, `invert` — take their operands from an edge *or* from the control
the port is named for (§14: a node's parameter is an addressable input, and
the port name is the key), and `display` shows what it is handed.

Data only — the module announces exactly this, and a display draws it
without knowing anything about the module (see `gui/node_catalog.py`).
"""

from __future__ import annotations

from typing import Any, Dict, List

#: The port kinds this module speaks.  Each pairs only with itself
#: (`central/graph.kinds_compatible`), so a number never lands on a text.
NUMBER = "number"
BOOL = "boolean"
TEXT = "text"

#: The range the module's numeric *knobs* are drawn with.  A port takes any
#: number; this is the control's, so a display has something to draw.
KNOB_MIN = -100.0
KNOB_MAX = 100.0


def _knob(key: str, label: str) -> Dict[str, Any]:
    """A numeric control, drawn as a bounded slider.

    Derived from the port it is named for: the port's name **is** the
    control key (§14), which is why every one of them is spelled after the
    input it drives.
    """
    return {"key": key, "widget": "number", "label": label,
            "min": KNOB_MIN, "max": KNOB_MAX}


TYPES: List[Dict[str, Any]] = [
    # -- the sources: each has an output port and reports what it holds ----
    {
        "type_id": "number",
        "schema": 1,
        "ports": [{"name": "out", "kind": NUMBER, "direction": "out"}],
        "controls": [{"key": "value", "widget": "slider", "label": "Value",
                      "min": KNOB_MIN, "max": KNOB_MAX}],
        "display": {"name": "Number", "category": "Primitives",
                    "description": "A number you set.", "color": "accent_color"},
    },
    {
        # The literal to `number`'s knob: any number, typed rather than
        # dragged, with no range of its own (math needs 1e6 and -0.001 too).
        "type_id": "const",
        "schema": 1,
        "ports": [{"name": "out", "kind": NUMBER, "direction": "out"}],
        "controls": [{"key": "value", "widget": "field", "kind": NUMBER,
                      "label": "Value"}],
        "display": {"name": "Constant", "category": "Primitives",
                    "description": "A number literal, routable like any value.",
                    "color": "accent_color"},
    },
    {
        # 0.0–1.0 and nothing else: no `min`/`max` — the node *is* the range,
        # and the engine clamps to it whatever a display or a driver sends.
        "type_id": "slider",
        "schema": 1,
        "ports": [{"name": "out", "kind": NUMBER, "direction": "out"}],
        "controls": [{"key": "value", "widget": "slider", "label": "Value"}],
        "display": {"name": "Slider", "category": "Primitives",
                    "description": "A number between 0 and 1 you drag.",
                    "color": "accent_color"},
    },
    {
        "type_id": "toggle",
        "schema": 1,
        "ports": [{"name": "out", "kind": BOOL, "direction": "out"}],
        "controls": [{"key": "on", "widget": "switch", "label": "On"}],
        "display": {"name": "On/Off", "category": "Primitives",
                    "description": "A boolean you flip.", "color": "dim_label_color"},
    },
    {
        "type_id": "string",
        "schema": 1,
        "ports": [{"name": "out", "kind": TEXT, "direction": "out"}],
        "controls": [{"key": "value", "widget": "field", "label": "Value"}],
        "display": {"name": "String", "category": "Primitives",
                    "description": "A text literal (a pattern, a name, a path).",
                    "color": "accent_color"},
    },
    # -- the operations: operands arrive on an edge or as a driven value ----
    {
        "type_id": "math",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": NUMBER, "direction": "in", "drives_control": True},
            {"name": "b", "kind": NUMBER, "direction": "in", "drives_control": True},
            {"name": "out", "kind": NUMBER, "direction": "out"},
        ],
        "controls": [
            _knob("a", "A"),
            _knob("b", "B"),
            {"key": "op", "widget": "choice", "label": "Operation",
             "choices": ["add", "subtract", "multiply", "divide"]},
        ],
        "display": {"name": "Math", "category": "Primitives",
                    "description": "Add, subtract, multiply or divide two numbers.",
                    "color": "success_color"},
    },
    {
        "type_id": "compare",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": NUMBER, "direction": "in", "drives_control": True},
            {"name": "b", "kind": NUMBER, "direction": "in", "drives_control": True},
            {"name": "out", "kind": BOOL, "direction": "out"},
        ],
        "controls": [
            _knob("a", "A"),
            _knob("b", "B"),
            {"key": "op", "widget": "choice", "label": "Comparison",
             "choices": ["<", "<=", "==", "!=", ">=", ">"]},
        ],
        "display": {"name": "Compare", "category": "Primitives",
                    "description": "Compare two numbers.", "color": "success_color"},
    },
    {
        "type_id": "logic",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": BOOL, "direction": "in", "drives_control": True},
            {"name": "b", "kind": BOOL, "direction": "in", "drives_control": True},
            {"name": "out", "kind": BOOL, "direction": "out"},
        ],
        "controls": [
            {"key": "a", "widget": "switch", "label": "A"},
            {"key": "b", "widget": "switch", "label": "B"},
            {"key": "op", "widget": "choice", "label": "Operation",
             "choices": ["and", "or", "xor"]},
        ],
        "display": {"name": "Logic", "category": "Primitives",
                    "description": "Combine two on/off signals.",
                    "color": "dim_label_color"},
    },
    {
        "type_id": "invert",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": BOOL, "direction": "in", "drives_control": True},
            {"name": "out", "kind": BOOL, "direction": "out"},
        ],
        "controls": [{"key": "a", "widget": "switch", "label": "A"}],
        "display": {"name": "Invert", "category": "Primitives",
                    "description": "Not: flips an on/off signal.",
                    "color": "dim_label_color"},
    },
    {
        "type_id": "display",
        "schema": 1,
        "ports": [
            # The number it shows: a wire inside this module drives it, and
            # (being named for its own control) any other module's value can
            # drive it through the daemon's relay.
            {"name": "in", "kind": NUMBER, "direction": "in", "drives_control": True},
            # The text it shows.  A port only, no control: there is no
            # generic text-entry widget to be a parameter of, and a display
            # shows what it is handed.
            {"name": "text", "kind": TEXT, "direction": "in"},
            {"name": "out", "kind": NUMBER, "direction": "out"},
        ],
        "controls": [_knob("in", "Value")],
        "display": {"name": "Display", "category": "Primitives",
                    "description": "Shows the value it is handed (and passes it on).",
                    "color": "warning_color"},
    },
]

MENU = [
    {"category": "Primitives", "items": [
        {"type_id": "number", "label": "Number"},
        {"type_id": "const", "label": "Constant"},
        {"type_id": "slider", "label": "Slider"},
        {"type_id": "toggle", "label": "On/Off"},
        {"type_id": "string", "label": "String"},
        {"type_id": "math", "label": "Math"},
        {"type_id": "compare", "label": "Compare"},
        {"type_id": "logic", "label": "Logic"},
        {"type_id": "invert", "label": "Invert"},
        {"type_id": "display", "label": "Display"},
    ]},
]
