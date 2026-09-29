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

## [2026-09-28] Virtual cameras publish through v4l2loopback
- Decision: a Virtual Camera node writes frames to a v4l2 output device
  (the machine's v4l2loopback pool, VirtualCamN) rather than publishing a
  PipeWire stream directly.
- Why: one write serves both worlds - the loopback is a /dev/videoN for
  v4l2 apps *and* WirePlumber republishes it as a PipeWire Video/Source
  (verified: frames written to a loopback round-trip through PipeWire).
  A PipeWire-only stream would be invisible to v4l2 consumers.
- Reversible: yes (output_device is a property; a PW-publishing interior
  is a pipeline swap).
