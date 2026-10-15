"""
spaces.py

First-class space containers: the file-backed, nestable boxes the graph
lives in.

A *space* is a rectangular container on the canvas that owns nodes and
child spaces.  The whole graph has a single ``root`` space; every
non-root space is one file, and child spaces are referenced by their
file stem.  Nodes and spaces are laid out in their parent's coordinate
frame (node ``x``/``y`` are space-relative, a space's ``x``/``y`` are
parent-relative); an absolute position is the fold of the ancestor
offsets.

Naming
------
A space's id is its path of stems from the root, joined by ``::``: a
top-level space ``kit`` is ``kit``, its child ``eq`` is ``kit::eq``.
The root's id is the empty string.  A node/group id is
``<space-id>::<local>`` (bare ``<local>`` at the root).  Local ids may
not contain ``::``; ``rsplit`` splits a fully-qualified id back apart.

Edges
-----
An edge is *owned* by the deepest space that contains both of its
endpoints: the least common ancestor of their spaces.  A space file
therefore stores exactly the edges whose LCA is that space - internal
ones in the leaf space, cross-space ones in the common ancestor, and
edges between different top-level spaces in the root file.  An edge is
never stored twice.

File shape
----------
::

    {
      "type": "space",
      "mode": "read-write" | "read-only",
      "label": "...", "color": "#rrggbb",
      "placement": {"x":0,"y":0,"w":420,"h":260,"anchored":false},
      "config": {
        "nodes":  {"<local>": {"type","params"}},   # x/y space-relative
        "edges":  [{"from","to","to_port?","from_port?"}],
        "spaces": ["<child-stem>", ...],            # references by stem
        "groups": [{"id","label","color","nodes":[...]}]
      }
    }

``nodes`` are the space's *direct* nodes; descendants live in their own
files.  Edge endpoints inside this space's subtree are written relative
to it (``eq::a``).  Placement of a child space lives in the child file,
not the parent (child owns its placement).

Legacy spellings (S0, `panel` -> `space`, one-release window)
-------------------------------------------------------------
Everything old keeps loading; everything new is written new.  Two keys of
the old spelling are still *read*, never written:

* the file's ``"type": "panel"`` marker (``LEGACY_TYPE_SPACE``): a space
  file written before the rename is a space file, not a legacy session -
  the daemon must not "migrate" it away (see ``is_space_type``);
* the child-reference list ``config.panels`` (``_child_entries``), read
  when ``config.spaces`` is absent.

``build_payload`` emits only the new spelling, so the first save rewrites
an old file as a new one.  The node type ids that used to spell a space
port (`panel_in`/`panel_out`/`bool_panel_in`/`bool_panel_out`) are read
through the module's ``legacy_aliases`` (catalog.json), not here.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

SPACE_SUFFIX = ".json"
NAMESPACE_SEP = "::"
TYPE_SPACE = "space"
#: The pre-rename spelling of the file's ``type`` marker.  Read, never
#: written (S0 migration; see the module docstring).
LEGACY_TYPE_SPACE = "panel"
#: The pre-rename spelling of a file's child-reference list.  Read, never
#: written.
LEGACY_CHILD_KEY = "panels"

MODE_RW = "read-write"
MODE_RO = "read-only"

ROOT_ID = ""
DEFAULT_COLOR = "#3584e4"
#: Horizontal gap left between an auto-placed space and the spaces it is
#: placed beside (see main._place_unplaced_spaces).
PLACE_GAP = 80.0

DEFAULT_W = 420.0
DEFAULT_H = 260.0
MIN_W = 140.0
MIN_H = 90.0


# ---------------------------------------------------------------------------
# namespacing
# ---------------------------------------------------------------------------


def file_stem(path: str) -> str:
    return os.path.splitext(os.path.basename(path))[0]


def make_id(space_id: str, local: str) -> str:
    """The fully-qualified id of ``local`` inside ``space_id``."""
    if not space_id:
        return local
    return f"{space_id}{NAMESPACE_SEP}{local}"


def split_id(qualified: str) -> Tuple[str, str]:
    """(space_id, local_id).  Splits on the *last* ``::`` so nested
    spaces resolve to the deepest one; a bare id belongs to the root."""
    if NAMESPACE_SEP in qualified:
        space_id, _, local = qualified.rpartition(NAMESPACE_SEP)
        return space_id, local
    return ROOT_ID, qualified


def space_of(qualified: str) -> str:
    """The id of the space a node/group lives in."""
    return split_id(qualified)[0]


def local_of(qualified: str) -> str:
    return split_id(qualified)[1]


def parent_space(space_id: str) -> Optional[str]:
    """The id of a space's parent (root has parent ``None``)."""
    if space_id == ROOT_ID:
        return None
    parent, _, _ = space_id.rpartition(NAMESPACE_SEP)
    return parent or ROOT_ID


def lca(a: str, b: str) -> str:
    """Least common ancestor of two space ids (deepest common space)."""
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
    """The space that owns an edge between two fully-qualified ids."""
    return lca(space_of(a_qualified), space_of(b_qualified))


def relative_id(space_id: str, qualified: str) -> str:
    """``qualified`` expressed relative to ``space_id`` (a descendant)."""
    if space_id == ROOT_ID:
        return qualified
    prefix = space_id + NAMESPACE_SEP
    return qualified[len(prefix):] if qualified.startswith(prefix) else qualified


def is_descendant(space_id: str, ancestor: str) -> bool:
    if ancestor == ROOT_ID:
        return True
    return space_id == ancestor or space_id.startswith(ancestor + NAMESPACE_SEP)


def child_name(entry) -> str:
    """A child reference's placement local name (str is legacy shorthand:
    the name and the file stem are the same)."""
    if isinstance(entry, str):
        return entry
    return str(entry.get("name") or entry.get("stem") or "")


def child_stem(entry) -> str:
    """The space file stem a child reference points at."""
    if isinstance(entry, str):
        return entry
    return str(entry.get("stem") or entry.get("name") or "")


def child_ref(name: str, stem: str, placement: Optional[dict] = None):
    """A child reference.  Placements of the same file each carry their own
    geometry here (in the *parent's* file, since the space file itself is
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


def apply_placement(space: "Space", placement: dict) -> None:
    if not isinstance(placement, dict):
        return
    if placement.get("x") is not None or placement.get("y") is not None:
        # A geometry on the child reference is a placement like any other:
        # it is the user's drag, remembered in the parent's file.
        space.placed = True
    if placement.get("x") is not None:
        space.x = float(placement["x"])
    if placement.get("y") is not None:
        space.y = float(placement["y"])
    if placement.get("w") is not None:
        space.w = float(placement["w"])
    if placement.get("h") is not None:
        space.h = float(placement["h"])
    if placement.get("anchored") is not None:
        space.anchored = bool(placement["anchored"])


# ---------------------------------------------------------------------------
# space model
# ---------------------------------------------------------------------------


@dataclass
class Space:
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
    # Load automatically at start-up?  A space with auto_load False is only
    # instantiated when referenced as a child (or placed by hand).
    auto_load: bool = False
    # Does x/y come from an explicit placement (the space file, or the child
    # reference in the parent)?  A space with no coordinates at all is
    # *unplaced*: the daemon drops it in beside the spaces that are placed
    # instead of leaving it at the origin - see main._place_unplaced_spaces.
    placed: bool = False
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
        return list(self.config.get("spaces") or [])

    @property
    def children(self) -> List[str]:
        """Local placement names of this space's child placements."""
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
# legacy spellings (S0: panel -> space)
# ---------------------------------------------------------------------------


def is_space_type(raw: dict) -> bool:
    """Is `raw` a space file, under either spelling of the ``type`` marker?

    A bare session cache (or an old flat declarative file) carries no
    marker, or a non-space one, and is migrated; a pre-rename space file
    says ``"panel"`` and must *not* be mistaken for one."""
    return raw.get("type") in (TYPE_SPACE, LEGACY_TYPE_SPACE)


def raw_child_entries(config: dict) -> list:
    """A raw config's child-reference list, under either spelling.

    ``spaces`` wins when both are present (that is the spelling written);
    the old ``panels`` key is read for a file that predates the rename.
    """
    if "spaces" in config:
        return list(config.get("spaces") or [])
    return list(config.get(LEGACY_CHILD_KEY) or [])


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
        logger.warning("Space file %r could not be read: %s", path, exc)
        return None
    return data if isinstance(data, dict) else None


def list_files(directory: Optional[str]) -> List[str]:
    """Every space file under `directory`, recursively.

    Space files may live in sub-folders (the side view groups them), so
    the scan walks the tree; hidden directories are skipped."""
    if not directory or not os.path.isdir(directory):
        return []
    found = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.endswith(SPACE_SUFFIX) and not name.endswith(".tmp"):
                found.append(os.path.join(root, name))
    return sorted(found)


def snapshot(directories) -> Dict[str, float]:
    """{path: mtime} over the space directories, for change detection."""
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
    # "placed" travels with the parsed placement (and is consumed by
    # load_space): it is what distinguishes "this space was put at the
    # origin on purpose" from "this space has never been positioned".
    explicit = any(
        source.get("x") is not None
        for source in (p, raw)
    ) or any(
        source.get("y") is not None
        for source in (p, raw)
    )
    return {
        "x": float(p.get("x", raw.get("x", 0.0)) or 0.0),
        "y": float(p.get("y", raw.get("y", 0.0)) or 0.0),
        "w": float(p.get("w", raw.get("w", DEFAULT_W)) or DEFAULT_W),
        "h": float(p.get("h", raw.get("h", DEFAULT_H)) or DEFAULT_H),
        "anchored": bool(p.get("anchored", raw.get("anchored", False))),
        "auto_load": bool(raw.get("auto_load", False)),
        "placed": explicit,
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
        "spaces": raw_child_entries(config),
        "groups": list(config.get("groups") or []),
    }


def build_payload(space: Space) -> dict:
    payload = {
        "type": TYPE_SPACE,
        "mode": space.mode,
        "label": space.label,
        "color": space.color,
        "auto_load": space.auto_load,
        "placement": space.placement(),
        "config": {
            "nodes": space.nodes,
            "edges": space.edges,
        },
    }
    if space.config.get("spaces"):
        payload["config"]["spaces"] = space.child_entries
    if space.config.get("groups"):
        payload["config"]["groups"] = space.groups
    return payload


def write_file(path: str, space: Space) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    # Keep the previous content beside the file before replacing it.  A
    # space file *is* the user's graph and every autosave overwrites it in
    # place: without this, one bad write - an interrupted reload, a bug -
    # is unrecoverable.  One previous version is enough to undo one step.
    # (The .bak never ends up in the space scan: it doesn't end in .json.)
    if os.path.exists(path):
        try:
            shutil.copy2(path, path + ".bak")
        except OSError as exc:
            logger.warning("Could not back up %r before writing: %s", path, exc)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(build_payload(space), f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# loading a tree
# ---------------------------------------------------------------------------


def load_space(path: str, space_id: str, parent: Optional[str],
               writable: bool, stem: Optional[str] = None) -> Space:
    raw = read_file(path) or {}
    placement = placement_from_raw(raw)
    placed = placement.pop("placed", False)
    if not space_id and stem is None:
        stem = None
    elif stem is None:
        stem = file_stem(path)
    return Space(
        id=space_id,
        parent=parent,
        label=raw.get("label") or (file_stem(path) if not space_id else space_id),
        color=raw.get("color") or DEFAULT_COLOR,
        mode=mode_from_raw(raw),
        path=path,
        writable=writable,
        stem=stem,
        config=config_from_raw(raw),
        placed=placed,
        **placement,
    )


def load_tree(root_path: str, directories: List[str],
              dir_writable: Optional[Dict[str, bool]] = None,
              _seen: Optional[set] = None) -> Dict[str, Space]:
    """Load the root space file and every space reachable from it.

    ``directories`` is searched in order for a child stem (later wins);
    ``dir_writable`` maps a directory to whether files there may be
    written (a Nix store dir cannot).  Returns ``{space_id: Space}`` with
    the root under id ``""``.
    """
    dir_writable = dir_writable or {}
    spaces: Dict[str, Space] = {}
    seen = _seen if _seen is not None else set()

    root = Space(
        id=ROOT_ID,
        parent=None,
        label="root",
        color=DEFAULT_COLOR,
        mode=MODE_RW,
        path=root_path,
        writable=True,
        config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    )
    if root_path and os.path.isfile(root_path):
        root = load_space(root_path, ROOT_ID, None, True)
        root.path = root_path

    def _load(space: Space) -> None:
        if space.id in seen:
            return
        seen.add(space.id)
        spaces[space.id] = space
        for entry in space.child_entries:
            name = child_name(entry)
            stem = child_stem(entry)
            if not name or not stem:
                continue
            child_path = None
            child_writable = False
            for directory in directories:
                if not directory:
                    continue
                candidate = os.path.join(directory, stem + SPACE_SUFFIX)
                if os.path.isfile(candidate):
                    child_path = candidate
                    child_writable = bool(dir_writable.get(directory, True))
            if child_path is None:
                logger.warning(
                    "Space %r references unknown child %r (stem %r)",
                    space.id or "root", name, stem,
                )
                continue
            child_id = make_id(space.id, name)
            child = load_space(
                child_path, child_id, space.id, child_writable, stem=stem
            )
            apply_placement(child, child_placement(entry) or {})
            _load(child)

    _load(root)
    return spaces


# ---------------------------------------------------------------------------
# migration from the legacy shapes
# ---------------------------------------------------------------------------


def migrate_legacy_session(raw: dict) -> Space:
    """The old autosave ``{nodes,edges,groups}`` -> a root space."""
    return Space(
        id=ROOT_ID,
        parent=None,
        label=raw.get("label") or "root",
        color=raw.get("color") or DEFAULT_COLOR,
        mode=MODE_RW,
        writable=True,
        config={
            "nodes": dict(raw.get("nodes") or {}),
            "edges": list(raw.get("edges") or []),
            "spaces": raw_child_entries(raw),
            "groups": list(raw.get("groups") or []),
        },
    )


def migrate_declarative_file(stem: str, raw: dict) -> Space:
    """A declarative file -> a top-level space file payload."""
    config = raw.get("config") if isinstance(raw.get("config"), dict) else raw
    placement = placement_from_raw(raw)
    return Space(
        id=stem,
        parent=ROOT_ID,
        label=raw.get("label") or stem,
        color=raw.get("color") or DEFAULT_COLOR,
        mode=mode_from_raw(raw),
        writable=False,
        config={
            "nodes": dict((config or {}).get("nodes") or {}),
            "edges": list((config or {}).get("edges") or []),
            "spaces": [],
            "groups": list((config or {}).get("groups") or []),
        },
        **placement,
    )
