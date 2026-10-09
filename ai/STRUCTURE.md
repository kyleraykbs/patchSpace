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

Note: `central/session_repair.py` still imports `gui.node_specs` for type/port
knowledge — one of the three mirrors the plan collapses into the daemon registry
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

* `node_specs.py` — the single source of truth for what a node type *is*: ports, kinds,
  inline controls, settings rows, menu/category/color/icon/description.  (Dies in
  PLAN.md phase 2: specs arrive from the daemon's module registry.)
* `view_mixin.py` — pan/zoom/undo shared by both canvases; `wire_router.py` — A* orthogonal
  routing; `force_layout.py` — physics; `render_utils.py` — palette + cairo helpers;
  `socket_client.py` — async client thread; `daemon_control.py` — start/adopt/stop;
  `bool_state.py` — the one poll-vs-pending rule for boolean controls.

**There is no GUI test suite.**  The daemon suite (`src/tests/`) is headless and never
touches real PipeWire; canvas/GUI behaviour is verified by hand (see `agents/NOTES.md` for
the offscreen-render and private-PipeWire recipes).
