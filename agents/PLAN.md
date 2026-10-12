# PLAN — the three-layer split (patchSpace / modules / patchSpaceGui)

Status: awaiting approval. Decisions marked [K] are Kyle's calls; [D] are mine,
rationale inline, veto on approval.

## 0. Target shape

```
~/projects/patchSpace                  the generalized node daemon (this repo, renamed from `patch`)
├── central/                           graph core: protocol, registry, nodes/edges/spaces, persistence
└── modules/
    ├── pipewire/                      built-in: today's pwgraph/pwproc/pwmatch + PW node classes
    └── primitives/                    built-in (new): math, boolean logic, number display

~/projects/patchSpaceGui               the display layer (split out of src/gui, own flake)
```

[K] Folder layout as specified. [K] PipeWire is a built-in module like any other.
[K] A primitives module exists in this cut, not later.

## 1. One protocol, three client roles

Transport stays JSON-lines over `$XDG_RUNTIME_DIR/patchspace.sock`. Every client
says `hello {role, proto:1, name, version}`:

- **module** — announces node types + instances, receives control sets, edge
  realizations, offline signals, migration requests.
- **display** — subscribes to a graph snapshot + incremental deltas, sends user
  actions (move, wire, control, panel ops).
- **app** — registry queries: "does an attached module provide type X /
  capability Y". [K] Query flow goes through patchspace, apps never dial modules.

### Module API (the core new contract)

- `type_declare {types: [{type_id, schema, ports:[{name,kind,direction}],
  controls:[{key,widget,label,...}], display:{name,category,color,icon}}]}` —
  display metadata replaces `node_specs.py` entirely.
- `node_announce {nodes: [{instance_id, type_id, space, config}]}` — bulk on
  attach; incremental after.
- central→module: `edge_apply`, `control_set`, `status_change`,
  `migrate {config, from_schema}` → `migrate_result`.
- `value_set {instance_id, port, value}` — central relays number/boolean values
  from producer to consumer modules. Semantics stay inside modules; the daemon
  is the bus.

[D] Modules are subprocesses, built-ins included: daemon spawns enabled built-ins,
external modules attach over the socket themselves — same wire protocol either
way. Rationale: crash isolation (a wedged pipewire stack must not take the graph
daemon down; it has wedged on this box), and enable/disable + offline semantics
come out uniform. In-process built-ins would need a second API for no gain.

## 2. Core model (generalized out of pwnodes.py)

- node = `{instance_id, type_id, module_id, space, controls, config(opaque), status}`
- edge = `{from, to, kind}`; **the daemon is the single compatibility authority**
  (kills today's three-way mirror: `pwnodes.add_edge` / `node_specs.ports_compatible`
  / `session_repair`).
- status enum: `online | offline_module | offline_dep | unbacked`.
  - `unbacked` = the gray state: placement persisted, no module has ever
    announced the type. Survives reboots. [K]
  - `offline_module` = module disabled/detached; its instances go offline and
    every node whose declared `requires` closure touches them goes
    `offline_dep`. Wire edges do NOT propagate offline — an edge to an offline
    node just carries nothing. [K]
- Structural node types the daemon itself provides: `panel`, `space_input`,
  `space_output`.

## 3. Spaces merge with panels [K]

- A panel *is* a space. Blueprints (definitions) vs active instances
  (blueprint + per-instance diff). Apps route in the background inside an
  instance; the user can look at and edit it without touching the blueprint.
- `space_input` / `space_output` nodes placed inside a panel; the panel's face
  ports are **generated from them**. Internal→external wiring = interior node →
  space I/O node, plus a parent-side wire off the face port.
- Per-instance diff: control overrides, placement overrides, boundary + interior
  edge add/remove. Instance edits never write back to the blueprint; blueprint
  changes rebase the diffs (keys that vanished land in a parked-diffs log, not
  silently destroyed). Diffs persist per instance → placement state
  reconstructs across reboots.
- Nesting is free: a panel face with ports can be instanced inside another panel.
- Root/default space special case: blueprint == session file (today's
  `last_session.json` semantics, no diff indirection) until a blueprint is
  instantiated twice. Existing panel files import under this rule unchanged.
- Declarative panels (nix-generated, `--panel-dir`, read-only) are blueprints.
  `effectiveConfig` keeps its shape.

## 4. Lifecycle

1. module connects → `hello` → `type_declare` → `node_announce` (bulk)
2. central hands over persisted instances of its type ids → `migrate`
   (update) → recreate-from-placement-and-defaults (fallback) [K: update
   contract with migration chain]
3. module detaches / disabled → its instances `offline_module`, helpers it owns
   (pw-cat etc.) are killed with it.
4. module re-announces a type id never seen before while placements exist for a
   different id → those stay `unbacked` (gray).

Type ids are a persistent contract, not names. Central stores configs it cannot
interpret; `schema` bumps live in the module's migration functions (today's
`migrations.py` split: per-type migrations move to modules, structural
migrations stay central).

## 5. Built-in module toggling

- `~/.config/patchspace/modules.json` is the single source of truth; the nix
  module writes it (`services.patchspace.modules.pipewire.enable`,
  `.primitives.enable`), the CLI (`patchspace module enable|disable <m>`)
  edits the same file and asks the daemon to apply live. No precedence games.
- Disable = kill the subprocess + offline its instances + dependent closure.

## 6. Primitives module (new)

Node types: `const` (number literal), `number_slider` (user control → number),
`toggle` (boolean), `math` (add/sub/mul/div), `compare`, `logic`
(and/or/not/xor), `display` (number readout). New `number` port kind;
`boolean` already exists. [K] "stuff like doing math and displaying the
outputs, boolean logic".

## 7. Display layer (patchSpaceGui)

- `node_specs.py` dies; specs arrive via `type_declare`. Generic widget
  vocabulary per control: toggle / slider / field / picker / indicator — the
  set that exists today, now declared by modules instead of hardcoded.
- GUI adopts the daemon; it may still spawn one for dev convenience, but never
  owns it (systemd owns it in deployment).
- [D, veto at approval] The raw PipeWire tab (`pipewire_widget.py`) is dropped
  from the generic client: it is pw-specific debug UI. A module-shipped display
  extension is the eventual home; not in this cut.

## 8. Phases

Status (2026-10-09).  Each completed phase ended green: the daemon suite
(`src/tests/`) has run headless at every step, plus a per-phase proof (an
offscreen canvas render, a real-socket module attach, a spawned module process,
a packaged CLI).

| # | Phase | Status |
|---|---|---|
| 0 | rename `patch`→`patchSpace`, restructure into `central/` + `modules/pipewire/`, `.gitignore`/untrack pyc | **done** |
| 1 | central core: hello/roles, registry, generic node/edge model, status; PW behind an in-process module-API adapter | **done** |
| 2 | GUI port: `node_specs.py` deleted, specs from `type_declare`; gray/inert nodes drawn from the registry's status | **done** |
| 5 | primitives module (8 types, local engine), `number` kind, `node_values` | **done** (out of order: it validates the boundary) |
| 6 | toggling: one `modules.json`, `patchspace-modules` CLI, `services.patchspace.modules`, offline + `requires` closure | **done** |
| 8a | docs (`nixos.md`, `homeManager.md`, LLM_README, STRUCTURE) | **done** |
| 3 | **the extraction proper**: move the model (`pwnodes.PatchSpace` + node classes) and the pw substrate into the module process; the daemon keeps identity/placement/status and drives it over the API | **open — the big one.**  Its groundwork is all in: `modules/host.py`, `ModuleSupervisor`, `node_control`/`node_edge` routing, `node_spawn`/`node_values`, the scoped orphan sweep |
| 4 | spaces merged with panels: blueprint/instance split, `space_input`/`space_output` face ports, per-instance diff persistence + rebase | **open** (`topology` already holds spaces/placements/edges; `panel_in`/`panel_out` types exist) |
| 7 | split `patchSpaceGui` into its own repo/flake; mycelion's second input | **done.**  `~/projects/patchSpaceGui` (flake `patchSpaceGui`) holds the client: `gui/`, `packaging/`, its own tests (113 passing, including the canvas suite and the catalog parser) and its own flake (`packages.patchspace` + a `checks.tests` whose only cross-repo need is the daemon's source, for the catalog fixture).  The daemon's tree is GTK-free (its dev shell no longer carries GTK, its suite is 465 passing), `clientPackage` is an explicit option with an assertion the module-scopes check exercises, and mycelion carries the second input.  What made it more than a move: the client's tests use the same *flat* import graph the app does (a `gui.X` import is a second module object, which silently breaks the catalog install and a wire-router monkeypatch), one test asserted a cross-repo invariant (the socket path) and now does so on both sides, and two "canvas" tests were really daemon tests (they moved back) |
| 8b | final live smoke (needs Kyle at the machine) | **open** |

Two known gaps left deliberately, each with its own step:

* The display's **control vocabulary is closed**: the canvas draws the widgets it
  knows (`volume`/`slider`/`toggle`/`field`/`picker`/`indicator`) and ignores the
  rest, so a module-declared control is not yet draggable.  The value *is*
  settable through `node_control` and displayed from `node_values`.
* The canvas still gets the **graph** from the legacy `get_nodes` poll (enriched
  with the registry's status), not from `snapshot_reply`.  Moving it belongs with
  phase 3, when the legacy protocol retires.

Verification: daemon suite headless at every phase; new protocol tests per phase
(announce/migrate/offline/values/spawn); GUI via the offscreen cairo render
recipe; live smoke only at the end, against Kyle's session with his say-so (never
a second daemon against live pipewire).

## Known landmines (from agents/NOTES.md)

- Never run a second daemon: `_terminate_orphan_helpers` sweeps by argv marker.
- GUI tests segfault headless without the ignore list.
- `path:` flake inputs bake narHash: every mycelion-side edit needs
  `nix flake update`.
- The GUI-spawned daemon vs systemd daemon coexistence (socket moved once
  already, two daemons fought over the default sink).

## TL;DR

patch becomes patchSpace: a PipeWire-agnostic graph daemon (nodes, edges,
panels=spaces, gray placements for missing modules) with one module protocol;
pipewire moves under `modules/pipewire/` as a built-in subprocess module and a
new `modules/primitives/` does math/logic/display. Panels merge with spaces:
blueprints + per-instance diffs, face ports generated from space-input/output
nodes. The GUI moves to `~/projects/patchSpaceGui` and becomes a dumb display
driven by module-declared metadata. Executed in 9 phases, tests green each step;
approve and I start at phase 0.
