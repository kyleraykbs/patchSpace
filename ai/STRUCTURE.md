# STRUCTURE — how Patch Space's modules connect

Top-level map of the pieces and the wires between them.  Depth lives in
`LLM_README.md` (1000+ lines of "why", failure modes and rejected designs) — read that
before changing anything non-trivial, and read the module docstrings of the file you touch.

The three-layer split (central daemon / modules / display) is `agents/PLAN.md`; this
file describes today's tree, which phase 0 of that plan restructured in place.

## Processes

```
gui/patchspace_gui.py  ──(unix socket, JSON lines, $XDG_RUNTIME_DIR/patchspace.sock)──  central/main.py (daemon)
      │                                                                                    │
   GTK4 canvas                                                     modules/pipewire/*  │   │
   (src/gui/*)                                                          │              │   │
                                                     pw-dump -m / pw-cli / pw-cat / wpctl  │
                                                          the real PipeWire graph         │
```

* **The daemon owns the graph.** `central/main.py` (`PatchSpaceDaemon`) is the only
  writer: it keeps a model (`modules/pipewire/pwnodes.py`) and reconciles it onto live
  PipeWire objects.
* **The GUI is a client.** It sends commands and polls `get_nodes` every 400 ms; it
  applies user actions optimistically and each poll is guarded so a stale echo can't
  undo them.
* **Spaces are files.** `central/spaces.py` stores a nestable container tree;
  nodes/edges/group membership serialize into the space file that
  last-common-ancestor owns.  The root space is the session autosave
  (`~/.cache/patchspace/last_session.json`).  (Both the module, the class and
  the container word were `panel`/`panels.py` before S0, `agents/SPACES.md`.)
  A space has a **kind** (`live`/`template` - the file's `type` key), a
  **`permanent`** flag that decides whether it is written to disk at all (and
  is filtered per space, outermost first: nothing inside a non-permanent space
  is written either), a `color`, a `node_view` default and, for an instance, the
  `template` it came from.  Its **face** is derived, not stored: the daemon
  reads the `space_in`/`space_out` nodes placed in it (the node carries the
  per-input `mode`/`kind`/`method`/`choices`) and exposes `face[]` on the space
  record a client reads.  A template's interior is a definition and is never
  spawned; `instantiate_template` copies it into a nested live space.
  A space may also **embed** another by reference - a **portal**
  (`agents/SPACES.md` §13): the parent's record carries
  `portals[] = {space_id, view, x, y}`, the target keeps its one interior and
  reports the parents in `embedded_in[]` (derived, never stored), and the three
  commands (`embed_space`/`remove_space_embedding`/`set_embedding_view`) are
  the only writers.  A portal's jacks are the target's own `face[]` entries, so
  a client resolves the real node behind one as `<space_id>::<key>` and a wire
  dropped there lands on the target's `space_in`/`space_out` - no new wire
  concept, and no second interior.
  A space can also be **an app's own** (`agents/SPACES.md` §18): a module sends
  `space_declare {space_id, label?, color?, node_view?, parent?}` on every
  attach, and the daemon creates or updates that space, non-permanent and
  owned by the declaring client (`declared_by`, with `declared_by_name` in
  every payload).  Claiming an id that is permanent, declared by a file or
  another client's is refused (editing a space *by name* stays allowed); a node
  announced into a space nobody declared *makes* it, as the announcer's own,
  so pre-S8 modules keep working.  Nothing of any of this is ever written
  (`prune_unpersisted` drops it), so the declaration is what makes the space
  exist and re-declaring after a restart reproduces it.  The user can still
  take it into the session (`set_space_meta {permanent: true}` gives the space
  a file of its own and ends the app's claim on it).

Everything runs with `src/` as the import root (CWD or `PYTHONPATH`): daemon code
imports as `central.*`, the PipeWire substrate as `modules.pipewire.*`.  The packaged
entry points are `python -m central.main` / `python -m central.session_repair`.

## Daemon-side layering

| Layer | File | Job |
|---|---|---|
| Substrate | `modules/pipewire/pwproc.py` | Owns real PipeWire objects: `OwnedPwNode` (a pw-cli session holding a created node), `OwnedPwProcess` (pw-cat/pw-loopback), `Backoff`, `Ticker`. |
| Graph snapshot | `modules/pipewire/pwgraph.py` | `pw-dump -m` stream → live nodes/ports/links; connect/disconnect; node-created/removed callbacks. |
| Matching | `modules/pipewire/pwmatch.py` | Filter dicts → live node ids; port groups → channel pairs (a lone `MONO` endpoint bridges every channel of its peer); which objects are Patch Space's own. |
| Model | `modules/pipewire/pwnodes.py` | `Node` taxonomy + `PatchSpace` (the reconcile/supervise engine: `sync_locked` = structure, `supervise` = health).  It lives on the module's side (it imports `modules.pipewire.*` and nothing from `central`), so the daemon reaches into it in-process — the transitional state before the report (EXTRACTION.md step 2). |
| Containers | `central/spaces.py` | Space tree, namespaced ids, edge ownership (LCA), read-only snapshots, legacy migration (pre-S0 spellings of the file/marker still read), the space's `type`/`permanent`/`node_view`/`template`/`declared_by` fields, the spawn rule (`spawns`), the write filter (`persists`/`prune_unpersisted`) and portals (`portals`/`embedded_in`, `embed_problem`, `drop_portals_to`).  Also the app-owned space rules (§18): `space_record` (the one payload builder), `node_space` (which space a node is in), `declare_space`/`declare_problem`, `announce_space` (an announcement makes the space it names, as its own) and `release_declared`.  And the declarative-interior rule (§20): `readonly_node_problem`/`readonly_edge_problem` (plus the one message builder, `readonly_edit_problem`, beside `readonly_portal_problem`) answer "may this node/this edge be edited?" from the space tree — the daemon's command handlers and `protocol._node_edge` both ask them, so the "a declarative space is uneditable" refusal is the same sentence on every path, and the reload path never asks it (the file *is* the declaration being applied). |
| Daemon | `central/main.py` | Socket protocol, node factory (`NODE_TYPE_REGISTRY`/`_create_node`), property handling, session load, watchdogs, built-in devices. |
| Config | `central/migrations.py`, `central/session_repair.py`, `central/apply_config.py`, `central/export_config.py`, `central/patchspace_cli.py` | Versioned node migrations; validate/repair a session; CLI import/export. |

## The module layer (agents/PLAN.md phases 1, 3, 5, 6)

The PipeWire-agnostic core is in place and modules are real: the daemon runs
built-in ones as child processes and serves what they declare.  The PipeWire
machinery has started moving out (PLAN.md phase 3, "the extraction"): the model
(`modules/pipewire/pwnodes.py`) now lives on the module's side, so what remains
is the *report* — the daemon reading node data through the API instead of walking
the model — and then the process move.  See `agents/EXTRACTION.md`:

| Piece | File | Job |
|---|---|---|
| Vocabulary | `central/module_api.py` | Roles (`module`/`display`/`app`), message shapes, validation. JSON-stable: it crosses the socket unchanged.  Also the control vocabulary: which widget carries which value kind (`WIDGET_VALUE_KINDS`), and `control_input_ports` — the input port every value control auto-declares (§14).  **The value grammar (§19) is here too**: the two kind lists (`NODE_KINDS`, `VALUE_KINDS`, and `PORT_KINDS` as their union), `is_value_kind`, and the one listing of the slots a type declares (`value_slots` → `{name, kind, control, choices}`), which the registry stores and the consuming module rebuilds from its own declarations.  And `space_declare`/`space_declared` + `validate_space_declare`: the app-owned space message (§18). |
| Catalogue | `central/registry.py` | Attached modules, declared types, instances; `announce`'s `on_space` hook hands each accepted node's container space to the daemon's tree, which is where a declared space is made (§18); `client_name` is the friendly name behind a `declared_by`; the status model (`online`/`offline_module`/`offline_dep`/`unbacked`), the `requires` closure, migration handoff, capability queries, `port_kind`/`port_cardinality`/`port_drives_control` (and `port_multi`/`port_merge`) for the graph rules, reported `values`, and the value slots: `value_slot`/`value_problem` (the §19 acceptance question, from `api.value_problem`) plus a slot's stored local value (`capture_slot`/`set_slot_value`/`local_slot_value`, keyed by port name — the control key for a `drives_control` port). |
| Structure | `central/graph.py` | Spaces, placements, edges, and the **single connection authority** (`kinds_compatible` + the cardinality rule, resolved through the registry): audio/bundle mix either way, every other *declared* kind pairs only with itself — `choice` included — and an undeclared kind pairs with nothing. `edges_from`/`edges_into` are what the value relay walks. Edge ids equal `PatchSpace._edge_id`, so persisted sessions survive. |
| Wire | `central/protocol.py` | Daemon side of the API: `hello` + role gating, `type_declare` → migration fan-out → `node_announce`, `space_declare` (a module's own space: create/update, the takeover refusals, `release_spaces` when its connection goes — the snapshot's `spaces` are the container records), `registry_query`, `catalog_request`/`snapshot_request`, `node_control`/`node_edge` (routed to the owning module; a value the target's declaration cannot take is refused with the reason, and an edge whose both endpoints are declared is refused by §20's rule before anything is recorded — the same predicate the `add_edge`/`remove_edge` commands ask), `node_values`, and the **value relay** (`relay_values`/`bind_value_edge`/`deliver_value`: a reported value drives every value slot its edges reach — `control_set` for a control port, `value_set` for any other value port — and unwiring restores the slot's stored value), and module drop-off (a dead socket offlines its nodes). |
| Config | `central/module_config.py` | Which built-ins are enabled (`--modules-config`, else `~/.config/patchspace/modules.json`; a store path is deployment-managed).  `central/modules_cli.py` is `patchspace-modules`. |
| Provider | `modules/pipewire/declarations.py` + `catalog.json` | The node types this module declares: ports, controls, display, the add-node menu and the vocabulary.  Emitted once from the legacy tables during phase 2 and **the source from then on** — a test asserts it covers every type the daemon can construct. |
| Provider | `modules/pipewire/module.py` | The in-process module: greets through the same handler a socket client would use, answers migrations, announces the inventory the daemon hands it, and answers `control_set`/`edge_apply`. |
| Provider | `modules/primitives/` | The first *out-of-process* module: the value sources (`number`/`const`/`slider`/`toggle`/`string`) and the operations (`math`/`compare`/`logic`/`invert`/`display`) in `types.py`, a bounded local fixpoint over one slot per input, one resolution order (edge, then what the slot holds, then the node's own default) and the same §19 acceptance rule asked of its own declarations (`engine.py`: `Engine(types.TYPES)` → `slots`, `refuses`, `set_control`), a `ModuleHost` program (`main.py`). |
| Runtime | `central/module_supervisor.py` | Runs built-in modules as **child processes**: start on attach, restart with backoff (and give up on one that keeps dying), stop with the daemon.  Specs come from the deployment config (phase 6). |
| Runtime | `modules/host.py` | The client a module *process* runs: connect, greet, declare, announce, answer migrations, dispatch the daemon's pushes, reconnect.  The same protocol the in-process provider speaks — which is what makes moving a provider out of the daemon a transport change. |

The daemon's `handle_command` routes on the message shape: `"type"` is the
module API, `"command"` is the legacy GUI protocol — both work, which is how
the GUI keeps running while the core lands. `sync_registry()` mirrors the
live nodes into the registry (a placement each), and the module's announce
binds the ones it runs; a placement nothing provides stays `unbacked` — the
gray node — and is never swept by a sync.

`session_repair.py` builds its own port/label table from the same catalog data
(`modules.pipewire.declarations`) and asks `central/graph.py` for the
compatibility rule, so an offline validator needs neither a daemon nor the client.

## The one big idea

**Every socket is a stable dummy; everything interesting is interior.**  An effect is a
sandwich (in-dummy → DSP module → out-dummy); user edges only ever attach to the dummies,
so a module reload can't drop a user edge.  Nodes that generate audio (a Sound Effect) are
the same shape: the dummy is the node's audio output, the player is interior and replaceable.

Consequences worth remembering:
* `add_node` creates structural pieces synchronously; the failure-prone module is left to
  the supervision tick (lazy loading).
* `_CAREFUL_NODE_TYPES` get a dedicated one-at-a-time bring-up on session load, because
  their multi-stream modules publish ports asynchronously.
* Anything a node owns that can *exit on its own* must stay out of `backings`
  (`BackedNode.owned_backings()` is how it still dies with the node).

## Control plane vs audio plane

Ports have a *kind* (`Node.port_kind`): `audio`, `bundle`, `boolean`, `filter`,
`impulse`, `sound`, `camera`.  `audio`/`bundle` become PipeWire links; the
control-plane kinds never do.  `sound` is a file reference.  `camera` is
data-plane like audio but carries a live video stream (Camera In ->
Virtual Camera): its link is owned by the consuming node's own GStreamer
stream (its `target.object` points at the camera node), so sync derives no
port pairs for it - see `pwnodes.VirtualCameraNode`.

| kind | direction | how it flows |
|---|---|---|
| `boolean` | level | pulled: `_resolve_boolean*` evaluates it at the top of every sync |
| `filter` | predicate | pulled: a Filter node ANDs its classifiers over a bundle |
| `impulse` | event | **pushed**: `PatchSpace.pulse()` walks the edges on a button press |

The pairing rules live in exactly one place — `central/graph.py`'s
`kinds_compatible` plus the cardinality rule.  `session_repair` calls it, the
daemon enforces it on every edge, and the client derives the same answer from the
declared port kinds (so it can refuse a drop locally).  There is no second copy.
S9 (`agents/SPACES.md` §19) pinned the vocabulary it judges against: only the
kinds `module_api` declares may pair (an undeclared kind pairs with nothing,
not even another copy of itself), every *value* kind — `number`, `boolean`,
`text`, `choice` — pairs only with itself, and the other half of the rule
(whether the **value** on a wire may land on the port) is
`module_api.value_problem`, asked by the relay and by the consuming module.

What reaches a consumer from the relay is decided the same way: an input port
whose kind is a value kind is a **value slot** (`api.value_slots`, stored on the
type as `Registry`'s `slots` and rebuilt by the module that owns the nodes), the
delivery is `control_set` when the port drives a control and `value_set` when it
does not, and the slot resolves — in the module — as edge, then stored value,
then the module's own default.

How many drivers a *receiving* port takes is declared, never assumed (S4,
`agents/SPACES.md` §15): a port states `cardinality: "one" | "many"` in its type
declaration, `graph.py` no longer names a kind list (`SINGLE_DRIVER_KINDS` is
deleted) and resolves each edge through `Registry.port_cardinality`, which reads
the declaration and falls back to the pre-S4 rule for the port's kind
(`boolean`/`impulse`/`sound`/`camera` take one; everything else mixes) — so a
declaration-less type behaves exactly as before.  A receiving node that needs a
merge declares `merge: "mix" | "sum" | "last"`; the daemon never merges and only
refuses what the declaration refuses (a declared `merge` is a declared `many`).
The four space port types (`space_in`/`space_out`, `bool_space_in`/
`bool_space_out`) declare `many` on their input: they are buses today
(`MIX_INPUTS`/`ALLOW_MULTIPLE_BOOLEAN` in the module), which is what the old
docstrings already claimed.

## The client (its own repo)

The GTK4 window lives in `~/projects/patchSpaceGui` (flake `patchSpaceGui`) and is
not part of this tree.  What this repo owes it:

* the **socket protocol** (`central/module_api.py`, `central/protocol.py`): a
  display greets (`hello` role=display), asks for the catalog once
  (`catalog_request`), polls the graph, and sends `node_control`/`node_edge`;
* the **catalog** — what the modules declare (`type_declare` → `catalog_reply`),
  which the client installs as its node specs.  A module the client has never
  heard of still draws, and a node nothing runs is drawn inert;
* the **daemon's own tests** (`src/tests/`, headless, no display): they assert
  what the *modules declare* (`tests/catalog.py` is a small adapter over
  `modules.pipewire.declarations`), never the client's parser.

