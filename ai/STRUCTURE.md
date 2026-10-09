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
  writer: it keeps a model (`central/pwnodes.py`) and reconciles it onto live
  PipeWire objects.
* **The GUI is a client.** It sends commands and polls `get_nodes` every 400 ms; it
  applies user actions optimistically and each poll is guarded so a stale echo can't
  undo them.
* **Panels are files.** `central/panels.py` stores a nestable container tree;
  nodes/edges/group membership serialize into the panel file that
  last-common-ancestor owns.  The root panel is the session autosave
  (`~/.cache/patchspace/last_session.json`).

Everything runs with `src/` as the import root (CWD or `PYTHONPATH`): daemon code
imports as `central.*`, the PipeWire substrate as `modules.pipewire.*`.  The packaged
entry points are `python -m central.main` / `python -m central.session_repair`.

## Daemon-side layering

| Layer | File | Job |
|---|---|---|
| Substrate | `modules/pipewire/pwproc.py` | Owns real PipeWire objects: `OwnedPwNode` (a pw-cli session holding a created node), `OwnedPwProcess` (pw-cat/pw-loopback), `Backoff`, `Ticker`. |
| Graph snapshot | `modules/pipewire/pwgraph.py` | `pw-dump -m` stream → live nodes/ports/links; connect/disconnect; node-created/removed callbacks. |
| Matching | `modules/pipewire/pwmatch.py` | Filter dicts → live node ids; port groups → channel pairs (a lone `MONO` endpoint bridges every channel of its peer); which objects are Patch Space's own. |
| Model | `central/pwnodes.py` | `Node` taxonomy + `PatchSpace` (the reconcile/supervise engine: `sync_locked` = structure, `supervise` = health).  PipeWire-bound classes are next in line to move into the module (PLAN.md phase 1/3). |
| Containers | `central/panels.py` | Panel tree, namespaced ids, edge ownership (LCA), read-only snapshots, legacy migration. |
| Daemon | `central/main.py` | Socket protocol, node factory (`NODE_TYPE_REGISTRY`/`_create_node`), property handling, session load, watchdogs, built-in devices. |
| Config | `central/migrations.py`, `central/session_repair.py`, `central/apply_config.py`, `central/export_config.py`, `central/patchspace_cli.py` | Versioned node migrations; validate/repair a session; CLI import/export. |

## The module layer (agents/PLAN.md phase 1)

The PipeWire-agnostic core is in place; the PipeWire machinery is not yet
extracted, but it now goes through the module API:

| Piece | File | Job |
|---|---|---|
| Vocabulary | `central/module_api.py` | Roles (`module`/`display`/`app`), message shapes, validation. JSON-stable: phase 3 puts it on a socket unchanged. |
| Catalogue | `central/registry.py` | Attached modules, declared types, instances; the status model (`online`/`offline_module`/`offline_dep`/`unbacked`), the `requires` closure, migration handoff, capability queries, `port_kind`/`port_multi` for the graph rules. |
| Structure | `central/graph.py` | Spaces, placements, edges, and the **single connection authority** (`kinds_compatible` + the one-driver rule). Edge ids equal `PatchSpace._edge_id`, so persisted sessions survive. |
| Wire | `central/protocol.py` | Daemon side of the API: `hello` + role gating, `type_declare` → one `migrate` per persisted instance → `migrate_result` → `node_announce`, `registry_query`, and module drop-off (a dead socket offlines its nodes). |
| Provider | `modules/pipewire/declarations.py` | The node type catalog, generated from `gui/node_specs.py` + `NODE_TYPE_REGISTRY` while both exist (an agreement test keeps them in step; phase 2 makes this the source). |
| Provider | `modules/pipewire/module.py` | The in-process module: greets through the same handler a socket client would use, answers migrations, announces the inventory the daemon hands it, and answers `control_set`/`edge_apply`. |

The daemon's `handle_command` routes on the message shape: `"type"` is the
module API, `"command"` is the legacy GUI protocol — both work, which is how
the GUI keeps running while the core lands. `sync_registry()` mirrors the
live nodes into the registry (a placement each), and the module's announce
binds the ones it runs; a placement nothing provides stays `unbacked` — the
gray node — and is never swept by a sync.

Note: `central/session_repair.py` still imports `gui.node_specs` for type/port
knowledge — one of the three mirrors the plan collapses into the registry
(phase 2).

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

The GUI must mirror the pairing rules (`gui/node_specs.ports_compatible`,
`ports_compatible`/`port_kind`) and `session_repair` re-checks the same function, so the
three can't disagree about what may connect.

## GUI-side map

`patchspace_gui.py` (app) → `main_window.py` (notebook: raw PW tab + editor tab, toolbars,
side panels, console, daemon start/adopt) → `patchspace_widget.py` (the editable canvas:
drawing, hit-testing, gestures, layout, panels, wires) with:

* `node_catalog.py` — what the daemon's modules declare: ports, kinds, controls,
  labels, colours, icons, the add-node menu, the settings rows.  The client asks for
  it once per connection (`hello` role=display → `catalog_request`) and installs the
  reply; `node_specs.py` (the GUI's own copy) is gone.  An unknown type renders as a
  generic in/out node, so a module the GUI has never heard of still draws.
* `view_mixin.py` — pan/zoom/undo shared by both canvases; `wire_router.py` — A* orthogonal
  routing; `force_layout.py` — physics; `render_utils.py` — palette + cairo helpers;
  `socket_client.py` — async client thread; `daemon_control.py` — start/adopt/stop;
  `bool_state.py` — the one poll-vs-pending rule for boolean controls.

`gui/` is a namespace package: its modules import each other **flat**, and
`gui.node_catalog` is a *different module object* from a flat `import node_catalog`.
Anything that installs the catalog (tests, tooling) must use the flat import or the
widget reads an empty one.

**There is no GUI test suite.**  The daemon suite (`src/tests/`) is headless and never
touches real PipeWire; canvas/GUI behaviour is verified by hand (see `agents/NOTES.md` for
the offscreen-render and private-PipeWire recipes).
