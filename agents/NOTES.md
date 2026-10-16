# NOTES — things that cost tokens to learn

Punch-in details for working in this repo.  `LLM_README.md` is the deep doc; `ai/STRUCTURE.md`
is the connective map.  This file is the fast path for the next session.

## Cheapest reliable loop

```bash
# from the repo root
nix develop -c python -m py_compile src/<changed files>      # syntax gate
cd src && nix develop ../ -c python -m pytest -q             # ~270 tests, ~7s, headless
```

* `.gitignore` (added 2026-10-09) excludes `__pycache__/`, `*.pyc`, `.pytest_cache/` —
  bytecode is **no longer tracked**.  The tree still frequently carries the user's own
  uncommitted work (`gui/*`, `.opencode/`, `org.patchspace.xcf`): check `git status` first.
* **`git commit -- <path>` writes the WORKTREE state of those paths, not the index.**  So
  (a) a `git rm --cached` (untracking bytecode) is silently undone by the next pathspec
  commit that covers the path, and (b) a bare `git commit --amend` sweeps in everything
  else staged.  Land a deletion by committing the index with nothing else staged.
* The tree frequently carries the user's own uncommitted work (`config/systems/*` in mycelion;
  `gui/*`, `.opencode/`, `org.patchspace.xcf` here) — check `git diff` before committing so
  you don't sweep it in.

## Verifying behaviour the suite can't reach

The suite is daemon-only and fake-PipeWire.  Two recipes cover the rest:

**1. Canvas / GUI drawing + hit-testing, offscreen (no window, no broadway).**

`PatchSpaceGraphWidget.on_draw(area, cr, w, h)` is a plain method, so render it into a cairo
`ImageSurface` and look at the PNG.  Two traps:

* The first frame draws every node at alpha 0 (`_anim_tick` starts the materialize pop and
  the loading fade).  Pump the GLib main loop first (`GLib.MainContext.default()` +
  `ctx.iteration(False)` in a loop for ~1 s); `on_draw` itself *starts* the pop for a node
  it hasn't seen.
* Physics is on by default: set `w.physics_active = False` (and `layout_awake = False`) or
  the nodes drift away from the coordinates you set before you render.

Driving the real handlers (`w.on_click(None, 1, wx, wy)`, `on_drag_begin`, …) with a stub
client that collects `send()` payloads is the cheapest way to prove a control's command
path.

**2. Real audio, without touching the user's session.**

> ⚠️ **Do not simply run a second `PatchSpaceDaemon`**, even against a private PipeWire.
> `PatchSpaceDaemon.start()` runs `_cleanup_stale_objects()` → `PipewireGraph._terminate_orphan_helpers`,
> which SIGTERMs every `pw-cat`/`pw-loopback` process on the **machine** whose argv contains a
> marker name — a *global* /proc scan, not scoped to the instance the daemon is attached to
> (the `pw-dump` half of the sweep *is* instance-scoped; the argv half is not).  Running a
> smoke daemon therefore kills the live session's helpers.  They self-heal (the running daemon
> respawns them on its supervise tick) but it is a real, audible disruption — tell the user if
> you do it.  Either drive `PatchSpace` directly with real objects, or accept and disclose it.

Run a *private* PipeWire + WirePlumber in a temp runtime dir and point everything at it:

```bash
XDG_RUNTIME_DIR=$RT/run PIPEWIRE_RUNTIME_DIR=$RT/run XDG_CONFIG_HOME=$RT/config   # ← not WIREPLUMBER_CONFIG_DIR
mkdir -p $RT/config/wireplumber/wireplumber.conf.d
# 90-no-hardware.conf:  wireplumber.profiles = { main = { monitor.alsa = disabled ... } }
pipewire &  ;  wireplumber &
```

* `WIREPLUMBER_CONFIG_DIR` **replaces** WP's config search path → WP starts with no context
  modules and logs `can't find protocol 'PipeWire:Protocol:Native'`.  Use `XDG_CONFIG_HOME`
  so the package config is still read and only your `.conf.d` override is added.
* Disable the hardware monitors, or a second WP will fight the user's session over ALSA.
* Set `main.SOCKET_PATH` to a temp path before `PatchSpaceDaemon()` so you can never collide
  with, or adopt, the user's daemon.
* A null-audio-sink's **monitor** ports are the audio-source side; measure them with
  `pw-cat --record --target <sink> --properties '{ stream.capture.sink = true }'` (that
  property is required — see `_ensure_drain`'s docstring).

## Geometry rules that bite

* A node's bottom block has exactly one source of truth: `_bottom_control_height(node)`
  (control heights + `spec.toggle`'s gap/row + `spec.field`).  `_socket_margins`,
  `_base_node_height` and `_compute_node_height` all read it, and any *drawn* control must
  derive its rect from one shared helper (`_gate_rect`, `_field_rect`, `_toggle_switch_rect`,
  `_device_row_rect`, …) so drawing, hit-testing and edge endpoints can't drift.
* Every socket position goes through `_socket_position`; never compute one by hand.
* Config-vs-live: a **configuration** action (inline field, settings gear, three dots,
  a spec's `toggle` switch) must be hit-testable on a not-ready node
  (`_hit_nodes(..., require_ready=False)`); a **live** control (slider, gate toggle) must
  not be.

## Daemon-side rules that bite

* `sync_locked` only visits `OutputNode`/`BackedNode`s; a control-plane edge that reaches a
  backed node must be skipped there (`_edge_is_control`) or it will be resolved as audio.
* Anything a node owns that can exit on its own belongs in `owned_backings()`, **not**
  `backings` - `dead_backings()`/`_node_is_ready` treat a dead backing as a fault.
* **Panel files are the user's graph, and (before this) had no backups.**
  ``panels.write_file`` overwrites in place (atomic tmp+rename, but no
  history) - a reload that empties the model and then autosaves is
  unrecoverable.  It now keeps the previous content as ``<file>.json.bak``
  (ignored by the panel scan, which matches ``.json`` only).  Recovery
  material that *did* exist when the Oct 2026 wipe happened, in order of
  usefulness: the user's own ``~/.local/share/patchspace/declarative/*.json``
  snapshots (full node configs), the previous boot's user journal
  (``journalctl --user -b -1 -u patchspace``: ``Wiring N -> N for <edge id>``
  lines carry fully-qualified ids and their panel path, plus
  ``Requested node '<backing>'`` lines), the emptied panel files
  themselves (structure/placements/nesting survive), and ``pw-dump`` /
  older ``get_nodes`` output for types+labels.  Rebuild with the
  ``panels`` module itself (``load_panel``/``edge_owner``/``relative_id``/
  ``write_file``) so ids and edge ownership match what the daemon writes -
  and note ``Panel.nodes``/``edges`` are copy-returning properties:
  mutate ``panel.config`` instead.
* **A "pink" node outline is the pending-wiring state, not a camera
  color.** `_draw_node` outlines `is_wiring` nodes in `pal["warning"]`,
  and this user's stylix theme defines `warning_color` as pink
  (`.config/gtk-4.0/gtk.css`).  When one node's outline stays coloured
  while others are normal, read the daemon log for that edge id
  (`journalctl --user -u patchspace`) - `pw-link ... failed: failed to
  link ports: Success` plus `not confirmed by the live graph within
  3.0s` means the link object is created but PipeWire cannot allocate
  its buffers, so it never lands.  `journalctl --user -u pipewire` then
  says why (`pw.link: (a.b.c -> d.e.f) allocating -> error Buffer
  allocation failed (-2)`); `invalid mem id N, expected M` means one
  client's memory-fd table is stale (seen after a device
  re-enumeration), and recreating the *other* endpoint's client does
  not clear it.  Verify with `pw-link` by hand - if the same pair keeps
  failing while neighbouring ports of both nodes link fine, the fix is
  to rebuild the stale client (restart wireplumber / reconnect the
  device), not anything in this repo.
* Video interiors (VirtualCameraNode / V4l2VirtualCameraNode) are
  `gst-launch-1.0` pipelines: the sink's argv carries the node's backing
  name (a v4l2sink element `name=` or a `stream-properties` node.name)
  so the startup orphan sweep can reap a crashed daemon's pipeline (gst
  counts as a helper next to pw-cat/pw-loopback), and the GStreamer
  stream's node.name may not resolve to a graph id, so both override
  `dead_backings()` (a live process is healthy; no "stuck" restarts) and
  `structural_ok()` compares the spawned argv, which is how a camera
  rewire gets repaired by respawn.  Two probed GStreamer quirks carry
  the label: the GstStructure behind `stream-properties` stops bare
  values at whitespace (labels with spaces go on `client-name`, which
  pipewiresink copies to node.description), and `pw-loopback` cannot
  carry video at all (its streams never connect).
* `_apply_node_config` (shared by add/load/panel-param-sync) copies any config key with a
  matching attr onto the node, so a new serialized attribute usually needs only
  `_SERIAL_ATTRS` + a `_create_node` argument + a `set_node_property` branch.
* `session_repair` validates compatibility through `gui/node_specs` (`port_kind`,
  `ports_compatible`) — the GUI tables *are* the validation source of truth.

## File paths on nodes

* A node that stores a file path (the Sound Effect's `path`) keeps the user's string
  verbatim - `~` included - and expands it at *use* time with `os.path.expanduser`
  (`SoundEffectNode._start_player`).  Storing absolute paths would break portability
  across sessions/panels/machines; nothing in pw-cat's argv is a shell, so a literal `~`
  has to be expanded somewhere.
* The desktop "pick a file" dialog is `portal_file_dialog.open_file(parent, title, cb,
  folder=…, filters=[(label, [globs])])` - the XDG FileChooser portal, which on a real
  desktop is the file manager's own open dialog (there is no API to drive a file manager
  to *return* a selection).  The widget writes the result back home-relative
  (`_home_relative_path`) so the two conventions meet.
* When smoke-testing anything that opens a picker, **stub `patchspace_widget.open_file`**;
  clicking a real folder button pops a dialog in the user's session.

## Style expectations here

* Long "why / what failed before" rationale comments are the house style — do not strip
  them, and write them for anything non-obvious.
* Todo list live and granular; update as work happens, not at the end.
* Ask before assuming on interpretation; batch questions (see `agents/TERMINOLOGY.md`).

## Overridable paths (env, for services)

`PATCHSPACE_SOCKET` (daemon `--socket`, GUI, CLI clients all read it), `PATCHSPACE_PANEL_DIR`,
`PATCHSPACE_ROOT_PANEL` — the three knobs a service/module needs to keep a daemon per-user
(socket in `$XDG_RUNTIME_DIR`, panel dirs from the store, root panel in state).

## The Nix module (nix/)

`nix/module.nix` (one body, `flake.modules.{nixos,homeManager}.patchspace`) runs the daemon as
a **user** service and generates read-only panel files into the store; `nix/lib.nix` holds
the JSON-merge helpers (`flake.lib.patchspace`).  Two gotchas worth remembering:

* NixOS and home-manager spell systemd units differently — `unitConfig`/`serviceConfig`/
  `wantedBy` vs `Unit`/`Service`/`Install`.  Hence the `homeManager ? false` closure arg.
* A rebuild must restart the daemon: the generated panel dir is a new store path, which
  changes `ExecStart`, which is what makes systemd pick the new config up.
* Verification recipe (no `nixos-rebuild`!): `pkgs.nixos [ module config ]` and read
  `config.systemd.user.services.patchspace.serviceConfig.ExecStart` /
  `config.services.patchspace.panelsDir`; `nix build` that dir to run the validation, and
  `nix build .#checks.<system>.module-merge` for the merge-precedence check.  Reach for a
  *worse* config to confirm a gate actually fails — the first version of the validation
  used `| tee`, whose exit status hid the failure entirely.

## Known follow-ups (found while diagnosing the close-pair wire)

The fix for "a close-pair wire hides a leg under a node" (`_close_pair_z`) covered the
*drawing* path.  A review of the routing code also turned up three places where a wire can
be drawn with endpoints that no longer match `_socket_position`, none of which is the
reported symptom (nothing to do until one is observed):

* `_route_fallback`'s small-drag shift (a known-unroutable edge is shifted with its
  endpoints instead of re-routed) - check it re-pins *both* sockets when only one moved.
* `_edge_ghosts` draws the last snapped path (intended, for the retract animation), so an
  edge removed *and* re-added inside the same animation window can show the old geometry.
* `_wire_routing_signature` is complete for single-socket nodes (both x/y and w/h are in
  it), but a *local* label/`meta` edit (`_on_settings_response`, `_rename_node_local`)
  mutates label/description without clearing `_node_h_cache`, so a multi-socket node
  (Echo Cancel, Switcher, Bundle Split, Filter) can draw its box at the old height until
  the next poll clears the cache - self-healing within one poll, cosmetic.

## The rename (PatchBay -> Patch Space) and its migration

"Everywhere" means identifiers, packaging, paths, node-type keys and prose; the pieces that
are *data* contracts stay readable under the old spelling and are never written under it:

* `PATCHBAY_*` env vars are read as fallbacks (`PATCHSPACE_*` wins); the daemon, GUI and CLI
  all do this, because a session that is already running exported them.
* `~/.local/share/patchbay/panels` + `~/.cache/patchbay/last_session.json` are **copied**
  into their `patchspace` equivalents once, on load, only where the destination is missing
  (`PatchSpaceDaemon._migrate_legacy_paths`; covered by
  `test_pre_rename_paths_are_adopted_once`). Never moved, never overwritten.
* `patchbay_*`/`PatchBay*` names stay in `pwmatch`'s owned-prefix/builtin lists so
  pre-rename objects are reaped and stay out of External Only bundles.
* node type keys: `patchbay_device`/`patchbay_mic_device` are aliases (daemon registry after
  `CLASS_TO_TYPE`, plus `NODE_TYPE_SPECS`) - old files load, new exports are canonical.

Renaming files: `src/patchbay_cli.py` -> `src/patchspace_cli.py`,
`src/gui/patchbay_gui.py` -> `src/gui/patchspace_gui.py` (flake.nix, tests and docs follow).
Reminder for module work: after editing this repo, a consumer using a `path:` input needs
`nix flake update patchspace`, since the narHash is baked into its lock.

## Panel fit, entries, and the stylix opacity (2026-09-23)

* **A panel's declared placement is a room for its physics, never a clip on its box.**  The
  widget only ever writes back x/y, so `w`/`h` is usually the creation default (420x260 root,
  320x320 daemon-made).  Clipping the auto-fit to it made panels under-fit their contents -
  obvious on a panel with no IO ports (a ported panel is pushed out by its port stack anyway)
  and worst for a parent whose children had been dragged apart, since a child panel can't be
  walled back in the way a member node can.  The box now always encloses members, children,
  groups and their titles; only the unanchored physics cloud is bounded, by
  `_wall_nodes_into_panels` against `_panel_growth_limits`.
* **Coordinates:** nodes carry *canvas* x/y and `_draw_panel_boxes` draws every rect raw, so a
  panel's box (and its children's boxes) are canvas coordinates too.  `panel.x`/`panel.y` are
  parent-relative and only get folded by `_panel_absolute`, which is used for the empty-panel
  fallback (and for dragging).  Do not "fix" the child fold by translating it.
* **Entry chrome is themed** (`field_bg`/`field_fg` from the theme's view colors) instead of a
  hardcoded near-black; `theme_palette` falls back to the old literals.
* **`services.patchspace.canvasOpacity`** defaults to `config.stylix.opacity.applications` (or
  `null`) and reaches the GUI as `PATCHSPACE_CANVAS_OPACITY` from the session environment; the
  `--canvas-opacity` flag still wins.

## The two-daemon incident (2026-09-23)

A root-owned `/tmp/patchspace.sock` + a daemon that only *logged* its failed
unlink = a headless daemon whose start-up sweep destroyed the serving
daemon's 98 objects, killed its helpers and left the user's mic graph
thrashing.  Fixes (all tested):

* default socket is `$XDG_RUNTIME_DIR/patchspace.sock` in all three places
  (daemon, CLI, GUI) - the /tmp name only when there is no runtime dir;
* `_bind_socket` runs synchronously in `start()` and a failure is fatal
  (exit 1 via `daemon.started`), so the daemon can never run socket-less;
* `reap_stale_for_names` raises `AnotherDaemonRunning` when a candidate's
  owner is alive *and* its parent is a daemon (`_is_daemon_process`) - it
  only sweeps true orphans (parent init/user manager);
* `_terminate_orphan_helpers` applies the same predicate.

## The auto-load adoption was a no-op (2026-09-23)

A Nix-generated panel dir is read-only and referenced by nothing, so its files
only ever load through the `auto_load` adoption in `_load_panels_tree`.  That
code appended the child reference to the in-memory root, then reloaded the root
*from disk* (which cannot see the unwritten reference) and wrote *that* back -
so the reference was lost on both sides and the panel never appeared (which is
why a declarative graph could be built, validated, passed into the unit and
still not show up in the GUI).  Fixed to: reload, append to the fresh root,
write, reload again.  Idempotent, and the root file keeps every node it had.

## Scope composition, icons, and the impulse color (2026-09-23)

* **NixOS and home-manager mirror each other and compose**: one `nix/module.nix`, two
  instantiations.  The home-manager scope reads the NixOS scope via `osConfig` and merges on
  top (nodes/edges/groups/panels/scalars; lists accumulate).  Whichever scope is *enabled*
  owns the daemon unit - never both, because a user service belongs to the user and two
  definitions of the same unit collide.  `installClient` puts the GUI client in whatever
  profile the owning scope has; `effectiveConfig` exposes the merged per-panel config (the
  `module-scopes` flake check asserts all of this by pure evaluation).
* **`_node_icon_pixbuf` was dead**: `render_texture(node, None)` is rejected by PyGObject, so
  every icon silently fell back to the hand-drawn glyph.  Icons used now:
  `view-refresh-symbolic` (the reset button on read-only panels) and
  `document-edit-symbolic` (the pencil).  Never pass an explicit None viewport.
* **Impulse port color** is `blue_3` from the theme (Adwaita's #3584e4; stylix maps it to its
  palette's blue - which in the current palette is a muted teal).

* **Declarative panels are placed beside the graph**: a panel file with no `placement.x`/`y`
  (the module's new default - it omits them) is *unpositioned*, and on load the daemon puts it
  to the right of the placed top-level panels, top-aligned, then marks it placed so the
  autosave remembers it (`_place_unplaced_panels`, `panels.PLACE_GAP`).  A user drag is the same
  thing (a geometry on the child reference marks it placed).  Nested panels are left alone.

## Colors follow the theme; header text never breaks mid-word (2026-09-23)

* `theme_palette` gained `accent` (`blue_3`) and `slider_track` (the card lifted like a field).
  The impulse port/button and all sliders share the accent; slider labels/handles are
  `subtext`/`text`.  A **panel with no color of its own** (`#3584e4`, what the daemon stores by
  default) is drawn from the palette per panel via `theme_class_color`; a panel someone colored
  keeps its color.  Never hardcode a color here - the fallbacks already carry the old literals.
* Node width = `max(NODE_WIDTH, _header_needed_width)` (chrome of each line + its widest word),
  and `_header_line_layout` no longer double-subtracts the left icon inset (it was 10px short,
  which is why line 0 overlapped the badge and wrapped "Invert" into "Inve"/"rt").
* The right-hand panels list is 300px and visible by default (the divider is still draggable,
  the toggle and its close button hide it).

## Selection ring, wire bends, themed pickers (2026-09-23)

* A selected node's ring is drawn inside `_draw_node` (2px inset, before the sockets) - the
  overlay ring crossed every port on the node's edge.  The marquee rectangle is still overlay.
* `draw_square_path` caps each blend so a segment keeps `MIN_STRAIGHT` (6px) of straight run:
  blending a short jog end to end made wires look like they folded back into themselves.
* Color pickers' presets come from the theme (`_group_colors`, `blue_3`…`teal_3`), so picking a
  color matches the desktop; `GROUP_COLORS` stays as the fallback list.

## Wires stay orthogonal; color slots (2026-09-23)

* The wire pipeline is `_simplify_orthogonal` → `_snap_to_grid` → `_drop_short_straights` →
  `_dehairpin`.  The sigmoid blending (`_sigmoid_short_segments`/`_smooth_jogs`) and
  `_round_short_ends` are gone: they made wires read as "weird curvature", and where a blend
  folded back, as the wire clipping into itself.  `_drop_short_straights` now only merges when
  the joined run stays *axis-aligned* (no short diagonals).  `_simplify_orthogonal` keeps the
  first/last segment's *direction*, not just its orientation, so a merge can't send the wire
  backwards out of its socket (the "stubs overshoot" hook).
* `tests/test_wire_escape.py::test_tight_span_stubs_meet...` used nodes that *overlapped* in x,
  so no non-diagonal route could satisfy it - it passed only through the endpoint-node-exempt
  cosmetic passes.  It now uses a real 30px gap and asserts the stub cap, the outward
  direction and that the jog is inside the gap.
* Colors: `@blue`…`@teal` slots resolved per frame (`resolve_color`), pickers store the slot,
  the module's panel color defaults to `@blue`, `#3584e4` reads as `@blue`, the side-view dot
  resolves the same way.  `FIELD_BOTTOM_PAD` (10) is the one place the pad below a node's field
  row is defined, for the rect and the height alike.

## Pinch to zoom (2026-09-23)

`view_mixin.py` had scroll-zoom (about the pointer) and middle-drag pan, but **no
`Gtk.GestureZoom`** - pinching on a touchscreen did nothing.  Added, sharing `zoom_about(x, y,
factor)` with the wheel.  Two traps: `::scale-changed` is cumulative since the gesture started
(record the starting zoom in `::begin`, or the zoom compounds), and a trackpad zoom gesture has
no bounding box (`get_bounding_box_center` fails) so the pointer is the fallback centre.
Single-finger panning relies on GTK emulating button 1 for touch, which the primary-button drag
gesture accepts; tested only structurally (no touchscreen here).

* **Declaring a graph is optional.** `enable = true` with no `panels`/`nodes`/`edges` is a
  complete configuration (the GUI's own panels are then the graph).  In that case the module
  generates *no* panel files: an empty `main` panel is still a file, and `auto_load` makes the
  daemon adopt it, putting an empty "Main" panel on the canvas.

## Curves, even corners, crisp text/icons (2026-09-23)

* `_sigmoid_short_segments` is back (single pass, not the old `_smooth_jogs` loop):
  a short perpendicular step between two parallel runs is blended into an S again - that
  is what a few-pixel jog between nearly-level sockets wants.  `_round_short_ends` stays
  gone (it was what folded a curve back over the path).  `_drop_short_straights` merges
  only axis-aligned runs.
* `draw_square_path` uses `square_path_radius`: **one** radius for every bend of a wire
  (the smallest that fits them all), instead of each vertex picking its own.
* `wrap_text_lines` decides the line breaks once, in world units, on the widget's Pango
  context; `draw_text_wrapped(cr, …, widget=self)` draws those lines.  Before, Pango
  wrapped at draw time on the zoom-scaled context, so a marginal word could fit at one
  zoom and wrap at another, and the wrap could disagree with `wrapped_text_height`.
* `_draw_node_icon` rasterises at `size * zoom` (quantised to 4px steps) and scales back
  down, so icons are crisp when zoomed in.

* **Icons are rasterised at one fixed size** (`ICON_RASTER_PX = 96`) and scaled to the
  target by `icon_placement`.  Rasterising per zoom looked right in theory but GTK returns
  a differently-sized pixbuf per request with a *different* padding fraction (13px for 20px,
  58px for 64px), so the drawn glyph changed size with the zoom.  `icon_placement` derives
  the scale from the pixbuf it got, so the target size is exact regardless.

* **The Filter node has no field.**  Kyle rejected a title box on it outright ("it's a
  filter node, it's just supposed to have those two inputs"): the node is its sockets (the
  bundle `in`, the dynamic `filterN` classifier inputs, `out`) plus the Include/Exclude
  switch.  Selecting by title is `TitleClassifierNode`'s job - a classifier like
  Regex/Description, plugged into the filter input.  Its `exclude` switch is the gate
  toggle's button captioned INCLUDE/EXCLUDE; `control="filter_mode"` must stay in
  `_bottom_control_height`'s GATE_AREA_HEIGHT branch or the button rides over the sockets.
* **Bundle/filter wire colors are theme lookups** (`yellow_3` / `purple_3`), not literals.
  Not the blue slot: under stylix the numbered palette is flattened onto base16, so
  `blue_1`..`blue_5`, `accent_color` and the audio `link` color are all the *same* color -
  a blue bundle wire would have been indistinguishable from an audio one.  (Same reason the
  impulse's `blue_3` collides with audio there; reported, not changed.)

* **A Button's face says what it does.**  `_impulse_label`: the node type is "Button", so
  an unrenamed node's label *is* "Button" - the face shows "Trigger" for that and the
  user's own label once they rename it.  The face also lifts one grey step while
  `hover_impulse` holds it (`on_motion` sets it, `on_leave`/motion clears it, repaint only
  on the edge); that hover is deliberately the only cue besides the press pulse.

* **Title / Application classifiers are chosen from live lists** (`field_choices=True`):
  the field shows a caret, and clicking it asks the daemon (`get_titles` - new;
  `get_applications` - reused, both directions deduped) then opens
  `_show_choice_popover(..., search_hint=...)`, the shared list popover with a search
  entry over a `Gtk.ScrolledWindow`.  The typed text is always offered as its own row -
  these classifiers match substrings.  `choice_row_visibility` holds the rule (tests pin
  it); connect the entry to `"changed"`, *not* `"search-changed"` (GTK debounces the
  latter ~150ms, which only adds lag to lists already in memory).
* A GTK popover has **no offscreen snapshot** without a realized window (WidgetPaintable
  returns nothing), so the pickers are verified by tests, not by a rendered image;
  `Gsk.RenderNode.draw(cr)` exists for the cairo-canvas side.

* **Application vs Subprocess classifiers.**  `AppNameClassifierNode` ("Subprocess")
  matches `application.name` - which for apps that delegate audio is the *subprocess*
  ("Chromium input", "WEBRTC VoiceEngine").  `AppClassifierNode` ("Application") matches
  `pwmatch.app_key`: the systemd app scope behind the stream's process
  (`/proc/<pid>/cgroup` -> `app-vesktop-3807669.scope` -> "vesktop"), falling back to the
  process binary then the application name.  `{"appKey": ...}` is the filter key; the
  picker gets its list from the daemon's `get_apps`.  Verified against Kyle's live graph:
  the Vesktop audio-service client (pid 3808060, binary electron) resolves to "vesktop".

* **Ports are named after what they carry** (`audio`/`impulse`/`sound`/`bundle`); role
  names stay (ctrl, filter1, in1, a/b).  Three places must agree: the specs, the node
  classes' `port_kind`, and the resolvers that match an edge's `to_port` - a rename that
  misses one silently stops a control from resolving (the boolean gates broke exactly
  that way).  `session_repair._normalized_port` rewrites legacy `"in"` on load, which is
  why no legacy edge is dropped as a bad port.
* A `WidgetPaintable` needs the widget allocated; a *GTK popover* still can't be
  snapshotted offscreen.  The canvas renders (cairo) are the way to check node faces.

* **Exclude means silence** (`PatchSpace._sync_silenced`): a Filter with `exclude` on
  records what it dropped each sync; those streams lose every link patchspace didn't
  create, and get them back when they are no longer excluded.  Without it, "exclude" only
  removed the graph's own link and the app kept playing through the session manager's.
* `Bundle -> Audio` is dummy-backed now (members sum into its internal sink, output = the
  monitor), so its downstream is stable across bundle changes.  Its backing name default
  is `bundle_audio_<id>`.

* **Rule of thumb (Kyle's): every node should have at least one sink; if it has none, give
  it a dummy sink.**  A private internal null sink whose monitor is the node's output.
  That is what makes the node's socket stable (upstream churn - members, a Filter's
  Include/Exclude, a player's sound - never moves the wire downstream) and gives it a
  private place to do its work (mix, drop, sum) without touching anyone else's links.  A
  *transparent* node has neither, which is why the Filter used to silence an excluded app
  globally instead of just dropping it from its own chain.  See `FilterNode`,
  `BundleToAudioNode`, `BundleOutputNode`, `SplitterNode`, `SoundPlayerNode`.

* **A serialized parameter name must never collide with a method or property.**
  The export walks `_SERIAL_ATTRS` and hands each value to json; the Recorder's
  take-controls were called `start`/`stop`, and `start` is a serialized
  parameter (the Clip's), so the autosave tried to encode a *bound method* - which
  raises **outside** `_tick`'s try/except, so the failure was not a logged
  "supervision tick failed" but a dead supervision loop: every node sat "not
  connected" forever with only a truncated three-line traceback in the log.
  The registry-wide `test_every_node_type_exports_to_json` covers the whole
  class of bug, not just this instance.

* The tick's two independent post-steps (the session autosave and the panel poll)
  each have their own guard now: sharing one meant a failing export starved the
  panel poll for as long as it kept failing.

* **A stable path is not a stable file.**  A Recorder's take always writes the
  same path (recording wipes the file and rewrites it), so the GUI's "ask for
  the waveform when `source_path` changes" rule never fired again: after a
  re-record the timeline kept showing the *previous* take - a silent one looked
  like a recorder that had recorded nothing.  The waveform is now re-asked when
  `recording` flips (either direction: the file is wiped at the start and
  rewritten at the end), which is the only signal that changes.

* The waveform refresh follows a *take*, not just the node that made it: a Clip
  fed by a Recorder has the same stable path, so it stayed stale too.  The poll
  collects the take paths whose recorder flipped `recording` *before* walking
  the nodes (the consumer can be visited before the recorder), and any node
  whose `source_path` is one of those re-asks.

* **A recursion guard has to precede the recursive branch.**  `bundle_members()`
  resolves a Split through its upstream, and that upstream can be another Split;
  the branch ran before `seen` was initialised, so every recursive call started
  from an empty `seen`.  A chain terminated, a *cycle* did not - and a cycle is
  easy to make by hand.  It is reached from `get_nodes` (served under the
  daemon's lock), so the symptom is a Split Bundle with no members and a view
  that stops updating rather than anything that names the recursion.
* Anything that *raises* inside `get_nodes` costs the GUI its whole update path,
  not just the offending node: `handle_command` turns it into an error reply and
  the view simply stops changing.  When a GUI report is "X stopped working", it
  is worth reading the daemon's log for the command that failed, not only the
  widget.

* **A Filter is not an ordinary bundle endpoint.**  It is a *backed* node, so
  its output is its own dummy sink's monitor - not a set of live sources.  A
  Bundle Split downstream of one therefore resolved to *no* members at all (the
  generic `find_source_nodes` over a sink identity matches nothing), so the
  split showed no lines and nothing wired: "it works, but not on the other side
  of a filter".  Members now resolve *through* a Filter and are narrowed by its
  classifiers, which is what the filter means everywhere else.
* Watch for `seen` sets shared across two functions that each guard with them:
  passing a caller's `seen` into a helper that has already added the node reads
  as a cycle and silently returns "nothing".

* **`pw-cat --record --target <name>` falls back to the *default source*.**
  A sink is not a capture target, so the recorder - which must read its own
  sink's monitor - silently recorded the *microphone* instead, and reported
  success.  The take is now started with `node.autoconnect = false` and linked
  to the monitor explicitly (`RecorderNode._link_to_monitor`), and a take that
  cannot be linked *fails* rather than recording the wrong stream.  This was
  also the recorder's "flakiness": whichever way the name lookup went, the take
  was either the node's input or the mic.

* **Measure before restructuring the draw.**  The instinct was "cache each
  node's rendering": a frame is 682k calls and looks like per-node cairo work.
  Profiling by *cumulative* time said otherwise - roughly half of it was
  *layout arithmetic* (`_socket_position` 220x a frame, `_socket_margins` 220x,
  header wraps and Pango measures), and the driver was `update_from_daemon`
  clearing the whole dimension cache on *every* poll (every 400ms), so the next
  frame re-derived every visible node's geometry.  Dropping a node's cached
  geometry only when its fingerprint changes took the poll+draw cycle from
  17.8ms to 15.7ms.  A render cache would have been a large change for less.
* Idle is cheap: 1.75% CPU, flat RSS over interaction.  A warm frame for a
  95-node session is 13-18ms, i.e. ~60fps, so "the UI freezes" was never
  visible in the steady state - it needs a specific reproduction (which action,
  how long) rather than more guessing.

* **A path is not a file** - the third time this bit in one session (the GUI's
  waveform re-ask, the daemon's source_rev, and now the sound caches).  A take
  exists as a *growing* file, so a per-path cache made the first read of a take
  the answer for the whole take and the timeline sat still until Stop.
  `probe_duration`/`probe_peaks` are keyed on (path, revision) - mtime and size
  - and that alone made `forget_sound` (a cache-buster called on stop)
  redundant, so it is gone.  When a cache is keyed on the wrong thing, the
  invalidation code it needs is a symptom, not a solution.
* ffmpeg reads a WAV whose header is not finalised yet (a recording in
  progress): `probe_peaks` on a live take returns its buckets so far.  That is
  what makes the waveform fill in as it records.

* **Never do slow work holding the command lock.**  `_cmd_get_peaks` ran
  `probe_peaks` (an ffmpeg pass over the *whole* file) inside `with self._lock`
  for a Recorder, and the GUI asks for a take's waveform on every poll while it
  is being recorded: every other command - the GUI's own polls included -
  queued behind one decode after another.  That is perceptible only in the
  sound chain, and it compounds with the number of sound nodes.  Resolve the
  path under the lock, decode outside it.
* **Apply the newest state, not every snapshot of it.**  `process_responses`
  applied every queued reply, so a backlog became a storm of full UI updates
  that could only end up showing the newest one anyway.  Poll replies coalesce
  to the last; events stay in order.
* A file that changes on every poll (a take being recorded) should be *loaded
  on a leash* - at most once per interval - with an exemption for the moment it
  stops, which is the state the user is waiting for.

* **A drag must own the graph.**  `on_layout_tick` ran the force layout *during*
  a node drag - `dragging_node` only governed whether the layout went to sleep -
  so the physics kept stepping underneath and sprang the node away from where
  the pointer put it.  Reported as "when I move around the handles it doesn't
  move": it moved, and then moved back.  The tick now returns early while a
  drag is in progress and resumes on drop, from where the node was dropped.
* **Closing the window closed nothing.**  `_on_close_request` left
  `app.window` pointing at the destroyed window and never quit, so a later
  activation called `present()` on a dead window instead of building a fresh
  one - the UI looked like it reopened itself.  It clears the reference and
  quits now; the daemon has its own service, so nothing needs the GUI to linger.
* **No live waveform.**  `probe_peaks` is an ffmpeg pass over the whole file,
  so following a take while it records is not worth it at any leash.  A take is
  loaded when it *ends*; other changes go through the 1.5s leash.

* **Clear the data you mean to clear, not the entry that holds it.**  Blanking
  a clip's waveform while its take recorded was done by dropping the
  `_clip_waves` entry - which also dropped the timeline's view state (zoom, its
  drag handles, the selection) kept beside it.  The entry is blanked now
  (`peaks = []`), not removed.
* **A flag on one node is not a fact about the graph.**  "Is this file being
  written?" was asked as `ndata.get("recording")`, which only a Recorder has, so
  a Clip downstream of one never saw it and kept loading the take live.  Collect
  the recording take *paths* before the node loop and judge every node against
  the file it is showing.

* **An optimistic placeholder has to be a *complete* node.**  The dict the GUI
  shows before the daemon confirms a new node was missing `"id"`, and drawing a
  node asks for it (`_bottom_control_height` -> `_impulse_wired(node["id"])`), so
  adding anything with an impulse input - Sound Player, Recorder, Button - raised
  KeyError and the node never appeared.  Adding one through the daemon worked,
  which is what localised it to the GUI's optimistic path.
* **Take the daemon's truth every poll; guard only the *echo*.**  `recording`
  was applied when a node was first seen and never again, so a take that ended
  on its own left the button showing Stop for ever - and pressing it asked to
  stop a take that was already over.  The poll's value is applied every time
  now, with `_accept_bool_echo` holding an optimistic press until the daemon
  agrees (the guard the gates and switchers already used).

* **An exception after an optimistic flip is a button that looks alive and does
  nothing.**  The Record press flipped its node locally and *then* raised (a
  mistyped local - `nid` for `record_hit`), so the command was never sent: the
  button showed Stop, the next poll put it back, and recording was dead - "I
  click record and it instantly stops".  Test the *press path* itself
  (`_record_button_rect` gives the coordinates, `on_click` drives it), not just
  the state it manages: the state-level tests all passed while the button did
  nothing at all.

* **A deferred popup must not run against a torn-down widget tree.**  The suite
  segfaulted, and the faulthandler pointed at `popup_context_menu`'s deferred
  `_show`: the idle it schedules can outlive the window or canvas it was
  requested from, and `popover.popup()` on a popover that is no longer rooted is
  not a warning in GTK - the tree behind it has been freed.  Both the popover's
  and the canvas's root are checked now.  This is the shape of "it crashes
  everything at random", and it is timing-dependent, which is why hand-driven
  event sequences never reproduced it: only pumping a real main loop after a
  teardown did.
* **An optimistic UI action must apply every field it owns.**  The panel colour
  dialog applied the colour and the auto-load flag only when the *name* field
  was non-empty, so an empty name silently discarded them ("Apply does nothing").
  Both settings dialogs now keep the name/id they have and apply the rest.
* Per-node caches are dropped with the node, not left behind: geometry,
  fingerprint, waveform, view state, asked-at, recording flag, pending
  positions, effect slider, physics velocity.  A node reusing a freed id would
  otherwise inherit the previous one's shape.

* **Don't cache a read that raced the writer.**  A take that has just stopped
  can be decoded before the file is finalised: no peaks and no length, for a
  file that has content.  Caching that under the file's revision meant every
  retry got the same nothing until some *later* revision came along - "the clip
  is blank and the waveform never shows up, and it resolves itself but takes
  forever".  Both `probe_peaks`/`probe_duration` and the GUI's own revision
  bookkeeping now treat an empty answer for a file with content as "ask again",
  and only cache answers worth keeping.
* **An autosave that runs per tick while the user is dragging is a lag machine.**
  Every morphing action marks the session dirty - a clip's times, a node's
  layout, dozens of times a second during a drag - and exporting the whole
  session each time left the daemon writing while the pointer moved on.  The
  GUI's polls then carried stale positions, so a dragged selection *snapped back*
  to where the daemon last knew it and the app only caught up when the drag
  stopped ("the numbers take forever to catch up, and only then is it responsive
  again").  The autosave waits for a quiet 2s now.  The same export-per-motion
  cost is the best explanation so far for the "bogs down as time goes on"
  reports, too: it scales with the size of the session.

* **Don't send every tick of a gesture, and don't do slow work while the user is
  moving.**  Three things made the daemon lag minutes behind the pointer, all of
  them in the sound path:
  - the clip's times were sent on *every* motion (`_drag_clip`); the daemon only
    needs where the clip was *dropped*.  The node still updates locally under the
    pointer, and `_flush_clip_times` sends once on release.
  - `probe_duration` runs while the nodes are being serialized, i.e. *under the
    command lock*, and a take that is being recorded changes its revision on
    every poll - so each poll ran ffprobe with every other command queued behind
    it.  Re-probing the same path is now floored at 2s (a still-recording take's
    length is not meaningful anyway).
  - the autosave exported the whole session per tick while every morphing action
    marked it dirty (see above) - it now waits for a quiet 2s.
  The general rule: anything that *executes* long after it was *sent* is a bug of
  its own, separate from whatever it was sent to do.

* **A new node type has more places to live than it looks.**  `SoundDumpNode`
  needed: the class, the daemon's registry and constructor, the
  `set_node_property` dispatch for its own fields, the payload it reports, the
  GUI spec (field + settings), the add menu, the icon map, and the class->type
  map.  Grep for an existing node of the same *shape* (the Sound Player: an
  impulse plus a sound) and mirror every one of them.

* **The bundle presets must exclude our own plumbing.**  A Patch Space keepalive
  *is* a `Stream/Output/Audio` (it is a pw-cat playback stream), so `All Apps`
  contained the pipeline's own signal - and a bundle that carries the pipeline's
  output back into it is a *loop*, which is what made a filter on All Apps look
  like it "let everything through" and killed the chain.  The source and sink
  matchers take an `externalOnly` key now (backed by `is_patchspace_owned`, the
  predicate the External Only classifier already used) and All Apps / All Inputs
  / All Outputs ask for it.  Mic sinks and virtual speakers were never members:
  Audio/Source and Audio/Sink are not Stream/Output/Audio.

* **A control can be shadowed by a *generic* branch above it.**  The Sound Dump's
  own height branch was unreachable because `_bottom_control_height` checks "an
  impulse input with nothing wired" first, and a dump matches that too - so the
  node never grew and its face crowded the socket labels.  Put the specific case
  first; and put clearance in the *node's* height rather than in the block's own
  rows, or growing it cancels out and nothing moves.
* **The XDG portal's FileChooser has `OpenFile`, `SaveFile`, `SaveFiles` - there
  is no plain `Open`.**  Calling a method that doesn't exist fails into the
  fallback, which looks exactly like "no file picker available" while the same
  dialog works elsewhere.
* A name that carries an audio extension should have it *replaced* by the
  encoder's, not appended: "take.mp3" means "take", and "take.mp3.opus" is a
  file nobody goes looking for.
## A press, its reply, and the stale GUI (2026-09-24)

"Sometimes the stop button stops working and appears to get detached from the running
recording."  Three separate faults, all real, all fixed here:

* **Ownership is given up only when the process is really gone.**  `OwnedPwProcess.destroy`
  cleared `_proc` *before* signalling and `RecorderNode.stop_take` dropped `_recorder`
  before calling it, so a take whose `pw-cat` survived SIGTERM+SIGKILL (a child stuck in the
  kernel, e.g. writing to a disk that filled) read as idle: `recording` False, no handle
  left to signal, the file still being written, and the next press asked the daemon to
  *start* a take it thought nothing was doing (`start_take` early-returns, so the button
  did nothing at all).  Now both keep their handle when the kill did not take, log it, and
  report the truth - so a second press tries again.  `ReplayBufferNode.sync_take` gets the
  same protection for free: it used to spawn a *second* pw-cat on the same file.
* **Known leftover, deliberately not touched:** `_coalesce_responses` drops superseded
  `get_nodes` replies but *applies the surviving one last*, after the events.  A pre-press
  poll that lands in the same drain batch as the press's reply is therefore applied after it,
  which can flip a button for one frame (~50 ms) before the next poll corrects it.  Fixing it
  means applying each nodes reply at its arrival position (skipping only superseded ones),
  which also moves the peaks/nodes order - worth doing on purpose, not as a drive-by.
* **The reply to a press is a fence.**  The GUI's optimistic value was only released when a
  poll *agreed* with it, which never happened when the daemon had not done what was asked -
  the button held the press for ever.  A command's reply comes back on the same connection,
  in order, so every poll taken before the press has already been delivered: the reply is
  authoritative.  `PatchSpaceGraphWidget.on_record_reply` (routed by `main_window`, matched
  to the press by the `_record_asks` FIFO) releases it, and `_pending_bool` entries now
  expire after `PENDING_BOOL_TTL_S` for the commands that are never answered at all (the
  client drops a command when the connection goes).  `_cmd_record`'s error replies carry
  `node_id` for exactly this, and `_coalesce_responses` passes error replies that name a
  node through instead of only logging them.

**Diagnose a stale GUI before believing a UI report.**  The daemon and the GUI are launched
separately (service vs `systemd-run --scope` from the session), so a GUI started before the
last activation goes on running the *old* store path for the life of the session - on
2026-09-24 the running GUI was two commits behind, missing the fix for the permanent
poll-timer-per-action leak that makes the UI degrade with use.  `which` and the profile
cannot tell you; the argv can:

```bash
ps -o cmd= -p "$(pgrep -f patchspace_gui.py | head -1)"          # which src?
nix-store -q --references /run/current-system/sw/bin/patchspace  # which src the profile runs
```

Same check after every activation, and treat a mismatch as "restart the GUI" before
debugging anything visual.

**Proving a regression test is red:** `git stash push -- <the fixed files>` (leave the new
tests in the tree), run them, `git stash pop`.  Both GUI tests here failed that way -
`assert False is True` on the frozen button, and `AttributeError` for the method that did
not exist yet - and the daemon test failed on `node.recording is True` while its own
stubborn fake pw-cat was still alive.


## Mono nodes bridge both channels (2026-09-26)

`resolve_channel_pairs` matched ports by shared `audio.channel`, so a node whose port is
`capture_MONO` (a mono mic / a mono layout) shared no channel name with FL/FR and resolved
to **no pairs at all**: a mono source fed silence, a mono sink received nothing.  A lone
MONO port is now bridged - when the mono side is the *target* every channel of the other
side is linked into it, when it is the *source* the one MONO output fans out to every
channel of the target.  PipeWire accepts and mixes both (verified on a private instance:
two `monitor_FL` -> one `playback_MONO`, and one `monitor_MONO` -> both `playback_FL/FR`;
every `pw-link` exited 0 and `pw-link -l` lists all of them).  Detection is by
`audio.channel == "MONO"` only (`pwmatch.MONO_CHANNEL`): a single port with *no* channel -
a filter-chain module's bare `Input`/`Output` - is still skipped by
`port_groups_for_node`, unchanged.

## 2026-10-09 — three-layer plan, phase 0 (repo surgery)

* Plan of record: `agents/PLAN.md` (approved).  `patch` -> **patchSpace** on disk; the GUI
  becomes `~/projects/patchSpaceGui` in phase 7.
* Tree now: `src/central/*` (daemon core) + `src/modules/pipewire/*` (pw substrate) +
  `src/gui/*`.  Everything imports with `src/` as the import root: `central.main`,
  `modules.pipewire.pwgraph`.  Entry points are `python -m central.main` and
  `python -m central.session_repair` (flake wrappers set `PYTHONPATH=${source}`); the GUI's
  dev-spawn runs `[sys.executable, "-m", "central.main"]` with `cwd=<checkout>/src`.
* `PipewireGraph._is_daemon_process` matches BOTH dev entry shapes (`main.py` from an old
  daemon, `central.main` from a new one) - the orphan-helper guard depends on it.
* 90 bytecode files untracked; `git ls-files | grep -c pyc` is 0.
* Suite: 357 passed, 28 skipped headless (the 9 GUI files still segfault without a display).
* mycelion's `patchspace` input is the **github** url (its `path:` line is commented out),
  so this split only reaches a host after a push + `nix flake update patchspace` there.

## 2026-10-09 — phase 2 (GUI draws from the daemon's catalog)

* The node-type data moved out of the GUI: `modules/pipewire/catalog.json` is
  the module's catalog (types, ports, controls, display, the add-node menu, the
  media-class vocabulary, the legacy aliases).  `gui/node_catalog.py` installs
  what the daemon serves (`catalog_request` -> `catalog_reply`) and answers
  every lookup the canvas makes; `gui/node_specs.py` is gone.
* **Namespace-package trap:** `gui/` has no `__init__.py`, so `gui.node_catalog`
  and a flat `import node_catalog` are *two module objects* for the same file.
  The widget imports its neighbours flat, so anything that installs the catalog
  must use the flat import or the widget reads an empty catalog (the header
  shows "Unknown" and every port is "audio").  `tests/conftest.py` puts
  `src/gui` on `sys.path` at collection time and installs the catalog in an
  autouse fixture (process-global state: one test clearing it must not poison
  the next).
* Offscreen canvas proof (recipe unchanged): install the catalog, build the
  widget with a stub client, `update_from_daemon`, pump the main loop ~1 s
  (first frame is alpha 0), render `on_draw` into a cairo surface.  It draws
  type labels, per-type icons, socket labels ("audio"/"ctrl"), the gate's
  on/off fallback, a filter's dynamic sockets and camera wires - all from the
  wire payload.
* `central/session_repair.py` no longer imports the GUI: it builds a small
  port/label table from the same catalog data and asks `central/graph.py` for
  the compatibility rule (the one authority).

## 2026-10-09 — phase 2 tail: the gray nodes are real

* `get_nodes` now carries the registry's `status` (+ `status_reason`) per node,
  and appends synthetic entries for placements nothing is running (a node kept
  for a module that is not loaded): id, type, remembered position, status.  So
  the canvas can draw a node that has no model object behind it.
* The canvas draws those inert: dim dashed outline, the body washed out with a
  60% `node_bg` overlay, and a one-line badge ("◌ no module" / "◌ offline" /
  "◌ needs input").  Keep the badge *short* — a long one wraps and the header
  height/drawing disagreement shows up as a clipped line.  The registry's full
  reason rides in the node's hover tooltip.
* Which placements are "kept" vs "gone": `sync_registry` remembers the instance
  ids it mirrored from live model nodes (`self._mirrored`).  An id in there that
  is no longer live was deleted/renamed/reset/rebuilt → forget it; a placement
  that was never mirrored came from persistence → keep it (that is the gray
  node).  The sync hook runs after `add_node`/`remove_node`/`rename_node`/
  `reset`/`rebuild`.
* `main.py` needed `from central import module_api as api` for the status
  constants — a missing import there turns every command into a silent error
  reply (`_serialize_nodes` raising is caught by the dispatcher).

## 2026-10-09 — phase 3 groundwork: modules as processes

* `modules/host.py` is the module-side client (`ModuleHost`): connect, hello,
  declare, announce, answer the migration fan-out, dispatch `control_set` /
  `edge_apply` / `status_change` / `value_set`, reconnect with backoff.  The
  in-process PipeWire provider speaks the same protocol through
  `ProtocolHandler`, so this is the transport half of the extraction.
* **The declaration fan-out needs a terminator.**  `type_declare` answers with
  the `migrate` messages first and one terminal `declared`/`error` last; a client
  reads until it sees something that is not a `migrate`.  Without that, the host
  blocked on `recv` waiting to find out the fan-out was over (the test suite
  hung for the full timeout).
* `ModuleHost.pump(timeout=0)` must `select()` before `recv()` — a zero-timeout
  "poll" that blocks is not a poll.
* `central/module_supervisor.py` runs module processes: start on attach, restart
  with backoff (give up after 5 fast failures — a failed `Popen` counts, so
  `proc is None` has to go through the same path as an exit), stop with the
  daemon.  Children use `start_new_session=True` so a daemon *crash* leaves them
  (the sweep/restart story), which means `stop()` must kill them explicitly.
* The test rig `tests/test_module_host.DaemonSide` is the real handler on a real
  socket; it must detach a module when its socket drops, exactly like the
  daemon's client loop, or a restarted module gets a fresh id (`childmod#2`) and
  its types are refused as already-declared.
* `number` is now a declared port kind (the primitives module's wire).

## 2026-10-09 — the daemon routes a display's requests to the owning module

* `api.node_control` / `api.node_edge` are the *request* forms (a display
  asking); `control_set` / `edge_apply` stay the daemon -> module forms.  The
  daemon looks up `registry.instance(id).declared_by`, validates an edge through
  `topology.can_connect` (the one authority), records it, and forwards.
* `Connection.send` is the single forwarding path: a socket connection writes
  over its socket, an in-process module has a callable (`conn.deliver`).  The
  in-process provider sets `conn.deliver = self.apply` in `attach`, so the same
  routing works before and after a module moves to its own process.
* `ProtocolHandler.register_module/unregister_module` keep the module_id ->
  connection map; `_hello` registers, the daemon's client loop unregisters on
  drop (alongside `registry.detach`).
* Why this had to come before the primitives module: a module-owned node can be
  *drawn* (phase 2 serves its declared types) but could not be **wired or
  controlled** — the GUI's legacy commands target the daemon's model, which has
  no object for a node a module owns.  With this path, a module's nodes are
  usable, so primitives can land on top of it.

## 2026-10-09 — the primitives module, and what a module-owned node needed

* A module-owned node (one the daemon does not model) needed three things to be
  usable, all added here: **`node_spawn`** (daemon -> module: run this instance;
  the daemon creates the identity/placement, the module runs it),
  **`node_values`** (module -> daemon: what the nodes currently compute, so a
  display can show it) and the daemon's `add_node`/`remove_node` falling through
  to `ProtocolHandler.spawn_instance`/`stop_instance` when the type is declared by
  a module rather than in `NODE_TYPE_REGISTRY`.
* `_serialize_nodes` now also serves *online* module-owned instances (it used to
  append only non-online ones), with `values` when the module reported any.
* `attach_modules` starts the supervisor **only when the socket is bound**
  (`self._server is not None`): module processes attach over the socket, and a
  test that builds a daemon without starting it must not spawn anything.
* `modules/primitives/` is the first out-of-process module: `types.py` (8 node
  types), `engine.py` (a bounded fixpoint over its own edges — a user-drawn cycle
  settles instead of hanging), `main.py` (a `ModuleHost` + the daemon's requests).
  Its controls are `number`/`slider`/`toggle`/`choice` widgets — the display's
  widget vocabulary is still closed (it draws what it knows and ignores the
  rest), so a primitives node's slider is not yet draggable in the canvas; the
  value *is* settable through `node_control` and shown via `values`.

## 2026-10-09 — phase 6: choosing the modules

* `central/module_config.py` is the one source of truth for the enabled set:
  `~/.config/patchspace/modules.json` (or `$PATCHSPACE_MODULES_CONFIG`, or
  `--modules-config PATH`).  A module not mentioned keeps its default (the
  daemon's own built-ins are on).  A **store path** is treated as
  deployment-managed: readable, and `set_enabled` refuses (the CLI says so).
* `attach_modules()` is now the single config-driven path: the in-process
  provider attaches or detaches, and the process specs are rebuilt from the
  enabled set.  Commands `modules` (report) and `set_module` (write + apply
  live).  Disabling takes a module's nodes offline without touching the graph.
* `central/modules_cli.py` → package `patchspace-modules` (`list`/`enable`/
  `disable`); with no daemon it prints what the file says and exits 1.
* nix: `services.patchspace.modules` writes a store JSON and adds
  `--modules-config`; the read-only `modulesConfig` value is exposed.  Verified
  by eval: `{ primitives.enable = false; }` →
  `{"modules":{"primitives":{"enable":false}}}` on the unit's ExecStart.
* **Git-backed flake trap, again:** a new file must be `git add`ed before any
  `nix build`, or the store copy simply does not contain it (the packaged CLI
  failed with "No module named central.modules_cli" until `modules_cli.py` was
  tracked).

## 2026-10-09 — phase 7 is not a pure move (attempted, reverted)

Tried the `patchSpaceGui` split and backed it out.  The **repo** splits cleanly —
a tree with `gui/`, `packaging/`, its own flake builds `packages.patchspace` and
runs — but the **suite** does not:

* nine files under `src/tests/` (test_gui_interaction, test_button_node_ui,
  test_clip_ui, test_filter_node_ui, test_canvas_opacity, test_view_zoom,
  test_panel_physics, test_wire_escape, test_classifier_pickers) drive the canvas
  against *real daemon objects* (`from tests.test_pwnodes import FakeGraph`,
  `central.main.PatchSpaceDaemon`).  They are integration tests of both halves, so
  moving them needs a stub-daemon fixture first — they are not display tests.
* several daemon tests (`test_bundles`, `test_camera_nodes`, `test_impulse`,
  `test_sound_node`, parts of `test_daemon_protocol`) import `gui.node_catalog` to
  assert the *display's* view.  After a split they must assert the module's
  catalog (`modules.pipewire.declarations`) through a small shim — which is the
  right structure anyway (the display's parser is the display's business).
* the nix interface changes with it: `clientPackage` must become an explicit
  option (this flake can no longer supply the GUI), with an assertion when
  `installClient` is on and it is null, and mycelion gains the second input.

Do those three first, then the move is mechanical.  Nothing was left half-split:
`src/gui` and `packaging/` are untouched, and the attempted tree was removed.

* Pre-step (b) of phase 7 is done: `src/tests/catalog.py` is the daemon-side
  adapter over `modules.pipewire.declarations` (with `graph.kinds_compatible` as
  the rule), and the daemon tests use it instead of importing
  `gui.node_catalog`.  The display's parser is the display's business; the
  daemon asserts what the module *declares*.  When the display moves out, these
  tests need no change.

## 2026-10-12 — phase 4: placements keep their own diffs

* A panel file is the **blueprint**, a placement is an **instance**, and each
  instance now keeps its own diff: a control turned or a node moved in one
  instance stays there.  The diff lives in a sidecar beside the session
  autosave (`placement_diffs.json` — the panel-file payload is whitelisted by
  `panels.build_payload`, and the diffs are session state, not part of any
  panel's schema).  It is replayed on load by `_flatten_panels`, which is the
  one place a placement's effective config is produced; a key whose node the
  blueprint no longer has is dropped (the rebase).
* The old behaviour was the opposite: `_sync_placements` *fanned a change out*
  to the file's other placements, and `_canonical_by_stem` picked "the placement
  that changed" to write the file from.  Both had to go: `_capture_diffs`
  records the change instead, and a file is now written **only from a placement
  that committed a change** (edit mode) — or, for a lone placement, as before.
* Order inside `_write_panels` matters: panel files first, then the snapshot
  refresh for what they committed, then the diffs captured against those
  snapshots, then the session.  Capturing first recorded a committed change as a
  local diff.
* Four tests pinned the fan-out ("sync across placements") and were rewritten to
  the new semantics: an instance diverges, the blueprint only changes in edit
  mode, and the file is never written from a local change.

## 2026-10-12 — the end-to-end smoke, and the sweep made instance-scoped

* `scripts/smoke.sh` runs the whole thing against a **private** PipeWire +
  WirePlumber (the recipe above): a declarative panel loads and wires, the module
  set reports pipewire in-process + primitives as a live process, the daemon
  creates a primitives node and a control reaches it, two placements of one panel
  diverge (their diff lands in `placement_diffs.json`), and disabling pipewire
  takes its nodes offline and re-enabling brings them back.  No trace of the
  user's session.  Run it from `nix develop`.
* It found a real defect: the **argv half of the orphan sweep was machine-wide**,
  so a private instance refused to start while the user's daemon was running
  ("another daemon owns the pw-loopback helper").  The pw-dump half is
  instance-scoped; the argv half is not.  Fixed by intersecting it with the pids
  the dump identified (`_terminate_orphan_helpers(markers, candidates)`), so the
  sweep is instance-scoped end to end and the machine-wide hazard in this file's
  header is gone.
* Two smoke lessons: the daemon loads its session on a background thread and
  spawns modules asynchronously, so every check must *poll* (a socket that
  answers is not a graph that is up); and a display connection has to greet
  (`hello` role=display) before the daemon will route a `node_control`.

## 2026-10-15 — S0: panel -> space, and the aliases S1 has to delete

* The rename is **vocabulary only**; `agents/SPACES.md` §9 is the frozen
  contract, and the route's step S1 deletes what follows.  Two rules hold it
  together: **data migrates** (an old graph is the user's work, not something
  that can be re-derived) and **the protocol is read-old/write-new** (a GUI
  started before an activation runs the old store path for hours, so it keeps
  sending the old command names).
* **Deleted in S1 (done — the protocol half of the aliases):**
  * `central/main.py`: `_LEGACY_COMMAND_ALIASES` (the 16 `*_panel*` command
    names) and `_LEGACY_ARG_KEYS` (`panel_id` -> `space_id`, `panel_files` ->
    `space_files`), and `_normalize_legacy_command` itself, which
    `handle_command` called on every message.  Those spellings now fall
    through to `Unknown command`, and an old argument key is simply ignored -
    a GUI started before the update has to be relaunched (the S1 contract says
    so; the pair moves together in one release).
* **Kept: they are data, not protocol** (S1's frozen scope deletes the
  protocol aliases only, and each of these is read, never written):
  * `central/main.py`: `NODE_TYPE_REGISTRY`'s `panel_in`/`panel_out`/
    `bool_panel_in`/`bool_panel_out` entries (the module announces the same
    four through `catalog.json`'s `legacy_aliases`) - a *type id* is a
    persistent contract;
  * `central/main.py`: the env fallbacks (`PATCHSPACE_PANEL_DIR`,
    `PATCHBAY_PANEL_DIR`, `PATCHSPACE_ROOT_PANEL`, `PATCHBAY_ROOT_PANEL`) and
    `LEGACY_OLD_SPELLING_SPACE_DIR` (`~/.local/share/patchspace/panels`, the
    S0 one-time copy into `.../spaces`), plus `--panel-dir`/`--root-panel` in
    `build_parser`;
  * `central/spaces.py`: `LEGACY_TYPE_SPACE` (the `"type": "panel"` marker
    read by `is_space_type`) and `LEGACY_CHILD_KEY` (the `config.panels` child
    list read by `raw_child_entries`);
  * `nix/lib.nix`: `normalise`'s `"panel"`/`"space"` branches (an export
    written before either rename still unwraps to its config);
  * `nix/module.nix`: the three `mkRenamedOptionModule`s (`panelDirs`,
    `rootPanel`, `panels`), the read-only `panelsDir` alias, and the four
    `panel_*` ids kept in the `nodeTypes` enum.
* The one non-symmetry to keep: `LEGACY_SPACE_DIR` (`~/.local/share/patchbay/
  panels`) is *not* S0's - it is the older PatchBay -> Patch Space copy and
  stays.
* `--root-panel`/`PATCHSPACE_ROOT_PANEL` were renamed with the rest
  (`--root-space`) because they are the same vocabulary.

## 2026-10-15 — S1: space type, permanence, the face (what changed, and why)

* **`handle_command` checks `command` before `type`.**  The two protocols
  coexisted by "a message with a `type` key is the module API, one with
  `command` is the legacy command set" - and S1 gives two commands a `type`
  *of their own* (`set_space_meta`, `create_space`), which the old order read
  as a module message of type `"template"`.  The module API never sends a
  `command`, so `command` wins when both are present.
* **The file's `type` key now carries the space's *kind*** (`live`/
  `template`, `agents/SPACES.md` §12: "a space record (payload `spaces[]`, and
  the space files) gains `type`").  It used to be the file-format marker
  (`space`, `panel` before S0), so `is_space_type` accepts all four spellings:
  a root written by an older daemon must still read as *a space file*, or
  `_load_spaces_tree` would "migrate" it as a bare session cache and rewrite
  the graph away.  `spaces.type_from_raw` reads anything but `template` as
  live.  The daemon writes `live`/`template` from then on; the Nix module's
  generated files say `live` too, and `nix/lib.nix`'s `normalise` unwraps all
  four markers (it is what imports a GUI-exported space file).
* **`permanent` is the persistence switch**, filtered per space and outermost
  first (`spaces.persists` + `spaces.prune_unpersisted`, used by
  `_write_spaces`/`_capture_diffs`): a non-permanent space is written to
  nothing - no space file, no child reference in its parent, no edge owned by
  an ancestor that touches one of its nodes, no placement diff - and neither is
  anything nested inside it.  It stays live in the graph, and a *layout* for it
  is still accepted in memory (the app owns it and re-sends it on attach).  The
  GUI's `create_space` defaults `permanent` on, and a space the daemon has no
  record for (a node/announcement inside an id nobody loaded) is created
  `permanent: false` by `_build_spaces_from_space` - that is the app/module
  creation path.  Turning it *off* leaves any file the space already had alone
  (deleting is `delete_space_file`), and turning it off on the **root** is
  allowed and means exactly what it says: nothing is written at all, including
  the session.
* **Templates never spawn.**  `_flatten_spaces` skips a space whose type is
  `template`, and everything nested inside it; `_build_spaces_from_space` keeps
  a template's interior from its *definition* (the record's config) instead of
  rebuilding it from live state, so writing a template can never empty it.
  Because there is nowhere to put a live node inside a template, `add_node`
  refuses an id inside one (and `create_space` refuses `node_ids` with
  `type: "template"`): the way to make one is to build the space live and flip
  the Space Types switch - `set_space_meta {type: "template"}` captures the
  interior as the definition and despawns it, and flipping back materializes it
  again.  `instantiate_template {template_id, parent_space}` copies the
  definition into a nested **live** space (`template` = the source id, its own
  space file, spawned at once; a nested child of the template becomes another
  placement of the file its template child already points at).  The root may
  not become a template: it *is* the session, and a template root would spawn
  nothing on the next start.  Authoring a template *from scratch* (rather than
  freezing a live space) is deliberately left open: `create_space` under a
  template adds a nested space to the definition, and `create_space
  {type: "template"}` makes an empty definition, but a live editing mode for a
  definition does not exist yet - that is the "a template's own edit requires
  edit mode" item in `agents/SPACES.md` §11.
* **The face is derived, the node is the source of truth.**  `space_in`/
  `bool_space_in` carry `mode`/`kind`/`method`/`choices` (`pwnodes.
  SpaceFaceInput`), round-tripping through the ordinary node params
  (`_SERIAL_ATTRS`).  `_serialize_spaces` derives each space's `face[]` from
  the port nodes placed in it - from the *live* nodes when the interior is
  spawned, from the definition when it is not - and `set_space_input` is the
  only writer (it validates: unknown space, unknown key, and a configuration
  the kind does not allow - audio is node-only - are refused and change
  nothing).  `bool_space_in` is a face input of kind `boolean`; an output entry
  carries only `key`/`label`/`direction`.
* `color` no longer defaults to a hardcoded blue: it is `""` (the client
  derives one from the space's name out of the theme vocabulary), and the
  daemon stores whatever name it is given (`"@green"`, a hex string, `""`).
  `set_space_meta` is its one writer - `edit_space` (which keeps the label and
  the auto-load flag) used to take a color too, with different semantics (an
  empty string meant "no change", so a color could not be cleared).
* Not done here (S2+): portal embedding, cross-space wires, so nothing routes
  into an embedded space yet; the face is data a client can draw today.
  (The daemon half of portal embedding landed in S2 - see the next section;
  the drawing, the cross-space wire hit-test and the sidebar/dropdown are the
  client's.)

## 2026-10-15 — S2: portals, the daemon half (a placement by reference)

`agents/SPACES.md` §13, daemon side.  A **portal** is a placement of a live
space inside another space, **by reference**: there is never a second interior.

* **The record.**  The space that *embeds* carries `config.portals` (written:
  `[{"space_id", "view", "x", "y"}]`, `x`/`y` in the parent's frame), and the
  space record a client reads carries `portals[]` plus `embedded_in[]` - the
  spaces that portal this one, **derived** on every payload
  (`spaces.portal_index`), never stored (agents/DECISIONS.md, 2026-10-15).  The
  portal is written with the parent and nowhere else: the target's file is not
  touched by being shown, and a non-permanent target (§3) is still written
  nowhere while the portal that shows it is saved with its parent.
* **The commands.**  `embed_space {space_id, parent_space, x, y}` appends the
  entry (view seeded from the target's `node_view` - the "Node" checkbox is
  only the default), `remove_space_embedding {space_id, parent_space}` drops
  the placement and never the space, and `set_embedding_view {space_id,
  parent_space, view}` sets one embedding's `inside`/`node` (one embedding's
  view says nothing about another).  All three answer with the parent's new
  `portals` (and the target's `embedded_in` where it changed), so a client can
  treat the reply as the fence.
* **Validation is decided before anything is touched** (`spaces.embed_problem`
  + the x/y parse in the handler), so every refusal leaves no partial state: an
  unknown space (either side), a template as the target (its interior is a
  definition - `instantiate_template` is the drop path), a space embedded in
  itself, a space embedded in any of its own descendants (the `Parent::Child`
  id path makes that a prefix test), a duplicate embedding of the same target
  in the same parent, and a read-only parent (its portals are declared by its
  file).  The root can never be the target: it contains every space, so the
  descendant test refuses it.
* **`embedded_in` stays consistent on both sides.**  Removal is consistent by
  construction (it is derived), and a *deleted* space takes the portals that
  named it with it (`spaces.drop_portals_to`, called by `delete_space`,
  `delete_space_file` and `remove_space_placement`): a dangling portal does not
  survive.  A *move* re-keys ids, so the portals that named a moved space
  follow it (`spaces.retarget_portals`), and a move that would nest a space
  into the space it shows is refused with the cycle message
  (`spaces.move_cycle`) rather than left self-referential.
* **Jacks need no new model.**  A space's `face[]` already names each port node
  by its local id, so the real node behind a portal jack is
  `<space_id>::<key>` - the target's own record, resolved by the client.  The
  daemon's `face` output is unchanged, and a wire onto a portal is an ordinary
  edge onto the target's `space_in`/`space_out` (cross-space wires already
  work: the daemon deliberately does not enforce that a wire respects a box).
* **Templates.**  A template's portals are part of its definition, so
  `instantiate_template` copies them: the instance shows the *same* live spaces
  (that is what "by reference" means).  One that would show a space containing
  the instance itself is not copied (it would be a cycle).  `clone_space`
  carries the source's portals for the same reason.
* Not done here: anything the user *sees* - drawing the portal (node-faced box
  or the space box in place), the wire hit-test across a box, the tooltip, the
  sidebar's live-space list and the dropdown&search (S6), and the drag that
  moves a portal's `x`/`y` after it is placed (a client sends
  remove+embed today; a `set_space_layout`-style command for a portal is not
  written yet).

## 2026-10-15 — S4: cardinality is declared, never assumed

`agents/SPACES.md` §15.  How many drivers a *receiving* port takes used to be a
hard-coded list of *kinds* in `central/graph.py` (`SINGLE_DRIVER_KINDS`:
boolean, impulse, sound, camera), with a `multi: true` port opt-out that **no
shipped type declared** - so the restriction was by kind name and a port that
should take several edges had no way to say so.

* **The declaration.**  A port now states `cardinality: "one" | "many"` in its
  type declaration, and a receiving node that needs a merge states
  `merge: "mix" | "sum" | "last"`.  `api.CARDINALITIES`/`api.MERGES` are the
  closed vocabulary and `validate_type_decl` **refuses** an unknown value (the
  message names the type and port) instead of ignoring it.
* **The resolver.**  `Registry.port_cardinality(instance, port, direction)`
  answers the graph: the declaration wins, then a declared `merge` (which
  implies several drivers - `merge` says *how*, it cannot mean "one"), then the
  legacy `multi: true`, and otherwise `api.default_cardinality(kind)` - the
  pre-S4 rule, kept verbatim (`boolean`/`impulse`/`sound`/`camera` → `one`,
  everything else → `many`).  `port_merge` is the daemon-side reader; the
  declaration also travels to a display unchanged in `catalog_reply`
  (`TypeDecl.to_dict` copies ports).
* **The daemon path.**  `graph.can_connect`/`connect` take
  `cardinality_of=Registry.port_cardinality` (the `multi_of` parameter is gone)
  and refuse a second edge only where that answer is not `"many"`.  The graph
  holds no kind list at all now - `SINGLE_DRIVER_KINDS` is deleted - so the
  registry stays the one place that knows what a port is.  `protocol.py`'s
  `node_edge` passes the resolver; the legacy `add_edge` command path is
  unaffected (it stores through the model, whose rules did not change).
* **Shipped exactly where it matters.**  The four space port types declare
  `cardinality: "many"` on their input port: their input is a *bus* today
  (`SpaceInNode`/`SpaceOutNode` `MIX_INPUTS` + `allows_multiple_inputs`,
  `BoolSpaceInNode`/`BoolSpaceOutNode` `ALLOW_MULTIPLE_BOOLEAN` - "several
  sources may be offered, the first wired one wins"), which is exactly what the
  old docstrings claimed and the by-kind list denied.  Net effect: for the two
  *boolean* panel ports the daemon's `node_edge` path now accepts a second
  driver where it used to refuse one - the daemon now agrees with the model and
  the client instead of being a third opinion.  Nothing else declares, so
  nothing else changes: `test_pipewire_declarations` sweeps **every** port of
  the shipped catalog and asserts each declaration-less one still resolves the
  pre-S4 way, and pins the declaration sites to those four.
* Backward compatible on the wire: an old `type_declare` with no `cardinality`
  (including `multi: true`) keeps working exactly as it did.  No persisted data
  changes; edge ids untouched.

## 2026-10-15 — S3: a node's parameter is an input port, and the relay is real

`agents/SPACES.md` §14, the daemon half.  Before this, `modules/primitives`
declared eight node types and **not one input port**, and `value_set` - the
message that would let a node feed another node's control - was defined in
`central/module_api.py` and emitted nowhere.

* **The auto-declared port.**  `api.control_input_ports(ports, controls)`
  builds, for every *value* control, an input port named for it:
  `{name: <control key>, kind, direction: "in", drives_control: true,
  cardinality: "one"}`.  **The registry merges it into `TypeDecl.ports` at
  declare time** (`Registry.declare_types`), which is why a display, the
  snapshot, `type_query` and the connection rules all see it: there is one
  declaration, not a second one to keep in sync.  A name the module already
  declares as an *input* port wins (the module's own declaration is not
  touched); a control key declared twice (the shipped catalog does this: the
  sensitivity gate's `sensitivity`, once as its own widget and once as a
  generic `setting`) contributes one port.
* **What counts as a value.**  A module declares a control's *widget*, never a
  value kind, so the kind is derived: `api.WIDGET_VALUE_KINDS` maps the
  vocabulary (`slider`/`number`/`spin`/`gain`/`volume`/`level`/`sensitivity`/
  `wetdry` → number, `switch`/`toggle`/`checkbox`/`boolean`/`fallback_onoff` →
  boolean, `field`/`text`/`string` → text, `choice`/`select`/`enum` → choice).
  A widget that is not a value - `setting`, `impulse`, `indicator`, `picker`,
  `clip`, `recorder`, `dump`, `filter_mode` - gets **nothing**, which is the
  rule acceptance asks for ("a type declaring only `setting` widgets gains
  nothing"): `bool_space_in`/`bool_space_out` declare only settings and their
  ports are unchanged.  A module may state `kind` on a control instead (or as
  well); the stated kind wins, and an unknown one is refused at declare time.
  `text`/`choice` joined `api.PORT_KINDS` - a value kind has to be a kind a
  port may declare, or the four value kinds could not pair with themselves.
* **The relay.**  `ProtocolHandler.relay_values` follows the edges leaving each
  reported port (`Graph.edges_from`, the new mirror of `edges_into`) and hands
  the value to the consumer module that owns the other end, as a
  **`control_set`** addressed at the control the port is named for - the
  callback every module already has, so **no module needs a new callback** and
  the consumer applies it exactly as if the user had set the control.  It is
  triggered from `node_values` (a producer reported), `node_edge add` (relay
  whatever the producer last reported: the wiring itself carries no value) and
  `node_edge remove`.
* **The binding and the revert.**  `Registry.port_drives_control(instance,
  port)` is the one place that answers "does this port drive a control, and
  which".  Adding an edge onto such a port makes no state change beyond the
  edge; **removing the last driver puts the control back to its stored local
  value** (`Registry.local_control_value`), and while another driver remains
  (a port the module declared `many`) the remaining one's value is relayed
  instead.  The stored value is captured `when the drive begins`
  (`Registry.capture_control`, off the instance config) and updated whenever
  the user sets the control (`node_control` → `Registry.set_control_value`,
  which also records it in the instance config - what a re-attaching module is
  handed).  A control nothing has ever set has **no** stored value and is not
  restored to an invented one: the module's own default stands.
* **The auto port declares `one`.**  A parameter has one producer.  A module
  that wants a bus declares its own `drives_control` port with
  `cardinality: "many"` - `validate_type_decl` holds it to `drives_control ==
  true`, `direction: "in"`, and a name that *is* a control it declares.
* **Where it lands in the shipped catalog.**  Every numeric/boolean/text/choice
  control of the PipeWire catalog now has a port: `gate.fallback_onoff`,
  `volume.volume`, `sensitivity_gate.sensitivity`, `normalize.gain`,
  `reverb.wetdry`, the classifier `pattern`/`description`/`app_key` fields, the
  space ports' `default_state`, and the primitives types (`number`/`slider` →
  `value`, `toggle` → `on`, `math`/`compare`/`logic` → `op`).  Nothing about a
  module's own data changed: `modules/primitives/types.py` and
  `modules/pipewire/catalog.json` are untouched, and `declarations.py` still
  serves the raw file - the daemon is what adds the port.
* **Not touched by this step** (the boundary, in agents/DECISIONS.md): a space
  *face* whose `mode` is `value` + `method` `edge` is the same mechanism, but
  making the face jack routable needs the face node types to declare a
  value-kind output and the face record to carry the `field` method's stored
  value - S5's value sources and the client's face UI, not this step.

## 2026-10-15 — S5: the value sources, and one slot per input

`agents/SPACES.md` §16, the module half.  `modules/primitives` had eight types
and exactly one way to have a value: a knob or a switch the user set, or (since
S3) another producer's value relayed onto that control.  There was no literal to
route (the spec's "sliders should be broken out into a custom 0.0-1.0 node type
and then allowed to route" plus the string it names), and no *operation* had an
addressable input at all.

* **The sources.**  `const` (a number literal: `widget: "field"` with
  `kind: "number"`, so a display draws a typed entry rather than a knob) and
  `string` (a text literal: `field` → text) are **new type ids** beside the
  existing `number`/`slider`/`toggle`.  Every source has an output port of the
  kind it holds and reports `{"out": ...}` through `ModuleHost.report_values`,
  which is what S3's relay follows.  `slider`'s *declaration* is unchanged and
  needs to be: it is 0.0-1.0 by construction — its control declares no
  `min`/`max` (the client's own default range for a slider widget is 0..1 —
  `patchSpaceGui:gui/controls.py:_default_range`) and the engine clamps
  whatever a display or a driver sends.  `number` (-100..100 knob) keeps its
  range, which is what it is *for*.
* **One slot per input (§14, made uniform).**  A port that drives a control is
  named for it, so the port's name *is* the key: a wire inside this module
  (`Engine._inputs`), a relayed value (`control_set` — and `value_set` for a raw
  port, `Primitives.apply_value`) and the user's `node_control` all land in
  `Engine.nodes[id]["config"][<port>]`, and an input reads the wire first, the
  control second (`Engine._compute`'s `given`).  The module's own declarations
  follow the same rule, which is how the *operations* gained inputs: `math` and
  `compare` declare `a`/`b` as `drives_control` ports with controls of the same
  name, `logic` its `a`/`b`, `invert` its `a`, `display` its `in`.  No
  `cardinality` is declared anywhere, so each port keeps the rule its kind had
  (number → many, boolean → one): **no edge that was accepted before is refused
  now**.
* **`display` is this module's text consumer.**  Its `text` input (kind `text`)
  is a port and nothing else — no control, because there is no generic
  text-entry widget for it to be a parameter of — so a `string` node wires
  straight into it and the display reports the text it holds.  Where a text
  value really goes today is *other* modules' control ports: the daemon's relay
  carries a producer's text into any `drives_control` text port, and S3 gave
  the PipeWire catalog 21 types with `field` controls (`regex_classifier.pattern`,
  `app_classifier.app_key`, `sound.path`, the warp `name`s) exactly that.
* **Unwiring releases the drive.**  `Engine._release_drive` forgets the value a
  driver pushed into a port's slot when the last edge into that port goes away,
  so an operand nothing feeds is `0.0` again instead of the last value it saw.
  This leans on the daemon's message order: `protocol._node_edge` forwards
  `edge_apply` **and then** binds the control edge, so the value it froze when
  the drive began arrives *after* the clear and cannot be wiped (the docstring
  and the socket test both say so).
* **Not done here, and why:** the canvas cannot *type* into a `const`/`string`
  yet.  The client's only text entry is `spec.field`, which sends the legacy
  `set_node_property` — and the daemon refuses that for a node a module owns
  (`central/main.py:_cmd_set_node_property` looks the node up in
  `self.space.nodes`) — while its generic widgets (`slider`/`number`/`switch`/
  `choice`) send `node_control`.  So a literal is settable over the module API
  (`node_control "value"`) and by the relay onto its own `value` port, but not
  by dragging in the canvas: a client-side gap in `patchSpaceGui`, not a daemon
  one, and not worked around with a widget that lies about the value's shape.
* **A pre-existing hole this step's tests ran into** (`modules/host.py` — found,
  not fixed: it is outside this step's files, and a safe fix is its own piece of
  work).  `ModuleHost._call` treats the next line off the socket as the reply it
  is waiting for, so a *push* already queued in that window is consumed as the
  reply and **dropped**.  Repro: two `spawn_instance` calls with no pump between
  them, then one `pump` — only the first node arrives.  `report_values` `_call`s
  too, so the same window can eat a `control_set`/`edge_apply` that arrives while
  a module is waiting for its `ok` — i.e. a relayed value, which is now the
  load-bearing message.  Answering it *inside* `_call` (dispatch pushes, keep
  waiting) makes nested `_call`s likelier, and a nested one can eat the outer
  reply and hang the read, so the real fix is reply routing by `ref` plus a
  queue for pushes.  The primitives socket tests fetch one node per
  `_pump_until` for exactly this reason.

## 2026-10-15 — S8: an app declares its own space

`agents/SPACES.md` §18, the spec's opening promise and the last gap it named:
"an app can create its own space and know it can place nodes there".  S0–S6
built the container (a space is a record, a file, a face, a portal target) but
nothing ever *made* one for an app: `Registry.announce` stored
`instance.space` from a node payload and stopped there, so an app that named
its own space got its nodes filed under a space that did not exist — no record,
no label, no owner, and no way for a UI to say whose space it was.

* **`space_declare {space: {space_id, label?, color?, node_view?, parent?}}`**
  is the new message (`module_api.space_declare`/`space_declared`,
  `validate_space_declare`), answered with the record it ended up with.  It is
  idempotent by construction: the **id is the identity**
  (`spaces.declare_space`), so a second declaration updates the fields it
  carries and a declaration after a restart — where nothing of the space was
  persisted, so nothing came back — makes the same space again.  Declaring on
  every attach is therefore the whole mechanism, and `ModuleHost` sends it
  itself when a module was constructed with a `space` (on every `connect`,
  including a reconnect), so an app cannot forget.
* **App-owned, and no takeover.**  A declared space is
  `permanent=False` (forced; only the user can turn Permanent on in the GUI)
  with `declared_by` = the declaring client id, and the payload carries that
  plus `declared_by_name` (the client's `hello` name) so a UI can say whose
  space it is.  Refused, with a reason and no state touched: the root id, an id
  that is permanent, an id that is declared by a file (read-only — nix's or the
  deployment's word), and an id owned by a different client.  Editing a space
  *by name* through the ordinary commands stays allowed (the "white box with
  nice borders" rule) — the check refuses a *claim*, not access.
* **Which space a node lands in** is one function
  (`spaces.node_space(instance_id, named)`): the node's own id path when it has
  one (`myapp::a` → `myapp`: the persistent contract, and what a GUI-placed
  node is qualified with), else the space it names (`{"instance_id": "a",
  "space": "mycelion"}` — mycelion's shape), and `main`/nothing means the root.
  Treating `main` as a container id would have filed every PipeWire node into a
  space called "main"; ignoring the named space would have left an app whose
  ids are its own with nothing to name.  `Registry.announce` gained the
  `on_space` hook that hands each accepted node's container to the space tree
  (the registry knows *which node landed where*; the tree belongs to the
  daemon), and `spaces.announce_space` creates the space as the announcer's own
  when the daemon has never heard of it.  **Only when it has never heard of
  it**: announcing into somebody else's space never claims it.
* **A client going away gives up its claim, not its space.**
  `ProtocolHandler.release_spaces` (called by the daemon's client loop, exactly
  where `Registry.detach` already runs) clears `declared_by` on every space
  that client declared and leaves the record.  The record stays because the
  app's nodes stay — an offline placement is kept so a reconnecting module
  binds it again — so dropping the box would only lose its label and color
  while it was still drawn around those nodes; and *nothing of a declared space
  is written anywhere*, so it cannot outlive the session, meaning a claim that
  persisted past its connection would only block the id.  A reconnect gets the
  same space back (same module id by name); a *different* app may claim an
  ownerless id, which is not a takeover.
* **The user can take it into the session.**  `set_space_meta {permanent:
  true}` on a space with no file gives it one (`_give_space_a_file`: a free
  stem beside the other spaces, a `child_ref` from its parent) and clears
  `declared_by` in the same breath — the id is not declarable any more, so
  calling it app-owned would be a lie about a deletion that now sticks.  §18
  makes persistence the user's to grant; without that the checkbox would change
  nothing, because an app-made space has no file, no stem and no reference for
  `_write_spaces` to follow.
* **The persistence filter needed no change** and that is the point: a declared
  space is non-permanent, so `_write_spaces` → `prune_unpersisted` drops it —
  its nodes, its interior edges, its diffs — while a portal *from* a permanent
  space to it is still saved in the permanent space's own file (§13's rule).
  `_build_spaces_from_space`'s "no record" branch (the synthesized
  non-permanent space) is now the tail of the same story, and the rebuild
  carries `declared_by` so a snapshot of the tree cannot quietly un-own an
  app's space.
* **What a client reads.**  Every space record comes from one builder
  (`spaces.space_record`) — the GUI's `get_nodes`, `list_spaces` and a display's
  `snapshot_reply` — and carries `declared_by`/`declared_by_name`.  The
  snapshot's `spaces` are now the **container** spaces (the tree), each also
  carrying `space_id`/`readonly`/`anchored` under their pre-S1 names and a
  `nodes` list, so a display can line an app's nodes up with its space in one
  message; a daemon that has not loaded a tree still shows the legacy routing
  regions, which is what a pre-S1 display expects.
* **Tests** (`src/tests/test_space_declare.py`): the rule (create, update in
  place without duplicating, the four refusals, the role rule, the id-path
  `parent` assertion, an announcement creating the space it names, an
  announcement not claiming somebody else's, release-on-detach), and the
  promise end to end — a real `ModuleHost` over a real socket against the real
  daemon: declare on attach, run two nodes in the space, wire them, read the
  space-with-both-nodes out of the display's snapshot; `_write_spaces` writes
  no `mycelion.json`, no mention in the root session or the diffs, while a
  portal from a user-made permanent space to it lands in that space's file; and
  a second daemon on the same files comes up **without** the app's space and
  gets it back, identical, from the app's next attach.
* **Deliberately not done here:** nothing in the client — the GUI half (a space
  chip saying "app-owned", not pretending a deletion sticks) is the separate
  `patchSpaceGui` repo, whose author was told the exact wire fields
  (`declared_by`, `declared_by_name`, present on every space record, `""` when
  not app-owned).  `delete_space` on a declared space needs no daemon change:
  the record goes and the app's next attach brings it back, which is the
  honesty §18 asks for rather than a special case.
