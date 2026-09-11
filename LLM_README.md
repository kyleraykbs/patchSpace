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
    ├── declarative.py     # declarative node files: discovery, namespacing, load/write
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

**Declarative node files (`declarative.py`).** The daemon owns two watched directories — a
read-only one (a Nix store path, never written) and a read-write one — settable with
`--declarative-ro/--declarative-rw` (or `PATCHBAY_DECLARATIVE_RO/RW`). Each `*.json` file
wraps the exported-session shape in a metadata layer — `{"label", "color", "readonly"?,
"config": {nodes, edges, groups}}` (bare legacy configs are still read, defaulting label to
the stem) — and is a *source of truth*:
the daemon loads every file at start-up (`_load_startup_sessions`, run on a background
thread *after* the socket is listening so a slow effect-heavy session can't make the GUI
look disconnected; `get_nodes` reports a `loading` flag so the GUI raises its overlay for
that self-issued load — the flag is a counter (`_begin/_end_heavy_load`) covering startup,
every `reload_declarative`, and `rebuild`, so a rebuild that nests a reload can't clear it
early; the GUI *also* raises the overlay optimistically when it issues a declarative
command, because a synchronous command blocks that connection's `get_nodes` polls so it
would never observe the flag). On start-up `start()` opens the counter *before* binding the
socket, binds the socket immediately, then runs the orphan sweep; that way the overlay
covers the slow crash-recovery destruction of leftover helper processes too, not just the
session load (the startup-load thread owns closing the counter). Do not move the socket
bind after the sweep — then the GUI would just show "not connected" through the whole
cleanup. On `rebuild`
(`_cmd_rebuild` reloads them instead of replaying in-memory copies), and whenever a file is
added/edited/removed (the tick's `_poll_declarative`, mtime-based, debounced by
`DECLARATIVE_POLL_S`). Node ids are namespaced `<file-stem>::<local-id>`, so files can't
collide and provenance is visible; `declarative.py:namespaced()` does the qualification,
including edge endpoints (same-file ids only) and group members/ids. Nodes and edges carry a
`declarative` flag (reported by `get_nodes`, **not** written into exported params).
Declarative nodes/edges/groups are excluded from the imperative autosave
(`_build_export_config(imperative_only=True)`), so a deleted declarative node cannot be
resurrected from the cache. Edits to a declarative node are *supposed* to be lost on reload;
imperative edges that merely touch one are preserved by `reload_declarative` (snapshot
imperative edges, drop declarative state, re-apply files, re-add the edges).
**"Declare" is a move, not a copy, and it is *live*.** A file's node `foo` loads as
`<stem>::foo`, and the daemon treats an imperative node literally named `foo` as the *same*
node (`_declarative_duplicate_map`): after a declare/export reload it deletes those
imperative originals and re-points the edges that reached outside the selection at the
declarative copies (`_reconcile_declarative_duplicates`, also run at start-up so caches
written by older builds self-heal). This is what stops a declare from silently doubling a
chain. Create/add/remove do **not** call `reload_declarative`; `_apply_declarative_membership`
diff's the file's desired member set against the current owners and only renames/re-tags
the nodes that actually changed (via `space.rename_node`, which rebuilds their incident
edges; edge `declarative` flags are then flipped in place
(`space.set_edge_declarative`, no unlink/relink blip) to "both ends declarative"; groups re-pointed;
`_rename_owned_node` also renames a Sensitivity gate's hidden pre/post companions). Every
other declarative node keeps running, so no effect modules are torn down for a one-node
add. Any conflict falls back to a full `reload_declarative`. `edit_declarative` only
reloads when it renames; a label/colour tweak just updates `_declarative_meta`.
**Trap:** every writer of a declarative file (`_declarative_write`, `_cmd_edit_declarative`)
must refresh `_declarative_mtimes` after `write_file`. Otherwise the tick's mtime watcher
(`_poll_declarative`) treats our own write as an external change and fires a *full*
`reload_declarative` ~1.5s later — undoing the live move and tearing down every declared
effect (audio dropout, loading overlay on every declare). This is the "loading unstable /
noise suppression kills audio" regression.
Groups a file takes over move with their nodes (members re-pointed). Declaring captures
the selected nodes' canvas groups (label/colour/membership) into the file, installs them
live as declarative groups, and drops any imperative group whose member set exactly matches
a declarative one (the file owns that grouping, whatever its id/label —
`_declarative_groups_for`, `_apply_declarative_membership`, `_reconcile_declarative_duplicates`).
Declare dialog: target dropdown + label/colour, and **Update** merges the selection (and its
group properties) into the target; Replace overwrites; Remove detaches. `get_nodes` tags each declared node with
`declarative_label`/`declarative_color` (looked up in `_declarative_meta`) so the GUI can
draw a semi-transparent coloured label bubble hung *just below* the node, centred, in its
own draw pass after every node body (paint-only, no `find_*_at` hit-tester, so clicks pass
through to the canvas). Commands:
`list_declarative` (returns per-file label/color/readonly/writable + nodes),
`export_declarative` with `mode` = `create`/`replace`/`add`/`remove` (create needs
`name`+`label`+`color`+`overwrite`; add/remove edit an existing writable file, refusing
read-only ones; add/remove is only allowed in the RW dir or for files not marked
`readonly`), `edit_declarative` (change label/color and optionally rename),
`rename_declarative` (renaming re-prefixes the whole file), `delete_declarative`,
`reload_declarative`. GUI: hamburger (top-right) → "Declarative Nodes…" lists files
(coloured swatch + Select/Edit/Delete, plus "Select Group…"), and "Declare…" in the
selection toolbar opens a target dropdown (existing writable file or New group…) with
Add / Remove / Create·Replace, each with a `ColorPicker`. Reload re-applies. The old
"Import Last Session" button is gone — the imperative cache is now auto-loaded at startup
(declarative files first so cross-references resolve in either direction, with a second
idempotent pass for edges that needed the other half).

**Canvas selection.** Selection is a plain `set` of node ids. A plain left press on a node
selects it (replacing the selection unless it is already part of a multi-selection, so a
whole selection drags together); right-drag marquees replace the selection. Shift/Ctrl +
left-drag is a modifier marquee (`_marquee_mode`): Shift unions the swept nodes in, Ctrl
subtracts them, re-derived each update from `_marquee_base`; a Shift/Ctrl *click* (no sweep)
toggles the node under the pointer.

**Session load is asynchronous.** `_cmd_load_session` starts a background thread and
immediately returns `{"status": "ok", "started": true}`. There is **no completion
reply**; the GUI's periodic `get_nodes` poll observes nodes/edges landing. The GUI infers
"load done" from node `ready`/`health`.

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
  buttons iterate the merged view. Exact-member-set duplicates are already removed daemon-side
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
