"""The node catalog as the *daemon* side sees it.

The daemon's tests assert what the PipeWire module **declares** — not what a
display parses out of it.  `gui/node_catalog.py` is the client's parser (and
moves to the client's repo when the display is split out), so daemon tests
must not import it.  This adapter answers the same questions from
`modules/pipewire/catalog.json`, with the compatibility rule taken from
`central/graph.py` (the one authority) rather than re-stated.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set, Tuple

from central import graph
from modules.pipewire import declarations


class Spec:
    """The bits of a node type these tests read, in one object."""

    def __init__(self, entry: Dict[str, Any]) -> None:
        display = entry.get("display") or {}
        extra = entry.get("spec") or {}
        self.type_id = entry["type_id"]
        self.label = display.get("name", "")
        self.description = display.get("description", "")
        self.inputs: List[str] = list(extra.get("inputs") or [])
        self.outputs: List[str] = list(extra.get("outputs") or [])
        self.settings: List[Tuple[Any, ...]] = [
            tuple(row) for row in (extra.get("settings") or [])
        ]
        self.setting_tooltips: Dict[str, str] = dict(extra.get("setting_tooltips") or {})
        self.control: Optional[str] = None
        self.field: Optional[str] = None
        for control in entry.get("controls") or []:
            widget = control.get("widget")
            if widget == "field":
                self.field = control.get("key")
            elif widget not in ("toggle", "picker", "indicator", "setting"):
                self.control = control.get("key")
        for kind in ("boolean", "impulse", "bundle", "filter", "sound", "camera",
                     "number"):
            setattr(self, f"{kind}_inputs", set())
            setattr(self, f"{kind}_outputs", set())
        for port in entry.get("ports") or []:
            direction = "inputs" if port.get("direction") == "in" else "outputs"
            bucket = getattr(self, f"{port.get('kind')}_{direction}", None)
            if bucket is not None:
                bucket.add(port["name"])


_SPECS: Dict[str, Spec] = {}


def _load() -> Dict[str, Spec]:
    specs = {entry["type_id"]: Spec(entry) for entry in declarations.catalog()}
    for legacy, canonical in declarations.legacy_aliases().items():
        if canonical in specs:
            specs[legacy] = specs[canonical]
    return specs


_SPECS = _load()
NODE_TYPE_SPECS = _SPECS

_ADD_NODE_MENU_ITEMS: List[Tuple[str, str]] = [
    (item.get("label") or item["type_id"], item["type_id"])
    for group in declarations.menu()
    for item in group.get("items") or []
]
ADD_NODE_MENU_ITEMS = _ADD_NODE_MENU_ITEMS

_CLASS_TO_TYPE = {
    entry["class_name"]: entry["type_id"]
    for entry in declarations.catalog()
    if entry.get("class_name")
}


def spec_for(type_id: str) -> Spec:
    return _SPECS.get(type_id, Spec({"type_id": type_id}))


def normalize_node_type(raw: str) -> str:
    return _CLASS_TO_TYPE.get(raw, raw)


def description_for(type_id: str) -> str:
    return spec_for(type_id).description


def port_kind(type_id: str, port: str, direction: str) -> str:
    entry = declarations.type_decl(type_id)
    if not entry:
        return "audio"
    spec = spec_for(type_id)
    if direction == "in" and port == "in" and port not in spec.inputs:
        port = spec.inputs[0] if spec.inputs else "in"
    for candidate in entry.get("ports") or []:
        if candidate.get("direction") != direction:
            continue
        name = candidate["name"]
        if name == port:
            return str(candidate.get("kind") or "audio")
        if candidate.get("dynamic") and name.rstrip("0123456789") == port.rstrip("0123456789"):
            return str(candidate.get("kind") or "audio")
    if direction == "in" and type_id == "bundle":
        return "bundle"
    return "audio"


def ports_compatible(from_type: str, from_port: str, to_type: str,
                     to_port: str) -> bool:
    return graph.kinds_compatible(
        port_kind(from_type, from_port, "out"),
        port_kind(to_type, to_port, "in"),
    )


def setting_tooltip(type_id: str, attr: str, label: str) -> str:
    return spec_for(type_id).setting_tooltips.get(attr, label)


def color_name_for_node_type(type_id: str) -> str:
    entry = declarations.type_decl(type_id)
    return (entry.get("display") or {}).get("color", "") or "accent_color"


def icon_for_add_node_type(type_id: str) -> str:
    entry = declarations.type_decl(type_id)
    return (entry.get("display") or {}).get("icon", "")
