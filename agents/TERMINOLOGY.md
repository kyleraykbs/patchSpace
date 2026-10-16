# TERMINOLOGY — Kyle's words → this repo

Kyle builds in modules and thinks in terms of black boxes + how they wire together; he
often doesn't know the internal names.  This is the translation table.  When he uses a word
that isn't here, ask (and then add it).  When a mapping here is a guess, it is marked
**confirm**.

## The app itself

| Kyle says | Repo |
|---|---|
| "the patchspace" / "patch space" | the whole app: daemon `main.py` + canvas `gui/` |
| "the daemon" / "the backend" | `main.py`, `PatchSpaceDaemon` (owns the live PipeWire graph) |
| "the GUI" / "the canvas" | `gui/patchspace_widget.py` (`PatchSpaceGraphWidget`) |
| "session" / "save" | the root space file (`~/.cache/patchspace/last_session.json`) |
| "space" (until 2026-10-15: **"panel"**) | `spaces.py` space: a nestable, file-backed container of nodes |
| "Space Types" / the Template-Live switch | a space's `type`: `template` (a definition - its nodes are not loaded, see below) or `live` (spawned and running).  `set_space_meta {type}`, the file's `type` key |
| "Template" | a space whose interior is a *definition*: no live node, no module instance, no placement.  Dropping one in (`instantiate_template`) makes a nested live space that records the source in `template` |
| "Live" | a spawned, running space; the default for everything that already existed |
| "Permanent" (checkbox) | the space's `permanent`: whether it is written to disk *at all* - session, space file, placement diffs - and whether anything inside it is.  On for the root and for spaces you make in the GUI; **off** for spaces an app/module made, so nothing of them accumulates across updates |
| "Node" (checkbox) | `node_view`: the *default* view when the space is embedded elsewhere (collapsed to a node); the per-embedding toggle overrides it |
| "face" / "the space's inputs and outputs" | the space record's `face`: derived by the daemon from the `space_in`/`space_out` nodes placed in the space (see below) |
| "space input is a node input or an externalized value" | a face input's `mode`: `node` (an ordinary jack) or `value` (externalized).  A value input declares a `kind` (`number`/`boolean`/`text`/`choice`) and a `method` (`field` on the face, or `edge` from a value node).  Audio is node-only |

The container was called a **panel** until S0 (`agents/SPACES.md`, 2026-10-15), and
Kyle still says "panel" for it.  `space` is now the word in code, files, the
protocol, the daemon flags and the Nix options.  S1 closed the protocol's
one-release window (the old `*_panel*` command names and their argument keys
now fall through as unknown - a GUI started before the update must be
relaunched) and repurposed the file's `type` key to carry the space's *kind*;
old files (`type: "space"`/`"panel"`) still load as live spaces, as do the old
directory, flags and node type ids - those are data, not protocol.
("side panel"/"panel" in the UI sense - a GTK panel - stays a panel.)

A space's **face** is what a client reads instead of walking the nodes: the
space record carries `face[]`, one entry per `space_in`/`space_out` node placed
in the space (`direction` `in`/`out`; an input also carries
`mode`/`kind`/`method`, and `choices` when it is a choice).  The configuration
is *stored on the port node* (`SpaceFaceInput` in `pwnodes`, written by
`set_space_input`), so the node is the one source of truth and there is no
second copy to drift.  A template's face is derived from its definition, since
a template's nodes are not live.

## Graph vocabulary

| Kyle says | Repo |
|---|---|
| "node" | a `Node` in `PatchSpace.nodes` (a box on the canvas) |
| "port" / "socket" | one entry of a spec's `inputs`/`outputs`; kinds via `port_kind` |
| "connection type" | **port kind**: `audio` / `bundle` / `boolean` / `filter` / `impulse` |
| "wire" / "connection" / "edge" | `Edge` (id `from->to[:port][@port]`) |
| "audio out / audio in" | an `audio` port (`out` is a source, `in` a sink) |
| "mono node" / "capture_MONO" | a port whose `audio.channel` is `MONO` (no FL/FR peer); `resolve_channel_pairs` bridges every channel of the stereo/multichannel peer through it (**confirm**) |
| "impulse" | the momentary event kind: `ButtonNode` out → `SoundEffectNode` in |
| "fires / pulses / triggers" | `PatchSpace.pulse()` → `on_impulse()` |
| "stack" (the switch) | `SoundEffectNode.overlap`: On = takes stack, Off = restart |
| "bundle" | one wire standing for a *set* of endpoints (`all_inputs`, `bundle`, …) |
| "bool" / "control signal" | the `boolean` kind driving gates/switchers |
| "connection type for cameras" / "camera wire" | the **`camera`** port kind: a live video feed (Camera In -> Virtual Camera), paired only with camera ports |
| "Camera In" / "input node for cams" | `camera_input` node ("Camera In"): one selected camera stream on a camera out |
| "virtual output node for cams" / "Virtual Cam" | `virtual_camera` node ("Virtual Cam"): republishes the wired camera feed *into the PipeWire system* (a video source apps pick as a camera) |
| "V4L2 Virtual Cam" | `v4l2_virtual_camera` node: republishes the feed on a v4l2 output device (the VirtualCamN loopback pool) |
| "output device" (video) | a v4l2 output-capable device (the VirtualCamN loopback pool) the **V4L2 Virtual Cam** writes - visible to apps as both a v4l2 camera and a PipeWire video source |

## Node-body widgets

| Kyle says | Repo |
|---|---|
| "checkbox" / "on off switch type deal" | the segmented two-segment toggle: `_draw_gate_toggle`, `_draw_boolean_toggle`, and the compact `_draw_toggle_row` (a spec's `toggle`) |
| "the button face / press it" | `ButtonNode`'s face: `control="impulse"`, `_draw_impulse_button` |
| "path string box" / "text box" | the inline text field: `spec.field` + `_draw_text_field` |
| "folder icon" / "file picker" / "open my fm" | `spec.picker` → `_draw_path_picker` → the portal chooser (`portal_file_dialog.open_file`), i.e. the desktop's file-manager open dialog |
| "~/" (in a path) | stored as written, expanded by the daemon at play time (`os.path.expanduser`) |
| "slider" | `control="volume" / "gain" / "wetdry" / "sensitivity"` |
| "the green dot / the number" | `spec.indicator="playing"` → `_draw_play_indicator` (`playing` count) |
| "gear / hamburger / three dots" | settings gear (`show_settings_dialog`), space hamburger, node menu |
| "anchor" / "pin" / "pause physics" | `anchored` flag; `_space_is_paused` for a space |
| "the side panel" | the *spaces* side view (`main_window._build_spaces_view`), or the Add-Node panel - a UI panel, not a space |

## Behaviour words

| Kyle says | Repo |
|---|---|
| "ready" | daemon `ready` + `health` (`ok`/`starting`/`dead`) per node |
| "the sandwich" | the effect interior: dummies + module (`_ChainEffect`) |
| "black box" / "module" | a DSP plugin load (LADSPA/LV2 filter-chain, echo-cancel) |
| "reload the interior" | `_coalesce_reload` → `reload_module()` (load-time controls) |
| "wiring" / "it's wiring" | `edge_wired` (a poll-level "this edge's links aren't live yet") |
| "declare" | the declarative space files (`--space-dir`, read-only spaces) |

## Other repos Kyle references

| Kyle says | Where |
|---|---|
| "the nvidia tag" | `~/projects/mycelion` `config/tags/hardware/nvidia.nix` — the clan tag module that applies to every NVIDIA machine (amanita, psilocybe). "under config" = that repo's `config/` tree. |

## To confirm (guessed, not yet heard from Kyle)

* "chip" → the space-color square at a node's bottom-left (`_space_of_node` chip)?
* "the box" → a space box vs the node box - he has said both; ask which.
* "stamp" / "water-mark" → the EDIT MODE overlay on a space in edit mode.
