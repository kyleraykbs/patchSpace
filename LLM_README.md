# LLM_README — working notes for AI models on PatchBay

This file is for future AI agents (and humans) working in this repo. It captures the
architecture, the process that works here, and — most importantly — the hard-won
"do not try again" lessons that are otherwise buried in code comments.

Read this before making changes. Then read the relevant module docstrings; this
codebase deliberately documents *why* and *what failed before*, not just *what*.

---

## 0. TL;DR

- **Work in `src-rewrite/`.** `src/` is the legacy original implementation; ignore it
  unless the user explicitly points at it or asks about history.
- **Test command:** from `src-rewrite/`, run `python -m pytest -q` (currently **97
  passing, ~2s**). Tests are headless and do not touch real PipeWire.
- **Daemon:** from `src-rewrite/`, `python main.py`.
- **GUI:** from `src-rewrite/gui/`, `python patchbay_gui.py` (flat imports — see §4).
- **Dev shell:** `nix develop` (from repo root) provides GTK4, libadwaita, PipeWire,
  and exports `LADSPA_PATH` / `LV2_PATH` for the DSP plugins.
- **After changing daemon code, the running daemon/GUI must be restarted** to pick it
  up. Tests do not catch live-graph behavior.

---

## 1. Repo layout

```
/home/kyle/projects/patch
├── flake.nix              # devShell: python + gtk4/adwaita/pipewire + DSP plugins
├── src/                   # LEGACY original implementation (do not edit)
└── src-rewrite/           # CURRENT implementation
    ├── main.py            # daemon: socket API, session load, node factory, wiring
    ├── pwnodes.py         # node graph model: PatchSpace, all Node classes, effect sandwich
    ├── pwgraph.py         # live PipeWire graph model (pw-dump driven)
    ├── pwproc.py          # OwnedPwNode / OwnedPwProcess (pw-cli/pw-dump subprocesses)
    ├── pwmatch.py         # matching helpers for external nodes
    ├── patchbay_cli.py    # synchronous one-shot socket client (CLI tools)
    ├── apply_config.py    # CLI: apply an exported config
    ├── export_config.py   # CLI: export current config
    ├── tests/             # pytest suite (headless)
    └── gui/               # GTK4 client (flat imports; no __init__.py — namespace pkg)
        ├── patchbay_gui.py        # GUI entrypoint
        ├── main_window.py         # MainWindow, LogConsole, toolbar, loading overlay
        ├── patchspace_widget.py   # the editable node canvas (big file)
        ├── node_specs.py          # NodeSpec table: ports, inline control, Settings rows
        ├── view_mixin.py          # pan/zoom/undo shared by both graph widgets
        ├── pipewire_widget.py     # raw PipeWire graph view
        ├── socket_client.py       # async PatchBayClient (background thread)
        ├── render_utils.py        # cairo drawing helpers, theme palette
        ├── force_layout.py        # physics layout for nodes
        └── constants.py           # shared timing/zoom/size constants
```

Note: there is **no `.gitignore` and `__pycache__/*.pyc` files are tracked**. Ignore the
`.pyc` churn in `git status`; stage only source files.

---

## 2. Running it

```bash
# from repo root
nix develop            # provides python, gtk4, adwaita, pipewire, LADSPA_PATH, LV2_PATH

# terminal 1 — daemon (User will handle this don't touch/send)
cd src-rewrite && python main.py

# terminal 2 — GUI (must run from the gui/ dir; imports are flat) (User will handle this don't touch/send)
cd src-rewrite/gui && python patchbay_gui.py

# tests
cd src-rewrite && python -m pytest -q
```

The daemon listens on a Unix socket at `/tmp/patchbay.sock`. The GUI needs a running
daemon; the daemon does **not** start the GUI.

**Headless GUI testing (for verifying widget changes without a display):**
```bash
gtk4-broadwayd :5 &
BROADWAY_DISPLAY=:5 GDK_BACKEND=broadway python your_smoke_script.py
```
Constructing `MainWindow` works under broadway; note `get_width()/get_height()` can be
`0` until the window is actually mapped, so unit-style smoke tests should stub sizes if
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
- `Node` — base, no real PW objects (e.g. regex/media-class leaves, splitters).
- `LiveResolvableNode` — names an external device/app and resolves to a live id.
- `TransparentNode` — pass-through (gate/exclude).
- `BackedNode` — owns real PW objects (dummies/keepalives). **All supervised.**
- `_ChainEffect` — a "sandwich" effect (see below).

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

**Session load is asynchronous.** `_cmd_load_session` starts a background thread and
immediately returns `{"status": "ok", "started": true}`. There is **no completion
reply**; the GUI's periodic `get_nodes` poll observes nodes/edges landing. The GUI infers
"load done" from node `ready`/`health`.

**Node readiness surfaced to the GUI:** `ready` (bool) and `health` (`"ok"` /
`"starting"` / `"dead"`). `"dead"` means failed/timed out; `"starting"` means still
coming up. Do not treat a `"dead"` node as "still loading" (would hang a spinner).

**GUI:** one `Gtk.Notebook` with the raw PipeWire graph and the PatchSpace editor. It
shares one `PatchBayClient` connection; `MainWindow.process_responses` (50ms timer)
routes replies. The `LogConsole` deliberately owns a **separate** connection so a burst
of graph refreshes can't consume/miss its replies. The PatchSpace canvas polls
`get_nodes` every `REFRESH_INTERVAL_MS` (400ms).

---

## 4. Conventions & process that work here

### Imports / running
- Daemon modules (`src-rewrite/`) use absolute imports and are run from `src-rewrite/`.
- GUI modules use **flat imports** (`from constants import ...`, `from node_specs import ...`)
  and must be run from `src-rewrite/gui/` or with that dir on `sys.path`. There is no
  `gui/__init__.py`; tests reach it via `from gui import node_specs` (namespace package).
- So: a change to a GUI module can't be exercised by the daemon test suite unless it's
  pure logic.

### Code comment style (important)
This codebase intentionally uses **long rationale comments** describing failure modes and
rejected designs, e.g. `SensitivityGateNode`'s docstring lists every previous design that
failed and why. When you make a non-obvious fix, document *why* and *what not to retry*
in the same style. Do not strip these comments. (Note: the generic "never add comments"
rule does **not** apply to this repo's established style.)

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
  in sync.
- Add a `NodeSpec` in `gui/node_specs.py` (ports, `control=`, settings) and menu/category
  entries. `gui/node_specs.py` is the single source of truth for node UI.
- Effects with async multi-stream modules (`EchoCancelNode`, `NoiseCancelNode`,
  `SensitivityGateNode`, `NormalizeNode`, `ReverbNode`) must be in `_CAREFUL_NODE_TYPES`
  so session load gives them a dedicated per-input bring-up pass.

### Testing
- Run from `src-rewrite/`; `python -m pytest -q`.
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
  explicit paths, never `git add -A`.
- Branch in use: `stable-rollback`. Recent commits are feature/stability chunks.

---

## 5. Hard-won rules / traps (read before debugging effects)

1. **Plugin discovery uses the *daemon's* environment, not the PipeWire server's.** The
   `pw-cli` process the daemon spawns inherits the daemon's `LADSPA_PATH`/`LV2_PATH`. The
   flake devShell exports these. LADSPA plugins are also probed by absolute `.so` path in
   `pwnodes.py`; LV2 is found **by URI** (no path probing), so the bundle must be on
   `LV2_PATH`. Run the daemon from `nix develop` (or export the paths yourself).

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
   every other GUI command. Keep it threaded + `started: true`.

10. **Tracked `__pycache__`.** See §4.

---

## 6. GUI specifics

- **`node_specs.py` is the UI source of truth.** `NodeSpec(label, inputs, outputs,
  control=..., field=..., settings=[...])`.
  - `control` ∈ `None | "volume" | "gate" | "boolean" | "fallback_onoff" |
    "wetdry" | "sensitivity" | "gain"` selects the inline control drawn on the node body
    (`"gate"` is the legacy on/off toggle case still handled by the widget; boolean-node
    on/off uses `"fallback_onoff"`).
  - `settings` rows are `(attr, label, kind[, extra])` with kind `bool|number|choice|text`;
    `number` extra is `{min,max,step}`. These are rendered by
    `PatchSpaceGraphWidget.show_settings_dialog` and applied in `_on_settings_response`
    (each change becomes a `set_node_property`).
- **Timers:** `REFRESH_INTERVAL_MS` (get_nodes poll), `POST_MUTATION_REFRESH_MS`,
  `LAYOUT_TICK_MS` (physics), `POLL_RESPONSES_MS` (MainWindow response drain),
  `LOG_POLL_MS` (console).
- **Loading UX (recent work):** a session import triggers `_begin_load()` in the widget;
  `MainWindow._on_loading_changed` shows a translucent `Gtk.Spinner` overlay, auto-opens
  the `LogConsole`, and on finish auto-collapses it (only if *we* opened it). Load
  completion is inferred in `_update_loading_state` (no node `starting`, ignoring `dead`),
  with a min-visible window and a safety timeout (`SESSION_LOAD_OVERLAY_*`). On finish the
  fit is **deferred** (~80ms) because the console collapse changes the canvas height —
  fitting before re-layout centered against the shorter canvas and left the graph high.
- **`zoom_to_fit()`** (`patchspace_widget.py`) frames all nodes. Zoom is effectively
  unbounded downward (`ZOOM_MIN = 1e-6`); the toolbar's **Recenter** button (right of
  Logs) calls `zoom_to_fit()`.
- **Pan/zoom** lives in `view_mixin.py`; both graph widgets share it. `to_world()` divides
  by `self.zoom`, so keep `ZOOM_MIN > 0`.
- **`update_from_daemon`** is the per-poll entry point; node dicts are built there with
  cached UI fields (`meta` holds the raw daemon data). Drag guards (`slider_dragging`)
  prevent a poll from yanking a slider mid-drag.

---

## 7. When to stop and ask

This repo has genuine ambiguity in UI intent (e.g. "vertically center as well" could mean
toolbar alignment *or* fit centering). If a request is ambiguous and the fix could go two
very different ways, ask a short clarifying question rather than guessing. For mechanical
or well-scoped changes (add a property, fix a bound, add a button), proceed and verify.

Also: do not change the `src/` legacy tree, do not commit, and do not "simplify" the
rationale comments or the effect-sandwich/staging machinery without understanding the
failure modes recorded in §5.
