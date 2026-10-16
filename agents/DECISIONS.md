# DECISIONS — technical choices made without asking

## [2026-09-28] Camera connection type named "camera"
- Decision: the new video port kind is `camera` (not "video"), one of the
  peer kinds of `audio`/`sound`/`impulse`, mirrored in pwnodes.add_edge,
  gui/node_specs (port_kind/ports_compatible) and the canvas.
- Why: Kyle's word for the feature is "cameras"/"cams" (TERMINOLOGY maps
  "connection type" -> port kind), and a camera wire only ever carries a
  camera feed in this design.
- Reversible: yes (rename through the same three mirrors).

## [2026-09-28] Video interiors are GStreamer pipelines with stream-owned links
- Decision: a video node's interior is one `gst-launch-1.0` pipeline
  (`pipewiresrc target-object=<camera> ! videoconvert ! v4l2sink`); the
  consuming stream's `target.object` binding *is* the visible link in the
  PipeWire graph, so sync_locked derives no port pairs for camera edges
  and a rewire respawns the interior.
- Why: PipeWire has no null-video-sink to use as a stable dummy socket
  (support.null-audio-sink is audio-only, and the pw-cat/pw-loopback
  tooling cannot negotiate video - both probed), and ffmpeg 8.x has no
  PipeWire I/O at all; GStreamer's pipewiresrc is the one stock consumer
  that creates a real, linkable Stream/Input/Video (verified live).
- Reversible: yes - the interior is the replaceable part of the sandwich;
  a purpose-built pw-stream helper could replace the pipeline without
  touching node shapes.

## [2026-09-28] Two virtual camera nodes: "Virtual Cam" (PipeWire) and "V4L2 Virtual Cam"
- Decision: split the one virtual-camera node in two: `virtual_camera`
  ("Virtual Cam") publishes camera-shaped streams *into the PipeWire
  system* (live, unlimited), `v4l2_virtual_camera` ("V4L2 Virtual Cam")
  writes a v4l2loopback device (fixed pool).  Both take either feed kind.
- Why: Kyle wanted the PipeWire-native one primary ("use the pipewire
  system to create virtual cams") with v4l2 kept as the secondary path;
  kernel loopback devices cannot be created live, PipeWire nodes can.
- Reversible: yes (same camera edges, interior swap).

## [2026-09-28] pipewiresink publication via client-name + stream-properties
- Decision: the PW virtual cam's label rides on `client-name` (plain
  string property - takes spaces, pipewiresink copies it to
  node.description, what pickers show); `stream-properties` carries only
  bare-token fields (node.name, media.class=Video/Source,
  media.role=Camera).
- Why: probed against the real stack - the GstStructure parser behind
  stream-properties stops bare values at whitespace and rejects quoted
  ones, so a label with spaces cannot live there; pw-loopback was tried
  first (VirtualMicNode twin) but its video streams never connect at all
  (no ports, no links - also probed).
- Reversible: yes (argv shape only).


## [2026-10-09] Packages under src/: central/ + modules/pipewire/
- Decision: daemon core in `src/central/`, PipeWire substrate in `src/modules/pipewire/`;
  imports resolve with `src/` as the import root (`central.*`, `modules.pipewire.*`), and the
  packaged entry points are `python -m central.main` / `python -m central.session_repair`
  (wrappers export `PYTHONPATH=${source}`).
- Why: agents/PLAN.md's three-layer split needs the seam visible before the module-API work
  starts; `-m` execution keeps the working directory out of the contract.
- Reversible: yes (a move plus an import rewrite).

## [2026-10-09] Bytecode untracked
- Decision: `.gitignore` excludes `__pycache__/`, `*.pyc`, `.pytest_cache/`; 90 tracked
  bytecode files were removed from the index.
- Why: pyc churn polluted every `git status`, and LLM_README's "stage only source files"
  warning was a workaround for it.
- Reversible: yes.

## [2026-10-09] The module API is one dict vocabulary, and the legacy protocol stays
- Decision: messages are plain JSON-able dicts built in `central/module_api.py`;
  a message with a `"type"` key is module API, `{"command": ...}` is the legacy
  GUI protocol, and both are served by `handle_command`.  Roles
  (`module`/`display`/`app`) are set by `hello` per connection.
- Why: one vocabulary is what phase 3 sends over a socket unchanged; keeping
  the legacy shape serving means the GUI never has to be ported in the same
  commit as the core.
- Reversible: yes (the legacy branch is one `if`).

## [2026-10-09] Status is the daemon's, health is the module's
- Decision: `online` / `offline_module` / `offline_dep` / `unbacked` are computed
  by `central/registry.py` from module connectivity and the `requires` closure;
  a module only reports health (`ok`/`starting`/`dead`) about instances it runs.
- Why: the gray-node semantics (module missing vs module disabled vs dependency
  down) are graph-level facts; letting a module decide its own liveness would
  put the same judgement in every module.
- Reversible: yes.

## [2026-10-09] Structure in graph.py, identity in registry.py, one connection authority
- Decision: the registry owns instances (id, type, config, status); `central/graph.py`
  owns spaces, placements and edges, and holds the only copy of the connection rules
  (`kinds_compatible` + the single-driver rule; port kinds resolved via
  `Registry.port_kind`).  The rule is stricter than the legacy one: only declared
  kinds pair, except audio/bundle which interoperate.
- Why: the legacy rule let an unknown kind pair with anything, which a numeric
  primitive cannot live with; and the three-mirror problem (model, `node_specs`,
  `session_repair`) ends when there is one authority.
- Reversible: yes.

## [2026-10-09] The PipeWire catalog is generated while both sources exist
- Decision: `modules/pipewire/declarations.py` builds the type catalog from
  `gui/node_specs.py` (ports, labels, category, colour) + `NODE_TYPE_REGISTRY`,
  and `tests/test_pipewire_declarations.py` asserts the two agree for every type.
  Phase 2 moves the data into the module and deletes the import.
- Why: no copy to drift; a new node type without a declaration fails the suite
  instead of producing a node a display cannot draw.  Specific port families
  (boolean/impulse/…) win over the generic `inputs`/`outputs` list, because a
  gate lists "ctrl" in both.
- Reversible: yes.

## [2026-10-09] The in-process module talks through the protocol handler
- Decision: `modules/pipewire/module.py` holds no daemon reference; the daemon
  hands it an inventory callable and it sends real module-API messages through
  `ProtocolHandler.handle`, exactly as a socket client would.
- Why: phase 3 becomes a transport swap, not a rewrite.  The module process then
  owns its own graph and the callable disappears.
- Reversible: yes.

## [2026-10-09] The node catalog is module-owned data, relayed by the daemon
- Decision: `modules/pipewire/catalog.json` holds the types (ports, controls,
  display), the add-node menu, the vocabulary tables (field labels, media.class
  choices) and the legacy aliases.  A module sends them in `type_declare`; the
  registry stores them per module and `catalog_reply` serves the merged view;
  the display installs it (`gui/node_catalog.py`).
- Why: the GUI's private copy (`node_specs.py`) was the mirror the plan kills,
  and a display in another process (phase 7, or a niri fork) cannot import it.
  Keeping the menu/vocabulary on the wire means the display hardcodes no
  PipeWire vocabulary either.
- Reversible: yes (the payload is data; the module owns it).

## [2026-10-09] One compatibility rule, in central/graph.py
- Decision: `kinds_compatible` (+ the single-driver rule) is the only
  implementation.  The GUI mirrors it for *live* drag feedback
  (`node_catalog.ports_compatible`), `session_repair` calls it directly, and
  the daemon enforces it on every edge.
- Why: three copies disagreed by construction; a display must be able to
  answer "may I offer this drop" locally, and an offline validator must run
  without a daemon — both derive from the same declared port kinds.
- Reversible: yes.

## [2026-10-09] The GUI's flat-import convention is load-bearing
- Decision: `gui/` stays a namespace package and its modules import each other
  flat; anything that needs the GUI's modules (tests, tooling) must put
  `src/gui` on `sys.path` and import flat too.
- Why: `gui.node_catalog` and flat `node_catalog` are two module objects for
  one file — installing the catalog through the wrong one leaves the widget
  reading an empty catalog.  Cost a debugging round; the trap is in NOTES.md.
- Reversible: yes (adding `gui/__init__.py` would make the two names one
  module, but that changes how the GUI is launched).

## [2026-10-09] Modules are processes; the daemon supervises, the module declares
- Decision: `central/module_supervisor.py` runs a built-in module as a child
  process (start on attach, restart with backoff, give up after 5 fast failures,
  stop with the daemon; children are in their own session so a daemon crash
  leaves them, which is why `stop` kills them explicitly).  `modules/host.py` is
  the module-side client every module process uses.
- Why: a module that can take the graph daemon down with it is not a module
  (the PipeWire stack has wedged on this box).  The daemon owns the process, the
  module owns what it provides — the registry never decides that for it.
- Reversible: yes (the in-process provider path stays for built-ins until each
  one moves out).

## [2026-10-09] A module's declaration ends with its terminal reply
- Decision: `type_declare` answers with the migration fan-out first and one
  terminal `declared`/`error` last, so a client reads until it sees something
  that is not a `migrate`.
- Why: without a terminator a client cannot know the fan-out is over without
  timing out on the socket (which is exactly how the first host hung).
- Reversible: yes.

## [2026-10-09] `number` is a declared port kind
- Decision: `module_api.PORT_KINDS` gains `number`; like every other non-audio
  kind it pairs only with itself (`graph.kinds_compatible`).
- Why: the primitives module needs a numeric wire; declaring the kind centrally
  is what keeps a display from having to guess.
- Reversible: yes.

## [2026-10-09] Displays ask; the daemon routes to the owning module
- Decision: `node_control`/`node_edge` are the display-side request messages; the
  daemon resolves the owning module from the registry, validates an edge itself,
  records it in the topology, and forwards `control_set`/`edge_apply` over
  `Connection.send` (socket write or in-process callable).
- Why: a display must not know which module owns a node, and must never dial a
  module — the same reason apps query the registry.  It also means a node a
  module owns becomes wireable/controllable *before* the PipeWire model moves,
  which is what unblocks the primitives module.
- Reversible: yes.

## [2026-10-09] The daemon owns identity, the module owns running the node
- Decision: `node_spawn`/`node_stop` (daemon -> module) let the daemon create an
  instance of a *module-provided* type and ask the module to run it;
  `node_values` (module -> daemon) reports what the nodes currently compute so a
  display can show it.  `add_node`/`remove_node` fall through to that path when
  the type is not one the daemon models.
- Why: the plan's split, made concrete for a node the daemon has no class for —
  which is every node a new module provides.  Without it a module's nodes could
  be drawn (phase 2) but never created, wired or shown.
- Reversible: yes.

## [2026-10-09] One module-enable file, and a store copy is authoritative
- Decision: `modules.json` is the single source of truth (`--modules-config`,
  else `$PATCHSPACE_MODULES_CONFIG`, else `~/.config/patchspace/modules.json`);
  a store path means deployment-managed and the daemon's CLI refuses to edit it.
  Unlisted modules keep their own default.
- Why: two writable sources of truth for one switch is how "why is it off?" bugs
  are born; a deployment that declares the set should not be silently
  overridden by a CLI, and it should be told so rather than failing quietly.
- Reversible: yes.

## [2026-10-12] The orphan sweep is instance-scoped end to end
- Decision: the argv-based helper scan only considers pids the instance-scoped
  pw-dump half identified as owning this instance's stale objects.
- Why: the argv half was machine-wide, so a private instance (a smoke, a test, a
  second checkout) refused to start while the user's daemon ran, because their
  helpers' argv carried a matching backing name.  A helper attached to another
  PipeWire instance is not ours to reap, whatever its argv says.
- Reversible: yes (pass no candidates for the old machine-wide behaviour).

## [2026-10-15] panel -> space: data migrates, the protocol is a one-release window
- Decision: S0 (`agents/SPACES.md` §9) renames the container word to `space`
  in code, files, the protocol, the daemon flags and the Nix options, with two
  asymmetries: **data migrates** (old space files, the old `panels` directory,
  the old session cache, `--panel-dir`, `services.patchspace.panelDirs` all
  keep working) and **the protocol is read-old/write-new** (the daemon accepts
  the old command names and argument keys, logging one deprecation line each,
  and emits only `spaces`/`space_id`).
- Why: an existing graph is the user's work - it is not re-derived from
  anything, so a rename that loses it is a rename that lost the data.  The
  protocol is different in kind: a GUI started before an activation keeps
  running the old *store path* for hours, so its commands must not start
  failing the moment the daemon is upgraded - but the daemon has no way to
  know which client it is talking to, so it emits one spelling (the new one)
  and reads both.
- Reversible: yes - the alias tables are single dicts
  (`main._LEGACY_COMMAND_ALIASES`/`_LEGACY_ARG_KEYS`,
  `spaces.LEGACY_TYPE_SPACE`/`LEGACY_CHILD_KEY`, the `catalog.json`
  `legacy_aliases`, the nix `mkRenamedOptionModule`s).  The removal list and
  the step that deletes them (S1) are in `agents/NOTES.md`.

## [2026-10-15] S1: the file's `type` key carries the space's kind
- Decision: the space file's `type` is now `live`/`template` (the space's
  kind, `agents/SPACES.md` §12), and every older spelling of a space file
  (`space`, and `panel` from before S0) is still *read* as a live space
  (`spaces.is_space_type`/`type_from_raw`).  `nix/lib.nix`'s `normalise`
  unwraps all four, and the Nix module's generated files say `live`.
- Why: §12 says the space *files* gain `type`, and the file is where a
  template must be recorded or a reboot would spawn it.  The key already
  existed as the file-format marker, so the alternative was a second key
  (`space_type`) that means the same thing and can disagree with it.  Reading
  all four spellings matters for the same reason S0's did: `_load_spaces_tree`
  decides "space file vs bare legacy session" from this key, so a file it does
  not recognise gets rewritten as an *empty* migrated session - the graph would
  be gone.  (`nix/lib.nix` has the same decision to make about imports of
  GUI-exported space files.)
- Reversible: yes (one tuple in `spaces.py`, one line in `lib.nix`, one in
  `module.nix`).

## [2026-10-15] S1: a template is never spawned, so a live node cannot be put in one
- Decision: `add_node` refuses a node id inside a template (and `create_space`
  refuses `node_ids` with `type: "template"`).  The way to make a template is
  to build the space live and flip the "Space Types" switch
  (`set_space_meta {type: "template"}`), which captures the interior as the
  definition and takes it out of the graph; flipping back materializes it.
- Why: §12 is unconditional - "no module instance, no live node, no placement
  in the running graph".  The alternative readings both lose data: spawning the
  node contradicts the contract, and recording it only in the definition leaves
  a node the client was told exists but never sees (get_nodes reads live nodes).
  Refusing is the one behaviour that neither lies nor empties a file, and it
  makes the spec's own switch the creation path.
- Reversible: yes (one guard in `_cmd_add_node`, one in `_cmd_create_space`).

## [2026-10-15] S1: a non-permanent space leaves its old file alone
- Decision: flipping `permanent` off stops all writing of that space (and its
  subtree) but does *not* delete a file it wrote while it was permanent.
  Deleting stays an explicit user action (`delete_space_file`).
- Why: "written to nothing" is about what the daemon writes, and a space file
  is the user's graph - a metadata toggle must not be able to destroy it.  The
  stray file is not reachable any more (its reference was pruned from every
  parent) unless it opted in with `auto_load`, in which case it comes back
  live-but-unpersisted, which is still what its own record asks for.
- Reversible: yes (call `delete_space_file` on the flip).

## [2026-10-15] S1: face keys are the port node's local id
- Decision: a `face[]` entry's `key` (what `set_space_input` takes) is the
  `space_in`/`space_out` node's *local id* inside the space, and `label` is
  the node's label, else its `port_name`, else the key.  Output entries carry
  only `key`/`label`/`direction`.
- Why: the key has to identify the input unambiguously and survive a save/load
  round trip; a local id cannot collide inside one space (two `port_name`s
  can), and it is what every other command in the protocol already addresses a
  node by.  `port_name` is a display name, so it belongs in `label`.
- Reversible: yes (one expression in `main._space_face`).

## [2026-10-15] S1: an instance inherits its template's look, not its permanence
- Decision: `instantiate_template` gives the new live space the template's
  `label`, `color` and `node_view`, records `template`, and takes `permanent`
  from the command (default true: dropping a template in is a user action).
  A nested child of the template becomes another placement of the *same file*,
  while the top-level instance gets a space file of its own (deduplicated
  against the files on disk - the template's own file is usually named after
  it).
- Why: the definition is where a space's presentation lives, so an instance
  should look like what it was made from; permanence is a separate question
  (should this instance be in the session), and the nested-child rule is the
  existing placement model, which is what makes an instance diverge from its
  blueprint instead of copying it twice.
- Reversible: yes (per-field in `_materialize_template`).

## [2026-10-15] S2: `embedded_in` is derived, never stored
- Decision: a portal is written **once**, in the embedding space's own record
  (`config.portals`); the reverse list (`embedded_in[]`) is computed on every
  payload from all spaces' portals (`spaces.portal_index`), exactly as `face`
  is derived from the port nodes and `children` from the parent's child refs.
- Why: §13 says "a portal is part of the parent's saved record", and two
  stored copies of one relation is how "which side is right?" bugs are born -
  removing an embedding would have to remember to fix up the target's file,
  and a *non-permanent* target (§3) has no file to fix up: it is written
  nowhere, yet it is still shown, and the UI must be able to say where.
- Reversible: yes (compute it in `build_payload` and drop `portal_index`).

## [2026-10-15] S2: deleting a space takes the portals that named it
- Decision: when a space (a subtree, a file and all its placements, or a
  placement) goes, every portal that named a doomed id is removed from every
  space that survives - in memory, and therefore in the file
  (`spaces.drop_portals_to`, called by `delete_space`, `delete_space_file` and
  `remove_space_placement`).  A *move* is the other half of the same rule: the
  ids are re-keyed, so the portals that named a moved space follow it
  (`spaces.retarget_portals`) instead of dangling at the path it used to live
  at.  Nothing about the *target* is ever touched by removing an embedding.
- Why: a portal is a placement of a live space, and with the target gone it is
  a placement of nothing: the client would draw a box that opens onto no
  interior, and `embedded_in` would name a space that is not there.  The
  alternative readings are worse: keeping it invites a "space that comes back"
  promise the daemon cannot keep, and rewriting the file by hand is the user's
  job, not a bug we hand them.
- Reversible: yes (one call site each; the loop is 10 lines).

## [2026-10-15] S2: a read-only space's portals are declared by its file
- Decision: `embed_space`, `remove_space_embedding` and `set_embedding_view`
  refuse a parent whose `mode` is `read-only` ("its portals are declared by
  its file and cannot be changed at runtime").  A read-only space may still be
  *the target* - embedding it elsewhere is exactly what §2 says a declarative
  space is for.
- Why: every other way of putting something inside a space (`move_nodes`,
  `move_space`) already refuses a read-only target, and `_write_spaces` skips
  read-only spaces entirely - so accepting the command would place a portal
  that is silently gone on the next reload.  A refusal that says so is
  strictly better than an edit that evaporates, and the declarative
  file remains the one place that can declare a portal inside a read-only
  space (its file is read, so a Nix-generated space *may* ship portals).
- Reversible: yes (one guard in `embed_problem`, one in each of the two other
  handlers).

## [2026-10-15] S2: a second embedding of the same space in one parent is refused
- Decision: `embed_space` answers "space 'X' is already embedded in 'Y'" rather
  than moving or duplicating the existing entry.
- Why: a portal is addressed by `(space_id, parent_space)` - its identity is
  the pair - so a second entry for the same pair would make
  `remove_space_embedding` and `set_embedding_view` ambiguous about which one
  they mean.  The *user* affordance for "put it somewhere else" is a drag
  (`set_space_layout` on the entry is not a command yet; a client that wants to
  move one sends remove+embed).  Showing the same space twice in one parent is
  not in the spec's model; showing it in two *different* parents is, and works.
- Reversible: yes (return the existing entry instead of the error).

## [2026-10-15] S2: `embedded_in` includes the spaces whose *definition* portals it
- Decision: the derived `embedded_in` lists every space whose `portals` name
  the space, a template among them; it does not filter by the parent's
  `spawns`/`live` state.
- Why: a template's portals are part of its definition - they materialize
  when it is instantiated - so the entry is data, not a claim about where the
  space is drawn right now; the client holds the parent's record and can see
  its `type`.  Filtering would be a second rule to keep in sync with the
  spawn rule, and it would hide a placement the user can still be shown in an
  editor.
- Reversible: yes (one `if` in `spaces.portal_index`).

## [2026-10-15] S4: cardinality is a port declaration, and the kind list is gone
- Decision: a port declares `cardinality: "one" | "many"` (and, for a node
  that combines several drivers, `merge: "mix" | "sum" | "last"`) in its type
  declaration.  `central/graph.py`'s `SINGLE_DRIVER_KINDS` is deleted; the
  graph resolves each edge through `Registry.port_cardinality`, and a port
  that declares nothing keeps the pre-S4 rule for its kind
  (`api.default_cardinality`), so no undeclared type changes behaviour.  An
  unknown `cardinality`/`merge` value is refused by `validate_type_decl`.
- Why: the restriction was by *kind name*, so a port that can take several
  edges could not say so (the `multi: true` opt-out was declared by no shipped
  type, while the model already allowed it).  The registry stays the one place
  that knows what a port is; the graph keeps holding no type knowledge.
- Reversible: yes (the declaration is data; the fallback is the old rule).

## [2026-10-15] S4: the space ports declare the bus they already are
- Decision: the four space port types (`space_in`/`space_out`,
  `bool_space_in`/`bool_space_out`) declare `cardinality: "many"` on their
  input port, and nothing else in the catalog declares anything.
- Why: their input is a bus today - `MIX_INPUTS`/
  `allows_multiple_inputs()` for the audio pair, `ALLOW_MULTIPLE_BOOLEAN` for
  the boolean pair ("several sources may be offered, the first wired one
  wins"), which the model has always allowed and the old docstrings already
  claimed.  The by-kind list denied it, so the daemon was a third opinion: the
  `node_edge` path refused a second boolean edge into a panel port that the
  model and the client accepted.  Shipping the declaration here makes the
  daemon agree (S4's point: the daemon only refuses what the declaration
  refuses).  The audio pair's `many` states the rule that was already in
  force; the boolean pair's `many` is the deliberate opt-out the docstrings
  promised.
- Reversible: yes (drop the `cardinality` key and the pre-S4 rule returns).

## [2026-10-15] S3: the relay is spelled `control_set`, not `value_set`
- Decision: when a producer's reported value drives a `drives_control` port,
  the daemon sends the consumer module a **`control_set`** for the control that
  port is named for.  `value_set` stays defined as the same relay addressed at
  a *raw* input port (a module's `on_value`), and nothing emits it yet.
- Why: §14 says the consumer must apply the relayed value "exactly as if the
  user had set the control (its `on_control` path), so no module needs a new
  callback" - and `control_set` is that path, while `value_set` reaches a
  *different* callback (`on_value`) that no shipped module implements
  (`modules/pipewire/module.py` explicitly drops `value_set`; the primitives
  module has `apply_value` but never registers it).  Sending both would
  double-apply for a module that does implement both.  The port name is the
  control key by construction, so `control_set` needs no extra field to
  address it.
- Reversible: yes (one line in `ProtocolHandler.deliver_control`; the port
  carries the name either message needs).

## [2026-10-15] S3: a control's value kind is derived from its widget
- Decision: a module declares a control's **widget** and never its value kind;
  `api.WIDGET_VALUE_KINDS` maps the widget vocabulary to one of
  `number`/`boolean`/`text`/`choice`, and a widget that carries no value
  (`setting`, `impulse`, `indicator`, `picker`, `clip`, `recorder`, `dump`,
  `filter_mode`) declares no port.  A control may *also* state `kind`
  (`api.VALUE_KINDS`); the stated kind wins, and an unknown one is refused at
  declare time.
- Why: §14's rule is "every declared control whose value kind is number,
  boolean, text or choice", while the module's data has no such field - so
  something has to read the vocabulary it does have.  `widget` is it (PLAN §7's
  "generic widget vocabulary ... now declared by modules instead of
  hardcoded"), and the table lives in `module_api` so the daemon, the modules
  and the client read one list.  The alternative - the daemon guessing by
  control *key* ("volume" → number) - would be an unstated rule with no way for
  a module to opt out; the widget table is explicit and a module that needs an
  unusual one states `kind`.  `setting` is the load-bearing exclusion: it is
  the catalog's generic "inline setting" spelling, and the acceptance's
  "a type declaring only `setting` widgets gains nothing" is exactly this.
- Reversible: yes (the table is data; drop a row and that widget's controls
  stop being routable, drop the merge and nothing has a port).

## [2026-10-15] S3: the auto-declared port declares `one`, and the control's
stored value lives in the registry
- Decision: a port the daemon auto-declares for a value control carries
  `cardinality: "one"` (a parameter has one producer; a module that wants a bus
  declares its own `drives_control` port with `many`).  The value a control
  holds locally - what unwiring its driver restores - is kept in the registry:
  captured off the instance config when a drive *begins*
  (`capture_control`), and overwritten whenever the user sets the control
  (`set_control_value`, which also writes it into the instance config).
- Why: §15 says declare `one` for value inputs "unless a merge is meaningful",
  and §14's revert needs a value nothing else holds.  Reading it off the
  instance config at unwire time would be wrong: a module that re-announces its
  config (a reconnect, a re-spawn) announces what it currently holds, which
  while driven is the *relayed* value - so the daemon freezes the value the
  control had when the drive began.  A control nothing has ever set has no
  stored value: the daemon restores nothing rather than inventing one, because
  what an unset control means (the module's default) is the module's business.
- Reversible: yes (drop the `cardinality` key to fall back to the kind's
  default; `capture_control` is the only writer of the frozen value).

## [2026-10-15] S3: the space face in `value` mode is the same mechanism, later
- Decision: this step does not touch the space face.  A face input with
  `mode: "value"` + `method: "edge"` is, per §14, "the space's `space_in` node
  in value mode, a value source inside its own space" - the same relay - but
  making the *jack* routable needs two things this step does not carry: the
  face node types (`space_in`/`bool_space_in`) must declare a value-kind output
  for the face's `kind`, and the face record needs the `field` method's stored
  value (`set_space_input` writes mode/kind/method/choices today, no value).
  Those land with S5's value sources (the routable slider/`const`/`string`
  nodes) and the client's face jack.
- Why: the step's contract is the four pieces that make the relay real (the
  auto-declared port, the edge→control binding, the relay, the revert), and
  inventing half of the face's value field now would leave a stored value
  nothing applies (the daemon does not proxy the model; a face's `space_in`
  node is a module-owned node, so its ports come from the catalog).
- Reversible: yes (it is additive work, nothing here forecloses it).

## [2026-10-15] S5: an operation's operand is a port *and* a control
- Decision: `math`/`compare` (both operands), `logic` (both), `invert` (its
  one) and `display` (`in`) declare `drives_control: true` on the input ports
  they already had, and declare a control named for each of them (a bounded
  number knob, or a switch).  No `cardinality` is declared, so each port keeps
  the rule its kind always had (`number` → `many`, `boolean` → `one`).
- Why: §14's whole point is that a node's parameter is an addressable input,
  and an operand *is* that parameter — the two spellings are already the same
  slot in the module's engine.  What the flag buys is reach: the daemon relays
  a producer's value **only** onto a port that drives a control, so a plain
  operand port can be fed from inside this module and nowhere else (the engine
  reads its own edges), while a driven one is fed by any module's producer —
  the PipeWire catalog's boolean outputs (`boolean_and`, `bool_space_in`, …)
  into `logic`/`invert`, or any number source into a maths operand.  The
  alternative — a second port (`a_input`) driving the control — would put two
  names on one slot and leave the real port dead.  Undeclared cardinality is
  deliberate: the port was `many` (number) or `one` (boolean) before this step
  and still is, so no existing graph loses an edge it had.
- Reversible: yes (drop the flag and the control; the ports behave exactly as
  they did in S3, where only the daemon's auto-declared ports were driven).

## [2026-10-15] S5: one slot per input, whatever the relay is spelled
- Decision: `Engine._compute` reads an input as *the wire, else
  `config[<port>]`* — the port's name is the control key (§14), so a value the
  daemon relayed, a value the user set and a wire inside the module all reach
  the same place — and `Primitives.apply_value` (the `value_set` callback, now
  registered) writes that same slot instead of the `"in:<port>"` key nothing
  read.  `value_set` and `control_set` are therefore the same application.
- Why: S3 decided the relay is spelled `control_set` for a port that drives a
  control and left `value_set` defined for a *raw* port, with no module
  implementing it.  §16 gives the module a raw port for the first time — the
  display's `text` — so the two spellings have to agree, or a `value_set`
  addressed at it would be dropped on the floor.  Nothing emits `value_set`
  today; the callback is wired so that it cannot be.
- Reversible: yes (one call in `Primitives.__init__`, one expression in
  `_compute`).

## [2026-10-15] S5: unwiring the last driver clears the slot it drove
- Decision: `Engine._release_drive` pops `config[<port>]` when the last edge
  into that port is removed, so an input nothing feeds falls back to the node's
  own default (an operand is `0.0` again).  The module leans on the daemon's
  order in `protocol._node_edge`: `edge_apply` goes out **before**
  `bind_control_edge`, so the value the daemon froze when the drive began (or
  the remaining driver's value) arrives *after* the clear and stands.
- Why: the module cannot tell a relayed value from a user-set one — both arrive
  as the same `control_set` (S3's decision, "no module needs a new callback") —
  so without this an unfed operand would keep the last driven value forever and
  a maths node would compute with an operand whose wire the user just pulled.
  S3's rule stands for a control the user *has* set: the daemon restores it,
  and the restore lands after the clear.
- Reversible: yes (delete the call; the old behaviour is "the last value
  sticks", which is what the daemon alone cannot fix).

## [2026-10-15] S5: the literals are `field` controls, not knobs
- Decision: `const` declares `{"key": "value", "widget": "field", "kind":
  "number"}`, `string` `{"key": "value", "widget": "field"}`.  The value kind
  is stated for `const` because `field` derives `text` and the literal is a
  number.
- Why: §16 asks for a *literal* beside the two bounded knobs the module already
  has (`number` -100..100, `slider` 0..1) — a literal is typed, not dragged, so
  a slider widget would either lie about its range or duplicate `number`.  The
  consequence is honest and reported rather than papered over: today's canvas
  cannot type into them (the client's `field` path sends the legacy
  `set_node_property`, which the daemon refuses for a module-owned node),
  while the module API path (`node_control "value"`) and the relay onto the
  literal's own `value` port both work.
- Reversible: yes (the widget is data; the port kind is `number`/`text` either
  way).

## [2026-10-15] S5: no schema bump for the types that gained a control
- Decision: `number`, `slider`, `toggle`, `math`, `compare`, `logic`, `invert`
  and `display` all keep `schema: 1`, and the module still registers no
  migration callback.
- Why: the daemon sends one `migrate` per persisted instance of a declared
  type, so a bump is a promise that a stored config changes shape — and none
  does.  A control this step adds is simply *absent* from an old config, and
  the engine reads an absent operand as `0.0`, which is exactly what the old
  engine computed; the new types have no old instances by construction.  A bump
  would ask the module to migrate every saved primitives node into a config
  that comes back byte-identical.
- Reversible: yes (raise `schema` the day a key actually changes meaning).
