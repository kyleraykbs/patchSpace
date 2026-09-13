# LLM_README — working notes for AI models on PatchBay

This file is for future AI agents (and humans) working in this repo. It captures the
architecture, the process that works here, and — most importantly — the hard-won
"do not try again" lessons that are otherwise buried in code comments.

Read this before making changes. Then read the relevant module docstrings; this
codebase deliberately documents *why* and *what failed before*, not just *what*.

---

## 0. TL;DR

- **Work in `src/`.** The legacy tree was removed and the rewrite was promoted into its
  place (commit "Promote src-rewrite to src; drop the legacy tree"). There is no
  `src-rewrite/` any more.
- **Test command:** from `src/`, run `python -m pytest -q` (currently **114 passing,
  ~2s**). Tests are headless and do not touch real PipeWire.
- **Run it:** `nix run` (GUI) or `nix run .#daemon` (headless daemon) from the repo root;
  or, in `nix develop`, `cd src && python main.py` and `cd src/gui && python patchbay_gui.py`.
- **The GUI starts/owns the daemon.** On launch it adopts a daemon that is already
  running, otherwise it starts a background one; it only shuts that daemon down on window
  close if *it* started it. The hamburger menu has Start/Stop/Restart Daemon. See §3.
- **Dev shell:** `nix develop` (from repo root) provides GTK4, libadwaita, PipeWire,
  and exports `LADSPA_PATH` / `LV2_PATH` for the DSP plugins.
- **After changing daemon code, the running daemon/GUI must be restarted** to pick it
  up. Tests do not catch live-graph behavior.

---

## 1. Repo layout

```
/home/kyle/projects/patch
├── flake.nix              # packages patchbay + patchbay-daemon, apps, devShell
├── flake.lock
├── LLM_README.md
└── src/                   # the whole implementation (former src-rewrite/)
    ├── main.py            # daemon: socket API, session load, node factory, wiring
    ├── pwnodes.py         # node graph model: PatchSpace, all Node classes, effect sandwich
    ├── panels.py          # panel containers: namespacing, LCA edges, tree IO, migration
    ├── pwgraph.py         # live PipeWire graph model (pw-dump driven)
    ├── pwproc.py          # OwnedPwNode / OwnedPwProcess (pw-cli/pw-dump subprocesses)
    ├── pwmatch.py         # matching helpers for external nodes
    ├── patchbay_cli.py    # synchronous one-shot socket client (CLI tools)
    ├── apply_config.py    # CLI: apply an exported config
    ├── export_config.py   # CLI: export current config
    ├── tests/             # pytest suite (headless)
    └── gui/               # GTK4 client (flat imports; no __init__.py — namespace pkg)
        ├── patchbay_gui.py        # GUI entrypoint (Gtk.Application)
        ├── main_window.py         # MainWindow, LogConsole, toolbar, hamburger, loading overlay
        ├── patchspace_widget.py   # the editable node canvas (big file)
        ├── node_specs.py          # NodeSpec table: ports, inline control, Settings rows
        ├── daemon_control.py      # start/adopt/stop the background daemon
        ├── view_mixin.py          # pan/zoom/undo shared by both graph widgets
        ├── pipewire_widget.py     # raw PipeWire graph view
        ├── socket_client.py       # async PatchBayClient (background thread)
        ├── render_utils.py        # cairo drawing helpers, theme palette
        ├── force_layout.py        # physics layout for nodes
        ├── color_picker.py        # self-drawn HSV picker (no GSettings dependency)
        ├── portal_file_dialog.py  # xdg portal file chooser
        └── constants.py           # shared timing/zoom/size constants
```

Note: there is **no `.gitignore` and `__pycache__/*.pyc` files are tracked**. Ignore the
`.pyc` churn in `git status`; stage only source files.

---

## 2. Running it

### Packaged (recommended)

```bash
# from repo root
nix run                 # builds + runs the GUI (default app = packages.patchbay)
nix run .#daemon        # runs the headless daemon
nix build .#patchbay .#patchbay-daemon
```

- `packages.patchbay` is the GTK client. It is built with `wrapGAppsHook4` +
  `makeWrapper`, so the GTK/Adwaita typelibs, GSettings schemas and XDG data dirs are
  baked into the wrapper; it also sets `PATCHBAY_DAEMON` (so the GUI knows how to spawn
  the daemon) and `LADSPA_PATH` / `LV2_PATH`.
- `packages.patchbay-daemon` is the headless daemon (`writeShellApplication`) with
  `pipewire`, `wireplumber` and the DSP plugin packages and their env paths.

### From the dev shell

```bash
nix develop            # provides python, gtk4, adwaita, pipewire, LADSPA_PATH, LV2_PATH

# daemon
cd src && python main.py

# GUI (must run from the gui/ dir; imports are flat)
cd src/gui && python patchbay_gui.py

# tests
cd src && python -m pytest -q
```

The daemon listens on a Unix socket at `/tmp/patchbay.sock`. The GUI talks to it over
that socket; it can start/adopt the daemon itself (see §3), so you don't have to launch
the daemon by hand.

**Headless GUI testing (for verifying widget changes without a display):**
```bash
gtk4-broadwayd :5 &
BROADWAY_DISPLAY=:5 GDK_BACKEND=broadway python your_smoke_script.py
```
Constructing `MainWindow` works under broadway and even with no daemon running (widgets
tolerate a missing daemon; the first `get_nodes` just errors). `get_width()/get_height()`
can be `0` until the window is mapped, so unit-style smoke tests should stub sizes if
they exercise geometry.

**Diagnosing UI freezes:** launch the GUI with `PATCHBAY_TRACE_HANG=1`; a watchdog dumps
all thread stacks via `faulthandler` if the main thread stalls >4s.

---

## 3. Architecture in one page

**Daemon (`main.py`)** — a single-threaded JSON command server over the Unix socket:
reads one JSON object per line, replies one object per line, in request order.
Threads: a tick thread runs `PatchSpace.supervise()` every `SUPERVISE_INTERVAL_S`
(0.5s), and `load_session` runs on its own short-lived thread.

**Graph model (`pwnodes.py`)** — `PatchSpace` owns user nodes + edges and reconciles:
1. *structure* — desired edges/sockets from user edits;
2. *health* — every backed node's real PipeWire objects are alive, restarting dead ones
   with bounded backoff.

**Node taxonomy:**
- `Node` — base, no real PW objects (regex/media-class leaves, boolean logic, splitters).
- `LiveResolvableNode` — names an external device/app and resolves to a live id.
- `TransparentNode` — pass-through (gate/exclude).
- `BackedNode` — owns real PW objects (dummies/keepalives). **All supervised.**
- `_ChainEffect` — a "sandwich" effect (see below).

Boolean control-plane nodes (gray ports, never a PipeWire link): `BooleanSourceNode`
(On/Off), `BooleanSplitterNode`, `BooleanInvertNode` ("Invert"), `BooleanAndNode`
(`boolean_and`), `BooleanOrNode` (`boolean_or`), and the boolean warps. `_resolve_boolean*`
evaluates them; AND/OR ignore unwired inputs (one wired input passes through) and emit
nothing when fully unwired.

**Effect sandwich (central idea):** every effect is
```
source -> [ in-dummy sink ] --link--> [ fx capture ] <-> DSP <-> [ fx playback ] --link--> [ out-dummy sink ] -> sink
```
User edges attach to the stable dummy sockets, never to the DSP module. A module reload
(config change or crash recovery) swaps only the interior and cannot drop a user edge.
Internal links are re-derived by name each sync, so they self-heal.

**Lazy loading:** structural pieces (dummies/keepalives) are created synchronously on
add and basically can't fail. The module (the failure-prone part) is materialised by the
supervision tick. A module failure degrades one node's interior — not the add, not the
sync, not the chain.

**Serialization:** `_SERIAL_ATTRS` (main.py) is the single list of node attributes that
`get_nodes`/`export` emit, read via `getattr(node, attr)`. `_LAYOUT_ATTRS` (x/y/anchored)
is separate so unpositioned nodes don't emit nulls.

**Panels (`panels.py`) — the file-backed container model (supersedes declarative files).**
A *panel* is a nestable box that owns nodes and child panels; the whole graph has one
`root` panel and every non-root panel is one `*.json` file, referenced by its file stem.
Panel ids are the `::`-joined path of stems from the root (`kit`, `kit::eq`); a
node/group id is `<panel-id>::<local>` (bare `<local>` at the root). Local ids may not
contain `::`; split with `rsplit("::", 1)`. A root-level node and a root-level panel may
not share a name.

*Placement* is parent-relative and lives in the **child** file (`placement: {x,y,w,h,
anchored}`), so a panel is self-describing. Node runtime coordinates stay **absolute**
in `PatchSpace`/the GUI and are authoritative: the GUI moves a panel's nodes itself and
persists their absolute positions (`set_node_layout`) alongside the panel's placement
(`set_panel_layout`); the daemon only stores the panel metadata and never derives node
positions from it (doing both moved nodes twice and made the boxes fight their contents).
`_flatten_panels` adds a panel's folded absolute origin when loading and
`_build_panels_from_space` subtracts it when writing, so only the serialization is
panel-relative. Child panels are never shifted when a parent moves - they derive their
absolute position by folding ancestors.

*Edges are owned by the least common ancestor* of the two endpoints' panels
(`panels.edge_owner`): same-panel edges live in that panel's file, cross-panel edges in
the deepest common panel, and edges between different top-level panels in the root file.
An edge is never stored twice; reparenting a node re-homes its incident edges
automatically because ownership is derived from ids.

*Load dirs* are a list (`--panel-dir PATH[:rw|:ro]`, repeatable, later shadows earlier;
`PATCHBAY_PANEL_DIR` for the default). The **root panel** is the session autosave
(`--root-panel`, default `~/.cache/patchbay/last_session.json`) — a legacy
`{nodes,edges,groups}` cache and old declarative files are migrated in place on load
(`_load_panels_tree`).

*Read-only panels* (`mode: "read-only"`) keep a frozen snapshot
(`self._readonly_snapshots`); the GUI controls stay live but `reset_panel` / reload /
restart re-apply the file's membership, positions, edges and params. `read-write` (the
default) reads and writes. The GUI draws a panel as a tinted-grid box (grid shares the
world grid's origin/spacing so it overlays it). Its title floats just above the box like a
group's title (panel colour, zoom-scaled font clamped so it stops growing past a point).
A left-drag on the title *or any empty panel background* moves the panel; on the right of
the title row are rounded-square buttons in the same outline style as the group +/-
buttons: the physics-stop (pin/pause) toggle, then a delete (trash) button on writable
panels (a Reset button on read-only ones), and the **hamburger menu** at the very edge.
The hamburger opens `Settings\u2026` (writable; rename/recolour via `edit_panel`),
`Duplicate\u2026` (asks for a name, then `clone_panel` copies the panel's *current* nodes into
a new file and reloads so the copy is live), and `Copy as JSON` / `Save to File\u2026`
(which ask the daemon for the panel's current state via `export_panel`, so they work on
read-only panels too). Delete asks whether to also delete the panel's nodes or keep them
(moved up into the parent panel; `delete_panel` with `keep_nodes`). The box **tightly
auto-fits its contents** in every direction (like a group) with `PANEL_PADDING`: member
nodes, child panels, and the outlines **and title blocks** of groups owned by the panel
(all members inside it), so group titles/edges are never clipped. It has a **square
minimum size** (`PANEL_MIN_SIDE`, centred on the content); there is no manual resize
handle. Each node living in a non-root panel also gets a tiny panel-coloured chip at its
bottom-left corner (root-level nodes get none), drawn in `on_draw` from
`_panel_of_node`/the panel's colour. A **nested panel** gets the same chip in its *parent*
panel's colour (top-level panels get none). While
a node is being dragged, its panel is held at the size it had when the drag began
(`_panel_drag_baseline`) and may only **grow** toward the node, capped at
`PANEL_DRAG_GROW` past the baseline - so picking a node up never shrinks the box, nudging
the edge makes room, and dragging well past the cap takes the node out.

*Edit mode.* A panel's parameter values (volumes, switches, effect knobs) are **not**
written back to its file by default - the daemon serves the committed file values from
`_panel_snapshots` (only layout stays live), so runtime tweaks are ephemeral until you
commit them. The pencil button in the title row sends `set_panel_edit_mode`; enabling it
first refreshes the panel to its file's values in place
(`_refresh_panel_params_from_file` re-applies params to the existing live nodes - no
node teardown/rebuild), then edits persist (`_write_panels` refreshes the snapshot for
panels in `_edit_panels`). The panel is darkened and stamped with a large "EDIT MODE"
water-mark while on. Read-only panels can't enter edit mode.

*Files vs placements.* A panel *file* (its `stem`) is the backend; a *placement* is one
loaded instance with its own `id` (its path) and file (`path`), so the same file can be
loaded in several places. A child reference is a stem string, or `{name, stem}` when the
placement name differs from the file stem (`panels.child_name/child_stem/child_ref`).
Each placement's geometry is stored in its **parent's** child reference
(`child_ref(name, stem, placement)`, `child_placement`), not in the shared panel file —
so several placements of one file each keep their own position. Placements keep
independent live settings (their own nodes), but structure, positions and (in edit mode)
parameters sync: `_canonical_by_stem` finds the placement that changed, `_write_panels`
writes the file from it, and `_sync_placements` copies positions to the others (cheap) and
syncs structural/parameter changes: a structural difference reverts the sibling
subtree, while a parameter-only difference is applied in place
(`_apply_panel_params`, no node reload) with the sibling's snapshot updated - so editing
one placement updates the others and repeated writes can't clobber the file back. `place_panel` adds a placement of a file (root by default); the
side view's **Place** button puts one in the middle of the viewport at the top level, and
rows are **draggable onto the canvas** (dropped over a panel it nests there, else root).

*Auto-load.* `auto_load` (default **false**, top-level in the file, a checkbox in the
right side view) controls whether a standalone panel placement is spawned at the root on
start-up; a nested sub-panel is always loaded as a dependency of its parent regardless of
its own flag. `list_panel_files`/`set_panel_file_autoload` drive the side view;
`list_panels`/`get_nodes` report per-placement `stem`/`auto_load`.

*Panel ports (IO).* A panel can expose inputs/outputs as lightweight **pass-through proxy
nodes** placed at its edges: `panel_in`/`panel_out` (audio, `TransparentNode` subclasses)
and `bool_panel_in`/`bool_panel_out` (boolean; `_resolve_boolean` relays them). Each panel
draws a thin bar straddling its left (inputs) and right (outputs) edges with a `+` at the
foot (`_panel_io_rects`); the `+` opens a name/type dialog and drops a port node near that
edge (`_prompt_add_port`/`_add_panel_port`), showing **only its label** (no type/id) and
sized to the label (capped at `NODE_WIDTH`). Port nodes are centered on the edge (the bar
runs through the middle of each), locked `anchored` and always pinned in the physics - no
unanchor control, no three-dots badge (right-click for the menu). Ports can be dragged
**vertically only** to reorder the stack (clamped to the box); the settings dialog has a
`description` row. The bar extends
`PANEL_IO_BAR_PAD` past the top/bottom port. `_layout_panel_ports` re-centers the stack
after every poll so ports follow the box as it grows/moves, and the panel reserves
`NODE_WIDTH/2 + PANEL_IO_MARGIN` inside each port edge (and enough height for the stack) so
ports never overlap nodes. Wire the port from outside the panel on its outer side and to
internal nodes on its inner side - ordinary edges, so LCA edge-ownership already places
them in the right file.

*Panels side view.* A docked, scrollable list of panel files lives on the right (the endchild of an outer `Gtk.Paned`), toggled by a button directly under the top-right
hamburger (`main_window._panels_toggle` / `_build_panels_view`). Each row shows the
panel's colour/label with Select and an X to remove that panel (delete moved here from
the title row).

*Physics* is hierarchical (`_hierarchical_step`, `on_layout_tick`): node physics runs
inside each panel in that panel's local frame (internal edges only). Node physics and the
auto-fit both use a panel's **direct** nodes (`_panel_direct_nodes`), not its subtree -
using the subtree double-integrated descendants (two force steps in two origin frames,
which drifted them and grew the parent box without bound) and double-counted them in the
box. **All** physics (node layout *and* panel-vs-panel) is **paused by default**
(`physics_active = False`): `_hierarchical_step` returns early, so nothing moves until the
user resumes it from the hamburger menu ("Physics") or the floating pause/resume button
under the panels toggle at the top-right (the two stay in sync via `_set_physics`). When
on, per parent,
the direct child panels repel each other in the parent's local frame (siblings only -
running every panel through one flat pass made a child fight its own parent). Panels use
a dedicated `panel_force_layout` with *size-aware* springs: an edge's rest length grows by
each box's half-extent projected onto the edge, so connected panels settle edge-to-edge
around their bounding boxes. There is **no** global centre pull (it beat `1/d^2` repulsion
past ~600px, so distant panels crept toward each other) and repulsion is **short-range**
(cutoff ~ one box), so nearby panels visibly push apart but don't drift together or fling
apart without bound; the rectangle-aware `_resolve_overlaps` pass handles the rest. The
layout re-arms whenever the
node/edge/panel set changes, so it also runs right after a panel is created or loaded
(not only after a node is dragged). Nodes never exert forces across a panel boundary
(cross-panel edges only spring the two panels together). Panel placement is *locally*
authoritative while physics/drag moves it (`_pending_panels`): a poll carries the last
flushed placement, so accepting it would snap the panel - and, since moving a panel moves
its nodes, drag every node back - each refresh. It is handed back to the daemon when the
daemon echoes exactly what was sent.

*Reparenting:* dragging a node across a boundary sends `move_nodes`; the daemon
re-qualifies its id, re-homes its edges (ownership is derived from ids) and re-points
group membership, so a selected set moved into a panel keeps its groups. Dragging a
*panel* onto another panel sends `move_panel`: the panel keeps its absolute placement,
is re-keyed under the target's path (`fx` -> `kit::fx`), and every descendant panel/node
id is re-qualified (edges rebuilt, groups re-pointed), to arbitrary depth; cycles are
refused. Refused (GUI snaps back) when the source or target panel is read-only. While a node is being dragged
it is excluded from its panel's auto-grown bounds, so the source panel doesn't stretch
under the cursor and steal the drop. After the rename the GUI immediately re-sends the
moved nodes' positions under their *new* ids (`_reparent_after_drag`), because the
debounced layout save still holds the old ids and would be ignored - otherwise the nodes
bounce back to where they were picked up. The drop target is resolved from the *drag-time*
geometry (baseline + capped growth) while the drag state is still active; after it's
cleared every panel re-fits to include the dropped node, so the source would always win
and a node could never be dragged out. Groups land in the panel that is the LCA of
their members.

*Creating a panel* (`Create Panel…`): with a selection, the new panel is fitted around
the selected nodes' bounds and **nested into the panel those nodes live in** (the GUI sends
their panel as `parent_id`, or root for a mixed/none selection); with none, a square opens
in the middle of the viewport. The daemon takes the placement (`x/y/w/h`, converted to
parent-relative) in `create_panel`.

Commands: `list_panels`, `reload_panels`, `create_panel`, `delete_panel`, `edit_panel`,
`export_panel`, `clone_panel`, `list_panel_files`, `set_panel_file_autoload`,
`place_panel`, `move_panel`, `set_panel_layout`, `move_nodes`, `reset_panel`,
`set_panel_edit_mode`; `get_nodes` carries `panels` and per-node provenance is the id
prefix.

**Canvas selection.** Selection is a plain `set` of node ids. A plain left press on a node
selects it (replacing the selection unless it is already part of a multi-selection, so a
whole selection drags together); right-drag marquees replace the selection. Shift/Ctrl +
left-drag is a modifier marquee (`_marquee_mode`): Shift unions the swept nodes in, Ctrl
subtracts them, re-derived each update from `_marquee_base`; a Shift/Ctrl *click* (no sweep)
toggles the node under the pointer. Shift/Ctrl + right-click on a node does the same
(add/remove) without opening the context menu, and Shift/Ctrl + right-drag is a modifier
marquee (Shift adds the swept nodes, Ctrl removes them; a bare right-drag replaces).
Right-clicking **while a left node-drag is in progress** cancels it: the dragged nodes
snap back to where they were picked up and the click is swallowed (no menu/marquee). A
floating circular Delete button sits at the canvas's bottom-right whenever there is a
selection and confirms (Gtk.AlertDialog) before removing the selected nodes.

**Session load is asynchronous.** `_cmd_load_session` starts a background thread and
immediately returns `{"status": "ok", "started": true}`. There is **no completion
reply**; the GUI's periodic `get_nodes` poll observes nodes/edges landing. The GUI infers
"load done" from node `ready`/`health`.

**Incremental load reveal (GUI).** While the loading overlay is up, the widget tracks a
`_revealed` set (None = show everything); each poll it adds nodes whose `ready`/`health`
have landed, draws only revealed nodes/edges, and calls `zoom_to_fit()` (which frames only
the revealed set) so the graph assembles live and stays centred. `_begin_load` resets the
set, `_set_loading(False)` clears it to None and does the final fit.

**Node readiness surfaced to the GUI:** `ready` (bool) and `health` (`"ok"` /
`"starting"` / `"dead"`). `"dead"` means failed/timed out; `"starting"` means still
coming up. Do not treat a `"dead"` node as "still loading" (would hang a spinner).

**Daemon lifecycle / ownership (`gui/daemon_control.py`).** The GUI's rule is: if a
daemon is already answering on the socket, adopt it and never touch its lifetime; if
none is running, start one and treat it as owned. `DaemonManager.shutdown_owned()` (called
on window close) stops the daemon only when owned. The hamburger menu's Start/Stop/Restart
Daemon actions run off the GTK thread (starting/stopping waits on the socket). The daemon
exposes a `shutdown` command that flips `_running` and replies *before* teardown, so the
caller can wait for a graceful exit.

The GUI also *retries*: `_poll_daemon_connection` keeps a `_daemon_should_run` flag (True
unless the user explicitly hit Stop, cleared on close) and re-attempts `start()` at most
every `DAEMON_RETRY_INTERVAL_S` while the socket is unreachable, off the GTK thread. A
one-shot `ensure_started()` at launch would otherwise leave the GUI stuck disconnected if
the daemon happened to be mid-startup or died. On the daemon side, `_socket_is_live()`
guards `_socket_server`: if a live daemon already answers on `SOCKET_PATH`, a second
instance refuses to start rather than unlinking and stealing the first one's socket (which
leaves the victim running but unreachable) — only a stale socket file is reaped.

**Process teardown is batched.** `PatchSpace.detach_nodes()` removes nodes from the model
and hands back all their backings; `PatchBayDaemon._teardown_public_graph()` / `_cmd_rebuild`
destroy them all in parallel, outside the lock, so a graph full of effects costs the single
slowest process rather than the sum (and the tick/GUI stay responsive).

**GUI:** one `Gtk.Notebook` with the raw PipeWire graph and the PatchSpace editor. It
shares one `PatchBayClient` connection; `MainWindow.process_responses` (50ms timer)
routes replies. The `LogConsole` deliberately owns a **separate** connection so a burst
of graph refreshes can't consume/miss its replies. The PatchSpace canvas polls
`get_nodes` every `REFRESH_INTERVAL_MS` (400ms).

---

## 4. Conventions & process that work here

### Imports / running
- Daemon modules (`src/`) use absolute imports and are run from `src/`.
- GUI modules use **flat imports** (`from constants import ...`, `from node_specs import ...`)
  and must be run from `src/gui/` or with that dir on `sys.path`. There is no
  `gui/__init__.py`; tests reach it via `from gui import node_specs` (namespace package).
- So: a change to a GUI module can't be exercised by the daemon test suite unless it's
  pure logic.

### Code comment style (important)
This codebase intentionally uses **long rationale comments** describing failure modes and
rejected designs, e.g. `SensitivityGateNode`'s docstring lists every previous design that
failed and why. When you make a non-obvious fix, document *why* and *what not to retry*
in the same style. Do not strip these comments. (Note: the generic "never add comments"
rule does **not** apply to this repo's established style.)

### Optimistic GUI updates (important pattern)
The GUI applies user actions locally *before* the daemon echoes them, and each poll
(`update_from_daemon`) would otherwise clobber the optimistic value with the daemon's
stale, pre-command copy — the "it flashes back for a second" / "changes don't stick"
class of bug. There is a pending-guard for each of these; follow the pattern when adding
a new interactive control:
- effect sliders → `_pending_effect_slider` + `_accept_effect_slider_echo()`;
- boolean toggles (`enabled` / `output`) → `_pending_bool` + `_accept_bool_echo()`;
- groups → `_pending_groups` (checked in the group-sync block of `update_from_daemon`).
Each keeps the local value until the daemon reports the same value back, then hands
control back to the daemon.

### Packaging changes (flake-parts)
`flake.nix` defines `packages.patchbay` (GUI) and `packages.patchbay-daemon`, plus
`apps.default`/`apps.patchbay`/`apps.daemon`. The GUI package must use
`wrapGAppsHook4` so GTK/Adwaita typelibs and GSettings schemas land in the wrapper;
`PATCHBAY_DAEMON` is what lets the packaged GUI find the daemon binary. There is no
`.gitignore`, so `nix build` on a dirty tree is normal.

### Adding/changing a node property (checklist)
A new tunable/serialized attribute usually touches all of these:
1. `pwnodes.py` — the node class: default constant, bounds, clamp in `__init__`, and use
   it in `_module_command_args()` (for load-time filter-graph controls).
2. `main.py`:
   - add the name to `_SERIAL_ATTRS`;
   - pass it through in `_create_node()`;
   - handle it in the big `set_node_property` handler (clamp + validate), and call
     `self._coalesce_reload(node)` if it's a load-time control.
3. `gui/node_specs.py` — add a row to the node type's `NodeSpec.settings`
   (`bool` / `number` / `choice` / `text`).
4. `tests/` — add a regression test (round-trip serialization + set behavior + bounds).
5. Run `python -m pytest -q`.

### Adding a node type
- Define the class in `pwnodes.py`.
- Register it in `NODE_TYPE_REGISTRY` in `main.py` **and** keep `CLASS_TO_TYPE` (derived)
  in sync; add a `_create_node()` branch if it takes constructor args.
- Add a `NodeSpec` in `gui/node_specs.py` (ports, `control=`, settings, `socket_labels=`)
  and menu/category/icon entries. `gui/node_specs.py` is the single source of truth for
  node UI. Pure control-plane nodes are `boolean_inputs`/`boolean_outputs` and carry no
  backing.
- Effects with async multi-stream modules (`EchoCancelNode`, `NoiseCancelNode`,
  `SensitivityGateNode`, `NormalizeNode`, `ReverbNode`) must be in `_CAREFUL_NODE_TYPES`
  so session load gives them a dedicated per-input bring-up pass.

### Testing
- Run from `src/`; `python -m pytest -q`.
- Daemon tests construct `PatchBayDaemon()` directly (no subprocess starts until
  `start()`), then drive `d.handle_command({...})`.
- To avoid real PipeWire, monkeypatch `pwnodes.OwnedPwNode` / `pwnodes.OwnedPwProcess`
  with fakes (see `tests/test_sensitivity_hidden.py::FakeCli/FakeProc`).
- `tests/test_daemon_protocol.py` uses only leaf (non-backed) node types.
- Prefer a small, targeted regression test for every bug fix — the existing suite is
  fast and mostly regression tests.

### Validate/test cycle (the loop that works well here)
This is the cycle to follow for basically any change:
1. **Read the real code first** (grep/read the exact functions) - don't guess at names or
   behavior. Most regressions came from a wrong assumption about an existing path.
2. **Reproduce the bug / pin the behavior in a scratch script** before editing: a tiny
   `PYTHONPATH=. nix develop -c python` script that builds a `PatchBayDaemon()` and drives
   `handle_command`/`_write_panels` directly. Run it again after the fix to confirm.
3. **Make the smallest change** that fixes it, in the established style.
4. **Syntax gate**: `python -m py_compile <changed files>` (from the dev shell).
5. **Targeted tests first**, then the whole fast suite - both from `src/`:
   `nix develop -c python -m pytest -q`. Add a regression test for the fix (daemon
   round-trip / bounds / no-rebuild identity checks are cheap and catch most of these).
6. **Pure GUI logic** (geometry, rect math, width/height) can't be reached by the daemon
   suite: exercise it with a small stub object and call the class method directly
   (`PatchSpaceGraphWidget._some_method(dummy, ...)`), or under broadway. Verify the
   numbers, not just that it runs.
7. **Update a test whose contract legitimately changed** (e.g. `set_panel_layout` now
   moves nodes) instead of deleting it - the rename/adjustment is part of the change.
8. **Commit focused chunks** with a short why-first message; stage explicit source paths
   (never `git add -A`). Re-run the suite right before committing.
9. **Keep a visible todo list.** For anything with more than a couple of steps, write the
   steps down (the `todowrite`/task list) *before* starting, mark the one you're on
   `in_progress`, and update it as you go - don't batch completions. The user has
   explicitly asked for more, smaller todo items: break work into concrete, verifiable
   units (research X, reproduce Y, fix Z, add test for Z, run suite, commit) rather than
   one vague item. This is the single best guard against silently dropping a requested
   sub-task when several arrive at once.

The value is in step 2 + 5: reproduce, fix, regression-test, full suite. It has caught
several "looks done but isn't" cases (double-moves, sync clobbers, placement drift).

### Verify like a user
Tests don't cover live PipeWire behavior. For anything touching real audio/graph,
**restart daemon + GUI and test with a real session** (or use the broadway smoke
approach for pure GUI behavior). "It passed pytest" is not proof an effect works.

### Git
- Only commit when explicitly asked.
- `__pycache__` pyc files are tracked (no `.gitignore`) — stage only source files with
  explicit paths, never `git add -A`. Running pytest dirties tracked `.pyc`; restore them
  (`git checkout -- '*/__pycache__'`) before switching branches.
- The only branch is `master` (the feature branches were merged and deleted). Commit in
  feature/stability chunks.

---

## 5. Hard-won rules / traps (read before debugging effects)

1. **Plugin discovery uses the *daemon's* environment, not the PipeWire server's.** The
   `pw-cli` process the daemon spawns inherits the daemon's `LADSPA_PATH`/`LV2_PATH`. The
   flake devShell and the `patchbay-daemon` wrapper export these. LADSPA plugins are also
   probed by absolute `.so` path in `pwnodes.py`; LV2 is found **by URI** (no path
   probing), so the bundle must be on `LV2_PATH`. Run the daemon from `nix develop` or via
   `nix run .#daemon` (or export the paths yourself).

2. **Live `set_param` through the daemon's persistent pw-cli session is unreliable.**
   A *fresh* interactive `pw-cli` can move controls, but `OwnedPwNode.set_param` on the
   session that loaded the module often doesn't take effect. Therefore many controls are
   treated as **load-time**: changing them schedules a debounced **interior-only reload**
   via `main.py::_coalesce_reload`. This applies to the sensitivity gate's
   `threshold`/`level`/`sensitivity` and tuning, and `ReverbNode.wet_dry`. Do not "fix"
   these by switching to live `set_param`. (See `SensitivityGateNode` docstring item 2.)

3. **Do not re-introduce the sensitivity pre-gain stage.** The hidden pre/post
   `VolumeProcessNode`s around a sensitivity gate are now **unity pass-throughs only**,
   kept so edge routing/serialization/saved sessions don't change. A null-sink monitor
   volume feeding a `_ChainEffect` capture stream does **not** actually change the module
   input level (measured), so gain-staging a fixed threshold made sensitivity invert
   loudness. See `SensitivityGateNode` docstring item 1.

4. **Sensitivity semantics:** `sensitivity` (0..1, the inline slider) is the single source
   of truth; `level` (0..100) is derived as `(1-sensitivity)*100`. The threshold maps to
   −45 dB (most sensitive) … −15 dB (least) as a *linear* Calf port value. `range_db`
   defaults to −96 dB so a closed gate is silent (Calf's own default is only −24 dB and
   bleeds). Release default is 2000 ms. `level`/`sensitivity` are inconsistent defaults
   historically — the constructor derives one from the other; keep that invariant.

5. **Hidden nodes must be staged during session load.** `_load_session` holds the daemon
   lock while creating nodes, so the graph's node-created callbacks can't resolve hidden
   pre/post nodes before `RESOLVE_GRACE_S`; a mid-load supervision tick would judge them
   stuck and tear them down/rebuild (the "live object disappeared while alive" thrash).
   `_load_session` stages every backed node until its own bring-up turn. Regression test:
   `test_load_stages_hidden_nodes_so_a_mid_load_tick_cannot_prune_them`. If that thrash
   reappears, look at staging, not the gate.

6. **Reap before recreating same-named backings.** `_load_session` calls
   `graph.reap_stale_for_names(...)` (outside the lock, waiting for the graph to drop the
   objects) before creating replacements, otherwise PipeWire can reuse a freed node id
   and a lagging removal tears down the new backing. Only reap names the load will
   actually *create*; existing nodes keep their live objects.

7. **Effects are sandwiches; edges attach to dummies.** Never attach user edges directly
   to a module's capture/playback streams. The interior is reloadable; the dummies are
   stable.

8. **Finicky nodes get a care pass.** `_CAREFUL_NODE_TYPES` are brought up one at a time
   during load and their edges wired one input at a time, each confirmed live. Adding a
   new async-module effect means adding it here, or session import can leave it
   structurally present but acoustically dead.

9. **The GUI infers load completion; don't add a synchronous multi-second reply.** The
   socket protocol is strictly one-in-flight; a blocking `load_session` reply would stall
   every other GUI command. Keep it threaded + `started: true`. The daemon's `shutdown`
   command follows the same rule: reply first, then tear down.

10. **Tracked `__pycache__`.** See §4.

11. **Default promotion must retry, not latch optimistically.** `_assert_defaults` may
    resolve the built-in sink a tick or two before `wpctl set-default` will accept it.
    Record `_set_default_sink_id`/`_set_default_source_id` **only after** `_set_default`
    returns success, otherwise one transient failure means PatchBay never becomes the
    default while everything looks fine (the sink exists and routes). `_set_default`
    returns bool for exactly this. Each builtin also has a `force_default` flag (mirrored
    from the Speaker/Mic Line node's force button, default on): while on, `_assert_defaults`
    re-reads the live default (`wpctl inspect @DEFAULT_AUDIO_SINK@/SOURCE`) every
    `DEFAULT_CHECK_INTERVAL_S` and puts it back on the builtin if something moved it; while
    off, the old once-per-resolved-id behaviour applies. `_default_check_at` throttles the
    check so we don't shell out to wpctl every tick.

12. **Drains are `pw-cat --record` keepalives whose target semantics are subtle.** The
    `--target <sink>` on a record stream does not reliably link to a dummy; internal-link
    wiring and the exact `--target`/autoconnect behaviour have been the source of several
    "it worked one wiring order, not the other" bugs. Before touching
    `_ChainEffect.internal_links()`, `_ensure_drain()`, or the effect sandwich, read the
    relevant docstrings and test on live PipeWire with both wiring orders.

13. **`LightNoiseCancelNode` is a plain `EchoCancelNode` with the `probe` hidden.**
    It is NOT a separate engine and it does NOT need a daemon-wired reference: it worked
    before with the probe dummy fed only by its silence keepalive, like the base class.
    An earlier attempt to "fix" it (a hidden auto-reference edge from the built-in sink,
    plus `_build_export_config`/`_serialize_edges` filters to hide that edge) broke audio
    across the whole graph — do not re-introduce it. Keep the class as a bare subclass and
    let the user wire (or not) the hidden probe via the module's own plumbing.

14. **Internal-link ids are node-id-derived; preserve them across a rename.** `sync_locked`
    keys an effect's interior as `__internal__:<node_id>:<i>` (`_ChainEffect`).
    `PatchSpace.rename_node` MUST re-key those in `_edge_links` (and the matching
    `_inflight_links` values) or the next `sync` disconnects the whole capture/playback
    sandwich and re-makes it — a window where the out side has no consumer, which stalls
    RNNoise (see `NoiseCancelNode`'s docstring; the live declarative move renames nodes).

15. **Standardize per-node setup; don't special-case the declarative path.** Two shared
    helpers own the per-node fixups:
    - `PatchBayDaemon._apply_node_config(node, config)` — re-adopting an existing node from
      a config (params with per-type guards, `_adopt_line_volume`, volumes through setters,
      `SensitivityGateNode.set_level`, `apply_device_settings`). Used by **both**
      `_cmd_add_node` and `_load_session`; a raw `setattr` loop in `_load_session` was missing
      the volume/level handling the add path had.
    - `PatchBayDaemon._standardize_nodes(node_ids)` — post-ownership-change setup
      (sensitivity internals, `_try_immediate_resolve`, device settings, live refresh) plus
      the staged `_careful_bring_up` for `_CAREFUL_NODE_TYPES`. The live declarative move
      (`_apply_declarative_membership`) calls it after the rename; a bare `supervise()` there
      was what left moved finicky effects half-wired.
    If you add a per-node fix, put it in one of these (and/or `_create_node`) so
    add/load/move all get it. There is deliberately **no** ad-hoc tick-level effect healer
    anymore — do not re-add `_repair_effect_interiors`; route the fix through the standard
    paths.

---

## 6. GUI specifics

- **`node_specs.py` is the UI source of truth.** `NodeSpec(label, inputs, outputs,
  control=..., field=..., settings=[...], boolean_inputs/outputs=..., socket_labels=...)`.
  - `control` ∈ `None | "volume" | "gate" | "boolean" | "fallback_onoff" | "wetdry" |
    "sensitivity" | "gain"` selects the inline control drawn on the node body. Boolean
    source nodes use `"boolean"`; gate/switcher fallback on/off uses `"fallback_onoff"`
    (read-only white when a ctrl signal is wired).
  - `socket_labels=False` suppresses per-port name labels on the symmetric boolean gates.
  - `settings` rows are `(attr, label, kind[, extra])` with kind `bool|number|choice|text`;
    `number` extra is `{min,max,step}`.
- **Groups merge by local id.** Canvas groups whose id shares a local part (`grp` and
  `file::grp`, or the same id in two declarative files) render as **one** outline with the
  contributing groups' titles **stacked** (each in its own colour), via `_merged_groups()` /
  `_group_merge_key()` — all group geometry, headers, hit-testing and the +/- membership
  buttons iterate the merged view. The colour chip, the `+`/`-` buttons and a settings
  hamburger sit on the **right edge** of the group's box (hamburger rightmost; the
  hamburger and clicking the title both open `show_group_settings_dialog`). Exact-member-set
  duplicates are already removed daemon-side
  (`_reconcile_declarative_duplicates`), so normally only genuinely-different same-id groups
  stack. The `+`/`-` buttons fan the change out to every group in the merge
  (`_merged_member_gids`).
- **Compact nodes.** `_COMPACT_NODE_TYPES` (splitter + the boolean gates) render as a
  small square when unlabelled; the gates keep their type name at the top so they stay
  distinguishable, and the splitter shows only its (optional) label. The node id line is
  ellipsized (never wrapped) — see `_header_block_is_id`.
- **Group headers** can overlap when groups share members without one enclosing the other.
  `_all_group_header_layouts()` stacks colliding headers; `_encloses()` has a small
  tolerance so a group that contains another to within a few px is treated as its
  container (and its box expands around the inner group + its header).
- **Timers:** `REFRESH_INTERVAL_MS` (get_nodes poll), `POST_MUTATION_REFRESH_MS`,
  `LAYOUT_TICK_MS` (physics), `POLL_RESPONSES_MS` (MainWindow response drain),
  `LOG_POLL_MS` (console).
- **Loading UX:** a session import or rebuild triggers `_begin_load()`; the loading
  overlay/console are driven by `_update_loading_state`, which treats an *empty* graph as
  still-loading (a rebuild's teardown empties it before staging back in) and clears once
  nodes land and none are `starting`. On finish the fit is **deferred** because the
  console collapse changes the canvas height.
- **Auto-recenter on startup:** `_needs_initial_fit` frames the graph the first time it is
  drawn with nodes and a real allocation; deferred (`_schedule_fit`) so the page/toolbar/
  console have laid out. The hamburger menu's Rebuild Graph is the "turn it off and on
  again" recovery action (daemon `rebuild` command).
- **Pan/zoom** lives in `view_mixin.py`; both graph widgets share it. `to_world()` divides
  by `self.zoom`, so keep `ZOOM_MIN > 0`.
- **`update_from_daemon`** is the per-poll entry point; node dicts are built there with
  cached UI fields (`meta` holds the raw daemon data). Drag guards (`slider_dragging`) and
  the pending-guards above prevent a poll from yanking a control the user just changed.
- **Daemon controls** live in the top-right hamburger menu (`_build_patchspace_page`):
  Export / Import / Declarative Nodes… / Reload Declarative Files / Rebuild Graph /
  Start·Stop·Restart Daemon. The bottom toolbar has Logs / Recenter / Group / Anchor /
  Declare…. "Declare…" exports the selection to the read-write declarative directory.

---

## 7. When to stop and ask

This repo has genuine ambiguity in UI intent (e.g. "vertically center as well" could mean
toolbar alignment *or* fit centering). If a request is ambiguous and the fix could go two
very different ways, ask a short clarifying question rather than guessing. For mechanical
or well-scoped changes (add a property, fix a bound, add a button), proceed and verify.

Also: do not commit unless asked, don't add a `.gitignore`/untrack code as a side effect,
and do not "simplify" the rationale comments or the effect-sandwich/staging machinery
without understanding the failure modes recorded in §5.
