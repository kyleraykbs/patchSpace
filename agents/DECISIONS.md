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
