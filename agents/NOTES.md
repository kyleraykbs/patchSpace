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
* **Entry chrome is themed** (`field_bg`/`field_fg` from the theme's view colours) instead of a
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

## Scope composition, icons, and the impulse colour (2026-09-23)

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
* **Impulse port colour** is `blue_3` from the theme (Adwaita's #3584e4; stylix maps it to its
  palette's blue - which in the current palette is a muted teal).

* **Declarative panels are placed beside the graph**: a panel file with no `placement.x`/`y`
  (the module's new default - it omits them) is *unpositioned*, and on load the daemon puts it
  to the right of the placed top-level panels, top-aligned, then marks it placed so the
  autosave remembers it (`_place_unplaced_panels`, `panels.PLACE_GAP`).  A user drag is the same
  thing (a geometry on the child reference marks it placed).  Nested panels are left alone.

## Colours follow the theme; header text never breaks mid-word (2026-09-23)

* `theme_palette` gained `accent` (`blue_3`) and `slider_track` (the card lifted like a field).
  The impulse port/button and all sliders share the accent; slider labels/handles are
  `subtext`/`text`.  A **panel with no colour of its own** (`#3584e4`, what the daemon stores by
  default) is drawn from the palette per panel via `theme_class_color`; a panel someone coloured
  keeps its colour.  Never hardcode a colour here - the fallbacks already carry the old literals.
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
* Colour pickers' presets come from the theme (`_group_colors`, `blue_3`…`teal_3`), so picking a
  colour matches the desktop; `GROUP_COLORS` stays as the fallback list.

## Wires stay orthogonal; colour slots (2026-09-23)

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
* Colours: `@blue`…`@teal` slots resolved per frame (`resolve_color`), pickers store the slot,
  the module's panel colour defaults to `@blue`, `#3584e4` reads as `@blue`, the side-view dot
  resolves the same way.  `FIELD_BOTTOM_PAD` (10) is the one place the pad below a node's field
  row is defined, for the rect and the height alike.
