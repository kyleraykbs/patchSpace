"""
session_repair.py

Validate a Patch Space session config (the JSON shape written by
``main.PatchBayDaemon._build_export_config`` - ``{"nodes", "edges",
"groups"}``) and, optionally, repair the parts that are objectively
broken.

Why this exists
---------------
A session is a hand-editable document that accumulates history: node
type names change (``a``/``b`` switch ports became ``on``/``off``),
nodes are renamed, features are removed, and a crashed daemon can leave
edge entries pointing at nodes that no longer exist.  The loader is
deliberately forgiving (it logs per-node/per-edge failures and keeps
going), which is right for a live edit but means a config can carry
dead weight forever.  This module is the explicit pass that says what is
wrong and - for the unambiguous cases - fixes it.

Two entry points:

  * :func:`validate` reports every problem it can find without touching
    anything.  Use it from the CLI (``--check``) or the daemon's
    ``validate_session`` command.
  * :func:`repair` returns a fixed *copy* of the config plus the list of
    fixes it applied.  Only safe, lossless fixes are on by default:
    nodes of unknown type, edges with missing endpoints or invalid
    ports, exact-duplicate edges, legacy switch port names.  Destructive
    cleanups (dropping orphan nodes, collapsing duplicate built-in lines)
    are opt-in.

The port/type knowledge comes from ``gui.node_specs`` (pure data, no
GTK), so the daemon can import this without pulling in the GUI.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Set, Tuple

import migrations
from gui.node_specs import (
    NODE_TYPE_SPECS,
    normalize_node_type,
    port_kind,
    ports_compatible,
)

# Switch ports were renamed a/b -> on/off; the engine tolerates the old
# names (see pwnodes.ABSwitchNode._LEGACY_PORTS) but a config should be
# normalized so the GUI, serialization and the graph all agree.
LEGACY_PORTS = {"a": "on", "b": "off"}

# Node types that are just a "line" to one of the daemon's built-in
# virtual devices.  Several may legitimately exist, but they all resolve
# to the same underlying object, so a config with more than one is worth
# flagging (and, when asked, collapsing).  Note this does NOT include
# virtual_speaker/virtual_mic - those are independent named devices with
# their own backing, one per node.
BUILTIN_LINE_TYPES = ("patchbay_device", "patchbay_mic_device")

ERROR = "error"
WARNING = "warning"


@dataclass
class Issue:
    severity: str
    code: str
    where: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity.upper()}] {self.code} {self.where}: {self.message}"


@dataclass
class RepairResult:
    config: dict
    issues: List[Issue] = field(default_factory=list)
    fixes: List[str] = field(default_factory=list)

    @property
    def errors(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == ERROR]

    @property
    def warnings(self) -> List[Issue]:
        return [i for i in self.issues if i.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors


def _known_types() -> Set[str]:
    return set(NODE_TYPE_SPECS)


def _node_type(config: dict, node_id: str) -> Optional[str]:
    node = config.get("nodes", {}).get(node_id)
    if node is None:
        return None
    return normalize_node_type(node.get("type", ""))


def _valid_inputs(node_type: str) -> Optional[Set[str]]:
    # A Merge Bundle's inputs are dynamic (in1, in2, ...) and a Filter's
    # classifier inputs are too (filter1, filter2, ...): the edge names
    # them, so any port is valid and there is no fixed set to check.
    if node_type in ("bundle", "filter"):
        return None
    spec = NODE_TYPE_SPECS.get(node_type)
    return set(spec.inputs) if spec is not None else None


def _valid_outputs(node_type: str) -> Optional[Set[str]]:
    # A Split Bundle's outputs are dynamic (one per live member, keyed by
    # node.name), so any output port is valid.
    if node_type == "bundle_split":
        return None
    spec = NODE_TYPE_SPECS.get(node_type)
    return set(spec.outputs) if spec is not None else None


def _normalized_port(node_type: str, port: str, direction: str) -> Tuple[str, bool]:
    """Return ``(port, changed)`` with a legacy switch port mapped to its
    modern name when (and only when) that makes it valid for the node."""
    valid = _valid_inputs(node_type) if direction == "in" else _valid_outputs(node_type)
    if valid is None or port in valid:
        return port, False
    modern = LEGACY_PORTS.get(port)
    if modern is not None and modern in valid:
        return modern, True
    return port, False


def _edge_key(e: dict) -> Tuple[str, str, str, str]:
    return (
        e.get("from"),
        e.get("to"),
        e.get("to_port", "in"),
        e.get("from_port", "out"),
    )


def _builtin_identity(node_type: str, params: dict) -> Optional[str]:
    """A stable key for the built-in device a line node resolves to, or
    None when the type isn't an alias of a built-in.

    Only ``patchbay_device``/``patchbay_mic_device`` are aliases of the
    daemon's single built-in speaker/mic.  ``virtual_speaker``/
    ``virtual_mic`` are separate user-named devices (one backing each),
    so two of those are two real devices, not a duplicate."""
    if node_type == "patchbay_device":
        return "PatchBay"
    if node_type == "patchbay_mic_device":
        return "PatchBay Mic"
    return None


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------


def validate(config: dict, known_types: Optional[Iterable[str]] = None) -> List[Issue]:
    """Report every problem in ``config`` without changing it.

    ``known_types`` is the authoritative set of node type keys (the
    daemon passes its ``NODE_TYPE_REGISTRY`` keys; the CLI defaults to
    the GUI specs).  Types outside it are reported as unknown; types
    inside it but absent from the specs (e.g. a test stub) are left
    alone - their edges simply aren't port-checked.
    """
    issues: List[Issue] = []
    nodes = config.get("nodes", {}) or {}
    edges = config.get("edges", []) or []
    groups = config.get("groups", []) or []
    known = set(known_types) if known_types is not None else _known_types()

    # Normalize types once for all lookups.
    types: Dict[str, str] = {}
    for nid, node in nodes.items():
        raw = node.get("type", "")
        ntype = normalize_node_type(raw)
        types[nid] = ntype
        if ntype not in known:
            issues.append(Issue(ERROR, "unknown-type", nid, f"node type {raw!r} is not recognized"))

    # Backing-name collisions: two nodes fighting over one real object.
    by_backing: Dict[str, List[str]] = {}
    for nid, node in nodes.items():
        backing = (node.get("params") or {}).get("backing_node_name")
        if backing:
            by_backing.setdefault(backing, []).append(nid)
    for backing, ids in by_backing.items():
        if len(ids) > 1:
            issues.append(
                Issue(
                    WARNING,
                    "backing-collision",
                    backing,
                    f"{len(ids)} nodes share it: {sorted(ids)}",
                )
            )

    # Duplicate / invalid edges.
    seen: Dict[Tuple[str, str, str, str], int] = {}
    for idx, e in enumerate(edges):
        f, t = e.get("from"), e.get("to")
        where = e.get("id") or f"{f}->{t}"
        key = _edge_key(e)
        seen[key] = seen.get(key, 0) + 1
        if f not in nodes or t not in nodes:
            missing = [n for n in (f, t) if n not in nodes]
            issues.append(Issue(ERROR, "missing-endpoint", where, f"edge references missing node(s) {missing}"))
            continue
        ftype, ttype = types[f], types[t]
        to_port = e.get("to_port", "in")
        from_port = e.get("from_port", "out")
        nin, _ = _normalized_port(ttype, to_port, "in") if ttype in known else (to_port, False)
        nout, _ = _normalized_port(ftype, from_port, "out") if ftype in known else (from_port, False)
        ins = _valid_inputs(ttype)
        outs = _valid_outputs(ftype)
        if ins is not None and nin not in ins:
            issues.append(Issue(ERROR, "bad-port", where, f"{ttype} has no input {to_port!r} (inputs: {sorted(ins)})"))
        if outs is not None and nout not in outs:
            issues.append(Issue(ERROR, "bad-port", where, f"{ftype} has no output {from_port!r} (outputs: {sorted(outs)})"))
            continue
        if ins is None or outs is None:
            continue
        if not ports_compatible(ftype, nout, ttype, nin):
            issues.append(
                Issue(
                    ERROR,
                    "port-kind",
                    where,
                    f"{ftype}:{nout} is {port_kind(ftype, nout, 'out')} but "
                    f"{ttype}:{nin} is {port_kind(ttype, nin, 'in')}",
                )
            )
    for key, count in seen.items():
        if count > 1:
            issues.append(Issue(ERROR, "duplicate-edge", f"{key[0]}->{key[1]}", f"appears {count} times"))

    # Duplicate built-in line nodes.
    line_nodes: Dict[str, List[str]] = {}
    for nid, ntype in types.items():
        ident = _builtin_identity(ntype, nodes.get(nid, {}).get("params") or {})
        if ident:
            line_nodes.setdefault(ident, []).append(nid)
    for ident, ids in line_nodes.items():
        if len(ids) > 1:
            issues.append(
                Issue(
                    WARNING,
                    "duplicate-line",
                    ident,
                    f"{len(ids)} line nodes resolve to the same built-in {ident}: {sorted(ids)}",
                )
            )

    # Orphans and overlapping groups.
    out_deg: Dict[str, int] = {}
    in_deg: Dict[str, int] = {}
    for e in edges:
        if e.get("from") in nodes:
            out_deg[e["from"]] = out_deg.get(e["from"], 0) + 1
        if e.get("to") in nodes:
            in_deg[e["to"]] = in_deg.get(e["to"], 0) + 1
    for nid in nodes:
        if out_deg.get(nid, 0) == 0 and in_deg.get(nid, 0) == 0:
            issues.append(Issue(WARNING, "orphan-node", nid, "no edges connect to it"))
    memberships: Dict[str, List[str]] = {}
    for g in groups:
        for nid in g.get("nodes", []):
            memberships.setdefault(nid, []).append(g.get("id", "?"))
    for nid, gids in memberships.items():
        if len(gids) > 1:
            issues.append(Issue(WARNING, "multi-group", nid, f"listed in {len(gids)} groups: {gids}"))
    return issues


# ---------------------------------------------------------------------------
# repair
# ---------------------------------------------------------------------------


def repair(
    config: dict,
    *,
    known_types: Optional[Iterable[str]] = None,
    dedupe_groups: bool = False,
    drop_orphans: bool = False,
    collapse_duplicate_lines: bool = False,
    migrate: bool = True,
) -> RepairResult:
    """Return a repaired copy of ``config`` plus the fixes applied.

    Safe by default: unknown-type nodes and edges that can't possibly
    connect are dropped, legacy ports are renamed, and exact duplicate
    edges are removed.  ``dedupe_groups`` (overlapping group membership
    may be intentional tagging), ``drop_orphans`` and
    ``collapse_duplicate_lines`` are opt-in.

    ``migrate`` (default True) brings an old-schema config forward to the
    current node shapes.  Incremental single-placement loads pass False so
    a live sibling placement built from the same file can't be rewritten
    out from under it (see main._load_session).
    """
    # Bring the config's schema forward first (legacy node shapes -> the
    # current ones), on a shallow copy so the caller's dict is untouched.
    config = dict(config)
    config["nodes"] = {
        nid: dict(node) for nid, node in (config.get("nodes") or {}).items()
    }
    config["edges"] = [dict(e) for e in (config.get("edges") or [])]
    config["groups"] = [dict(g) for g in (config.get("groups") or [])]
    migration_fixes: List[str] = []
    if migrate:
        config, migration_fixes = migrations.migrate(config)

    nodes_in = config.get("nodes", {}) or {}
    edges_in = config.get("edges", []) or []
    groups_in = config.get("groups", []) or []
    known = set(known_types) if known_types is not None else _known_types()

    issues = validate(config, known)
    result = RepairResult(config={"nodes": {}, "edges": [], "groups": []}, issues=issues)
    fixes = result.fixes
    fixes.extend(migration_fixes)

    # 1. Nodes of a known type survive (normalized to their canonical key).
    nodes: Dict[str, dict] = {}
    for nid, node in nodes_in.items():
        raw = node.get("type", "")
        ntype = normalize_node_type(raw)
        if ntype not in known:
            fixes.append(f"dropped node {nid!r}: unknown type {raw!r}")
            continue
        copy = dict(node)
        if ntype != raw:
            copy["type"] = ntype
            fixes.append(f"normalized type of {nid!r}: {raw!r} -> {ntype!r}")
        nodes[nid] = copy

    # 2. Edges: normalize legacy ports, then keep only well-formed ones.
    edges: List[dict] = []
    seen: Set[Tuple[str, str, str, str]] = set()
    for e in edges_in:
        f, t = e.get("from"), e.get("to")
        if f not in nodes or t not in nodes:
            fixes.append(f"dropped edge {f}->{t}: endpoint no longer exists")
            continue
        ftype = nodes[f]["type"]
        ttype = nodes[t]["type"]
        to_port = e.get("to_port", "in")
        from_port = e.get("from_port", "out")
        to_port, to_changed = _normalized_port(ttype, to_port, "in")
        from_port, from_changed = _normalized_port(ftype, from_port, "out")
        ins = _valid_inputs(ttype)
        outs = _valid_outputs(ftype)
        if (ins is not None and to_port not in ins) or (
            outs is not None and from_port not in outs
        ):
            fixes.append(f"dropped edge {f}->{t}: invalid port {from_port}->{to_port}")
            continue
        if (
            ins is not None
            and outs is not None
            and not ports_compatible(ftype, from_port, ttype, to_port)
        ):
            fixes.append(
                f"dropped edge {f}->{t}: boolean/audio/filter port mismatch"
            )
            continue
        new_edge = {"from": f, "to": t}
        if to_port != "in":
            new_edge["to_port"] = to_port
        if from_port != "out":
            new_edge["from_port"] = from_port
        key = (f, t, to_port, from_port)
        if key in seen:
            fixes.append(f"dropped duplicate edge {f}->{t}")
            continue
        seen.add(key)
        if to_changed or from_changed:
            fixes.append(f"normalized ports on {f}->{t}: {from_port}->{to_port}")
        edges.append(new_edge)

    # 3. Groups: keep only known nodes, drop empty groups, collapse
    #    overlapping membership (a node belongs to at most one group).
    claimed: Set[str] = set()
    groups: List[dict] = []
    for g in groups_in:
        members = []
        for nid in g.get("nodes", []):
            if nid not in nodes:
                fixes.append(f"group {g.get('id')!r}: dropped missing node {nid!r}")
                continue
            if dedupe_groups and nid in claimed:
                fixes.append(f"group {g.get('id')!r}: removed {nid!r} (already in another group)")
                continue
            members.append(nid)
            claimed.add(nid)
        if not members and g.get("nodes"):
            fixes.append(f"dropped empty group {g.get('id')!r}")
            continue
        copy = dict(g)
        copy["nodes"] = members
        groups.append(copy)

    # 4. Optional destructive cleanups.
    if collapse_duplicate_lines:
        _collapse_line_nodes(nodes, edges, fixes)
    if drop_orphans:
        connected = set()
        for e in edges:
            connected.add(e["from"])
            connected.add(e["to"])
        for nid in list(nodes):
            if nid not in connected:
                del nodes[nid]
                fixes.append(f"dropped orphan node {nid!r}")

    result.config = {"nodes": nodes, "edges": edges, "groups": groups}
    if "schema_version" in config:
        result.config["schema_version"] = config["schema_version"]
    result.issues = validate(result.config, known)
    return result


def _collapse_line_nodes(nodes: Dict[str, dict], edges: List[dict], fixes: List[str]) -> None:
    """Drop redundant built-in line nodes, keeping the busiest one.

    A line node (``patchbay_device`` / ``patchbay_mic_device`` / a
    virtual speaker/mic) owns no backing - it is only an alias for the
    built-in device - so two of them are two names for one object and
    their edges are summed.  Keeping the node with the most edges and
    removing the others (with their now-dangling edges) is the safe
    consolidation; nothing else in the graph is re-pointed, so a chain
    that deliberately mixed an extra source into the built-in via a
    second line is removed rather than silently merged."""
    by_ident: Dict[str, List[str]] = {}
    for nid, node in nodes.items():
        ident = _builtin_identity(node.get("type", ""), node.get("params") or {})
        if ident:
            by_ident.setdefault(ident, []).append(nid)
    for ident, ids in by_ident.items():
        if len(ids) < 2:
            continue
        deg = {nid: 0 for nid in ids}
        for e in edges:
            if e.get("from") in deg:
                deg[e["from"]] += 1
            if e.get("to") in deg:
                deg[e["to"]] += 1
        # Keep the best-connected; tie-break on the id for determinism.
        keep = sorted(ids, key=lambda n: (-deg[n], n))[0]
        for nid in ids:
            if nid == keep:
                continue
            del nodes[nid]
            fixes.append(f"collapsed duplicate {ident} line: removed {nid!r} (kept {keep!r})")
        edges[:] = [e for e in edges if e.get("from") in nodes and e.get("to") in nodes]


# ---------------------------------------------------------------------------
# reporting / CLI
# ---------------------------------------------------------------------------


def format_report(issues: Iterable[Issue]) -> str:
    lines = [str(i) for i in issues]
    return "\n".join(lines) if lines else "no issues"


def main(argv: Optional[List[str]] = None) -> int:
    import argparse
    import json
    import sys

    parser = argparse.ArgumentParser(description="Validate and repair a Patch Space session JSON.")
    parser.add_argument("path", nargs="?", help="session JSON (default: last-session cache)")
    parser.add_argument("--check", action="store_true", help="report problems only, never write")
    parser.add_argument("--write", action="store_true", help="write the repaired config back")
    parser.add_argument("--collapse-duplicate-lines", action="store_true", help="drop redundant built-in line nodes")
    parser.add_argument("--drop-orphans", action="store_true", help="drop nodes with no edges")
    parser.add_argument("--dedupe-groups", action="store_true", help="keep each node in at most one group")
    args = parser.parse_args(argv)

    if not args.path:
        from main import SESSION_CACHE_PATH

        args.path = SESSION_CACHE_PATH

    with open(args.path) as f:
        config = json.load(f)

    result = repair(
        config,
        collapse_duplicate_lines=args.collapse_duplicate_lines,
        drop_orphans=args.drop_orphans,
        dedupe_groups=args.dedupe_groups,
    )
    print("Issues:")
    print(format_report(result.issues))
    print()
    print(f"Fixes: {len(result.fixes)}")
    for fix in result.fixes:
        print("  -", fix)

    if args.write and not args.check:
        with open(args.path, "w") as f:
            json.dump(result.config, f, indent=2, sort_keys=True)
        print(f"\nWrote {args.path}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
