# NOTES — things that cost tokens to learn

Punch-in details for working in this repo.  `LLM_README.md` is the deep doc; `ai/STRUCTURE.md`
is the connective map.  This file is the fast path for the next session.

## Cheapest reliable loop

```bash
# from the repo root
nix develop -c python -m py_compile src/<changed files>      # syntax gate
cd src && nix develop ../ -c python -m pytest -q             # ~270 tests, ~7s, headless
```

* `git status` is always noisy: **`.pyc` files are tracked** (no `.gitignore`) and pytest
  dirties them.  Stage explicit source paths only, never `git add -A`.
* The tree frequently carries the user's own uncommitted work in `main.py`, `pwnodes.py`,
  `gui/*` — check `git diff` before committing so you don't sweep it into your commit.

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
  `backings` — `dead_backings()`/`_node_is_ready` treat a dead backing as a fault.
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
