"""
declarative.py

Declarative node files: the file-backed, re-derivable half of a Patch
Space session.

A declarative file is a *source of truth* rather than a saved snapshot.
The daemon loads every file on start / rebuild / when one changes and
re-derives the nodes and edges from it, tagging them ``declarative``.
Declarative nodes/edges are never written back to the imperative
autosave, and edits or deletions to them in the GUI revert on the next
reload - that is the point: Nix (or any other tool) can own those files,
and the daemon will converge to them.

File shape
----------
The config itself is the same JSON the daemon exports::

    {"nodes": {id: {"type", "params"}}, "edges": [...], "groups": [...]}

That is wrapped in a metadata layer with the group's presentation::

    {
      "label": "My Group",
      "color": "#3584e4",
      "readonly": false,          # optional; marks an RW file uneditable
      "config": { ...same shape as above... }
    }

Files written by older builds were the bare config; those are still
read, with the file stem standing in for the label and a default colour.

Two directories are scanned:

  * a read-only directory (typically a Nix store path - never written),
  * a read-write directory (the GUI exports groups here).

Node ids inside a file are plain (``mic``, ``gate``); the daemon namespaces
them as ``<file-stem>::<id>`` so two files can't collide and provenance is
visible.  An edge endpoint that names one of the file's own nodes is
qualified the same way; anything else is left as written, so a file can
reference another file's node (``otherfile::node``) or an imperative node.
Group ids and their members are namespaced the same way.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

DECLARATIVE_SUFFIX = ".json"
# Separates the file stem from the local node id.  Node ids never contain
# a double colon (edge ids use a single ":" for ports), so this is
# unambiguous when splitting a namespaced id back apart.
NAMESPACE_SEP = "::"
DEFAULT_GROUP_COLOR = "#3584e4"


def file_stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def namespace_id(stem: str, node_id: str) -> str:
    return f"{stem}{NAMESPACE_SEP}{node_id}"


def split_namespaced(node_id: str):
    """(stem, local_id) for a namespaced id, or (None, node_id)."""
    if NAMESPACE_SEP in node_id:
        stem, _, local = node_id.partition(NAMESPACE_SEP)
        return stem, local
    return None, node_id


def extract_config(raw: dict) -> dict:
    """The inner config from a parsed file, accepting both the wrapped
    shape and the bare legacy shape."""
    inner = raw.get("config")
    if isinstance(inner, dict):
        return inner
    return raw


def read_meta(path: str, raw: dict) -> dict:
    """Presentation metadata for one parsed file.  Missing fields fall
    back to the file stem / default colour / not-read-only."""
    return {
        "stem": file_stem(path),
        "label": raw.get("label") or file_stem(path),
        "color": raw.get("color") or DEFAULT_GROUP_COLOR,
        "readonly": bool(raw.get("readonly", False)),
    }


def list_files(directory: Optional[str]) -> List[str]:
    if not directory or not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(DECLARATIVE_SUFFIX) and not name.endswith(".tmp")
    )


def read_file(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Declarative file %r could not be read: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def namespaced(path: str, raw: dict) -> dict:
    """Turn one parsed file into a load-ready config: ids qualified with
    the file stem, ``declarative: true`` on every node/edge/group, plus
    the per-file metadata (so the GUI can label the nodes)."""
    stem = file_stem(path)
    meta = read_meta(path, raw)
    config = extract_config(raw)
    raw_nodes = config.get("nodes") or {}
    own = set(raw_nodes)

    nodes: Dict[str, dict] = {}
    for local_id, cfg in raw_nodes.items():
        if not isinstance(cfg, dict):
            continue
        params = dict(cfg.get("params") or {})
        params["declarative"] = True
        nodes[namespace_id(stem, local_id)] = {
            "type": cfg.get("type"),
            "params": params,
        }

    edges: List[dict] = []
    for edge in config.get("edges") or []:
        if not isinstance(edge, dict):
            continue
        src, dst = edge.get("from"), edge.get("to")
        if not src or not dst:
            continue
        # Same-file endpoints are written plain; qualify them.  Anything
        # else is already a full (namespaced or imperative) id.
        if src in own:
            src = namespace_id(stem, src)
        if dst in own:
            dst = namespace_id(stem, dst)
        entry = {"from": src, "to": dst, "declarative": True}
        if edge.get("to_port"):
            entry["to_port"] = edge["to_port"]
        if edge.get("from_port"):
            entry["from_port"] = edge["from_port"]
        edges.append(entry)

    groups = []
    for raw_group in config.get("groups") or []:
        if not isinstance(raw_group, dict):
            continue
        gid = raw_group.get("id")
        if not gid:
            continue
        members = []
        for member in raw_group.get("nodes") or []:
            members.append(namespace_id(stem, member) if member in own else member)
        groups.append(
            {
                "id": namespace_id(stem, gid),
                "label": raw_group.get("label", "Group"),
                "color": raw_group.get("color", DEFAULT_GROUP_COLOR),
                "declarative": True,
                "nodes": members,
            }
        )

    return {"nodes": nodes, "edges": edges, "groups": groups, "meta": meta}


def load_all(read_only: Optional[str], read_write: Optional[str]) -> dict:
    """Merge every declarative file in both directories into one config.

    Read-write files are loaded after read-only ones, so a same-named
    read-write file shadows a read-only one (a user override of a
    Nix-provided group).  ``meta`` maps each stem to its label/colour so
    the daemon can tag the serialized nodes."""
    config: Dict[str, object] = {
        "nodes": {},
        "edges": [],
        "groups": [],
        "meta": {},
    }
    for directory, writable in ((read_only, False), (read_write, True)):
        for path in list_files(directory):
            raw = read_file(path)
            if raw is None:
                continue
            ns = namespaced(path, raw)
            meta = dict(ns["meta"])
            # A file is editable only if it lives in the RW directory and
            # does not declare itself read-only.
            meta["writable"] = writable and not meta["readonly"]
            meta["path"] = path
            meta["directory"] = directory
            config["nodes"].update(ns["nodes"])  # type: ignore[union-attr]
            config["edges"].extend(ns["edges"])  # type: ignore[union-attr]
            config["groups"].extend(ns["groups"])  # type: ignore[union-attr]
            config["meta"][meta["stem"]] = meta  # type: ignore[index]
    return config


def snapshot(directories) -> Dict[str, float]:
    """{path: mtime} over both directories, for change detection."""
    state: Dict[str, float] = {}
    for directory in directories:
        for path in list_files(directory):
            try:
                state[path] = os.path.getmtime(path)
            except OSError:
                pass
    return state


def build_payload(config: dict, label: str, color: str, readonly: bool = False) -> dict:
    payload = {
        "label": label,
        "color": color,
        "config": {
            "nodes": config.get("nodes", {}),
            "edges": config.get("edges", []),
        },
    }
    if config.get("groups"):
        payload["config"]["groups"] = config["groups"]
    if readonly:
        payload["readonly"] = True
    return payload


def write_file(
    path: str, config: dict, label: str = "", color: str = "", readonly: bool = False
) -> None:
    """Atomically write a declarative file (tmp + rename)."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    payload = build_payload(
        config,
        label or file_stem(path),
        color or DEFAULT_GROUP_COLOR,
        readonly,
    )
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)
