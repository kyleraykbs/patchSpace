"""
panels.py

First-class panel containers: the file-backed, nestable boxes the graph
lives in.

A *panel* is a rectangular container on the canvas that owns nodes and
child panels.  The whole graph has a single ``root`` panel; every
non-root panel is one file, and child panels are referenced by their
file stem.  Nodes and panels are laid out in their parent's coordinate
frame (node ``x``/``y`` are panel-relative, a panel's ``x``/``y`` are
parent-relative); an absolute position is the fold of the ancestor
offsets.

Naming
------
A panel's id is its path of stems from the root, joined by ``::``: a
top-level panel ``kit`` is ``kit``, its child ``eq`` is ``kit::eq``.
The root's id is the empty string.  A node/group id is
``<panel-id>::<local>`` (bare ``<local>`` at the root).  Local ids may
not contain ``::``; ``rsplit`` splits a fully-qualified id back apart.

Edges
-----
An edge is *owned* by the deepest panel that contains both of its
endpoints: the least common ancestor of their panels.  A panel file
therefore stores exactly the edges whose LCA is that panel - internal
ones in the leaf panel, cross-panel ones in the common ancestor, and
edges between different top-level panels in the root file.  An edge is
never stored twice.

File shape
----------
::

    {
      "type": "panel",
      "mode": "read-write" | "read-only",
      "label": "...", "color": "#rrggbb",
      "placement": {"x":0,"y":0,"w":420,"h":260,"anchored":false},
      "config": {
        "nodes":  {"<local>": {"type","params"}},   # x/y panel-relative
        "edges":  [{"from","to","to_port?","from_port?"}],
        "panels": ["<child-stem>", ...],            # references by stem
        "groups": [{"id","label","color","nodes":[...]}]
      }
    }

``nodes`` are the panel's *direct* nodes; descendants live in their own
files.  Edge endpoints inside this panel's subtree are written relative
to it (``eq::a``).  Placement of a child panel lives in the child file,
not the parent (child owns its placement).
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

PANEL_SUFFIX = ".json"
NAMESPACE_SEP = "::"
TYPE_PANEL = "panel"

MODE_RW = "read-write"
MODE_RO = "read-only"

ROOT_ID = ""
DEFAULT_COLOR = "#3584e4"
DEFAULT_W = 420.0
DEFAULT_H = 260.0
MIN_W = 140.0
MIN_H = 90.0


# ---------------------------------------------------------------------------
# namespacing
# ---------------------------------------------------------------------------


def file_stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def make_id(panel_id: str, local: str) -> str:
    """The fully-qualified id of ``local`` inside ``panel_id``."""
    if not panel_id:
        return local
    return f"{panel_id}{NAMESPACE_SEP}{local}"


def split_id(qualified: str) -> Tuple[str, str]:
    """(panel_id, local_id).  Splits on the *last* ``::`` so nested
    panels resolve to the deepest one; a bare id belongs to the root."""
    if NAMESPACE_SEP in qualified:
        panel_id, _, local = qualified.rpartition(NAMESPACE_SEP)
        return panel_id, local
    return ROOT_ID, qualified


def panel_of(qualified: str) -> str:
    """The id of the panel a node/group lives in."""
    return split_id(qualified)[0]


def local_of(qualified: str) -> str:
    return split_id(qualified)[1]


def parent_panel(panel_id: str) -> Optional[str]:
    """The id of a panel's parent (root has parent ``None``)."""
    if panel_id == ROOT_ID:
        return None
    parent, _, _ = panel_id.rpartition(NAMESPACE_SEP)
    return parent or ROOT_ID


def lca(a: str, b: str) -> str:
    """Least common ancestor of two panel ids (deepest common panel)."""
    if a == b:
        return a
    pa = a.split(NAMESPACE_SEP) if a else []
    pb = b.split(NAMESPACE_SEP) if b else []
    common: List[str] = []
    for x, y in zip(pa, pb):
        if x != y:
            break
        common.append(x)
    return NAMESPACE_SEP.join(common)


def edge_owner(a_qualified: str, b_qualified: str) -> str:
    """The panel that owns an edge between two fully-qualified ids."""
    return lca(panel_of(a_qualified), panel_of(b_qualified))


def relative_id(panel_id: str, qualified: str) -> str:
    """``qualified`` expressed relative to ``panel_id`` (a descendant)."""
    if panel_id == ROOT_ID:
        return qualified
    prefix = panel_id + NAMESPACE_SEP
    return qualified[len(prefix):] if qualified.startswith(prefix) else qualified


def is_descendant(panel_id: str, ancestor: str) -> bool:
    if ancestor == ROOT_ID:
        return True
    return panel_id == ancestor or panel_id.startswith(ancestor + NAMESPACE_SEP)


def child_name(entry) -> str:
    """A child reference's placement local name (str is legacy shorthand:
    the name and the file stem are the same)."""
    if isinstance(entry, str):
        return entry
    return str(entry.get("name") or entry.get("stem") or "")


def child_stem(entry) -> str:
    """The panel file stem a child reference points at."""
    if isinstance(entry, str):
        return entry
    return str(entry.get("stem") or entry.get("name") or "")


def child_ref(name: str, stem: str, placement: Optional[dict] = None):
    """A child reference.  Placements of the same file each carry their own
    geometry here (in the *parent's* file, since the panel file itself is
    shared); ``name == stem`` with no placement stays a bare string."""
    if placement is None and name == stem:
        return name
    ref = {"name": name, "stem": stem}
    if isinstance(placement, dict):
        ref["placement"] = dict(placement)
    return ref


def child_placement(entry) -> Optional[dict]:
    """Per-placement geometry stored on a child reference, or None."""
    if isinstance(entry, dict):
        p = entry.get("placement")
        if isinstance(p, dict):
            return p
    return None


def apply_placement(panel: "Panel", placement: dict) -> None:
    if not isinstance(placement, dict):
        return
    if placement.get("x") is not None:
        panel.x = float(placement["x"])
    if placement.get("y") is not None:
        panel.y = float(placement["y"])
    if placement.get("w") is not None:
        panel.w = float(placement["w"])
    if placement.get("h") is not None:
        panel.h = float(placement["h"])
    if placement.get("anchored") is not None:
        panel.anchored = bool(placement["anchored"])


# ---------------------------------------------------------------------------
# panel model
# ---------------------------------------------------------------------------


@dataclass
class Panel:
    id: str
    parent: Optional[str]
    label: str
    color: str
    mode: str = MODE_RW
    x: float = 0.0
    y: float = 0.0
    w: float = DEFAULT_W
    h: float = DEFAULT_H
    anchored: bool = False
    path: Optional[str] = None
    writable: bool = False
    # The file stem this placement references (placements of the same file
    # share it).  ``None`` for the root.
    stem: Optional[str] = None
    # Load automatically at start-up?  A panel with auto_load False is only
    # instantiated when referenced as a child (or placed by hand).
    auto_load: bool = False
    config: dict = field(default_factory=dict)

    @property
    def is_root(self) -> bool:
        return self.id == ROOT_ID

    @property
    def is_readonly(self) -> bool:
        return self.mode == MODE_RO

    @property
    def child_entries(self) -> List:
        """Raw child references (a stem string, or {name, stem})."""
        return list(self.config.get("panels") or [])

    @property
    def children(self) -> List[str]:
        """Local placement names of this panel's child placements."""
        return [child_name(e) for e in self.child_entries]

    def child_stems(self) -> Dict[str, str]:
        """{placement local name: file stem} for the children."""
        return {child_name(e): child_stem(e) for e in self.child_entries}

    @property
    def nodes(self) -> Dict[str, dict]:
        return dict(self.config.get("nodes") or {})

    @property
    def edges(self) -> List[dict]:
        return list(self.config.get("edges") or [])

    @property
    def groups(self) -> List[dict]:
        return list(self.config.get("groups") or [])

    def child_ids(self) -> List[str]:
        return [make_id(self.id, stem) for stem in self.children]

    def placement(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "w": self.w,
            "h": self.h,
            "anchored": self.anchored,
        }


# ---------------------------------------------------------------------------
# file IO
# ---------------------------------------------------------------------------


def read_file(path: str) -> Optional[dict]:
    try:
        with open(path) as f:
            data = json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Panel file %r could not be read: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def list_files(directory: Optional[str]) -> List[str]:
    if not directory or not os.path.isdir(directory):
        return []
    return sorted(
        os.path.join(directory, name)
        for name in os.listdir(directory)
        if name.endswith(PANEL_SUFFIX) and not name.endswith(".tmp")
    )


def snapshot(directories) -> Dict[str, float]:
    """{path: mtime} over the panel directories, for change detection."""
    state: Dict[str, float] = {}
    for directory in directories:
        for path in list_files(directory):
            try:
                state[path] = os.path.getmtime(path)
            except OSError:
                pass
    return state


def placement_from_raw(raw: dict) -> dict:
    p = raw.get("placement")
    if not isinstance(p, dict):
        p = {}
    return {
        "x": float(p.get("x", raw.get("x", 0.0)) or 0.0),
        "y": float(p.get("y", raw.get("y", 0.0)) or 0.0),
        "w": float(p.get("w", raw.get("w", DEFAULT_W)) or DEFAULT_W),
        "h": float(p.get("h", raw.get("h", DEFAULT_H)) or DEFAULT_H),
        "anchored": bool(p.get("anchored", raw.get("anchored", False))),
        "auto_load": bool(raw.get("auto_load", False)),
    }


def mode_from_raw(raw: dict) -> str:
    mode = raw.get("mode")
    if mode in (MODE_RW, MODE_RO):
        return mode
    # Legacy declarative shape: a read-only flag, else read-write.
    if raw.get("readonly"):
        return MODE_RO
    return MODE_RW


def config_from_raw(raw: dict) -> dict:
    inner = raw.get("config")
    config = inner if isinstance(inner, dict) else raw
    return {
        "nodes": dict(config.get("nodes") or {}),
        "edges": list(config.get("edges") or []),
        "panels": list(config.get("panels") or []),
        "groups": list(config.get("groups") or []),
    }


def build_payload(panel: Panel) -> dict:
    payload = {
        "type": TYPE_PANEL,
        "mode": panel.mode,
        "label": panel.label,
        "color": panel.color,
        "auto_load": panel.auto_load,
        "placement": panel.placement(),
        "config": {
            "nodes": panel.nodes,
            "edges": panel.edges,
        },
    }
    if panel.config.get("panels"):
        payload["config"]["panels"] = panel.child_entries
    if panel.config.get("groups"):
        payload["config"]["groups"] = panel.groups
    return payload


def write_file(path: str, panel: Panel) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(build_payload(panel), f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# loading a tree
# ---------------------------------------------------------------------------


def load_panel(path: str, panel_id: str, parent: Optional[str],
               writable: bool, stem: Optional[str] = None) -> Panel:
    raw = read_file(path) or {}
    placement = placement_from_raw(raw)
    if not panel_id and stem is None:
        stem = None
    elif stem is None:
        stem = file_stem(path)
    return Panel(
        id=panel_id,
        parent=parent,
        label=raw.get("label") or (file_stem(path) if not panel_id else panel_id),
        color=raw.get("color") or DEFAULT_COLOR,
        mode=mode_from_raw(raw),
        path=path,
        writable=writable,
        stem=stem,
        config=config_from_raw(raw),
        **placement,
    )


def load_tree(root_path: str, directories: List[str],
              dir_writable: Optional[Dict[str, bool]] = None,
              _seen: Optional[set] = None) -> Dict[str, Panel]:
    """Load the root panel file and every panel reachable from it.

    ``directories`` is searched in order for a child stem (later wins);
    ``dir_writable`` maps a directory to whether files there may be
    written (a Nix store dir cannot).  Returns ``{panel_id: Panel}`` with
    the root under id ``""``.
    """
    dir_writable = dir_writable or {}
    panels: Dict[str, Panel] = {}
    seen = _seen if _seen is not None else set()

    root = Panel(
        id=ROOT_ID,
        parent=None,
        label="root",
        color=DEFAULT_COLOR,
        mode=MODE_RW,
        path=root_path,
        writable=True,
        config={"nodes": {}, "edges": [], "panels": [], "groups": []},
    )
    if root_path and os.path.isfile(root_path):
        root = load_panel(root_path, ROOT_ID, None, True)
        root.path = root_path

    def _load(panel: Panel) -> None:
        if panel.id in seen:
            return
        seen.add(panel.id)
        panels[panel.id] = panel
        for entry in panel.child_entries:
            name = child_name(entry)
            stem = child_stem(entry)
            if not name or not stem:
                continue
            child_path = None
            child_writable = False
            for directory in directories:
                if not directory:
                    continue
                candidate = os.path.join(directory, stem + PANEL_SUFFIX)
                if os.path.isfile(candidate):
                    child_path = candidate
                    child_writable = bool(dir_writable.get(directory, True))
            if child_path is None:
                logger.warning(
                    "Panel %r references unknown child %r (stem %r)",
                    panel.id or "root", name, stem,
                )
                continue
            child_id = make_id(panel.id, name)
            child = load_panel(
                child_path, child_id, panel.id, child_writable, stem=stem
            )
            apply_placement(child, child_placement(entry) or {})
            _load(child)

    _load(root)
    return panels


# ---------------------------------------------------------------------------
# migration from the legacy shapes
# ---------------------------------------------------------------------------


def migrate_legacy_session(raw: dict) -> Panel:
    """The old autosave ``{nodes,edges,groups}`` -> a root panel."""
    return Panel(
        id=ROOT_ID,
        parent=None,
        label=raw.get("label") or "root",
        color=raw.get("color") or DEFAULT_COLOR,
        mode=MODE_RW,
        writable=True,
        config={
            "nodes": dict(raw.get("nodes") or {}),
            "edges": list(raw.get("edges") or []),
            "panels": list(raw.get("panels") or []),
            "groups": list(raw.get("groups") or []),
        },
    )


def migrate_declarative_file(stem: str, raw: dict) -> Panel:
    """A declarative file -> a top-level panel file payload."""
    config = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    placement = placement_from_raw(raw)
    return Panel(
        id=stem,
        parent=ROOT_ID,
        label=raw.get("label") or stem,
        color=raw.get("color") or DEFAULT_COLOR,
        mode=mode_from_raw(raw),
        writable=False,
        config={
            "nodes": dict((config or {}).get("nodes") or {}),
            "edges": list((config or {}).get("edges") or []),
            "panels": [],
            "groups": list((config or {}).get("groups") or []),
        },
        **placement,
    )
