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
      "type": "live" | "template",
      "permanent": true | false,
      "node_view": false,
      "template": "<space id>"?          # the template an instance came from
      "mode": "read-write" | "read-only",
      "label": "...", "color": "@blue" | "#rrggbb" | "",
      "placement": {"x":0,"y":0,"w":420,"h":260,"anchored":false},
      "config": {
        "nodes":  {"<local>": {"type","params"}},   # x/y space-relative
        "edges":  [{"from","to","to_port?","from_port?"}],
        "spaces": ["<child-stem>", ...],            # references by stem
        "portals": [{"space_id","view","x","y"}],   # spaces this one embeds
        "groups": [{"id","label","color","nodes":[...]}]
      }
    }

``nodes`` are the space's *direct* nodes; descendants live in their own
files.  Edge endpoints inside this space's subtree are written relative
to it (``eq::a``).  Placement of a child space lives in the child file,
not the parent (child owns its placement).

``portals`` are the spaces this space shows *by reference* (agents/SPACES.md
§13): the target keeps its own file, nodes and diffs, and is never written
here - only the placement (``x``/``y`` in this space's frame, and the
per-embedding ``view``).  The reverse list, ``embedded_in``, is derived from
every space's ``portals`` (``portal_index``), never stored: one source of
truth, and a non-permanent target has no file to store it in anyway.

Legacy spellings (S0, `panel` -> `space`, one-release window)
-------------------------------------------------------------
Everything old keeps loading; everything new is written new.  Two keys of
the old spelling are still *read*, never written:

* the file's ``"type": "panel"`` marker (``LEGACY_TYPE_SPACE``): a space
  file written before the rename is a space file, not a legacy session -
  the daemon must not "migrate" it away (see ``is_space_type``);
* the child-reference list ``config.panels`` (``_child_entries``), read
  when ``config.spaces`` is absent.
* the file's old ``"type": "space"`` marker, which S1 repurposed: the key
  now carries the space's *kind* (``live``/``template``), so both old
  spellings read as a live space (``type_from_raw``).

``build_payload`` emits only the new spelling, so the first save rewrites
an old file as a new one.  The node type ids that used to spell a space
port (`panel_in`/`panel_out`/`bool_panel_in`/`bool_panel_out`) are read
through the module's ``legacy_aliases`` (catalog.json), not here.

App-declared spaces (S8, agents/SPACES.md §18)
----------------------------------------------
A space a *module or app* declares is its own: ``declared_by`` names the
declaring client, ``permanent`` is forced off (only the user can make a
space persist, by turning Permanent on in the GUI), and none of it is ever
written anywhere.  An app declares on every attach — that is exactly how its
space survives updates and reboots with nothing persisted — and a node it
announces into that space lands there.  ``declared_by`` is a *live claim*,
never file data: it is not in ``build_payload`` and is cleared when the
declaring client's connection goes (``release_declared``), so it cannot
outlive the process that made it.  ``declare_space`` is the whole rule
(create, update, and the four refusals); ``announce_space`` is what an
announcement does with a space id it has never seen.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Tuple

from central import module_api as api

logger = logging.getLogger(__name__)

SPACE_SUFFIX = ".json"
NAMESPACE_SEP = "::"
#: A file's ``type`` key says what *kind* of space the file defines
#: (``LIVE`` / ``TEMPLATE``).  ``TYPE_SPACE`` is what that key said before S1
#: and ``LEGACY_TYPE_SPACE`` before the S0 rename: both are still *read* as a
#: live space, because a file on disk is data (agents/SPACES.md §12) - only a
#: bare session cache, which carries no marker at all, is migrated.
TYPE_SPACE = "space"
LEGACY_TYPE_SPACE = "panel"

LIVE = "live"
TEMPLATE = "template"
SPACE_TYPES = (LIVE, TEMPLATE)

#: The pre-rename spelling of a file's child-reference list.  Read, never
#: written.
LEGACY_CHILD_KEY = "panels"

MODE_RW = "read-write"
MODE_RO = "read-only"

# --- a face input's configuration (agents/SPACES.md §5, §12) --------------
#
# Stored on the ``space_in`` node itself (one source of truth) and *derived*
# into the space record's ``face`` by the daemon.
FACE_MODE_NODE = "node"
FACE_MODE_VALUE = "value"
FACE_MODES = (FACE_MODE_NODE, FACE_MODE_VALUE)

FACE_KIND_AUDIO = "audio"
#: A face input's kind: an ordinary jack (`audio`) or a value.  The value
#: half is **the** value vocabulary (`central.module_api.VALUE_KINDS`), read
#: rather than respelled (agents/SPACES.md §19): a face is one more place a
#: value port lives, so it cannot drift from the kinds the relay and the
#: connection rules are written against.
FACE_KINDS = (FACE_KIND_AUDIO,) + api.VALUE_KINDS

FACE_METHOD_FIELD = "field"
FACE_METHOD_EDGE = "edge"
FACE_METHODS = (FACE_METHOD_FIELD, FACE_METHOD_EDGE)

# --- a portal: a placement of a live space inside another (SPACES.md §13) --
#
# A portal is a *reference*, never a copy: the embedded space keeps its own
# nodes, module instances and diffs, and the embedding contributes only a
# position and a view mode.  It is stored in the embedding space's own record
# (`config.portals`), so the portal is part of the parent's file and the
# target's file stays exactly as it was - see `portal_entry`.
PORTAL_KEY = "portals"

#: The portal's view mode: expanded in place, or collapsed to a node-faced
#: box whose tooltip shows the interior.  A pure view flag, per embedding;
#: the space's `node_view` is only the default a *new* embedding starts from.
VIEW_INSIDE = "inside"
VIEW_NODE = "node"
VIEWS = (VIEW_INSIDE, VIEW_NODE)

ROOT_ID = ""
#: The name of the default *routing region*: `graph.Graph`'s own pre-container
#: space, and what a module that names no space of its own announces with
#: (`modules/pipewire`).  It is not a container anybody declared — a node that
#: names only this, or names nothing, belongs to the **root** (see
#: `node_space`), which is what keeps every pre-S1 announcement where it was.
ROUTING_DEFAULT = "main"
#: A space with no color of its own.  The client derives one from the space's
#: own name out of the theme's class-color vocabulary (agents/SPACES.md §11);
#: the daemon stores the name it is given, never a resolved color.
DEFAULT_COLOR = ""
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
    #: ``LIVE`` (spawned and running) or ``TEMPLATE`` (a definition whose
    #: interior is never spawned until it is instantiated; §2).
    type: str = LIVE
    #: Whether this space is written to disk *at all* - session, space file,
    #: placement diffs - and whether anything inside it is (§3).  True for the
    #: root and for spaces a user made; False for anything a module/app made,
    #: so nothing of it accumulates across updates.
    permanent: bool = True
    #: The *default* view this space takes where it is embedded (the GUI's
    #: "Node" checkbox).  The embedding's own toggle overrides it.
    node_view: bool = False
    #: The id of the template this live space was instantiated from.
    template: Optional[str] = None
    #: The client id of the app that *declared* this space (agents/SPACES.md
    #: §18), "" when nobody did.  A live claim, never file data: a declared
    #: space is non-permanent and writes no file, and a claim belongs to a
    #: connection (``release_declared`` clears it when that connection goes),
    #: so a space that came back from disk is ownerless until some client
    #: declares it again.
    declared_by: str = ""

    @property
    def is_root(self) -> bool:
        return self.id == ROOT_ID

    @property
    def is_readonly(self) -> bool:
        return self.mode == MODE_RO

    @property
    def is_template(self) -> bool:
        return self.type == TEMPLATE

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

    @property
    def portals(self) -> List[dict]:
        """The spaces this space embeds, by reference (§13): a list of
        ``{space_id, view, x, y}``, normalized."""
        return portals_from_raw(self.config)

    @property
    def portal_ids(self) -> List[str]:
        """The ids of the spaces this space portals."""
        return [portal["space_id"] for portal in self.portals]

    def portal(self, space_id: str) -> Optional[dict]:
        """This space's portal entry naming ``space_id``, or None."""
        for portal in self.portals:
            if portal["space_id"] == space_id:
                return portal
        return None

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
    """Is `raw` a space file, under any spelling of the ``type`` marker?

    A bare session cache (or an old flat declarative file) carries no
    marker, or a non-space one, and is migrated.  Every spelling of a space
    file must answer yes: ``live``/``template`` (what S1 writes) and the
    older ``space``/``panel`` markers - a pre-S1 root mistaken for a legacy
    session would be rewritten as an empty one and the graph would be gone."""
    return raw.get("type") in (TYPE_SPACE, LEGACY_TYPE_SPACE, LIVE, TEMPLATE)


def raw_child_entries(config: dict) -> list:
    """A raw config's child-reference list, under either spelling.

    ``spaces`` wins when both are present (that is the spelling written);
    the old ``panels`` key is read for a file that predates the rename.
    """
    if "spaces" in config:
        return list(config.get("spaces") or [])
    return list(config.get(LEGACY_CHILD_KEY) or [])


def type_from_raw(raw: dict) -> str:
    """A file's space *kind*.  Anything but an explicit ``template`` is a
    live space: the pre-S1 markers (``space``/``panel``) and a file with no
    marker at all (a migrated session/declarative file) are all live."""
    return TEMPLATE if raw.get("type") == TEMPLATE else LIVE


def face_input_problem(mode: str, kind: str, method: str) -> Optional[str]:
    """Why this face-input configuration is not allowed, or None.

    The rule (agents/SPACES.md §5): a face input is either an ordinary jack
    (``mode == "node"``) or an externalized value (``mode == "value"``),
    whose declared ``kind`` says what it carries.  Audio is node-only - a
    media stream is not a value - so it admits neither a ``value`` mode nor
    a ``field``/``edge`` method; every other kind may be either."""
    if mode not in FACE_MODES:
        return f"mode must be one of {', '.join(FACE_MODES)}"
    if kind not in FACE_KINDS:
        return f"kind must be one of {', '.join(FACE_KINDS)}"
    if method not in ("",) + FACE_METHODS:
        return f"method must be one of {', '.join(FACE_METHODS)}"
    if kind == FACE_KIND_AUDIO:
        if mode != FACE_MODE_NODE:
            return "kind 'audio' is node-only: it cannot be an externalized value"
        if method:
            return f"kind 'audio' is node-only: it takes no {method!r} method"
    return None


# ---------------------------------------------------------------------------
# portals (agents/SPACES.md §13)
# ---------------------------------------------------------------------------
#
# A portal is a placement of a live space inside another space, **by
# reference**: there is never a second interior.  The embedded space keeps its
# own nodes, module instances and diffs; the embedding contributes a position
# (`x`/`y`, in the *parent's* frame) and a view mode, and is stored in the
# parent's own record - so the portal is saved with the parent, and the target
# is saved (or not) exactly as it would have been without the portal.
#
# The target side, `embedded_in`, is *derived* from every space's portal list
# rather than stored: the parent's entry is the one source of truth, so the
# two sides can never disagree, a removal is consistent by construction, and
# a non-permanent target - which has no file to write - still reports where it
# is being shown.


def _space_label(space_id: str) -> str:
    """A space named the way a message should read (the root has no id)."""
    return "the root space" if space_id == ROOT_ID else f"space {space_id!r}"


def portal_entry(space_id: str, view: str = VIEW_INSIDE,
                 x: float = 0.0, y: float = 0.0) -> dict:
    """One portal: the space it shows, its view mode and its position."""
    return {
        "space_id": str(space_id),
        "view": view if view in VIEWS else VIEW_INSIDE,
        "x": float(x or 0.0),
        "y": float(y or 0.0),
    }


def portal_from_raw(entry) -> Optional[dict]:
    """A raw portal entry, normalized, or None when it names no space.

    A missing ``view`` reads as ``inside``: the space's ``node_view`` is only
    the default a *new* embedding is *created* with (``embed_space``), never
    something a stored entry is re-interpreted against - the portal's own view
    wins once set (agents/SPACES.md §13)."""
    if not isinstance(entry, dict):
        return None
    target = entry.get("space_id")
    if target is None:
        return None
    return portal_entry(
        str(target), entry.get("view") or VIEW_INSIDE,
        entry.get("x") or 0.0, entry.get("y") or 0.0,
    )


def portals_from_raw(config: dict) -> List[dict]:
    """A raw space config's portal list, normalized."""
    found = []
    for entry in config.get(PORTAL_KEY) or []:
        portal = portal_from_raw(entry)
        if portal is not None:
            found.append(portal)
    return found


def portal_index(tree: Dict[str, "Space"]) -> Dict[str, List[str]]:
    """``{space id: [ids of the spaces that portal it]}`` - ``embedded_in``
    for the whole tree, in one pass.

    Every space's portal list counts, templates included: a template's portals
    are part of its *definition* (they materialize when it is instantiated),
    and the client has the parent's record, so it can see for itself which of
    the entries are a place the space is really shown in right now."""
    index: Dict[str, List[str]] = {}
    for pid, space in tree.items():
        for portal in space.portals:
            target = portal["space_id"]
            if target == pid:
                continue    # never: no space portals itself
            index.setdefault(target, []).append(pid)
    return index


def embedded_in(space_id: str, tree: Dict[str, "Space"]) -> List[str]:
    """The spaces that portal ``space_id`` (its ``embedded_in``)."""
    return list(portal_index(tree).get(space_id, []))


def drop_portals_to(tree: Dict[str, "Space"], doomed) -> List[Tuple[str, str]]:
    """Remove every portal that names a space in ``doomed``, returning the
    ``(parent space id, space id)`` pairs dropped.

    Deleting a space deletes the portals that pointed at it: a portal is a
    placement of a *live* space, and with the target gone the entry would be a
    placement of nothing - a dangling portal must not survive, and neither may
    its ``embedded_in`` entry.  Only the entries naming a doomed space go; the
    other spaces the same parent portals are untouched."""
    doomed = set(doomed)
    dropped: List[Tuple[str, str]] = []
    for pid, space in tree.items():
        if pid in doomed:
            continue
        entries = space.portals
        kept = [e for e in entries if e["space_id"] not in doomed]
        if len(kept) == len(entries):
            continue
        dropped.extend(
            (pid, e["space_id"]) for e in entries if e["space_id"] in doomed
        )
        space.config[PORTAL_KEY] = kept
    return dropped


def retarget_portals(tree: Dict[str, "Space"], id_map: Dict[str, str]) -> int:
    """Follow the spaces a portal names through a re-keying (a space moved
    under a new parent changes id), returning how many entries changed.

    Without this a move would leave every portal aimed at the path the space
    used to live at - a dangling portal that says nothing about a space that
    exists."""
    changed = 0
    for space in tree.values():
        entries = space.config.get(PORTAL_KEY)
        if not entries:
            continue
        rewrote: List[dict] = []
        local = 0
        for entry in entries:
            portal = portal_from_raw(entry)
            if portal is None:
                continue
            new = id_map.get(portal["space_id"])
            if new is not None and new != portal["space_id"]:
                portal["space_id"] = new
                local += 1
            rewrote.append(portal)
        if local:
            space.config[PORTAL_KEY] = rewrote
            changed += local
    return changed


def readonly_portal_problem(space_id: str) -> str:
    """The refusal for changing a *read-only* space's portals.

    A declarative (Nix-generated) space's interior is not editable - its
    portals are written in the file that declares it - so an embedding change
    there would be silently dropped on the next reload instead of persisted.
    Refusing it says so."""
    return (
        f"space {space_id!r} is read-only: its portals are declared by its "
        f"file and cannot be changed at runtime"
    )


def readonly_edit_problem(space_ids: List[str], what: str, action: str) -> str:
    """The refusal for editing one thing inside a *declarative* space.

    One sentence for the whole rule (agents/SPACES.md §20): the space (or
    spaces) that declare ``what`` first — ``space 'fx' is read-only`` — then
    what was refused and which change was refused, so every refusal names what
    it refused and why.  `readonly_portal_problem` says the same thing for a
    portal; this is the interior half, and the two are one rule.
    """
    unique = sorted(set(space_ids))
    if len(unique) == 1:
        subject = f"space {unique[0]!r} is read-only"
    else:
        subject = (
            "spaces " + " and ".join(repr(s) for s in unique) + " are read-only"
        )
    return (
        f"{subject}: {what} is declared by its file and cannot be "
        f"{action} at runtime"
    )


def readonly_node_problem(tree: Dict[str, "Space"], node_id: Any,
                          action: str, what: str = "") -> Optional[str]:
    """Why the node ``node_id`` may not be <action>, or None.

    A node inside a declarative (read-only) space is *declared*: that space's
    file is where it is written down, and the load/export/reload paths already
    treat everything in such a space that way (the reload re-applies it, the
    export never writes it back imperatively, the rebuild keeps the frozen
    snapshot) — so a runtime edit here is dropped on the next reload instead
    of persisted.  Refusing it says so, which is what §20's "a declarative
    live space is readonly and uneditable" means.

    The node's own space is the question (`space_of`), exactly the one the
    move/reparent refusal asks, so there is one notion of declared and never a
    second opinion about it.  ``what`` names the thing in the message when it
    is narrower than the node itself (a property, the node's position).
    """
    pid = space_of(str(node_id))
    space = tree.get(pid)
    if space is None or not space.is_readonly:
        return None
    return readonly_edit_problem([pid], what or f"node {node_id!r}", action)


def readonly_edge_problem(tree: Dict[str, "Space"], from_node: Any,
                          to_node: Any, action: str) -> Optional[str]:
    """Why an edge between two declared nodes may not be <action>, or None.

    Only an edge whose **both** endpoints are declared is refused: that is
    editing the declared wiring, and the file is the only thing that owns it.
    An edge that crosses the boundary — a declared node wired to one outside
    the space, either direction — is exactly "we should be able to plug things
    into it", and the reload path deliberately preserves those (they are
    imperative: no file declares them, so nothing re-applies or drops them).
    """
    declared: List[str] = []
    for endpoint in (from_node, to_node):
        pid = space_of(str(endpoint))
        space = tree.get(pid)
        if space is not None and space.is_readonly:
            declared.append(pid)
    if len(declared) < 2:
        return None
    return readonly_edit_problem(
        declared, f"the edge {from_node!r} -> {to_node!r}", action
    )


def portal_cycle(parent_id: str, target_id: str) -> bool:
    """Would a portal from ``parent_id`` to ``target_id`` be a cycle?

    A space may not be embedded in itself or in any of its own descendants -
    the id path makes both halves of the test cheap."""
    return target_id == parent_id or is_descendant(parent_id, target_id)


def move_cycle(tree: Dict[str, "Space"],
               id_map: Dict[str, str]) -> Optional[str]:
    """The first portal a re-keying would turn into a cycle, or None.

    Moving a space under a new parent changes the id path, and the id path is
    what says who contains whom: ``X`` showing ``T`` and then ``X`` being
    nested *into* ``T`` is the same self-referential tree ``embed_space``
    refuses, so the move is refused with it (the alternative - dropping the
    user's portal for them - hides the reason)."""
    for pid, space in tree.items():
        new_pid = id_map.get(pid, pid)
        for portal in space.portals:
            target = portal["space_id"]
            if portal_cycle(new_pid, id_map.get(target, target)):
                return (
                    f"the portal from {pid!r} to {target!r} would become a "
                    f"cycle: a space cannot be embedded in its own descendant"
                )
    return None


def embed_problem(target_id: str, parent_id: str,
                  tree: Dict[str, "Space"]) -> Optional[str]:
    """Why ``target_id`` may not be embedded in ``parent_id``, or None.

    The whole embed rule in one place (the daemon's ``embed_space`` calls
    this before it touches anything, so a refusal leaves no partial state):
    both spaces must exist; the parent must be editable (a read-only space's
    portals are declared by its file); the target must be **live** - a
    template is a definition, not something running anywhere, so the client
    instantiates it into a live space first; the embedding may not create a
    cycle, and the id path makes that test cheap (a space may not be embedded
    in itself or in any of its own descendants); and a space may be portaled
    into a given parent only once."""
    target = tree.get(target_id)
    if target is None:
        return f"no space {target_id!r}"
    parent = tree.get(parent_id)
    if parent is None:
        return f"no space {parent_id!r}"
    if parent.is_readonly:
        return readonly_portal_problem(parent_id)
    if target.is_template:
        return (
            f"space {target_id!r} is a template: its interior is a definition "
            f"and nothing of it runs - instantiate it into a live space and "
            f"embed that"
        )
    if target_id == parent_id:
        return f"space {target_id!r} cannot be embedded in itself"
    if portal_cycle(parent_id, target_id):
        return (
            f"{_space_label(target_id)} contains {parent_id!r}: a space cannot "
            f"be embedded in its own descendant"
        )
    if parent.portal(target_id) is not None:
        return f"space {target_id!r} is already embedded in {parent_id!r}"
    return None


# ---------------------------------------------------------------------------
# the space record, and app-declared spaces (agents/SPACES.md §18)
# ---------------------------------------------------------------------------

#: What an id path segment may use.  An id is a path *and* (for a permanent
#: space) the file stem it is loaded from, so a display name does not go here:
#: it goes in ``label``.
_ID_SEGMENT = re.compile(r"[A-Za-z0-9_.-]+")


def space_id_problem(space_id: Any) -> Optional[str]:
    """Why ``space_id`` cannot be a space's id, or None.

    The root is not an id anybody declares (it is the session's own space),
    an id is a ``::``-joined path of non-empty segments, and a segment uses
    only letters, digits, ``_``, ``-`` and ``.``."""
    if space_id == ROOT_ID:
        return "the root space is the session's own; it is not declared"
    if not isinstance(space_id, str) or not space_id.strip():
        return "a space needs an id"
    if space_id != space_id.strip():
        return f"space id {space_id!r} has leading or trailing whitespace"
    for segment in space_id.split(NAMESPACE_SEP):
        if not _ID_SEGMENT.fullmatch(segment):
            return (
                f"space id {space_id!r} is not an id path: every "
                f"{NAMESPACE_SEP!r}-separated segment must be non-empty and use "
                f"only letters, digits, '_', '-' and '.' (a display name goes "
                f"in `label`)"
            )
    return None


def node_space(instance_id: str, named: str = "") -> str:
    """The container space an announced (or placed) node lands in.

    A node's **own id path wins** when it has one: ``Parent::Child`` is the
    persistent contract (§1) and it is what a GUI-placed node is qualified
    with, so ``myapp::a`` is in ``myapp`` whatever else the payload says.  A
    node with a bare id is in the space it *names* — that is how an app whose
    instance ids are its own (``a``, ``b``) says which space it is building,
    and it is the sentence §18 is about.  Naming nothing, or naming only the
    default routing region (``main``), is the root: ``main`` is a
    pre-container leftover, not a space anybody declared."""
    own = space_of(instance_id)
    if own:
        return own
    named = str(named or "")
    return ROOT_ID if named in ("", ROUTING_DEFAULT) else named


def app_space(space_id: str, *, parent: Optional[str] = None, label: str = "",
              color: str = DEFAULT_COLOR, node_view: bool = False,
              declared_by: str = "") -> "Space":
    """A freshly declared space: the declaring app's own, and non-permanent.

    `permanent` is forced off — an app cannot make its space part of the
    saved session — and there is no file, no stem and nothing writable, so
    the persistence filter (§3) drops the whole record from every write."""
    return Space(
        id=space_id,
        parent=parent if parent is not None else parent_space(space_id),
        label=label or local_of(space_id) or space_id,
        color=color or DEFAULT_COLOR,
        mode=MODE_RW,
        permanent=False,
        node_view=node_view,
        declared_by=declared_by,
        config={"nodes": {}, "edges": [], "spaces": [], "groups": []},
    )


def declare_problem(space: "Space", declared_by: str) -> Optional[str]:
    """Why ``declared_by`` may not claim this space, or None.

    A **takeover** is what is refused (agents/SPACES.md §18): the id is
    permanent (the user's or the deployment's — an app cannot make its space
    persist, and only the user can turn Permanent on), the id is declared by
    a *file* (a read-only space belongs to its file, which is nix's or the
    deployment's word), or another client already owns it.  Editing a space
    *by name* through the ordinary commands stays allowed — the white box
    with nice borders rule — so this refuses a *claim*, not access."""
    if space.permanent:
        return (
            f"space {space.id!r} is permanent: an app cannot declare it (only "
            f"the user can make a space persist, and a permanent space is not "
            f"an app's to claim)"
        )
    if space.is_readonly:
        return (
            f"space {space.id!r} is declared by its file (read-only): it "
            f"belongs to the file, not to a client"
        )
    if space.declared_by and space.declared_by != declared_by:
        return (
            f"space {space.id!r} is owned by {space.declared_by!r}, not "
            f"{declared_by!r}"
        )
    return None


def declare_space(tree: Dict[str, "Space"], declaration: dict,
                  declared_by: str) -> Tuple[Optional["Space"], str]:
    """Create or update the space a client declares (agents/SPACES.md §18).

    Idempotent by construction: the **id is the identity**, so a second
    declaration returns the space the client already has, with the fields the
    declaration carries updated in place, and a declaration after a restart
    (where nothing of the space was persisted, so nothing came back) makes
    the same space again.  That is the whole mechanism: an app declares on
    every attach.

    Returns ``(space, "")``, or ``(None, why)`` when the claim is refused —
    in which case nothing is touched (see :func:`declare_problem`)."""
    problem = space_id_problem(declaration.get("space_id"))
    if problem:
        return None, problem
    space_id = str(declaration["space_id"])
    if not declared_by:
        return None, f"declaring {space_id!r} needs a declaring client"
    parent = declaration.get("parent")
    if parent is not None:
        # The id path *is* the nesting (§1: `Parent::Child`), so a declaration
        # stating a parent is held to it rather than moved somewhere else: the
        # app names its nodes with the same id path, and a space that quietly
        # lived elsewhere would not hold them.
        expected = parent_space(space_id) or ROOT_ID
        if str(parent) != expected:
            return None, (
                f"space id {space_id!r} is in {expected!r}; the declaration "
                f"says {parent!r}"
            )
    existing = tree.get(space_id)
    if existing is None:
        space = app_space(
            space_id,
            label=str(declaration.get("label") or ""),
            color=str(declaration.get("color") or DEFAULT_COLOR),
            node_view=bool(declaration.get("node_view") or False),
            declared_by=declared_by,
        )
        tree[space_id] = space
        return space, ""
    problem = declare_problem(existing, declared_by)
    if problem:
        return None, problem
    # The same client (or nobody yet): what the declaration states is applied,
    # what it leaves out is kept.
    if declaration.get("label") is not None:
        existing.label = str(declaration["label"])
    if declaration.get("color") is not None:
        existing.color = str(declaration["color"])
    if declaration.get("node_view") is not None:
        existing.node_view = bool(declaration["node_view"])
    existing.permanent = False
    existing.declared_by = declared_by
    return existing, ""


def announce_space(tree: Dict[str, "Space"], space_id: str,
                   module_id: str) -> Optional["Space"]:
    """Make sure the space an announcing node lands in exists (§18).

    A node that names a space the daemon has never heard of **creates** it,
    as the announcer's own — refusing the node instead would take an app's
    nodes offline, and they are its program; this is the same promise
    :func:`declare_space` makes, without a round trip per attach, which is
    what keeps every pre-S8 module working unchanged.

    An *existing* space is left exactly as it is: announcing into somebody
    else's space never claims it (every space may be extended, only a claim
    is refused).  Returns the space the node lands in, or None when the id is
    not one a space can have — node ids are free-form, and this is not the
    place to refuse one."""
    if space_id_problem(space_id) is not None:
        return None
    existing = tree.get(space_id)
    if existing is not None:
        return existing
    space = app_space(space_id, declared_by=module_id)
    tree[space_id] = space
    return space


def release_declared(tree: Dict[str, "Space"], client_id: str) -> List[str]:
    """Give up every space ``client_id`` declared — its connection is gone.

    The **record stays**: its nodes stay in the daemon too (an offline
    placement is kept so a reconnecting module binds it again), the space is
    non-permanent so it cannot outlive the session or reach a file, and
    dropping the record would only lose its label and color while the box was
    still drawn around those nodes.  What goes is the *claim*: ownership
    belongs to a connection, and a client that is gone owns nothing — exactly
    as `Registry.detach` already clears each instance's `declared_by`.  The
    app re-declares on its next attach and gets the same space back."""
    released: List[str] = []
    for space_id, space in tree.items():
        if space.declared_by == client_id:
            space.declared_by = ""
            released.append(space_id)
    return released


def space_record(space: "Space", *, declared_by_name: str = "") -> dict:
    """The space's own record, as every payload carries it.

    One builder, so the GUI's `get_nodes`, `list_spaces` and a display's
    snapshot cannot disagree about a space — in particular about who declared
    it (§18), which is what lets a UI say whose space it is.  A caller adds
    what is its own view of the space: the face, the portals, the nodes
    inside it, whether it is in edit mode, where it sits on the canvas."""
    return {
        "id": space.id,
        "parent": space.parent or ROOT_ID,
        "label": space.label,
        "color": space.color,
        "type": space.type,
        "permanent": bool(space.permanent),
        "node_view": bool(space.node_view),
        "template": space.template,
        "mode": space.mode,
        "readonly": space.is_readonly,
        "writable": bool(space.writable),
        "stem": space.stem,
        # Who declared it: the client id, and the friendly name that id is
        # known by ("" when the client is not attached any more).
        "declared_by": space.declared_by,
        "declared_by_name": declared_by_name,
    }


# ---------------------------------------------------------------------------
# the spawn rule and the write filter (agents/SPACES.md §2, §3, §12)
# ---------------------------------------------------------------------------


def spawns(space_id: str, tree: Dict[str, "Space"]) -> bool:
    """Is this space's interior materialized in the running graph?

    A *template* is a definition: its nodes exist as data only - no module
    instance, no live node, no placement - and neither does anything inside
    it.  ``instantiate_template`` is what materializes them."""
    pid = space_id
    for _ in range(64):
        space = tree.get(pid)
        if space is None or pid == ROOT_ID or space.parent is None:
            return True
        if space.type == TEMPLATE:
            return False
        pid = space.parent or ROOT_ID
    return True


def persists(space_id: str, tree: Dict[str, "Space"]) -> bool:
    """May this space be written to disk?

    Persistence is filtered **per space, outermost first** (§3): a space
    whose ``permanent`` is false is never written - session, space file,
    placement diffs - and neither is anything inside it.  An unknown space
    writes nothing (the daemon keeps no record to write from)."""
    pid = space_id
    for _ in range(64):
        space = tree.get(pid)
        if space is None or not space.permanent:
            return False
        if pid == ROOT_ID or space.parent is None:
            return True
        pid = space.parent or ROOT_ID
    return False


def prune_unpersisted(tree: Dict[str, "Space"]) -> Dict[str, "Space"]:
    """The part of `tree` that may be written, as a fresh dict.

    Non-permanent spaces and everything inside them are dropped, and so is
    every reference to them a surviving space carries: a child reference, an
    edge whose endpoint lives there (the edge is owned by an ancestor, so it
    would otherwise be written into a *permanent* space and reference a node
    that comes back nowhere), and a group member.  The surviving records are
    copies only where something had to be pruned, so a snapshot object is
    never mutated in place.

    **Portals are the exception**: a portal is not a reference a surviving
    space can drop without losing its own layout - the portal *is* the
    parent's placement, and the target's owner re-creates the target, so the
    portal stays even when the space it shows is written nowhere
    (agents/SPACES.md §13: "the parent still saves the portal placement and
    the target's interior is saved nowhere")."""
    keep = {pid for pid in tree if persists(pid, tree)}
    if len(keep) == len(tree):
        return tree
    out: Dict[str, "Space"] = {
        pid: space for pid, space in tree.items() if pid in keep
    }

    def node_kept(qualified: str) -> bool:
        return space_of(qualified) in keep

    for pid, space in out.items():
        refs = [
            entry for entry in space.child_entries
            if make_id(pid, child_name(entry)) in keep
        ]
        config = space.config
        if refs != space.child_entries:
            config = dict(config)
            config["spaces"] = refs
        edges = [
            e for e in space.edges
            if node_kept(e.get("from", "")) and node_kept(e.get("to", ""))
        ]
        if edges != space.edges:
            config = dict(config)
            config["edges"] = edges
        groups = [
            g for g in space.groups
            if all(node_kept(m) for m in (g.get("nodes") or []))
        ]
        if groups != space.groups:
            config = dict(config)
            config["groups"] = groups
        if config is not space.config:
            out[pid] = replace(space, config=config)
    return out


def copy_config(config: dict) -> dict:
    """A deep copy of a space's config.

    A space file is shared by every placement of it, and a template's
    definition is copied into each live space instantiated from it, so the
    copy must not alias the source's dictionaries.  Portals come along: a
    portal is a reference, so a copy of a space that embeds another space
    embeds the *same* space (both placements of it portal it)."""
    return {
        "nodes": copy.deepcopy(dict(config.get("nodes") or {})),
        "edges": copy.deepcopy(list(config.get("edges") or [])),
        "spaces": copy.deepcopy(list(config.get("spaces") or [])),
        "groups": copy.deepcopy(list(config.get("groups") or [])),
        PORTAL_KEY: copy.deepcopy(portals_from_raw(config)),
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
        # The portals this space embeds: part of its own saved record (§13).
        PORTAL_KEY: portals_from_raw(config),
    }


def build_payload(space: Space) -> dict:
    payload = {
        "type": space.type,
        "mode": space.mode,
        "label": space.label,
        "color": space.color,
        # Explicit even when true: whether a space is part of the session at
        # all is not something to infer from a key's absence.
        "permanent": bool(space.permanent),
        "node_view": bool(space.node_view),
        "auto_load": space.auto_load,
        "placement": space.placement(),
        # `declared_by` is deliberately absent (§18): it is a claim by a live
        # connection, not something about the space, so it is not file data —
        # and a declared space is non-permanent, so nothing of it is written
        # anywhere in the first place.
        "config": {
            "nodes": space.nodes,
            "edges": space.edges,
        },
    }
    if space.template:
        payload["template"] = space.template
    if space.config.get("spaces"):
        payload["config"]["spaces"] = space.child_entries
    if space.config.get("groups"):
        payload["config"]["groups"] = space.groups
    if space.portals:
        # A portal is written with the space that *embeds* it, never with the
        # space it shows: the target's file is not touched by becoming
        # embedded, and a non-permanent target writes no file at all while the
        # portal that shows it is still saved here (agents/SPACES.md §13).
        payload["config"][PORTAL_KEY] = space.portals
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
        type=type_from_raw(raw),
        # A file that exists is the user's (or the deployment's) own space
        # unless it says otherwise - only a space a *module/app* made, which
        # is never written, comes back non-permanent.  `declared_by` is not
        # read either: a claim does not survive the connection that made it
        # (§18) - whoever declared the space declares it again.
        permanent=bool(raw.get("permanent", True)),
        node_view=bool(raw.get("node_view", False)),
        template=raw.get("template") or None,
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
        # A generated file may declare itself a template (a definition the
        # deployment ships); everything else here is a live space.
        type=type_from_raw(raw),
        permanent=bool(raw.get("permanent", True)),
        node_view=bool(raw.get("node_view", False)),
        template=raw.get("template") or None,
        **placement,
    )
