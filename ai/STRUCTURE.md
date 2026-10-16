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
| Containers | `central/spaces.py` | Space tree, namespaced ids, edge ownership (LCA), read-only snapshots, legacy migration (pre-S0 spellings of the file/marker still read), the space's `type`/`permanent`/`node_view`/`template` fields, the spawn rule (`spawns`) and the write filter (`persists`/`prune_unpersisted`). |
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
| Vocabulary | `central/module_api.py` | Roles (`module`/`display`/`app`), message shapes, validation. JSON-stable: it crosses the socket unchanged. |
| Catalogue | `central/registry.py` | Attached modules, declared types, instances; the status model (`online`/`offline_module`/`offline_dep`/`unbacked`), the `requires` closure, migration handoff, capability queries, `port_kind`/`port_multi` for the graph rules, reported `values`. |
| Structure | `central/graph.py` | Spaces, placements, edges, and the **single connection authority** (`kinds_compatible` + the one-driver rule). Edge ids equal `PatchSpace._edge_id`, so persisted sessions survive. |
| Wire | `central/protocol.py` | Daemon side of the API: `hello` + role gating, `type_declare` → migration fan-out → `node_announce`, `registry_query`, `catalog_request`/`snapshot_request`, `node_control`/`node_edge` (routed to the owning module), `node_values`, and module drop-off (a dead socket offlines its nodes). |
| Config | `central/module_config.py` | Which built-ins are enabled (`--modules-config`, else `~/.config/patchspace/modules.json`; a store path is deployment-managed).  `central/modules_cli.py` is `patchspace-modules`. |
| Provider | `modules/pipewire/declarations.py` + `catalog.json` | The node types this module declares: ports, controls, display, the add-node menu and the vocabulary.  Emitted once from the legacy tables during phase 2 and **the source from then on** — a test asserts it covers every type the daemon can construct. |
| Provider | `modules/pipewire/module.py` | The in-process module: greets through the same handler a socket client would use, answers migrations, announces the inventory the daemon hands it, and answers `control_set`/`edge_apply`. |
| Provider | `modules/primitives/` | The first *out-of-process* module: numbers and boolean logic (`types.py`), a bounded local fixpoint (`engine.py`), a `ModuleHost` program (`main.py`). |
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
`kinds_compatible` plus the single-driver rule.  `session_repair` calls it, the
daemon enforces it on every edge, and the client derives the same answer from the
declared port kinds (so it can refuse a drop locally).  There is no second copy.

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

