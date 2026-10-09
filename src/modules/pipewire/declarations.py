"""
declarations.py — the node type catalog the PipeWire module announces.

The module owns its types; this file is where they are described in the
wire vocabulary of `central.module_api`.  Until phase 2 deletes it,
`gui/node_specs.py` is the source of the presentation data (labels,
descriptions, the add-node category, the theme colour name) and the
daemon's `NODE_TYPE_REGISTRY` supplies the key → class map — the generator
takes that map as an argument so this module never imports the daemon.

`tests/test_pipewire_declarations.py` asserts the catalog and both legacy
tables agree for every registered type, so adding a node type without
teaching the module about it fails the suite instead of producing a node a
display cannot draw.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

#: spec attribute -> the port kind those names carry.  The *specific*
#: families come first because a name may appear in two lists: a gate's
#: "ctrl" is listed in `inputs` for the node body's layout but its kind is
#: declared by `boolean_inputs`, and the specific family wins.
KIND_LISTS: Tuple[Tuple[str, str, str], ...] = (
    ("boolean_inputs", "boolean", "in"),
    ("boolean_outputs", "boolean", "out"),
    ("impulse_inputs", "impulse", "in"),
    ("impulse_outputs", "impulse", "out"),
    ("bundle_inputs", "bundle", "in"),
    ("bundle_outputs", "bundle", "out"),
    ("filter_inputs", "filter", "in"),
    ("filter_outputs", "filter", "out"),
    ("sound_inputs", "sound", "in"),
    ("sound_outputs", "sound", "out"),
    ("camera_inputs", "camera", "in"),
    ("camera_outputs", "camera", "out"),
    ("inputs", "audio", "in"),
    ("outputs", "audio", "out"),
)

#: Port families the runtime grows on demand (a filter takes one classifier
#: per wired input; a bundle one socket per member) - the spec lists one
#: placeholder name, the display draws as many as the node reports.
DYNAMIC_KINDS = frozenset({"bundle", "filter"})

_DYNAMIC_NAME = re.compile(r"^(in|out|filter)\d+$")

#: Port names that mean "the default socket" - the display draws the label
#: as "in"/"out" and nothing else, so they carry no special marker.
DEFAULT_PORTS = frozenset({"in", "out", "audio"})


def _spec_module():
    """`gui.node_specs` is pure data (no GTK), imported the same way
    `central/session_repair.py` does.  Phase 2 moves this data into the
    module and deletes the import."""
    import sys
    import os

    src_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if src_root not in sys.path:
        sys.path.insert(0, src_root)
    from gui import node_specs

    return node_specs


def category_map() -> Dict[str, str]:
    """type_key -> the add-node menu category it is grouped under."""
    node_specs = _spec_module()
    mapping: Dict[str, str] = {}
    for category, items in node_specs.ADD_NODE_CATEGORIES:
        for _label, type_key in items:
            mapping[type_key] = category
    return mapping


def canonical_types(type_map: Mapping[str, type]) -> Dict[str, type]:
    """Only the canonical key per class: the registry registers legacy
    aliases after the canonical key, so the first key wins."""
    seen: Set[type] = set()
    canonical: Dict[str, type] = {}
    for type_key, cls in type_map.items():
        if cls in seen:
            continue
        seen.add(cls)
        canonical[type_key] = cls
    return canonical


def _ports(spec: Any) -> List[Dict[str, Any]]:
    ports: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, str]] = set()
    for attr, kind, direction in KIND_LISTS:
        names: Sequence[str] = getattr(spec, attr, None) or ()
        for name in sorted(names):
            if (name, direction) in seen:
                continue
            seen.add((name, direction))
            port: Dict[str, Any] = {"name": name, "kind": kind, "direction": direction}
            if kind in DYNAMIC_KINDS or _DYNAMIC_NAME.match(name):
                port["dynamic"] = True
            ports.append(port)
    return ports


def _controls(spec: Any) -> List[Dict[str, Any]]:
    """The node body's inline controls, in the widget vocabulary a display
    already knows how to draw."""
    controls: List[Dict[str, Any]] = []
    if getattr(spec, "control", None):
        controls.append({"key": spec.control, "widget": spec.control})
    if getattr(spec, "toggle", None):
        controls.append({"key": "toggle", "widget": "toggle", "spec": spec.toggle})
    if getattr(spec, "field", None):
        controls.append({"key": spec.field, "widget": "field", "label": spec.field})
    if getattr(spec, "picker", None):
        controls.append({"key": "picker", "widget": "picker"})
    if getattr(spec, "indicator", None):
        controls.append({"key": spec.indicator, "widget": "indicator"})
    for row in getattr(spec, "settings", None) or ():
        key = row[0] if row else ""
        if key:
            controls.append({"key": key, "widget": "setting"})
    return controls


def _display(spec: Any, type_key: str, categories: Mapping[str, str],
             color_of: Any) -> Dict[str, Any]:
    return {
        "name": getattr(spec, "label", "") or type_key,
        "description": getattr(spec, "description", "") or "",
        "category": categories.get(type_key, ""),
        "color": color_of(type_key) if color_of else "",
    }


def declarations(type_map: Mapping[str, type]) -> List[Dict[str, Any]]:
    """The module's full type catalog, one entry per canonical type key."""
    node_specs = _spec_module()
    categories = category_map()
    color_of = getattr(node_specs, "color_name_for_node_type", None)
    out: List[Dict[str, Any]] = []
    for type_key, cls in sorted(canonical_types(type_map).items()):
        spec = node_specs.NODE_TYPE_SPECS.get(type_key)
        if spec is None:
            # No presentation data: a display could not draw it.  The
            # agreement test is what catches this on a new node type.
            continue
        out.append({
            "type_id": type_key,
            "schema": 1,
            "ports": _ports(spec),
            "controls": _controls(spec),
            "display": _display(spec, type_key, categories, color_of),
            "capabilities": [],
            "requires": [],
        })
    return out
