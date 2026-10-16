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

| # | Step | Status |
|---|---|---|
| S0 | **rename panel → space** across daemon, client, nix module, docs, with file/protocol migration | **done** — daemon `416b041`, client `12b64aa`, mycelion `b97d9b3` |
| S1 | space **type** (`template`/`live`), **`permanent`** + the persistence filter, `color`, `node_view` default, face-input `mode`/`kind`/`method` | **done** — daemon `8a66890`, client `68bb882` |
| S2 | **portal** embedding (shared reference) + **direct cross-space wires** | **done** — daemon `4758ab9`, client `af5b25f` |
| S3 | **externalized value inputs** + `value_set` emission from value nodes | **done** — daemon `5c7b174`, client `cc226e1` |
| S4 | **cardinality** declared per port, enforced; hard-coded rules deleted | **done** — daemon `ff49b6c` (two boolean bus ports deliberately go from one → many) |
| S5 | primitives: routable **slider (0.0–1.0)**, **string**, `const` nodes | **done** — daemon `63b02df` |
| S6 | **sidebar spaces menu** (live spaces) + **dropdown&search** to switch/embed anywhere | **done** — client `66df741` |
| S7 | mycelion wiring + live smoke | **wiring done** (mycelion `ed160ea`); the **live smoke is Kyle's** — it needs the machine and his session |
| S8 | **an app declares its own space** (§18) — `space_declare`, app-owned and non-permanent, takeover refused | open — the spec's opening promise, and the last gap it named |
| S9 | **one value grammar** (§19) — the S3 and S5 paths resolved, ranked and kind-checked in one place | **done** — client `6b0e63a` (patchSpaceGui); daemon half this change (one vocabulary, one `value_slot` listing, one `api.value_problem`, one resolution order) |
| S10 | **declarative means uneditable** (§20) — refuse interior edits inside a readonly space while still allowing it to be plugged into | open — an audit found the rule only partly enforced, which contradicts the spec |

Verified at each step: the daemon suite (560 passed, 28 skipped at S5), the
client suite (347 passed), `scripts/smoke.sh` on a private instance, the
offscreen canvas render, and `nix build .#checks…tests` (the sandbox check, which
needs each new GTK test file added to its ignore list — a real trap: without it
the check segfaults on a display-less machine).

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
* [D] when a template is edited while instances exist: **implemented as a copy,
  not a rebase.**  `instantiate_template` materializes an independent live space
  whose `template` field only records where it came from; editing the template
  afterwards does not touch the live space, and there is no diff model between
  them.  Rationale: a half-following child would need the blueprint/instance diff
  machinery pointed at templates as well as space files, and the spec does not
  ask for it — the spec asks that a template's nodes stay unloaded until it is
  dropped.  If live rebase is wanted, it is a follow-up, and this line is where to
  say so.

## 20. S10 contract (frozen) — declarative means uneditable, except being plugged into

The spec: "a declarative live space should be readonly and uneditable, however we
should be able to plug things into it and drop it into other spaces of course."

An audit of both repos found the first half only partly true today: of the node
operations, only *move/reparent* is refused inside a declarative space; delete,
rename, property/layout edits and (un)wiring are refused by neither the daemon
nor the client.  That contradicts "uneditable", so this step states and enforces
the rule:

* **Refused inside a declarative space**: deleting a declared node, renaming it,
  changing its properties or its stored position, and adding or removing an edge
  whose *both* endpoints are declared (that is editing the declared wiring).
  Each refusal names what it refused and changes nothing.
* **Allowed**: an edge between a declared node and a node outside the space, in
  either direction — "plug things into it" — and its removal; embedding the space
  as a portal target; instantiating a space *from* a readonly template (copying
  out is not editing); and every read.
* **Where**: the daemon is the authority (the client may grey an affordance for
  responsiveness, but the daemon's refusal is what makes it true), and the
  provenance is the same `declarative` flag the load/export/reload paths already
  use — one notion of declared, not a second one.
* **Not changed**: a read-only space's own portals remain declared by its file
  (S2), and a read-only space may still be an embedding *target*.
* [D] how a non-permanent space announces itself: the app re-creates it on
  attach and its placement comes from the app's own `space_announce` payload, so
  the daemon must accept a layout for a space it has never persisted.

## 12. S1 contract (frozen)

A space record (payload `spaces[]`, and the space files) gains:

| field | values | default |
|---|---|---|
| `type` | `"template"` \| `"live"` | `"live"` (existing spaces and the root) |
| `permanent` | bool | `true` for the root and for GUI-created spaces; `false` for module/app-created |
| `color` | a theme class-color name (`theme_class_color` vocabulary) or `""` | `""` (derived from the space's own name) |
| `node_view` | bool — the default view when embedded | `false` |
| `template` | the space id this live space was instantiated from, or absent | absent |

`face[]` is **derived** from the `space_in`/`space_out` nodes placed in the
space (one source of truth: the node), and each *input* entry gains a config
stored on that node:

| field | values |
|---|---|
| `mode` | `"node"` (an ordinary jack) \| `"value"` (externalized) |
| `kind` | `"audio"` \| `"number"` \| `"boolean"` \| `"text"` \| `"choice"` |
| `method` | `"field"` \| `"edge"` (audio is node-only; `value` allows either) |
| `choices` | the option list, for `kind == "choice"` |

New commands: `set_space_meta {space_id, type?, permanent?, color?, node_view?}`,
`set_space_input {space_id, key, mode?, kind?, method?, choices?}`,
`instantiate_template {template_id, parent_space}` → creates a **nested live
space** from the template (whose id is recorded in `template`).

Behaviour in this step:

* **A template's interior is not spawned.**  Its nodes exist as data in the
  definition only; no module instance, no live node, no placement in the running
  graph.  `instantiate_template` is what materializes them (as a live space).
* **Persistence is filtered per space, outermost first.**  A space whose
  `permanent` is false is written to nothing — session, space file, placement
  diffs — and neither is anything inside it.  The daemon must still accept a
  layout for a non-permanent space it has never persisted (the app owns it).
* **The S0 protocol aliases are deleted here.**  The pair moves together in one
  release; the *data* aliases (old files, old directory, old flags, old type
  ids) stay — they are data, not protocol.  A GUI process started before the
  update must be relaunched.

## 13. S2 contract (frozen) — portal embedding, and wires that leave a box

**A portal is a placement of a live space inside another space, by reference.**
There is never a second interior: the embedded space keeps its own nodes,
module instances and diffs, and the embedding contributes only a position and a
view mode.

| command | effect |
|---|---|
| `embed_space {space_id, parent_space, x, y}` | the parent gains a portal entry naming `space_id` |
| `remove_space_embedding {space_id, parent_space}` | drops it (never the target space itself) |
| `set_embedding_view {space_id, parent_space, view}` | `"inside"` \| `"node"` — a pure view flag |

Space record additions:

* `portals[]` — `{space_id, view, x, y}`: the spaces this space embeds.
* `embedded_in[]` — the spaces that portal this one (so a space can be shown in
  several places at once, and the UI can say where it is).

Rules:

* **Refuse a cycle.**  A space may not be embedded in itself or in any of its own
  descendants (the id path makes the test cheap); the command answers with a
  clear error rather than a self-referential tree.
* **A portal's jacks are the space's own face nodes**, drawn at the collapsed
  position: dropping a wire on a portal jack resolves to the real
  `space_in`/`space_out` node inside the embedded space, so "route nodes into
  that live space and have them aggregate into its input nodes" needs no new
  wire concept — it is the existing fan-in the `space_in` node already does.
* `node_view` on the space is the **default** view for a new embedding; the
  portal's own `view` wins once set (per the spec: the checkbox is just the
  default).
* **Direct cross-space wires need no daemon change** — edges already connect node
  ids across space boundaries (the daemon deliberately does not enforce that a
  wire respects a box).  The work is in the client: routing, drawing and
  hit-testing a wire whose endpoints are in different spaces, and not treating a
  box as a clipping wall.
* Persistence follows S1's per-space rule: a portal is part of the parent's saved
  record; if the embed *target* is non-permanent, the parent still saves the
  portal placement and the target's interior is saved nowhere.

## 14. S3 contract (frozen) — value inputs, and `value_set` becomes real

The load-bearing generalization: **a node's parameter is an addressable input.**

* Every declared control whose value kind is `number`, `boolean`, `text` or
  `choice` also becomes an **input port** on that node type, auto-declared by the
  daemon from the module's `controls` list: `{name: <control key>, kind,
  direction: "in", drives_control: true}`.  Modules declare no extra metadata.
* `value_set {instance_id, port, value}` — already in `module_api` and, today,
  **emitted nowhere** — becomes the relay: the daemon forwards a producer
  module's reported value to the consumer module owning the driven control.  The
  consumer applies it exactly as if the user had set the control (its
  `on_control` path), so no module needs a new callback.
* An edge landing on a `drives_control` port means: forward `edge_apply` (for
  wiring bookkeeping) and then relay values.  Removing the edge returns the
  control to its stored local value.
* On a space face, `mode: "value"` + `method: "edge"` is exactly this: the face
  jack is the space's `space_in` node in value mode, which is a value **source**
  inside its own space, so "route into the space and aggregate on its input
  nodes" and "drive a parameter from outside" are the same mechanism.
* `method: "field"` keeps the typed inline field on the face and stores the value
  on the space (`space_input` config), applied as the default when nothing drives
  it.

## 15. S4 contract (frozen) — cardinality is declared, never assumed

* A port declares its cardinality in the type declaration:
  `cardinality: "one" | "many"` (absent = `"one"` for the single-driver kinds,
  `"many"` for the mixable ones — the *existing* behaviour, now stated rather
  than hard-coded by kind name).
* `central/graph.py`'s `SINGLE_DRIVER_KINDS` list is deleted; validation reads
  the declaration through `registry`.  A receiving node that needs a merge
  declares `merge: "mix" | "sum" | "last"`; the daemon only refuses what the
  declaration refuses.
* Shipped where it is needed: the four space port types (`space_in`/`space_out`
  and their boolean variants) declare what they actually do today — the boolean
  panel ports are the ones the docstrings already claim opt out of the
  single-driver rule — and nothing else changes behaviour.

## 16. S5 contract (frozen) — the value sources

Primitives gains what the spec names, all of them routable:

* `slider` becomes the **0.0–1.0 slider node** (its own type, output port kind
  `number`, no `min`/`max` UI — it *is* 0..1); a separate `const` (number
  literal) and `string` (text literal) round it out.
* Every one of them has an output port and reports its value through the module's
  existing `report_values`, so S3's relay carries it to whatever consumes it.
* The existing `number`/`toggle`/`display`/`math`/`compare`/`logic`/`invert`
  types gain input ports where a value can meaningfully arrive (today none of
  them has one).

## 17. S6 contract (frozen) — finding and entering a space

* **Sidebar**: the spaces view lists **live** spaces (the spec), with their color
  and a marker when a space has portals in it; templates are a separate section
  (they are the source of a drop, not somewhere to go).
* **Dropdown + search**: one affordance available in every space — type to filter
  by name, Enter to *enter* (switch the canvas focus), Shift/ctrl-modified action
  to *embed* into the current space.
* A **breadcrumb** of the id path (`Parent::Child`) is the reverse affordance,
  since entering a space is navigation, not modal state.
* Both come from one list installed from the payload, ordered through
  `sorting.order_spaces`, so the sidebar and the dropdown cannot disagree.

## 18. S8 contract (frozen) — an app declares its own space

The spec's opening sentence is the one promise S0–S6 do not yet keep: "an app can
create its own space and know it can place nodes there".  Today a module's
`node_announce` may carry a `space` per node and the registry records it, but
**nothing creates the space** — an app that names its own space gets nodes in a
space that does not exist.

* New message `space_declare {space: {space_id, label?, color?, node_view?,
  parent?}}` → creates the space or updates the one it declared, and is
  **idempotent**: an app declares on every attach, which is exactly how its space
  survives updates and reboots with nothing persisted.  It answers with the
  record it ended up with.
* A declared space is **app-owned**: `permanent` is forced **false** (an app
  cannot make its space persist — only the user can, by turning Permanent on in
  the GUI), and the daemon records `declared_by` = the declaring client id, so
  the payload and the UI can say whose space it is.
* **Refuse a takeover**: a declare may not claim an id that is permanent,
  nix-declared, or owned by a different client.  Editing a space *by name*
  through the ordinary edit paths stays allowed (the "white box with nice
  borders" rule) — what is refused is claiming ownership.
* The nodes an app announces into its own space are placed there and appear in
  the payload with it, so the app can wire them inside the space or out of it
  (S2: wires may cross).
* GUI-visible consequence, stated honestly: deleting an app's space from the GUI
  lasts until that app's next attach, because the app re-declares.  The UI must
  say the space is app-owned rather than pretend the deletion sticks.

## 19. S9 contract (frozen) — the value grammar, unified

After S3 and S5 there are two ways a value reaches a consumer: an auto-declared
`drives_control` port (S3) and a declared value input port (S5's
`display.text`).  They must not diverge:

* one resolution order everywhere — an edge on the port wins, then the stored
  control value, then the declared default;
* one kind vocabulary for value ports (`number`, `boolean`, `text`, `choice`) and
  one place that decides whether two kinds may connect;
* a `choice` value travels as its option value, and an unknown option is refused
  at the consumer rather than silently kept.

This is a coherence step, not a feature: it exists so the generalization cannot
grow a second, slightly different rule.