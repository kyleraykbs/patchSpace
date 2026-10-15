"""
declarations.py — the PipeWire module's node type catalog.

The catalog is *data*: `catalog.json` ships beside this file and is what the
module announces.  It was emitted once from the legacy tables
(`gui/node_specs.py` + the daemon's `NODE_TYPE_REGISTRY`) during the phase-2
move and is the source of truth from then on — the GUI no longer keeps its
own copy, it draws what the daemon serves.

`tests/test_pipewire_declarations.py` asserts the catalog covers every node
type the daemon can construct and that every declaration is wire-valid, so
adding a node type without describing it here fails the suite.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Set

logger = logging.getLogger(__name__)

CATALOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "catalog.json")

#: Port families the runtime grows on demand (a filter takes one classifier
#: per wired input; a bundle one socket per member) - the catalog carries one
#: placeholder name and the display draws as many as the node reports.
DYNAMIC_KINDS = frozenset({"bundle", "filter"})


@lru_cache(maxsize=1)
def _payload() -> Dict[str, Any]:
    with open(CATALOG_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


@lru_cache(maxsize=1)
def _catalog_by_type() -> Dict[str, Dict[str, Any]]:
    return {entry["type_id"]: entry for entry in _payload().get("types", [])}


def catalog() -> List[Dict[str, Any]]:
    """Every declared type, sorted by type_id (a fresh list each call)."""
    return [dict(entry) for entry in sorted(_catalog_by_type().values(),
                                            key=lambda t: t["type_id"])]


def menu() -> List[Dict[str, Any]]:
    """The add-node menu: categories in order, each with the types a user
    may create from it.  Module data, relayed to a display unchanged."""
    return [dict(group) for group in _payload().get("menu", [])]


def vocabulary() -> Dict[str, Any]:
    """Presentation vocabulary the module owns: field labels and the
    media.class choice tables."""
    return dict(_payload().get("vocabulary", {}))


def type_decl(type_id: str) -> Dict[str, Any]:
    return dict(_catalog_by_type().get(type_id, {}))


def canonical_types(type_map: Mapping[str, type]) -> Dict[str, type]:
    """Only the canonical key per class: the daemon's registry registers
    legacy aliases after the canonical key, so the first key wins."""
    seen: Set[type] = set()
    canonical: Dict[str, type] = {}
    for type_key, cls in type_map.items():
        if cls in seen:
            continue
        seen.add(cls)
        canonical[type_key] = cls
    return canonical


def missing_types(type_map: Mapping[str, type]) -> List[str]:
    """Canonical types the daemon can build but the catalog does not
    describe — a display could not draw them.  Empty is the healthy state;
    the test suite asserts it."""
    known = set(_catalog_by_type())
    return sorted(set(canonical_types(type_map)) - known)


def declarations(type_map: Mapping[str, type]) -> List[Dict[str, Any]]:
    """The types this module announces, given what the daemon can construct.

    A registered type with no catalog entry is skipped and logged rather
    than sinking the whole attach — the suite is what makes it an error.
    """
    missing = missing_types(type_map)
    if missing:
        logger.error("no type declaration for %s", ", ".join(missing))
    wanted = set(canonical_types(type_map))
    return [entry for entry in catalog() if entry["type_id"] in wanted]


def legacy_aliases() -> Dict[str, str]:
    """Pre-rename type keys this module still answers to (a space or
    session written under one loads and renders as its canonical type).

    Two renames contribute: PatchBay's `patchbay_device`/`patchbay_mic_device`
    and the S0 panel -> space ones (`panel_in`/`panel_out`/`bool_panel_in`/
    `bool_panel_out` -> their `space_*` spellings - a type id is a persistent
    contract).  Read only: what `declarations()` announces is canonical."""
    return dict(_payload().get("legacy_aliases", {}))
