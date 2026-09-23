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

> ⚠️ **Do not simply run a second `PatchBayDaemon`**, even against a private PipeWire.
> `PatchBayDaemon.start()` runs `_cleanup_stale_objects()` → `PipewireGraph._terminate_orphan_helpers`,
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
* Set `main.SOCKET_PATH` to a temp path before `PatchBayDaemon()` so you can never collide
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
