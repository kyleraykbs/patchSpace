"""
migrations.py

Versioned, in-memory migration of saved session / panel node configs.

Why this exists
---------------
A session is a hand-editable document that outlives the code that wrote
it.  When a node's design changes, the old node type must keep loading
*and* be quietly rewritten into the new shape, or every saved session
carries dead nodes forever and the user has to rebuild by hand.

This module is the one place that says "config schema N is upgraded to
N+1 by this function".  A config records its ``schema_version``; the
loader calls :func:`migrate` before creating nodes, so an old session is
brought forward in memory without touching the file until the panel is
next written (the daemon's normal write-back persists it).  The repair
CLI can force-persist the result (``session_repair.repair`` runs the same
migrate pass, so ``--write`` rewrites it).

Adding a migration: append an entry to ``MIGRATIONS`` with a new id, and
bump ``SCHEMA_VERSION``.  Migrations run in id order and must be
idempotent (the loader may migrate an already-current config).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Set, Tuple

# Bump when a migration is added.
SCHEMA_VERSION = 1


@dataclass
class Migration:
    version: int
    description: str
    apply: Callable[[dict], List[str]]


def _unique(base: str, taken: Set[str]) -> str:
    if base not in taken:
        return base
    i = 2
    while f"{base}_{i}" in taken:
        i += 1
    return f"{base}_{i}"


def _split_id(node_id: str) -> Tuple[str, str]:
    """Split a possibly panel-qualified id into (prefix incl. "::",
    local).  New nodes are created under the same panel as the legacy
    node they replace, so LCA edge ownership keeps them together."""
    if "::" in node_id:
        prefix, local = node_id.rsplit("::", 1)
        return prefix + "::", local
    return "", node_id


def _migrate_legacy_filter_leaves(config: dict) -> List[str]:
    """Rewrite the six legacy Regex/Media Class/Description input/output
    leaves as bundle pipelines.

    A legacy *input* leaf (regex_input, ...) selected sources to route
    from, so it becomes: All Inputs -> Filter(classifier) -> its old
    destinations.  A legacy *output* leaf selected sinks to route into,
    so it becomes: All Outputs -> Filter(classifier) -> Bundle Output,
    with its old sources wired into the terminal.  ``port_type`` (the
    legacy channel-group selector) has no equivalent yet and is dropped.
    """
    fixes: List[str] = []
    nodes: Dict[str, dict] = config.get("nodes", {}) or {}
    edges: List[dict] = config.get("edges", []) or []

    # legacy type -> (side, classifier_type, classify_param, legacy_field)
    SPEC: Dict[str, Tuple[str, str, str]] = {
        "regex_input": ("source", "regex_classifier", "pattern"),
        "media_class_input": ("source", "media_class_classifier", "media_class"),
        "description_input": ("source", "description_classifier", "description"),
        "regex_output": ("sink", "regex_classifier", "pattern"),
        "media_class_output": ("sink", "media_class_classifier", "media_class"),
        "description_output": ("sink", "description_classifier", "description"),
    }
    legacy_ids = [nid for nid, node in nodes.items() if node.get("type") in SPEC]
    if not legacy_ids:
        return fixes

    taken = set(nodes)
    presets: Dict[str, str] = {}

    def preset_for(side: str) -> str:
        if side in presets:
            return presets[side]
        base = "all_inputs" if side == "source" else "all_outputs"
        pid = _unique(base, taken)
        taken.add(pid)
        nodes[pid] = {"type": base, "params": {}}
        presets[side] = pid
        fixes.append(f"migration: added preset {pid!r} ({base})")
        return pid

    for nid in legacy_ids:
        node = nodes[nid]
        legacy_type = node.get("type")
        side, classifier_type, field = SPEC[legacy_type]
        params = node.get("params") or {}
        prefix, local = _split_id(nid)

        classifier_id = _unique(f"{prefix}__{field}_classifier__{local}", taken)
        taken.add(classifier_id)
        filter_id = _unique(f"{prefix}__filter__{local}", taken)
        taken.add(filter_id)
        terminal_id: Optional[str] = None
        if side == "sink":
            terminal_id = _unique(f"{prefix}__bundle_output__{local}", taken)
            taken.add(terminal_id)

        x = params.get("x")
        y = params.get("y")
        classifier_params: Dict[str, object] = {field: params.get(field, "")}
        nodes[classifier_id] = {"type": classifier_type, "params": classifier_params}
        filter_params: Dict[str, object] = {}
        if x is not None:
            filter_params["x"] = x
        if y is not None:
            filter_params["y"] = y
        if node.get("label"):
            filter_params["label"] = node.get("label")
        nodes[filter_id] = {"type": "filter", "params": filter_params}
        if terminal_id is not None:
            nodes[terminal_id] = {"type": "bundle_output", "params": {}}

        # Re-point / drop the legacy node's edges.
        kept: List[dict] = []
        for e in edges:
            frm, to = e.get("from"), e.get("to")
            if frm != nid and to != nid:
                kept.append(e)
                continue
            if side == "source":
                if frm == nid:
                    new_edge = {"from": filter_id, "to": to}
                    if e.get("to_port") not in (None, "in"):
                        new_edge["to_port"] = e["to_port"]
                    kept.append(new_edge)
                else:
                    fixes.append(
                        f"migration: dropped edge into source leaf {nid!r}"
                    )
            else:
                if to == nid:
                    new_edge = {"from": frm, "to": terminal_id}
                    if e.get("from_port") not in (None, "out"):
                        new_edge["from_port"] = e["from_port"]
                    kept.append(new_edge)
                else:
                    fixes.append(
                        f"migration: dropped edge out of sink leaf {nid!r}"
                    )
        edges = kept

        preset = preset_for(side)
        edges.append({"from": preset, "to": filter_id, "to_port": "in"})
        edges.append({"from": classifier_id, "to": filter_id, "to_port": "filter"})
        if terminal_id is not None:
            edges.append({"from": filter_id, "to": terminal_id, "to_port": "bundle"})

        del nodes[nid]
        fixes.append(
            f"migration: {legacy_type} {nid!r} -> "
            f"{classifier_type} + filter"
            + (" + bundle_output" if terminal_id else "")
        )

    config["nodes"] = nodes
    config["edges"] = edges
    return fixes


MIGRATIONS: List[Migration] = [
    Migration(
        version=1,
        description="legacy regex/media class/description leaves -> bundles",
        apply=_migrate_legacy_filter_leaves,
    ),
]


def migrate(config: dict) -> Tuple[dict, List[str]]:
    """Return ``(migrated_config, fixes)`` for any schema older than
    ``SCHEMA_VERSION``.  Mutates and returns the same dict (the loaders
    own their copy); idempotent, so a current config comes back
    unchanged with no fixes."""
    version = int(config.get("schema_version") or 0)
    fixes: List[str] = []
    for migration in sorted(MIGRATIONS, key=lambda m: m.version):
        if version >= migration.version:
            continue
        fixes.extend(migration.apply(config))
        version = migration.version
    config["schema_version"] = version
    return config, fixes
