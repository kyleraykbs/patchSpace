# EXTRACTION — phase 3's model move, step by step

`agents/PLAN.md` phase 3 is the last architectural step: the PipeWire machinery
leaves the daemon process.  This file is the *route*: what moves where, in what
order, and what must stay true at each step.  It exists because the phase is a
rewrite, not a move — the daemon reads its model richly and synchronously all
over, so "move `pwnodes` out" is only possible once the reading has been
re-routed.

Sizes today (for scale): `central/main.py` 6289 lines,
`modules/pipewire/pwnodes.py` 6061 (moved here in step 1),
`modules/pipewire/{pwgraph,pwproc,pwmatch}` 2043, `central/spaces.py` 560.

**Naming**: this file was written before S0 (`agents/SPACES.md`, 2026-10-15),
which renamed the container to *space*: read `panels.py`/`panels.*` here as
`central/spaces.py`/`spaces.*`, and `_panels`/`panel` in a symbol name as
`spaces`/`space`.

**Status**: step 1 is done (2026-10-12) — the model lives in the module and the
daemon reaches into it in-process.  Step 2 (the report) is the next one and the
only load-bearing step; 3–5 follow.

## The target, precisely

**The daemon keeps** (nothing about PipeWire):

* the **registry** (identity, types, instances, status) and **topology**
  (spaces, placements, edges) — `central/registry.py`, `central/graph.py`;
* the **protocol** (`central/protocol.py`), including routing a display's
  `node_control`/`node_edge` to the owning module (done);
* **persistence**: panel files and the session autosave, the per-placement diffs
  sidecar, migrations and repair (`central/panels.py`, `migrations.py`,
  `session_repair.py`);
* **module lifecycle**: config, supervisor, the CLI (`module_config.py`,
  `module_supervisor.py`, `modules_cli.py`);
* what is genuinely process-level: the socket server, the autosave quiet period,
  the module log dir.

**The PipeWire module owns**:

* the node taxonomy and the model's reconcile engine (`pwnodes.py`);
* the substrate (`pwgraph`, `pwproc`, `pwmatch`);
* the *live* supervision of both (`sync`, `supervise`, the careful bring-up,
  device profiles, volume locks, the sensitivity gate's internals, the camera
  streams' owned links);
* what its nodes are doing (health, values, readiness) — reported to the daemon,
  which serves it to displays.

**Not** moved: panels/session files (the daemon owns the *placement* of nodes;
the module owns what a node *is*), the protocol, and the registry.

## The one interface decision that makes this possible

The daemon must never need attribute-level access to a module's node objects.
That is the trap: a proxy that answers `node.volume` is a *replica* of the model,
and a replica is the model moved back into the daemon.

So the module reports **data**, not objects:

| the daemon asks | the module answers |
|---|---|
| `node_spawn {nodes}` (exists) | it creates them; `node_announce` binds them |
| `node_stop {ids}` (exists) | it tears them down; the placement stays |
| `node_control {id, key, value}` (exists) | it applies it (its own setter) |
| `node_edge {edge, op}` (exists) | it realizes/drops the PipeWire links |
| `node_values {id: {port: value}}` (exists) | what its nodes compute |
| `control_set {id, key, value}` (exists, **S3**) | the daemon's half of the relay: a *producer's* reported value, delivered to the consumer that owns the driven control — the same message a user's `node_control` becomes, so a module applies both through one path |
| **`node_report {id: {type, params, ready, health, ports}}` (new)** | the data a display draws: exactly what `_serialize_nodes`/`_export_node_params` produce today, per node |
| **`node_edge_report {id: {wired}}` (new, or fold into the above)** | whether an edge's links are live (`edge_wired`) |

`node_report` is the load-bearing one: it replaces every place the daemon walks
`space.nodes` to build a payload or a panel file.  It is *data*, so the daemon can
keep it in the registry (`InstanceRecord.config`/`values`, plus a `params`
blob) without ever holding a node object.

`node_values` stopped being display-only in S3 (`agents/SPACES.md` §14): the
daemon follows each reported port's edges and delivers the value to the consumer
module that owns the port at the other end, as a `control_set` — the same
message a user's `node_control` becomes.  So a report is now *both* "what a node
computes" (for a display) and "drive this parameter" (for a consumer), which is
one more reason the report has to be data the daemon can read: the relay is
built entirely out of `InstanceRecord.values` + `Graph.edges_from` +
`Registry.port_drives_control`, with no attribute-level access anywhere.

## The steps (each ends green; the daemon serves the GUI at every point)

### 1. The boundary is drawn (a pure move) — DONE 2026-10-12
`pwnodes.py` moved to `modules/pipewire/`, its importers repointed
(`central/main.py`, the suite: `from modules.pipewire.pwnodes import …`), nothing
else changed — the daemon still reaches into the model in-process
(`self.space.nodes`), so this was a *location* change that ended green.  It came
first because every later step needs the model on the module's side: **a step
cannot report what it does not hold.**  (The first draft of this file had the
module creating and reporting its nodes *before* the move; neither is
implementable, because the factory and the report both need the model.  The order
below is the one that can actually be executed.)  It was cheap because the model
imports only `modules.pipewire.*` — nothing from `central` — so the dependency
runs one way.
*Verified*: the daemon's suite (465 tests) and the private-instance smoke.
*Invariant held*: no behaviour change; the client and the protocol untouched.

### 2. The daemon stops reaching through (the load-bearing step)
Its *driving* calls go through the API: `_create_node` + `NODE_TYPE_REGISTRY` move
behind `node_spawn` (called through `ProtocolHandler.spawn_instance`, the path a
primitives node already takes), and `_load_session`, `_bring_node_up`,
`_standardize_nodes`, `_ensure_sensitivity_internals`, `_apply_sensitivity`,
`_coalesce_reload` and the command handlers behind `node_spawn`/`node_control`/
`node_edge`.  Its *building* calls read data instead of the model: `node_report`
carries the `_SERIAL_ATTRS`/`_LAYOUT_ATTRS` walk, the per-class extras and
`_node_is_ready`/`_node_health`, and `_serialize_nodes`, `_export_node_params`,
`_build_panels_from_space`, `_logical_edge` and the group handling become registry
instances + the last report.
*Verify*: a test asserts `get_nodes` is byte-identical before/after for a session
with every node type (capture the payload once, then compare) — the strongest
available check, and the reason this step is safe to attempt.
*Invariant*: `get_nodes`'s shape does not change; the client is untouched.  The
daemon stops walking `space.nodes` — that is the point of the step, and the
replica problem (see Hazards) is why nothing may be left reading attributes.

### 3. The substrate follows
`pwgraph`/`pwproc`/`pwmatch` move with `pwnodes` (the node classes own the pw
objects, so they cannot be separated — one move in practice, done file by file).
The daemon's `_tick` loses everything that touches the model
(`space.supervise()`, `_assert_defaults`, `_enforce_volume_locks`,
`_ensure_device_profiles`) and the module runs its own tick.
*Verify*: the private-instance smoke (`scripts/smoke.sh`) — it already exercises
panel load, wiring, node creation, control, values, and module disable/enable.
*Invariant*: the panel files and the session autosave are written by the daemon
from reports, and a round trip (write → load) is unchanged.

### 4. The module becomes a process
It already speaks the API through `ProtocolHandler`; running it under
`ModuleHost` is a transport change (the supervisor and host are done and tested).
The in-process path stays for tests.
*Verify*: the smoke again, plus a kill-and-restart (the supervisor's test already
covers the rebind).

### 5. The legacy command surface retires
The GUI's `command` messages become module calls: `set_volume` → `node_control`,
`add_edge` → `node_edge`, `add_node` → `node_spawn`, and the rest of the model
handlers either move to the module or are deleted if the module owns them.  This
is when `{"command": ...}` can go, and when the client reads the graph from
`snapshot_reply` instead of the legacy `get_nodes` poll (the last known gap).
*Verify*: the client suite plus the offscreen render.

## Hazards (each has cost real time already)

* **Two daemons**: the sweep is instance-scoped now, but never run a second
  daemon against the live session.  The smoke uses a private PipeWire.
* **Careful nodes**: `_CAREFUL_NODE_TYPES` (echo/noise cancel, sensitivity,
  normalize, reverb) publish ports asynchronously and get a one-at-a-time
  bring-up.  That logic is part of what moves; do not simplify it.
* **Sensitivity gate internals**: hidden pre/post Volume nodes the daemon owns
  and routes through transparently (`_ensure_sensitivity_internals`).
* **Camera links are stream-owned**: `sync` derives no port pairs for a camera
  edge; the consuming GStreamer stream's `target.object` *is* the link.  A naive
  "edges → pw links" mapping breaks it.
* **The autosave's frozen params**: `_build_panels_from_space(freeze_params=...)`
  is what keeps a placement's local change out of the blueprint.  It reads the
  snapshot; with reports, the snapshot is the daemon's own record — keep it that
  way.
* **Optimistic UI**: the client applies an action before the daemon confirms and
  treats the *reply* as the fence.  Any change to reply ordering breaks it.

## What not to do

* **Do not proxy the model.**  Attribute-level access (`node.volume`,
  `space.edges`) cannot be proxied without shipping the whole model, i.e. a
  replica in the daemon.  Route *requests* and consume *reports*.
* **Do not move the panels/session.**  Placement is the daemon's; node identity
  and config are the module's.  The seam is `node_report` + `node_spawn`.
* **Do not split the model from the substrate.**  A node class *is* its pw
  objects (`OwnedPwNode`/`OwnedPwProcess`); separating them means rewriting every
  node class.
