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
