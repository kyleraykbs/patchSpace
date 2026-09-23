# STRUCTURE — how PatchBay's modules connect

Top-level map of the pieces and the wires between them.  Depth lives in
`LLM_README.md` (1000+ lines of "why", failure modes and rejected designs) — read that
before changing anything non-trivial, and read the module docstrings of the file you touch.

## Processes

```
patchbay_gui.py  ──(unix socket, JSON lines, /tmp/patchbay.sock)──  main.py (daemon)
      │                                                                  │
   GTK4 canvas                                                     pw-dump -m / pw-cli / pw-cat / wpctl
   (src/gui/*)                                                          │
                                                              the real PipeWire graph
```

* **The daemon owns the graph.** `PatchBayDaemon` (main.py) is the only writer: it keeps a
  model (`PatchSpace`, pwnodes.py) and reconciles it onto live PipeWire objects.
* **The GUI is a client.** It sends commands and polls `get_nodes` every 400 ms; it applies
  user actions optimistically and each poll is guarded so a stale echo can't undo them.
* **Panels are files.** `panels.py` stores a nestable container tree; nodes/edges/group
  membership serialize into the panel file that last-common-ancestor owns.  The root panel
  is the session autosave (`~/.cache/patchbay/last_session.json`).

## Daemon-side layering

| Layer | File | Job |
|---|---|---|
| Substrate | `pwproc.py` | Owns real PipeWire objects: `OwnedPwNode` (a pw-cli session holding a created node), `OwnedPwProcess` (pw-cat/pw-loopback), `Backoff`, `Ticker`. |
| Graph snapshot | `pwgraph.py` | `pw-dump -m` stream → live nodes/ports/links; connect/disconnect; node-created/removed callbacks. |
| Matching | `pwmatch.py` | Filter dicts → live node ids; port groups → channel pairs; which objects are PatchBay's own. |
| Model | `pwnodes.py` | `Node` taxonomy + `PatchSpace` (the reconcile/supervise engine: `sync_locked` = structure, `supervise` = health). |
| Containers | `panels.py` | Panel tree, namespaced ids, edge ownership (LCA), read-only snapshots, legacy migration. |
| Daemon | `main.py` | Socket protocol, node factory (`NODE_TYPE_REGISTRY`/`_create_node`), property handling, session load, watchdogs, built-in devices. |
| Config | `migrations.py`, `session_repair.py`, `apply_config.py`, `export_config.py`, `patchbay_cli.py` | Versioned node migrations; validate/repair a session; CLI import/export. |

## The one big idea

**Every socket is a stable dummy; everything interesting is interior.**  An effect is a
sandwich (in-dummy → DSP module → out-dummy); user edges only ever attach to the dummies, so
a module reload can't drop a user edge.  Nodes that generate audio (a Sound Effect) are the
same shape: the dummy is the node's audio output, the player is interior and replaceable.

Consequences worth remembering:
* `add_node` creates structural pieces synchronously; the failure-prone module is left to
  the supervision tick (lazy loading).
* `_CAREFUL_NODE_TYPES` get a dedicated one-at-a-time bring-up on session load, because
  their multi-stream modules publish ports asynchronously.
* Anything a node owns that can *exit on its own* must stay out of `backings`
  (`BackedNode.owned_backings()` is how it still dies with the node).

## Control plane vs audio plane

Ports have a *kind* (`Node.port_kind`): `audio`, `bundle`, `boolean`, `filter`,
`impulse`.  `audio`/`bundle` become PipeWire links; the other three never do.

| kind | direction | how it flows |
|---|---|---|
| `boolean` | level | pulled: `_resolve_boolean*` evaluates it at the top of every sync |
| `filter` | predicate | pulled: a Filter node ANDs its classifiers over a bundle |
| `impulse` | event | **pushed**: `PatchSpace.pulse()` walks the edges on a button press |

The GUI must mirror the pairing rules (`gui/node_specs.ports_compatible`,
`ports_compatible`/`port_kind`) and `session_repair` re-checks the same function, so the
three can't disagree about what may connect.

## GUI-side map

`patchbay_gui.py` (app) → `main_window.py` (notebook: raw PW tab + editor tab, toolbars,
side panels, console, daemon start/adopt) → `patchspace_widget.py` (the editable canvas:
drawing, hit-testing, gestures, layout, panels, wires) with:

* `node_specs.py` — the single source of truth for what a node type *is*: ports, kinds,
  inline controls, settings rows, menu/category/colour/icon/description.
* `view_mixin.py` — pan/zoom/undo shared by both canvases; `wire_router.py` — A* orthogonal
  routing; `force_layout.py` — physics; `render_utils.py` — palette + cairo helpers;
  `socket_client.py` — async client thread; `daemon_control.py` — start/adopt/stop;
  `bool_state.py` — the one poll-vs-pending rule for boolean controls.

**There is no GUI test suite.**  The daemon suite (`src/tests/`) is headless and never
touches real PipeWire; canvas/GUI behaviour is verified by hand (see `agents/NOTES.md` for
the offscreen-render and private-PipeWire recipes).
