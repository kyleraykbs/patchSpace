# SPACES — the container model (active goal)

Status: **active**, approved 2026-10-15.  Supersedes the panel half of
`PLAN.md` §3 and closes that phase's "left: the naming" item.  Panels become
**spaces**, and a space is a first-class graph region that a program owns, the
user extends, and any space can embed.

Kyle's words, 2026-10-15 (the spec, unabridged):

> each program can have its own space, all spaces should be able to cross
> between other spaces, spaces should have inputs and outputs, we basically want
> to replace panels with spaces.  The goal here is an app can create its own
> space and know it can place nodes there without them likely getting tampered
> with (other things should be able to tamper with spaces by name but its a
> white box with nice borders situation), but the user and other programs can
> extend on those or edit those spaces too, like the user should have the
> default patchspace space and they should be able to switch to the other spaces
> with a dropdown&search in any space you should be able to drop a 'panel' of a
> space.  A space should have switch under "Space Types" 'Template' or 'Live'.
> If it's a template space, it's nodes aren't loaded in until we drop in a
> template into a live space.  Live spaces are always live, there should be a
> spaces menu on the right sidebar as well that shows you live spaces, when
> dropping a live space into another space we should be able to route nodes into
> that live space and have all of those aggregate into the input nodes of the
> space.  Along with a nice system for handling nodes that can only have one
> thing plugged in, but ANY node that should be able to have multiple things
> plugged into it shouldn't be arbitrarily restricted.  there should still be
> the imperative declarative thing, a declarative live space should be readonly
> and uneditable, however we should be able to plug things into it and drop it
> into other spaces of course.  There should be one more option per space:
> 'Node', if node is checked then by default it should always display as a node
> with a lil tooltip to show the whole space embedded in the other space, spaces
> should have colors.  This space embedding thing, there should always be a
> toggle for node per instance of a space in another space that allows the end
> user to switch between seeing inside and seeing the node, the Node checkbox is
> just the default.  Also space inputs should have an configuration option in
> the space of whether its a node input or a externalized value, if its an
> externalized value you should be able to choose between the types of input
> that make sense for that node type, audio is obviously only a node, but
> numbers/sliders can have a few different input methods.  Also all nodes with
> inputs should have the majority of inputs that make sense to be broken out
> broken out and generalized.  Sliders should be broken out into a custom
> 0.0-1.0 node type and then allowed to route, same thing with strings.

> The goal here is to hyper-generalize this

Added later the same day (the persistence rule):

> it shouldn't save spaces that don't have the 'Permanent' flag on them, Any
> spaces made through the gui should have permanent and the default PatchSpace
> space should have permanent but by default when apps are creating spaces it
> shouldn't be on so they work fine across updates and reboots with no
> imperative garbage to cause problems

## 1. What a space is

A **space** is a named region of the graph with a face, a color, a provenance, a
type and a persistence flag.  It is what `PLAN.md` §3 called a panel — same
object, more surface:

| field | meaning |
|---|---|
| `id` | path id, `Parent::Child`; `""` is the **root space** (the default patchspace space) |
| `label` | display name (the space's own name, not its file) |
| `color` | one color per space (box border/tint, portal node, sidebar chip) |
| `type` | `template` \| `live` |
| `permanent` | whether this space is **written to disk at all** (see §3) |
| `readonly` | declarative (nix-generated) — interior uneditable, still wireable/embeddable |
| `node_view` | the **default** view when this space is embedded elsewhere (`Node` checkbox) |
| `face` | the space's inputs/outputs, generated from the `space_in`/`space_out` nodes placed inside it |
| `interior` | nodes + edges inside the space |

Interior nodes keep their existing `Parent::Child` prefix.  A space is
**tamper-evident, not tamper-proof**: another program may edit a space by name
(the "white box with nice borders" reading) — there are no ACLs, only a boundary
that is always visible and always respected by the UI's own affordances.

## 2. Template vs Live

* **Template** — a definition.  Its nodes are *not* spawned; nothing runs.
* **Live** — spawned and running, always.

Dropping a **template** into a live space creates a **new nested live space**
(its nodes materialize inside it) [K].  Dropping a **live** space into another
space embeds the *same* space — a **portal**: one interior, one set of module
instances, shown in more than one place [K].  Never a copy.

View mode is per embedding and is a pure view flag: **inside** (expanded in
place) or **node** (collapsed to a node-faced box, tooltip shows the interior).
The space's `Node` checkbox only sets the default.

## 3. Persistence — only permanent spaces are saved

`permanent` decides whether a space is part of the saved session at all [K]:

* **GUI-created** spaces: `permanent` **on** by default.
* The **default patchspace space** (root, `""`): `permanent` **on**.
* **App/module-created** spaces: `permanent` **off** by default.

A non-permanent space is never written to the session or to a space file —
and neither is anything inside it (its nodes, its interior edges, its
placements, its diffs) [D, consequence].  Nothing of it survives a reboot;
the app re-creates it, and its nodes and wiring are rebuilt by the app, from
scratch, at the version it is running now.  That is the point: **no imperative
garbage accumulates across updates**, and an app's space cannot come back
half-stale against a new catalog.

Consequences to keep straight:

* A non-permanent space is *live* while it exists, and its placement is
  recomputed at announce time — the app owns its layout, not the file.
* The user may still edit inside one (it is a white box with nice borders); the
  edits live only as long as the space does, and the UI must say so rather than
  silently dropping them on restart.
* Persistence is therefore filtered **per space, outermost first**: a permanent
  space containing a portal to a non-permanent space saves the portal placement
  but not the target space's interior.

## 4. Wires across spaces

A node in one space may wire **directly** to a node in any other space [K]; the
router and the hit-test handle wires that leave a box.  The face is the
*structured* path and the **aggregation surface** — wires into a portal land on
the space's input face nodes and aggregate there — not a wall.

## 5. Space inputs: node input vs externalized value

Each face input declares a mode:

* `node` — an ordinary jack (audio/bundle/filter kind): wired from other nodes.
* `value` — **externalized**: the input is a value the space publishes, with a
  declared `kind` (number/boolean/text/choice).  Methods [K]:
  * `field` — typed inline field on the face;
  * `edge` — driven by a value node (the 0.0–1.0 slider node, `const`, a string
    node), i.e. the space's parameter is routable.

Audio is node-only (a media stream is not a value) [K].

## 6. Cardinality — no arbitrary restriction

Cardinality is **declared per port by the owning module** and enforced by the
daemon; there is no hard-coded per-kind list (today's `SINGLE_DRIVER_KINDS` is
that hard-coded list, and the `multi: true` opt-out exists in code but no shipped
type declares it).  Per the spec: any port that *can* accept several edges must
not be restricted.  A receiving node that needs a merge declares how
(`mix`/`sum`/`last`), and the daemon only refuses what the declaration refuses.

## 7. Hyper-generalization

Controls that make sense as inputs break out into **value nodes** with output
ports, and a node's parameter becomes an addressable **input**:

* a **0.0–1.0 slider node** (custom, routable) and a **string node** — the two
  the spec names;
* already present in `modules/primitives`: `number`, `slider`, `toggle`, `math`,
  `compare`, `logic`, `invert`, `display` — none of which currently has an input
  port, and `value_set` (the message that would let a node feed a control) is
  defined but **emitted nowhere**.  Making controls routable is therefore the
  load-bearing piece of this section.

## 8. The route

Each step ends green (daemon suite + client suite + offscreen render), and the
client keeps working at every point.

| # | Step | Notes |
|---|---|---|
| S0 | **rename panel → space** across daemon, client, nix module, docs, with file/protocol migration | pure vocabulary + migration; no behaviour change |
| S1 | space **type** (`template`/`live`), **`permanent`** + the persistence filter, `color`, `node_view` default, face-input `mode`/`kind`/`method` in the model + payload | templates stop spawning their nodes; non-permanent spaces stop being saved |
| S2 | **portal** embedding (shared reference) + **direct cross-space wires** | view toggle per embedding |
| S3 | **externalized value inputs** + `value_set` emission from value nodes | the space face becomes routable |
| S4 | **cardinality** declared per port, enforced; hard-coded rules deleted | `SINGLE_DRIVER_KINDS` → declarations |
| S5 | primitives: routable **slider (0.0–1.0)**, **string**, `const` nodes | value sources |
| S6 | **sidebar spaces menu** (live spaces) + **dropdown&search** to switch/embed anywhere | the navigation affordance |
| S7 | mycelion wiring (`services.patchspace.spaceDirs` etc.) + live smoke | Kyle's call to update |

## 9. Migration contract (S0)

Everything old must keep loading; everything new is written new.

| old | new |
|---|---|
| protocol payload key `panels` | `spaces` |
| commands `*_panel*` (`set_panel_layout`, `move_panel`, `create_panel`, `edit_panel`, `delete_panel`, `place_panel`, `reset_panel`, `clone_panel`, `export_panel`, `list_panel_files`) | same names with `panel`→`space` |
| node types `panel_in`/`panel_out`/`bool_panel_in`/`bool_panel_out` | `space_in`/`space_out`/`bool_space_in`/`bool_space_out` (+ legacy aliases read) |
| panel file schema key `panels` | `spaces` (both read) |
| `~/.local/share/patchspace/panels/` | `…/spaces/` (one-time copy, as `_migrate_legacy_paths` did for patchbay→patchspace) |
| `central/panels.py`, `Panel` | `central/spaces.py`, `Space` |
| `--panel-dir`, `services.patchspace.panelDirs` | `--space-dir`, `services.patchspace.spaceDirs` (old accepted) |
| node id prefix `Parent::Child` | **unchanged** — it is an id path, not a word |

## 10. Invariants (do not break)

From `PLAN.md` / `EXTRACTION.md` / the notes:

* **Never run a second daemon**; the orphan sweep is instance-scoped.
* **Placement is the daemon's; what a node *is* belongs to the module.**  Do not
  proxy the model (attribute access = a replica).
* **Optimistic UI**: the client applies an action then treats the **reply** as
  the fence; reply ordering is load-bearing.
* The **blueprint** is only written from a committed edit; instances keep their
  own diffs (`placement_diffs.json`), and a blueprint change rebases them.
* Type ids are a persistent contract; `schema` bumps go through the module's
  migrations.

## 11. Open (decide while implementing)

* [D] portal node rendering: reuse the node body (icon = the space, label = the
  space name, ports = the face) so a portal *is* a node visually; expanded view
  reuses the space box.
* [D] sidebar lists **live** spaces (per the spec) and, separately, templates as
  the source of a drop; both come from one list with a type filter.
* [D] a space's color source: `theme_class_color` vocabulary, so stylix keeps
  working.
* [D] when a template is edited while instances exist: instances rebase (the
  existing diff-rebase rule), and a template's own edit requires edit mode.
* [D] how a non-permanent space announces itself: the app re-creates it on
  attach and its placement comes from the app's own `space_announce` payload, so
  the daemon must accept a layout for a space it has never persisted.