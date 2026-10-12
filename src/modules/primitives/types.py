"""
types.py — the primitives module's node types.

Numbers and booleans, wired: a value source, arithmetic, comparison,
boolean logic, and a display that shows what it is handed.  Data only — the
module announces exactly this, and a display draws it without knowing
anything about the module (see `gui/node_catalog.py`).
"""

from __future__ import annotations

from typing import Any, Dict, List

NUMBER = "number"
BOOL = "boolean"

TYPES: List[Dict[str, Any]] = [
    {
        "type_id": "number",
        "schema": 1,
        "ports": [{"name": "out", "kind": NUMBER, "direction": "out"}],
        "controls": [{"key": "value", "widget": "slider", "label": "Value",
                      "min": -100.0, "max": 100.0}],
        "display": {"name": "Number", "category": "Primitives",
                    "description": "A number you set.", "color": "accent_color"},
    },
    {
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
        "type_id": "math",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": NUMBER, "direction": "in"},
            {"name": "b", "kind": NUMBER, "direction": "in"},
            {"name": "out", "kind": NUMBER, "direction": "out"},
        ],
        "controls": [{"key": "op", "widget": "choice", "label": "Operation",
                      "choices": ["add", "subtract", "multiply", "divide"]}],
        "display": {"name": "Math", "category": "Primitives",
                    "description": "Add, subtract, multiply or divide two numbers.",
                    "color": "success_color"},
    },
    {
        "type_id": "compare",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": NUMBER, "direction": "in"},
            {"name": "b", "kind": NUMBER, "direction": "in"},
            {"name": "out", "kind": BOOL, "direction": "out"},
        ],
        "controls": [{"key": "op", "widget": "choice", "label": "Comparison",
                      "choices": ["<", "<=", "==", "!=", ">=", ">"]}],
        "display": {"name": "Compare", "category": "Primitives",
                    "description": "Compare two numbers.", "color": "success_color"},
    },
    {
        "type_id": "logic",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": BOOL, "direction": "in"},
            {"name": "b", "kind": BOOL, "direction": "in"},
            {"name": "out", "kind": BOOL, "direction": "out"},
        ],
        "controls": [{"key": "op", "widget": "choice", "label": "Operation",
                      "choices": ["and", "or", "xor"]}],
        "display": {"name": "Logic", "category": "Primitives",
                    "description": "Combine two on/off signals.",
                    "color": "dim_label_color"},
    },
    {
        "type_id": "invert",
        "schema": 1,
        "ports": [
            {"name": "a", "kind": BOOL, "direction": "in"},
            {"name": "out", "kind": BOOL, "direction": "out"},
        ],
        "display": {"name": "Invert", "category": "Primitives",
                    "description": "Not: flips an on/off signal.",
                    "color": "dim_label_color"},
    },
    {
        "type_id": "display",
        "schema": 1,
        "ports": [
            {"name": "in", "kind": NUMBER, "direction": "in"},
            {"name": "out", "kind": NUMBER, "direction": "out"},
        ],
        "display": {"name": "Display", "category": "Primitives",
                    "description": "Shows the number it is handed (and passes it on).",
                    "color": "warning_color"},
    },
]

MENU = [
    {"category": "Primitives", "items": [
        {"type_id": "number", "label": "Number"},
        {"type_id": "slider", "label": "Slider"},
        {"type_id": "toggle", "label": "On/Off"},
        {"type_id": "math", "label": "Math"},
        {"type_id": "compare", "label": "Compare"},
        {"type_id": "logic", "label": "Logic"},
        {"type_id": "invert", "label": "Invert"},
        {"type_id": "display", "label": "Display"},
    ]},
]
