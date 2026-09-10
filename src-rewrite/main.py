#!/usr/bin/env python3
"""
main.py - Patchbay daemon with a Unix-socket API.

Layout
------
  * a pwgraph.PipewireGraph keeps a live model of the PipeWire graph;
  * a pwnodes.PatchSpace owns the user's node graph, reconciles edges,
    and supervises every backed node (restarting anything that dies,
    with backoff for anything that keeps failing);
  * a single tick thread runs PatchSpace.supervise() every ~0.5s (and
    immediately when a mutating command nudges it);
  * a Unix socket at /tmp/patchbay.sock serves the JSON command API the
    GUI (gui/) and the CLI scripts speak.

The daemon's own built-in virtual sink ("PatchBay") and virtual mic
("PatchBay Mic") are ordinary hidden VirtualSpeaker/VirtualMic nodes
added to the PatchSpace - they get exactly the same supervision as
user-created devices, and their default-device status is captured
before they are created and restored on shutdown.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

from pwgraph import PipewireGraph
from pwproc import Ticker
from pwnodes import (
    PatchSpace,
    Node,
    BackedNode,
    LiveResolvableNode,
    GateNode,
    ABSwitchNode,
    SwitcherNode,
    InverseSwitcherNode,
    ExcludeFilterNode,
    VolumeProcessNode,
    NoiseCancelNode,
    SensitivityGateNode,
    ReverbNode,
    NormalizeNode,
    EchoCancelNode,
    LightNoiseCancelNode,
    VirtualSpeakerNode,
    VirtualMicNode,
    DeviceInputNode,
    DeviceOutputNode,
    AppInputNode,
    AppOutputNode,
    PatchBayDeviceNode,
    PatchBayMicDeviceNode,
    RegexInputNode,
    RegexOutputNode,
    MediaClassInputNode,
    MediaClassOutputNode,
    DescriptionInputNode,
    DescriptionOutputNode,
    SplitterNode,
    PATCHBAY_VIRTUAL_SINK_NAME,
    PATCHBAY_VIRTUAL_MIC_NAME,
)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/patchbay.sock"
SESSION_CACHE_PATH = os.path.expanduser("~/.cache/patchbay/last_session.json")

SUPERVISE_INTERVAL_S = 0.5
RELOAD_DEBOUNCE_S = 0.35
DEVICE_OBJ_TYPE = "PipeWire:Interface:Device"

# How long a staged session load will wait for a single backed/effect
# node to come all the way up (structural + module + every backing
# resolved in the live graph - see PatchBayDaemon._node_is_ready)
# before giving up on it and moving on anyway. Generous: a filter-chain
# module loading an LV2/LADSPA plugin from disk is the slowest single
# step in the whole system.
SESSION_LOAD_NODE_TIMEOUT_S = 5.0
SESSION_LOAD_POLL_S = 0.1

# Bound on how long a staged session load will wait for the edges it
# just wired to actually land as live PipeWire links (see the wait
# loop at the end of _load_session, mirroring _bring_node_up's node
# bring-up discipline but for edges). Generous relative to
# LINK_CONFIRM_TIMEOUT_S (pwnodes.py) since a big session can have
# dozens of pairs to wire and the same lock-protected sync_locked()
# call handles them incrementally, not all at once.
SESSION_LOAD_EDGE_TIMEOUT_S = 10.0
SESSION_LOAD_EDGE_POLL_S = 0.1

# Node types whose real DSP is a module that publishes several streams
# asynchronously (an echo/noise-cancel capture+playback sandwich, or a
# sensitivity gate's filter-chain.  Wiring their edges in the same batch
# as the rest of a session import can attach a user edge to an input
# dummy before the module's own side is live - the node ends up
# structurally present but acoustically dead. _load_session gives these
# a dedicated second pass: each node is brought up on its own and waited
# on, then its edges are wired one at a time (inputs first), each
# confirmed live before the next.
_CAREFUL_NODE_TYPES = (
    EchoCancelNode,
    NoiseCancelNode,
    SensitivityGateNode,
    NormalizeNode,
)


NODE_TYPE_REGISTRY: Dict[str, type] = {
    "regex_input": RegexInputNode,
    "media_class_input": MediaClassInputNode,
    "description_input": DescriptionInputNode,
    "regex_output": RegexOutputNode,
    "media_class_output": MediaClassOutputNode,
    "description_output": DescriptionOutputNode,
    "splitter": SplitterNode,
    "gate": GateNode,
    "switcher": SwitcherNode,
    "inverse_switcher": InverseSwitcherNode,
    "exclude_filter": ExcludeFilterNode,
    "volume": VolumeProcessNode,
    "noise_cancel": NoiseCancelNode,
    "sensitivity_gate": SensitivityGateNode,
    "reverb": ReverbNode,
    "normalize": NormalizeNode,
    "echo_cancel": EchoCancelNode,
    "light_noise_cancel": LightNoiseCancelNode,
    "device_input": DeviceInputNode,
    "device_output": DeviceOutputNode,
    "app_input": AppInputNode,
    "app_output": AppOutputNode,
    "patchbay_device": PatchBayDeviceNode,
    "patchbay_mic_device": PatchBayMicDeviceNode,
    "virtual_speaker": VirtualSpeakerNode,
    "virtual_mic": VirtualMicNode,
}
CLASS_TO_TYPE = {cls: key for key, cls in NODE_TYPE_REGISTRY.items()}

# Hidden per-Sensitivity-gate gain-staging nodes. A SensitivityGateNode
# is always bracketed by two daemon-owned VolumeProcessNodes - a pre
# booster and a reciprocal post cut - so the GUI's sensitivity slider
# has a working, always-present control without the user ever creating
# or wiring a Volume node by hand. Both are added with public=False (so
# get_nodes/export never show them) and the daemon routes the user's
# edges through them transparently; see _ensure_sensitivity_internals
# and _stored_endpoints below.
_SENS_PRE_PREFIX = "__sens_pre__"
_SENS_POST_PREFIX = "__sens_post__"


def _sens_pre_id(gate_id: str) -> str:
    return f"{_SENS_PRE_PREFIX}{gate_id}"


def _sens_post_id(gate_id: str) -> str:
    return f"{_SENS_POST_PREFIX}{gate_id}"


def _hidden_sensitivity_gate_for(node_id: str) -> Optional[str]:
    """The Sensitivity gate a hidden pre/post volume node belongs to,
    or None if `node_id` is an ordinary node. Used to translate the
    daemon's internal edges into the logical edges the GUI sees."""
    if node_id.startswith(_SENS_PRE_PREFIX):
        return node_id[len(_SENS_PRE_PREFIX):]
    if node_id.startswith(_SENS_POST_PREFIX):
        return node_id[len(_SENS_POST_PREFIX):]
    return None


# Attributes a node may expose; included in serialization whenever
# present.  Kept in one place so get_nodes / export can't drift.
_SERIAL_ATTRS = (
    "label",
    "pattern",
    "media_class",
    "description",
    "device_name",
    "app_name",
    "device_label",
    "device_volume",
    "profile_index",
    "profile_description",
    "enabled",
    "output",
    "volume_min",
    "volume_max",
    "backing_node_name",
    "library_name",
    "aec_args",
    "monitor_mode",
    "ladspa_plugin",
    "ladspa_label",
    "vad_threshold",
    "wet_dry",
    "level",
    "sensitivity",
    "volume_locked",
    "boost_db",
    "max_boost_db",
    "ceiling_db",
    "leveling",
    "threshold_db",
    "ratio",
    "attack_ms",
    "release_ms",
    "knee_db",
    "limiter_release_s",
    "ladspa_dir",
)

# GUI layout state a node may carry.  Serialized separately (only when
# set) rather than via _SERIAL_ATTRS so a node the GUI hasn't positioned
# yet doesn't emit x:null / y:null / anchored:null into every export.
_LAYOUT_ATTRS = ("x", "y", "anchored")


def _apply_layout_attrs(node, config: dict) -> None:
    """Copy any x/y/anchored from a node config/command onto `node`.
    Used when adding/replaying nodes so a saved layout is restored."""
    for attr in _LAYOUT_ATTRS:
        if attr in config and config[attr] is not None:
            setattr(node, attr, config[attr])


class PatchBayDaemon:
    def __init__(self):
        self.graph = PipewireGraph(pw_cli_command=("pw-cli",))
        self.space = PatchSpace(self.graph)
        self._lock = self.space._lock

        self._running = False
        self._clients: set[socket.socket] = set()

        self._ticker: Optional[Ticker] = None
        self._reload_wake_timer: Optional[threading.Timer] = None
        self._dirty = False

        # GUI-only node groups: id -> {id, label, color, nodes:[node_id]}.
        # Pure canvas annotations (like x/y/anchored) - they never touch
        # the audio graph, they just persist through export/import.
        self.groups: Dict[str, Dict[str, Any]] = {}

        # Builtin hidden virtual devices (see module docstring).
        self.builtin_sink: Optional[VirtualSpeakerNode] = None
        self.builtin_mic: Optional[VirtualMicNode] = None
        self._prev_default_sink_id: Optional[int] = None
        self._prev_default_source_id: Optional[int] = None
        self._set_default_sink_id: Optional[int] = None
        self._set_default_source_id: Optional[int] = None

        self.graph.on_node_created(self._on_node_created)
        self.graph.on_node_removed(self._on_node_removed)
        self.graph.on_initial_sync(self._on_initial_sync)
        # Wake the supervisor thread whenever PatchSpace wants a nudge.
        self.space.on_change.append(self._wake_ticker)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def _wake_ticker(self) -> None:
        if self._ticker is not None:
            self._ticker.wake()

    # Backing-name prefixes that are exclusively this project's (see the
    # GUI's _build_add_node_command + the daemon's default
    # backing_node_name), used by the startup sweep to find and tear down
    # objects a previous, uncleanly-stopped daemon left behind even when
    # the last-session cache is missing.
    _OWNED_PREFIXES = (
        "patchbay_",
        "echo_cancel_node_",
        "light_noise_cancel_node_",
        "noise_cancel_node_",
        "reverb_node_",
        "volume_node_",
        "volume_mute_",
        "virtual_speaker_node_",
        "virtual_mic_node_",
        "splitter_",
    )

    def _startup_stale_markers(self) -> list:
        """Every name a previous patchbay run may have left live objects
        under: the built-in virtual devices, this project's known backing
        prefixes, and the backing_node_names recorded in the last-session
        cache (the exact names a re-import will reuse, so orphaned copies
        must not survive)."""
        markers = list(self._OWNED_PREFIXES) + [
            PATCHBAY_VIRTUAL_SINK_NAME,
            PATCHBAY_VIRTUAL_MIC_NAME,
        ]
        try:
            with open(SESSION_CACHE_PATH) as f:
                config = json.load(f)
            for node_cfg in config.get("nodes", {}).values():
                backing = (node_cfg.get("params") or {}).get("backing_node_name")
                if backing:
                    markers.append(backing)
        except (OSError, ValueError):
            pass  # no cache yet - prefix sweep still covers GUI-created names
        return markers

    def _cleanup_stale_objects(self) -> None:
        """Crash-recovery sweep run before anything is created: a daemon
        that was killed uncleanly leaves its helper processes orphaned,
        and the nodes they own keep exporting under the same names a
        fresh import will use - so the fresh objects would otherwise
        collide with them (duplicate node.names, sync routing through
        whichever copy it matched first).  Tearing down whatever matches
        our reserved names / last-session backing names removes the
        leftovers; best-effort, cheap when nothing is stale."""
        markers = self._startup_stale_markers()
        stale = self.graph.reap_stale_for_names(markers)
        logger.info("Startup cleanup swept %d stale object(s)", stale)

    def start(self) -> None:
        self._running = True

        # Clear anything a previous (uncleanly-killed) run left behind
        # before we request fresh objects.
        self._cleanup_stale_objects()

        # Capture the pre-existing defaults before creating our own
        # devices - see _capture_defaults docstring.
        self._capture_defaults()

        self.graph.start()
        # Wait for the first full snapshot (bounded).
        deadline = time.monotonic() + 15
        while not self.graph._initial_dump_received and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self.graph._initial_dump_received:
            logger.warning("No initial pw-dump snapshot after 15s - continuing")

        self.space.mark_graph_loaded()
        self._create_builtin_devices()

        self._ticker = Ticker(SUPERVISE_INTERVAL_S, self._tick)
        self._ticker.start()

        socket_thread = threading.Thread(target=self._socket_server, daemon=True)
        socket_thread.start()

        logger.info("PatchBay daemon running. Press Ctrl+C to exit.")
        logger.info(f"Connect via: socat - UNIX-CONNECT:{SOCKET_PATH}")
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("\nShutting down...")
        finally:
            self._running = False

    def stop(self) -> None:
        self._running = False
        if self._ticker is not None:
            self._ticker.stop()
        self._teardown_all()

    def _teardown_all(self) -> None:
        # Capture the builtins' ids before tearing them down so the
        # default-restore can tell "previous default" apart from one of
        # our own just-destroyed devices.
        sink_id, mic_id = self._builtin_resolved_ids()
        with self._lock:
            for node_id in list(self.space.nodes.keys()):
                try:
                    self.space.remove_node(node_id)
                except Exception as exc:
                    logger.warning("Failed to tear down %r: %s", node_id, exc)
            self._restore_defaults(skip_sink=sink_id, skip_mic=mic_id)
        try:
            self.graph.stop()
        except Exception as exc:
            logger.warning("graph stop: %s", exc)

    # ------------------------------------------------------------------
    # builtin virtual sink / mic
    # ------------------------------------------------------------------

    def _capture_defaults(self) -> None:
        """Remember whatever sink/source is default *before* we create
        our own devices, so shutdown can restore it.  If this is read
        after our own devices exist, WirePlumber may already have
        flipped the default onto them."""
        self._prev_default_sink_id = self._read_default_id("@DEFAULT_AUDIO_SINK@")
        self._prev_default_source_id = self._read_default_id("@DEFAULT_AUDIO_SOURCE@")

    @staticmethod
    def _read_default_id(token: str) -> Optional[int]:
        import re
        import subprocess

        try:
            result = subprocess.run(
                ["wpctl", "inspect", token], capture_output=True, text=True, timeout=5
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("wpctl inspect %s failed: %s", token, exc)
            return None
        if result.returncode != 0:
            return None
        match = re.search(r"\bid\s+(\d+)", result.stdout)
        return int(match.group(1)) if match else None

    def _create_builtin_devices(self) -> None:
        with self._lock:
            sink = VirtualSpeakerNode(
                "__patchbay_builtin_sink__",
                PATCHBAY_VIRTUAL_SINK_NAME,
                device_label="PatchBay Virtual Sink",
            )
            mic = VirtualMicNode(
                "__patchbay_builtin_mic__",
                PATCHBAY_VIRTUAL_MIC_NAME,
                device_label="PatchBay Mic",
            )
            self.builtin_sink = sink
            self.builtin_mic = mic
            self.space.add_node(sink, public=False)
            self.space.add_node(mic, public=False)

    def _builtin_resolved_ids(self) -> tuple:
        sink_id = None
        mic_id = None
        if self.builtin_sink is not None and self.builtin_sink.backings:
            sink_id = self.builtin_sink.backings[0].node_id
        if self.builtin_mic is not None and self.builtin_mic.backings:
            # The visible Audio/Source is the loopback backing.
            for b in self.builtin_mic.backings:
                if b.name == PATCHBAY_VIRTUAL_MIC_NAME:
                    mic_id = b.node_id
                    break
        return sink_id, mic_id

    # ------------------------------------------------------------------
    # Speaker Line / Mic Line shared volume
    # ------------------------------------------------------------------
    #
    # The visible Speaker Line / Mic Line nodes own no backing - they all
    # reference the daemon's single built-in virtual sink/mic.  The
    # volume state lives on that built-in device (one source of truth,
    # so several lines can't fight over it) and is mirrored onto every
    # visible line node on the supervision tick.  When the built-in
    # node's lock is on (default) its volume is re-asserted every tick;
    # unlocked, external changes are adopted instead.

    def _line_volume_target(self, node):
        """The built-in device a Speaker/Mic Line node controls, or None
        when `node` is not a line node."""
        if isinstance(node, PatchBayDeviceNode):
            return self.builtin_sink
        if isinstance(node, PatchBayMicDeviceNode):
            return self.builtin_mic
        return None

    def _line_nodes_for(self, target) -> list:
        if target is self.builtin_sink:
            cls = PatchBayDeviceNode
        elif target is self.builtin_mic:
            cls = PatchBayMicDeviceNode
        else:
            return []
        return [n for n in self.space.nodes.values() if isinstance(n, cls)]

    def _mirror_line_volume(self, target) -> None:
        if target is None:
            return
        for line in self._line_nodes_for(target):
            line.device_volume = target.device_volume
            line.volume_locked = target.volume_locked

    def _adopt_line_volume(self, node, config: dict) -> None:
        """A line node created/loaded with an explicit device_volume /
        volume_locked (an import round-trip) seeds the shared built-in
        state; a GUI-created one leaves it alone so the built-in's
        current volume flows onto the new line instead."""
        target = self._line_volume_target(node)
        if target is None:
            return
        if "volume_locked" in config:
            target.volume_locked = bool(config["volume_locked"])
        if "device_volume" in config:
            try:
                target.device_volume = max(
                    0.0, min(1.0, float(config["device_volume"]))
                )
            except (TypeError, ValueError):
                pass
        self._mirror_line_volume(target)

    def _enforce_volume_locks(self) -> None:
        """Per-tick volume policy.  Hardware device nodes re-apply their
        own volume in apply_device_settings() during supervise(); an
        unlocked one instead adopts the live value here so the slider
        follows external changes.  The built-in sink/mic are handled the
        same way, plus mirroring onto every visible line node."""
        for node in list(self.space.nodes.values()):
            if not isinstance(node, (DeviceInputNode, DeviceOutputNode)):
                continue
            if getattr(node, "volume_locked", True):
                continue
            try:
                node.sync_volume_from_live()
            except Exception as exc:
                logger.warning("Live volume sync for %r failed: %s", node.id, exc)
        for target in (self.builtin_sink, self.builtin_mic):
            if target is None:
                continue
            try:
                target.apply_device_settings()
                target.sync_volume_from_live()
            except Exception as exc:
                logger.warning("Volume enforcement for %r failed: %s",
                               target.id, exc)
            self._mirror_line_volume(target)

    def _assert_defaults(self) -> None:
        """Re-assert the builtins as default output/input, once per
        resolved id - a recreated device gets re-promoted on a later
        tick."""
        sink_id, mic_id = self._builtin_resolved_ids()
        if sink_id is not None and sink_id != self._set_default_sink_id:
            self._set_default_sink_id = sink_id
            self._set_default("@DEFAULT_AUDIO_SINK@", sink_id)
        if mic_id is not None and mic_id != self._set_default_source_id:
            self._set_default_source_id = mic_id
            self._set_default("@DEFAULT_AUDIO_SOURCE@", mic_id)

    @staticmethod
    def _set_default(token: str, node_id: int) -> None:
        import subprocess

        try:
            result = subprocess.run(
                ["wpctl", "set-default", str(node_id)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                logger.info("Set default to node id %s", node_id)
            else:
                logger.warning(
                    "wpctl set-default %s failed: %s",
                    node_id,
                    (result.stderr or result.stdout).strip(),
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("wpctl set-default %s failed: %s", node_id, exc)

    def _restore_defaults(
        self, skip_sink: Optional[int] = None, skip_mic: Optional[int] = None
    ) -> None:
        if (
            self._prev_default_sink_id is not None
            and self._prev_default_sink_id not in (skip_sink, None)
        ):
            self._set_default("@DEFAULT_AUDIO_SINK@", self._prev_default_sink_id)
        if (
            self._prev_default_source_id is not None
            and self._prev_default_source_id not in (skip_mic, None)
        ):
            self._set_default("@DEFAULT_AUDIO_SOURCE@", self._prev_default_source_id)

    # ------------------------------------------------------------------
    # periodic tick
    # ------------------------------------------------------------------

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            self.space.supervise()
            self._assert_defaults()
            self._enforce_volume_locks()
        except Exception:
            logger.exception("supervision tick failed")
        if self._dirty:
            self._dirty = False
            self._auto_export_session()

    def _coalesce_reload(self, node: BackedNode) -> None:
        """Coalesce interior-module reloads: reload once the property-
        edit burst goes quiet for RELOAD_DEBOUNCE_S, not once per
        message (a drag sends many messages in quick succession)."""
        with self._lock:
            node._reload_due = time.monotonic() + RELOAD_DEBOUNCE_S
            old = self._reload_wake_timer
            if old is not None:
                old.cancel()
            timer = threading.Timer(RELOAD_DEBOUNCE_S, self._fire_reload_wake)
            timer.daemon = True
            self._reload_wake_timer = timer
            timer.start()

    def _fire_reload_wake(self) -> None:
        with self._lock:
            self._reload_wake_timer = None
        self._wake_ticker()

    # ------------------------------------------------------------------
    # graph event handlers
    # ------------------------------------------------------------------

    def _on_initial_sync(self, graph: PipewireGraph) -> None:
        logger.info(
            "Initial graph loaded: %s nodes, %s ports",
            len(graph.nodes()),
            len(graph.ports()),
        )

    def _on_node_created(self, node_id: int, node_data: dict) -> None:
        props = node_data.get("info", {}).get("props", {})
        # A backing matches on its unique node.name; some objects set a
        # friendlier node.description too, so try both.
        candidates = []
        for key in ("node.name", "node.description"):
            value = props.get(key)
            if value:
                candidates.append(value)
        with self._lock:
            changed = False
            for name in candidates:
                for node in self.space.nodes.values():
                    if isinstance(node, BackedNode) and node.resolve_backing(
                        name, node_id
                    ):
                        if hasattr(node, "refresh_live"):
                            node.refresh_live()
                        changed = True
                        break
                if changed:
                    break
            for node in self.space.nodes.values():
                if isinstance(node, LiveResolvableNode) and node.matches_live_node(
                    props
                ):
                    node.resolve_live(node_id, props)
                    changed = True
        if changed:
            self.space.sync()

    def _on_node_removed(self, node_id: int) -> None:
        with self._lock:
            changed = False
            for node in self.space.nodes.values():
                if (
                    isinstance(node, LiveResolvableNode)
                    and node.live_node_id == node_id
                ):
                    node.resolve_live(None, None)
                    changed = True
            self.space.handle_node_removed(node_id)
        if changed:
            self.space.sync()

    # ------------------------------------------------------------------
    # session persistence
    # ------------------------------------------------------------------

    def _build_export_config(self) -> dict:
        with self._lock:
            nodes = {}
            for node_id, node in self.space.nodes.items():
                if node_id not in self.space.public_nodes:
                    continue
                node_type = CLASS_TO_TYPE.get(type(node), "unknown")
                params = {}
                for attr in _SERIAL_ATTRS:
                    if hasattr(node, attr):
                        params[attr] = getattr(node, attr)
                for attr in _LAYOUT_ATTRS:
                    value = getattr(node, attr, None)
                    if value is not None:
                        params[attr] = value
                if isinstance(node, VolumeProcessNode):
                    params["initial_volume"] = node.volume
                if node_id.startswith("mute_"):
                    params["is_mute"] = True
                label = getattr(node, "label", "")
                if label:
                    params["label"] = label
                nodes[node_id] = {"type": node_type, "params": params}
            edges = []
            for e in self.space.edges.values():
                logical = self._logical_edge(e)
                if logical is None:
                    continue
                from_node, to_node, to_port, from_port = logical
                entry = {"from": from_node, "to": to_node}
                if to_port != "in":
                    entry["to_port"] = to_port
                if from_port != "out":
                    entry["from_port"] = from_port
                edges.append(entry)
        groups = [dict(g) for g in self.groups.values()]
        return {"nodes": nodes, "edges": edges, "groups": groups}

    def _auto_export_session(self) -> None:
        try:
            config = self._build_export_config()
            cache_dir = os.path.dirname(SESSION_CACHE_PATH)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            tmp = SESSION_CACHE_PATH + ".tmp"
            with open(tmp, "w") as f:
                json.dump(config, f, indent=2, sort_keys=True)
            os.replace(tmp, SESSION_CACHE_PATH)
        except OSError as exc:
            logger.warning("Failed to auto-save session cache: %s", exc)

    # ------------------------------------------------------------------
    # staged session load
    # ------------------------------------------------------------------
    #
    # Why this exists (and why it lives on the daemon, not the GUI):
    #
    # The GUI used to "import" a config by replaying it as a burst of
    # add_node/add_edge commands over the socket, one right after the
    # next - _apply_config in patchspace_widget.py, still used for the
    # inline-JSON import dialog's shape but no longer for the two real
    # entry points (see below). That is fine for simple filter/gate
    # nodes, which are just name strings pwmatch re-resolves fresh on
    # every sync() regardless of when they were added. It is NOT fine
    # for a BackedNode with an interior DSP module (echo_cancel,
    # noise_cancel, sensitivity_gate, reverb): ensure_structural() is
    # synchronous and cheap, but the module is deliberately
    # materialised lazily by supervise() (see pwnodes.py's module
    # docstring on why), and "materialised" means a real pw-cli
    # load-module call that has to actually settle *and* show up in
    # the daemon's own live pw-dump snapshot before pwmatch can find
    # it. A human importing by hand gets that settling time for free -
    # drag a node in, wait, wire it up, wait some more, drag the next
    # one in. Firing every add_node for a whole session back-to-back,
    # immediately followed by every add_edge, does not: the *n*th
    # effect node's module might still be loading (or its resolution
    # still lagging the live graph by an event cycle) when the edges
    # that plug into it are added, and while _cmd_add_node's own
    # embedded supervise() call gives it one attempt to catch up, nothing
    # after that ever waits again - the periodic tick keeps repairing
    # a *dead* module forever, but a module that's simply new and not
    # yet visible isn't dead, so there's nothing for it to repair.
    #
    # The fix is procedural, not architectural: bring every backed node
    # up ONE AT A TIME - waiting for it to be genuinely ready before
    # moving to the next one - and only wire edges once every node in
    # the config has had its chance to settle. That's what
    # _load_session below does, entirely daemon-side, so import (from
    # a pasted config, a file path, or the auto-saved last-session
    # cache) is one traceable operation with one clear log line per
    # node instead of a pile of individually-innocuous add_node/
    # add_edge commands whose combined timing is where the bug lived.

    def _node_is_ready(self, node) -> bool:
        """A backed node is genuinely usable once its structural pieces
        and (if any) its module are healthy AND every backing it owns
        has actually been confirmed present in the live graph - not
        merely "the owning process is still running", which is true the
        instant pw-cli's create() call returns, well before pw-dump's
        snapshot (and therefore pwmatch) has caught up. Anything that
        isn't a BackedNode (plain filters, gates, ...) has no structural
        state to wait on at all."""
        if not isinstance(node, BackedNode):
            return True
        if not node.structural_ok():
            return False
        if node.has_module() and not node.module_ok():
            return False
        return all(b.node_id is not None for b in node.backings)

    def _node_health(self, node) -> str:
        """Coarse health for a backed node, surfaced to the GUI so an
        effect that has actually failed is distinguishable from one
        that's merely still coming up:

          * ``"dead"``     - a process-owning backing exited or blew
            past its resolve grace (see OwnedPwNode.stuck), or the node
            is otherwise ready but its own module interior never
            connected (see PatchSpace.node_internals_wired) - i.e. it
            exists but no audio can pass.
          * ``"starting"`` - not ready yet, but nothing has actually
            died; the next supervision tick may still bring it up.
          * ``"ok"``       - healthy and, if it has a module, internally
            wired.

        Non-backed nodes (gates, switches, plain filters) are always
        ``"ok"`` - they have no owned processes to die."""
        if not isinstance(node, BackedNode):
            return "ok"
        if node.dead_backings():
            return "dead"
        if not self._node_is_ready(node):
            return "starting"
        if node.has_module() and not self.space.node_internals_wired(node.id):
            return "dead"
        return "ok"

    def _bring_node_up(self, node) -> bool:
        """Poll a single node toward readiness, running the same
        per-node repair step supervise() would run on the periodic tick,
        but focused on just this node and returning as soon as it's
        ready instead of waiting out a fixed interval. The lock is held
        only for each short repair step, not across the sleep between
        them, so a slow node during a big load never starves the
        pw-dump event thread's own callbacks (which take this same
        lock) for the whole timeout."""
        deadline = time.monotonic() + SESSION_LOAD_NODE_TIMEOUT_S
        while True:
            with self._lock:
                try:
                    self.space._supervise_node(node)
                except Exception as exc:
                    logger.warning("Bring-up step for %r failed: %s", node.id, exc)
                ready = self._node_is_ready(node)
            if ready:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(SESSION_LOAD_POLL_S)

    def _careful_bring_up(self, node) -> bool:
        """Bring one finicky effect node (see _CAREFUL_NODE_TYPES) up on
        its own, staged so the periodic supervision tick leaves it alone,
        and wait for its module interior to actually connect. This is the
        single-node form of the dedicated careful pass _load_session runs
        over every finicky node; _cmd_add_node uses it so a node created
        at runtime can't have its edges wired before its capture/playback
        streams exist (the "new echo-cancel node breaks the chain" bug).
        Returns whether the node became ready; a timeout is not fatal -
        the normal supervision tick keeps repairing it."""
        self.space.stage([node.id])
        try:
            logger.info(
                "Careful bring-up of %r before wiring its inputs\u2026", node.id
            )
            ready = self._bring_node_up(node)
        finally:
            self.space.unstage(node.id)
        if not ready:
            logger.warning(
                "%r did not become ready within %.1fs - wiring it anyway; "
                "the normal supervision tick will keep repairing it",
                node.id,
                SESSION_LOAD_NODE_TIMEOUT_S,
            )
        if not self._wait_node_internals_wired(node.id):
            logger.warning(
                "%r's interior did not finish wiring within %.1fs - wiring "
                "its edges anyway",
                node.id,
                SESSION_LOAD_NODE_TIMEOUT_S,
            )
        return ready

    def _wait_node_internals_wired(self, node_id: str) -> bool:
        """Poll until `node_id`'s own module interior (capture/playback
        sandwich) is fully linked, the same short-poll discipline
        _bring_node_up uses. _node_is_ready only confirms the module's
        streams exist; this confirms they are actually connected, which
        is what decides whether audio can pass through the node at
        all."""
        deadline = time.monotonic() + SESSION_LOAD_NODE_TIMEOUT_S
        while True:
            with self._lock:
                if self.space.node_internals_wired(node_id):
                    return True
                self.space.supervise()
            if time.monotonic() >= deadline:
                return False
            time.sleep(SESSION_LOAD_POLL_S)

    def _store_session_edge(self, edge: dict):
        """Store one edge from a session config. Returns
        ``(logical_id, stored_id, created, error)`` where ``created`` is
        False when the stored edge already existed and ``error`` is a
        message string (else None). Shared by the batch wiring and the
        per-input careful wiring so both bookkeep identically."""
        from_node, to_node = edge.get("from"), edge.get("to")
        to_port = edge.get("to_port", "in")
        from_port = edge.get("from_port", "out")
        logical_id = PatchSpace._edge_id(
            from_node or "?", to_node or "?", to_port, from_port
        )
        if not from_node or not to_node:
            return logical_id, None, False, "missing from/to"
        stored_id = self._edge_stored_id(edge)
        if stored_id in self.space.edges:
            return logical_id, stored_id, False, None
        try:
            self._store_edge(from_node, to_node, to_port, from_port)
        except (KeyError, ValueError) as exc:
            return logical_id, stored_id, False, str(exc)
        return logical_id, stored_id, True, None

    def _edge_stored_id(self, edge: dict) -> Optional[str]:
        """The id a session-config edge will be stored under, without
        storing it (routes through a Sensitivity gate's hidden pre/post
        nodes - see _stored_endpoints). Used by the careful pass to skip
        edges already wired by the batch pass."""
        from_node, to_node = edge.get("from"), edge.get("to")
        if not from_node or not to_node:
            return None
        stored_from, stored_to = self._stored_endpoints(from_node, to_node)
        return PatchSpace._edge_id(
            stored_from,
            stored_to,
            edge.get("to_port", "in"),
            edge.get("from_port", "out"),
        )

    def _wire_edge_carefully(self, edge: dict):
        """Store one edge and wait for it to become a live PipeWire
        link before returning - the edge-side mirror of _bring_node_up,
        and the "input by input" half of the finicky-node pass. Returns
        ``(logical_id, stored_id, created, error)``; a wiring timeout is
        logged but not an error (the normal supervision tick keeps
        repairing it)."""
        logical_id, stored_id, created, err = self._store_session_edge(edge)
        if err is not None or stored_id is None:
            return logical_id, stored_id, created, err
        deadline = time.monotonic() + SESSION_LOAD_EDGE_TIMEOUT_S
        while True:
            with self._lock:
                if self.space.edge_wired(stored_id):
                    return logical_id, stored_id, created, None
                self.space.supervise()
            if time.monotonic() >= deadline:
                logger.warning(
                    "Edge %s did not become live within %.1fs during careful "
                    "wiring - leaving it for the normal supervision tick",
                    logical_id,
                    SESSION_LOAD_EDGE_TIMEOUT_S,
                )
                return logical_id, stored_id, created, None
            time.sleep(SESSION_LOAD_EDGE_POLL_S)

    def _downstream_edges(self, root_ids, edges_cfg):
        """Session edges on the downstream side of `root_ids`, in signal
        order: closest to the roots first, so an edge into a device
        output is re-created last. Walks the config's own from->to
        edges, so it follows straight through transparent nodes
        (switches, splitters) without needing the live graph."""
        depth: Dict[str, int] = {nid: 0 for nid in root_ids}
        adjacency: Dict[str, list] = {}
        order: Dict[int, int] = {}
        for i, edge in enumerate(edges_cfg):
            src, dst = edge.get("from"), edge.get("to")
            if src and dst:
                adjacency.setdefault(src, []).append(dst)
            order[id(edge)] = i
        queue = deque(depth)
        while queue:
            node = queue.popleft()
            for nxt in adjacency.get(node, []):
                if nxt not in depth:
                    depth[nxt] = depth[node] + 1
                    queue.append(nxt)
        ranked = [
            (depth[edge.get("from")], order[id(edge)], edge)
            for edge in edges_cfg
            if edge.get("from") in depth
        ]
        ranked.sort(key=lambda item: (item[0], item[1]))
        return [edge for _, _, edge in ranked]

    def _relink_edge_carefully(self, edge: dict):
        """Drop an edge's current live links, then re-create and wait for
        them the careful way - the automated equivalent of the manual
        unplug/replug of one line. Returns the same tuple as
        _wire_edge_carefully."""
        stored_id = self._edge_stored_id(edge)
        if stored_id is not None:
            self.space.drop_edge_links(stored_id)
            # Rederive the edge's desired pairs now, so the edge does not
            # look trivially "wired" (empty bookkeeping) to the wait
            # below; sync_locked() then issues the fresh connect.
            with self._lock:
                self.space.sync_locked()
        return self._wire_edge_carefully(edge)

    def _load_session(self, config: dict) -> dict:
        """Stage a full config (same shape export/import already use)
        onto the running PatchSpace: create every node's structural
        pieces up front, bring every BACKED node up one at a time (see
        module note above), then wire edges only once every node has
        had its chance to settle. Finicky effect nodes (see
        _CAREFUL_NODE_TYPES - echo/noise cancel today) get a dedicated
        second pass that brings each one up on its own and then wires its
        edges one input at a time, each confirmed live before the next,
        so a slow module can't leave a half-attached, silent chain.
        Never raises for a single bad node or edge - each failure is
        collected and the rest of the load proceeds, same
        idempotent-on-partial-overlap spirit as apply_config.py."""
        nodes_cfg = config.get("nodes", {}) or {}
        edges_cfg = config.get("edges", []) or []
        groups_cfg = config.get("groups", []) or []

        created, updated, node_failures = [], [], []
        backed_ids: List[str] = []
        # Backed nodes needing the dedicated second, per-input pass
        # (see _CAREFUL_NODE_TYPES), in creation order.
        finicky_ids: List[str] = []

        with self._lock:
            # One batched sweep for everything this config could
            # collide with, rather than the per-node reap _cmd_add_node
            # does - cheaper, and it can't mistake a node created
            # earlier in *this same* load for a stale leftover of one
            # created later in it just because they share a prefix.
            #
            # Only nodes this load will actually *create* are swept.
            # A node already in the space keeps its live objects: the
            # loop below re-adopts the existing node object unchanged,
            # so reaping its backing would kill the owning process of a
            # perfectly healthy running node and leave it structurally
            # present but silent - which then thrashes as the daemon
            # tries to rebuild it. (Re-importing the current session is
            # the common case: every node is "existing".)
            backing_names = [
                (node_cfg.get("params") or {}).get("backing_node_name")
                for node_id, node_cfg in nodes_cfg.items()
                if node_id not in self.space.nodes
            ]
            backing_names = [b for b in backing_names if b]
            if backing_names:
                self.graph.reap_stale_for_names(backing_names)

            # Stage every id that will turn out to be a BackedNode
            # *before* creating any of them - add_node() wakes the
            # ticker the moment the first one lands, and the ticker
            # only has to wait for this same lock to be released to
            # run a full supervise() pass. Staging up front (rather
            # than as each node is created) is what keeps that pass
            # from grabbing a node we haven't gotten to yet in the
            # one-at-a-time bring-up loop below - see the _staging
            # docstring on PatchSpace.__init__ for the race this
            # closes.
            to_stage = [
                node_id
                for node_id, node_cfg in nodes_cfg.items()
                if issubclass(
                    NODE_TYPE_REGISTRY.get(node_cfg.get("type"), object), BackedNode
                )
            ]
            if to_stage:
                self.space.stage(to_stage)

            for node_id, node_cfg in nodes_cfg.items():
                node_type = node_cfg.get("type")
                params = node_cfg.get("params", {}) or {}
                cls = NODE_TYPE_REGISTRY.get(node_type)
                if cls is None:
                    node_failures.append((node_id, f"unknown node type {node_type!r}"))
                    continue

                if node_id in self.space.nodes:
                    existing = self.space.nodes[node_id]
                    if type(existing) is not cls:
                        node_failures.append(
                            (node_id, "exists but with a different type")
                        )
                        continue
                    for key, value in params.items():
                        if hasattr(existing, key):
                            setattr(existing, key, value)
                    self._adopt_line_volume(existing, params)
                    if hasattr(existing, "apply_device_settings"):
                        existing.apply_device_settings()
                    if isinstance(existing, SensitivityGateNode):
                        self._ensure_sensitivity_internals(existing)
                    updated.append(node_id)
                    if isinstance(existing, BackedNode):
                        backed_ids.append(node_id)
                        if isinstance(existing, _CAREFUL_NODE_TYPES):
                            finicky_ids.append(node_id)
                    continue

                node = self._create_node(node_type, node_id, params)
                if node is None:
                    node_failures.append((node_id, f"unknown node type {node_type!r}"))
                    continue
                if "label" in params:
                    node.label = params["label"]
                _apply_layout_attrs(node, params)
                self.space.add_node(node)
                self._adopt_line_volume(node, params)
                if isinstance(node, SensitivityGateNode):
                    backed_ids.extend(self._ensure_sensitivity_internals(node))
                if isinstance(node, LiveResolvableNode):
                    self._try_immediate_resolve(node)
                created.append(node_id)
                if isinstance(node, BackedNode):
                    backed_ids.append(node_id)
                    if isinstance(node, _CAREFUL_NODE_TYPES):
                        finicky_ids.append(node_id)

        # Outside the lock: bring backed nodes up one at a time so a
        # slow module load never blocks the whole load, or the rest of
        # the daemon, for longer than it has to. Each node is unstaged
        # right after its own bring-up attempt (success or timeout) -
        # not staged for the whole loop - so node 2's turn still runs
        # under the same protection node 1's did, and a node that timed
        # out still falls back to normal periodic repair afterward
        # instead of being skipped forever.
        #
        # The finicky effect nodes are deliberately held back from this
        # first pass and left staged: their dedicated pass below brings
        # each one up on its own and then wires its inputs one at a
        # time, which must not overlap with the batch wiring of the rest
        # of the graph.
        careful_set = set(finicky_ids)
        regular_backed = [nid for nid in backed_ids if nid not in careful_set]
        not_ready = []
        try:
            for node_id in regular_backed:
                node = self.space.nodes.get(node_id)
                if node is None:
                    continue
                logger.info("Bringing up %r before wiring its edges\u2026", node_id)
                try:
                    if not self._bring_node_up(node):
                        not_ready.append(node_id)
                        logger.warning(
                            "%r did not become ready within %.1fs - wiring it anyway; "
                            "the normal supervision tick will keep repairing it",
                            node_id,
                            SESSION_LOAD_NODE_TIMEOUT_S,
                        )
                finally:
                    self.space.unstage(node_id)
        finally:
            # Belt-and-suspenders: make sure nothing (a failed node
            # lookup above, an exception from _bring_node_up itself)
            # can leave an id staged forever.
            for node_id in regular_backed:
                self.space.unstage(node_id)

        edges_created, edge_failures = [], []
        stored_edge_ids: List[str] = []
        with self._lock:
            for edge in edges_cfg:
                if (
                    edge.get("from") in careful_set
                    or edge.get("to") in careful_set
                ):
                    # Left for the finicky pass below.
                    continue
                logical_id, stored_id, created_edge, err = self._store_session_edge(
                    edge
                )
                if err is not None:
                    edge_failures.append((logical_id, err))
                elif created_edge:
                    edges_created.append(logical_id)
                if stored_id is not None:
                    stored_edge_ids.append(stored_id)
            for raw in groups_cfg:
                gid = raw.get("id")
                if not gid:
                    continue
                self.groups[gid] = {
                    "id": gid,
                    "label": raw.get("label", "Group"),
                    "color": raw.get("color", "#3584e4"),
                    "nodes": [
                        n for n in raw.get("nodes", []) if n in self.space.nodes
                    ],
                }
            self.space.supervise()

        # ------------------------------------------------------------------
        # Second, careful pass: finicky effect nodes, one at a time.
        # ------------------------------------------------------------------
        # Echo/noise-cancel modules publish their capture and playback
        # streams asynchronously, so wiring their edges in the same batch
        # as the rest of the graph can attach a user edge to an input
        # dummy before the module's own side is live - the node ends up
        # structurally present but acoustically dead (see
        # _CAREFUL_NODE_TYPES and PatchSpace.node_internals_wired). Each
        # such node is brought up on its own and waited on, its interior
        # confirmed connected, and only then are its edges wired - inputs
        # first, one at a time, each confirmed live before the next.
        careful_edges = [
            e
            for e in edges_cfg
            if e.get("from") in careful_set or e.get("to") in careful_set
        ]
        stored_edge_set = set(stored_edge_ids)
        # Both endpoints of an edge have to be live before it can be
        # wired: an edge between two finicky nodes is deferred until the
        # second one's turn rather than waiting out the timeout against a
        # node that's still staged. `other_endpoint_ready` encodes that.
        processed_finicky: set = set()

        def other_endpoint_ready(edge: dict, node_id: str) -> bool:
            other = edge["from"] if edge.get("to") == node_id else edge.get("to")
            return other not in careful_set or other in processed_finicky

        try:
            for node_id in finicky_ids:
                node = self.space.nodes.get(node_id)
                if node is None:
                    # Endpoint never got created; still mark it processed
                    # so edges to it are attempted (and fail) below rather
                    # than being silently dropped.
                    processed_finicky.add(node_id)
                    continue
                if not self._careful_bring_up(node) and node_id not in not_ready:
                    not_ready.append(node_id)
                # Inputs (edges landing on this node) first, then outputs;
                # one at a time, each confirmed live before the next.
                inputs = [
                    e
                    for e in careful_edges
                    if e.get("to") == node_id
                    and other_endpoint_ready(e, node_id)
                ]
                outputs = [
                    e
                    for e in careful_edges
                    if e.get("from") == node_id
                    and other_endpoint_ready(e, node_id)
                ]
                for edge in inputs + outputs:
                    logical_id, stored_id, created_edge, err = (
                        self._wire_edge_carefully(edge)
                    )
                    if err is not None:
                        edge_failures.append((logical_id, err))
                        continue
                    if created_edge:
                        edges_created.append(logical_id)
                    if stored_id is not None:
                        stored_edge_ids.append(stored_id)
                        stored_edge_set.add(stored_id)
                processed_finicky.add(node_id)

            # Safety net: anything touching a finicky node that never got
            # attempted (a failed node lookup, an edge between two
            # never-created finicky nodes) is still stored, so its failure
            # is reported rather than silently dropped.
            for edge in careful_edges:
                stored_id = self._edge_stored_id(edge)
                if stored_id is not None and stored_id in stored_edge_set:
                    continue
                logical_id, stored_id, created_edge, err = (
                    self._store_session_edge(edge)
                )
                if err is not None:
                    edge_failures.append((logical_id, err))
                elif created_edge:
                    edges_created.append(logical_id)
                if stored_id is not None:
                    stored_edge_ids.append(stored_id)
                    stored_edge_set.add(stored_id)
        finally:
            # Never leave a finicky node staged if something above threw.
            for node_id in finicky_ids:
                self.space.unstage(node_id)

        # ------------------------------------------------------------------
        # Post-bring-up re-link of the finicky nodes' downstream chains.
        # ------------------------------------------------------------------
        # An echo/noise-cancel module publishes its streams late, so
        # anything wired downstream of it while it was still coming up can
        # end up present-but-dead - most visibly a device link like
        # "Mic Line -> bluetooth sink", which then never carries audio.
        # Unlike a genuinely missing link, sync() never touches that one,
        # which is why it takes a manual unplug/replug to fix. Now that
        # every finicky node's interior is confirmed live, drop and re-make
        # each downstream edge once, in signal order, so the device link is
        # created last - exactly the manual sequence, automated.
        if finicky_ids:
            downstream = self._downstream_edges(finicky_ids, edges_cfg)
            logger.info(
                "Re-linking %d edge(s) downstream of finicky node(s) %s\u2026",
                len(downstream), finicky_ids,
            )
            for edge in downstream:
                logical_id, stored_id, _created, err = self._relink_edge_carefully(
                    edge
                )
                if err is not None:
                    edge_failures.append((logical_id, err))
                    continue
                if stored_id is not None and stored_id not in stored_edge_set:
                    stored_edge_ids.append(stored_id)
                    stored_edge_set.add(stored_id)

        # Wait for the edges just wired to actually land as live
        # PipeWire links, the same way backed nodes were brought up one
        # at a time above - instead of handing an unfinished graph to
        # the periodic tick and hoping it catches up. sync_locked() (run
        # inside supervise()) is incremental and lock-protected, so this
        # just re-runs it on a short poll until every stored edge from
        # this load reports wired, or the timeout is reached. Edges that
        # never resolve to any desired pairs (a switched-off/gated path)
        # already count as "wired" - see PatchSpace.edge_wired - so this
        # only waits on edges that genuinely have live links pending.
        edges_not_ready: List[str] = []
        deadline = time.monotonic() + SESSION_LOAD_EDGE_TIMEOUT_S
        pending = list(dict.fromkeys(stored_edge_ids))  # de-duped, order kept
        while pending:
            with self._lock:
                still_pending = [
                    eid for eid in pending if not self.space.edge_wired(eid)
                ]
            if not still_pending:
                break
            pending = still_pending
            if time.monotonic() >= deadline:
                edges_not_ready = list(pending)
                logger.warning(
                    "%d edge(s) did not finish wiring within %.1fs - leaving "
                    "them for the normal supervision tick: %s",
                    len(pending), SESSION_LOAD_EDGE_TIMEOUT_S, pending,
                )
                break
            time.sleep(SESSION_LOAD_EDGE_POLL_S)
            with self._lock:
                self.space.supervise()

        self._dirty = True
        logger.info(
            "Session load complete: %d node(s) created, %d updated, %d edge(s) "
            "wired (%d node failure(s), %d not-ready, %d edge failure(s), "
            "%d edge(s) still pending)",
            len(created),
            len(updated),
            len(edges_created),
            len(node_failures),
            len(not_ready),
            len(edge_failures),
            len(edges_not_ready),
        )
        return {
            "status": "ok",
            "nodes_created": created,
            "nodes_updated": updated,
            "nodes_failed": node_failures,
            "nodes_not_ready": not_ready,
            "edges_created": edges_created,
            "edges_failed": edge_failures,
            "edges_not_ready": edges_not_ready,
        }

    def _cmd_load_session(self, cmd: dict) -> dict:
        """Load a full config directly from disk (default: the
        auto-saved last-session cache) or from an inline dict, and
        stage it via _load_session. This is what "Import Last Session"
        and file-based import now call instead of the GUI reading the
        file itself and replaying it as individual commands - see the
        module note above _load_session for why that mattered, and
        constants.py's old comment on SESSION_CACHE_PATH for why the
        GUI reading this particular file directly was already
        considered a wart worth avoiding."""
        config = cmd.get("config")
        if config is None:
            path = cmd.get("path") or SESSION_CACHE_PATH
            try:
                with open(path) as f:
                    config = json.load(f)
            except FileNotFoundError:
                return {"status": "error", "message": f"No session file at {path}"}
            except (OSError, json.JSONDecodeError) as exc:
                return {"status": "error", "message": f"Could not load {path}: {exc}"}
        if not isinstance(config, dict):
            return {"status": "error", "message": "config must be a JSON object"}

        def _run():
            try:
                self._load_session(config)
            except Exception:
                logger.exception("Background session load failed")

        # Backed away from doing this synchronously on the command
        # thread: a session with several effect nodes can legitimately
        # take several seconds (SESSION_LOAD_NODE_TIMEOUT_S each, in the
        # worst case) to bring up, and socket_client.py's request/
        # response protocol is strictly one-in-flight - a synchronous
        # multi-second reply here would hold up every *other* GUI
        # command (a volume drag, a settings change) queued behind it
        # on the same connection. The GUI's own periodic get_nodes poll
        # (see patchspace_widget's REFRESH_INTERVAL_MS timer) already
        # picks up nodes/edges as they land, so there's nothing for
        # this response to carry beyond "the load has started".
        threading.Thread(target=_run, daemon=True).start()
        return {"status": "ok", "started": True}

    # ------------------------------------------------------------------
    # node factory
    # ------------------------------------------------------------------

    def _create_node(
        self, node_type: str, node_id: str, config: dict
    ) -> Optional[Node]:
        cls = NODE_TYPE_REGISTRY.get(node_type)
        if cls is None:
            return None
        g = config.get
        backing = g("backing_node_name") or f"patchbay_{node_id}"

        if cls is RegexInputNode:
            return cls(node_id, g("pattern", ""))
        if cls is MediaClassInputNode:
            return cls(node_id, g("media_class", ""))
        if cls is DescriptionInputNode:
            return cls(node_id, g("description", ""))
        if cls is RegexOutputNode:
            return cls(node_id, g("pattern", ""), g("port_type"))
        if cls is MediaClassOutputNode:
            return cls(node_id, g("media_class", ""), g("port_type"))
        if cls is DescriptionOutputNode:
            return cls(node_id, g("description", ""), g("port_type"))
        if cls is SplitterNode:
            return cls(node_id, backing)
        if cls is GateNode:
            return cls(node_id, g("enabled", True))
        if cls in (SwitcherNode, InverseSwitcherNode):
            return cls(node_id, g("output", 0))
        if cls is ExcludeFilterNode:
            return cls(node_id, g("pattern", ""))
        if cls is VolumeProcessNode:
            return cls(
                node_id,
                backing,
                g("initial_volume", 1.0),
                g("volume_min", 0.0),
                g("volume_max", 1.0),
            )
        if cls is NoiseCancelNode:
            return cls(
                node_id,
                backing,
                vad_threshold=g("vad_threshold", 50.0),
                ladspa_plugin=g("ladspa_plugin", ""),
                ladspa_label=g("ladspa_label", ""),
                method=g("method", "rnnoise"),
            )
        if cls is SensitivityGateNode:
            return cls(
                node_id,
                backing,
                level=g("level", 25.0),
                sensitivity=g("sensitivity", 0.0),
                ladspa_plugin=g("ladspa_plugin", ""),
                ladspa_label=g("ladspa_label", ""),
            )
        if cls is ReverbNode:
            return cls(
                node_id,
                backing,
                g("ladspa_plugin", ""),
                g("ladspa_label", ""),
                g("wet_dry", 0.3),
            )
        if cls is NormalizeNode:
            return cls(
                node_id,
                backing,
                boost_db=g("boost_db", 15.0),
                max_boost_db=g("max_boost_db", 24.0),
                ceiling_db=g("ceiling_db", -1.0),
                leveling=g("leveling", True),
                threshold_db=g("threshold_db", -35.0),
                ratio=g("ratio", 4.0),
                attack_ms=g("attack_ms", 10.0),
                release_ms=g("release_ms", 400.0),
                knee_db=g("knee_db", 6.0),
                limiter_release_s=g("limiter_release_s", 0.5),
                ladspa_dir=g("ladspa_dir", ""),
            )
        if cls in (EchoCancelNode, LightNoiseCancelNode):
            return cls(
                node_id,
                backing,
                g("library_name", ""),
                g("aec_args", ""),
                g("monitor_mode", False),
            )
        if cls in (DeviceInputNode, DeviceOutputNode):
            return cls(
                node_id,
                g("device_name", ""),
                g("description", ""),
                g("device_volume", 1.0),
                g("profile_index"),
                g("profile_description", ""),
                g("volume_locked", True),
            )
        if cls in (AppInputNode, AppOutputNode):
            return cls(node_id, g("app_name", ""))
        if cls is PatchBayDeviceNode:
            return cls(node_id, g("device_volume", 1.0), g("volume_locked", True))
        if cls is PatchBayMicDeviceNode:
            return cls(node_id, g("device_volume", 1.0), g("volume_locked", True))
        if cls in (VirtualSpeakerNode, VirtualMicNode):
            return cls(
                node_id,
                backing,
                g("device_label", ""),
                g("device_volume", 1.0),
                g("volume_locked", True),
            )
        raise AssertionError(f"unhandled node type {node_type}")

    # ------------------------------------------------------------------
    # commands
    # ------------------------------------------------------------------

    def _cmd_add_node(self, cmd: dict) -> dict:
        node_type = cmd.get("node_type")
        node_id = cmd.get("node_id")
        config = cmd.get("config", {}) or {}
        if not node_type or not node_id:
            return {"status": "error", "message": "node_type and node_id required"}
        cls = NODE_TYPE_REGISTRY.get(node_type)
        if cls is None:
            return {"status": "error", "message": f"Unknown node type: {node_type}"}

        careful_node = None
        with self._lock:
            if node_id in self.space.nodes:
                existing = self.space.nodes[node_id]
                if type(existing) is not cls:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} exists but with a different type",
                    }
                # Idempotent re-apply: update config fields. "level" on a
                # SensitivityGateNode is excluded here and handled below via
                # set_level() - a raw setattr skips both the 0..100 clamp
                # and the _control_applied_to cache reset, so a replayed
                # config wouldn't re-push the live LADSPA threshold a level
                # change needs (see SensitivityGateNode's docstring).
                for key, value in config.items():
                    if key == "level" and isinstance(existing, SensitivityGateNode):
                        continue
                    if hasattr(existing, key):
                        setattr(existing, key, value)
                self._adopt_line_volume(existing, config)
                if isinstance(existing, VolumeProcessNode):
                    if "initial_volume" in config:
                        existing.set_volume(config["initial_volume"])
                    if "volume_min" in config or "volume_max" in config:
                        existing.set_volume_range(
                            getattr(existing, "volume_min", 0.0),
                            getattr(existing, "volume_max", 1.0),
                        )
                if hasattr(existing, "apply_device_settings"):
                    existing.apply_device_settings()
                if isinstance(existing, SensitivityGateNode):
                    if "level" in config:
                        existing.set_level(config["level"])
                    else:
                        existing.refresh_live()
                    self._ensure_sensitivity_internals(existing)
                self.space.sync()
                return {"status": "ok", "node_id": node_id, "already_existed": True}

            node = self._create_node(node_type, node_id, config)
            if node is None:
                return {"status": "error", "message": f"Unknown node type: {node_type}"}
            if "label" in config:
                node.label = config["label"]
            _apply_layout_attrs(node, config)
            if isinstance(node, BackedNode):
                # Clean any leftover objects from an earlier (crashed) run
                # before creating ours - see PipewireGraph.reap_stale_for_names.
                self.graph.reap_stale_for_names([node.backing_node_name])
            careful = isinstance(node, _CAREFUL_NODE_TYPES)
            if careful:
                # Stage before add_node so the periodic supervision tick
                # can't touch this node while we bring it up below (same
                # contract as _load_session's careful pass).
                self.space.stage([node_id])
            self.space.add_node(node)
            self._adopt_line_volume(node, config)
            if isinstance(node, SensitivityGateNode):
                self._ensure_sensitivity_internals(node)
            if isinstance(node, LiveResolvableNode):
                self._try_immediate_resolve(node)
            if careful:
                careful_node = node
            else:
                self.space.supervise()
            self._dirty = True

        if careful_node is not None:
            # Finicky effect modules (echo/noise cancel) publish their
            # capture/playback streams asynchronously. Bring this one up
            # on its own and let its interior connect before returning,
            # so the edges the GUI wires next can't attach to a stream
            # that isn't live yet - the "new echo-cancel node breaks the
            # chain" failure mode.
            self._careful_bring_up(careful_node)
            with self._lock:
                self.space.supervise()
        return {"status": "ok", "node_id": node_id, "already_existed": False}

    def _cmd_remove_node(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        with self._lock:
            if (
                node_id not in self.space.nodes
                or node_id not in self.space.public_nodes
            ):
                return {"status": "error", "message": f"Node {node_id} not found"}
            if isinstance(self.space.nodes.get(node_id), SensitivityGateNode):
                # Drop the hidden gain-staging children this gate owns -
                # removing the gate alone would orphan them.
                for child in (_sens_pre_id(node_id), _sens_post_id(node_id)):
                    if child in self.space.nodes:
                        self.space.remove_node(child)
            self.space.remove_node(node_id)
            # Drop the node from any group it belonged to.
            for group in self.groups.values():
                if node_id in group["nodes"]:
                    group["nodes"] = [n for n in group["nodes"] if n != node_id]
            self.space.sync()
            self._dirty = True
            return {"status": "ok"}

    def _cmd_rename_node(self, cmd: dict) -> dict:
        old_id, new_id = cmd.get("old_node_id"), cmd.get("new_node_id")
        if not old_id or not new_id:
            return {
                "status": "error",
                "message": "old_node_id and new_node_id required",
            }
        with self._lock:
            is_sensitivity = isinstance(
                self.space.nodes.get(old_id), SensitivityGateNode
            )
            try:
                self.space.rename_node(old_id, new_id)
            except (KeyError, ValueError) as exc:
                return {"status": "error", "message": str(exc)}
            if is_sensitivity:
                # Hidden children are keyed off the gate id; carry them
                # along so the pre/post mapping keeps resolving.
                for old_child, new_child in (
                    (_sens_pre_id(old_id), _sens_pre_id(new_id)),
                    (_sens_post_id(old_id), _sens_post_id(new_id)),
                ):
                    if old_child in self.space.nodes:
                        try:
                            self.space.rename_node(old_child, new_child)
                        except (KeyError, ValueError):
                            pass
            for group in self.groups.values():
                group["nodes"] = [
                    new_id if n == old_id else n for n in group["nodes"]
                ]
            self.space.sync()
            self._dirty = True
            return {"status": "ok", "node_id": new_id}

    # ------------------------------------------------------------------
    # hidden Sensitivity-gate gain staging
    # ------------------------------------------------------------------

    def _ensure_sensitivity_internals(self, gate) -> list:
        """Create the hidden pre/post VolumeProcessNodes bracketing a
        Sensitivity gate (if absent) and wire the two internal edges
        that put them in the signal path: pre -> gate -> post. Called
        under self._lock whenever a Sensitivity node is added or a
        session carrying one is loaded. Returns the ids it created so a
        session load can also bring their modules up."""
        gate_id = gate.id
        pre_id = _sens_pre_id(gate_id)
        post_id = _sens_post_id(gate_id)
        created = []
        if pre_id not in self.space.nodes:
            pre = VolumeProcessNode(
                pre_id,
                f"patchbay_{pre_id}",
                initial_volume=gate.sensitivity,
                volume_min=SensitivityGateNode.PRE_GAIN_MIN,
                volume_max=SensitivityGateNode.PRE_GAIN_MAX,
            )
            self.space.add_node(pre, public=False)
            created.append(pre_id)
        if post_id not in self.space.nodes:
            post = VolumeProcessNode(
                post_id,
                f"patchbay_{post_id}",
                initial_volume=1.0,
                volume_min=0.0,
                volume_max=SensitivityGateNode.POST_GAIN_MAX,
            )
            self.space.add_node(post, public=False)
            created.append(post_id)
        for a, b in ((pre_id, gate_id), (gate_id, post_id)):
            eid = PatchSpace._edge_id(a, b, "in")
            if eid not in self.space.edges:
                try:
                    self.space.add_edge(a, b, "in")
                except (KeyError, ValueError):
                    pass
        self._apply_sensitivity(gate)
        return created

    def _apply_sensitivity(self, gate) -> None:
        """Push a Sensitivity gate's 0..1 slider value onto its hidden
        pre/post volume nodes: the pre node's gain swings across
        PRE_GAIN_MIN..PRE_GAIN_MAX, the post node applies the exact
        reciprocal so output loudness is unchanged. No-op (and never
        raises) until both hidden nodes exist."""
        pre = self.space.nodes.get(_sens_pre_id(gate.id))
        post = self.space.nodes.get(_sens_post_id(gate.id))
        if not isinstance(pre, VolumeProcessNode) or not isinstance(
            post, VolumeProcessNode
        ):
            return
        fraction = max(0.0, min(1.0, getattr(gate, "sensitivity", 0.0)))
        pre.set_volume(fraction)
        pre_actual = pre.volume_min + (pre.volume_max - pre.volume_min) * fraction
        if pre_actual <= 1e-6:
            return
        # The post node's own volume is a fraction of its range, not the
        # gain itself - convert the desired reciprocal gain into that
        # range so a post max above unity actually reaches the makeup
        # level instead of being clamped at 1.0.
        post_gain = 1.0 / pre_actual
        span = post.volume_max - post.volume_min
        if span <= 1e-9:
            return
        post_fraction = (post_gain - post.volume_min) / span
        post.set_volume(post_fraction)

    def _stored_endpoints(self, from_node: str, to_node: str):
        """Map a logical (GUI-visible) edge onto the daemon's stored
        endpoints, routing through a Sensitivity gate's hidden pre/post
        nodes: anything feeding a gate lands on its pre node, and
        anything a gate feeds starts at its post node."""
        node = self.space.nodes.get(from_node)
        if isinstance(node, SensitivityGateNode):
            from_node = _sens_post_id(from_node)
        node = self.space.nodes.get(to_node)
        if isinstance(node, SensitivityGateNode):
            to_node = _sens_pre_id(to_node)
        return from_node, to_node

    def _store_edge(
        self,
        from_node: str,
        to_node: str,
        to_port: str = "in",
        from_port: str = "out",
    ) -> str:
        stored_from, stored_to = self._stored_endpoints(from_node, to_node)
        return self.space.add_edge(stored_from, stored_to, to_port, from_port)

    def _logical_edge(self, edge):
        """(from_node, to_node, to_port, from_port) as the GUI should
        see `edge`, or None if `edge` is one of the hidden internal links
        (pre->gate / gate->post) that must never surface."""
        from_node, to_node = edge.from_node, edge.to_node
        if from_node.startswith(_SENS_PRE_PREFIX):
            return None
        if to_node.startswith(_SENS_POST_PREFIX):
            return None
        from_node = _hidden_sensitivity_gate_for(from_node) or from_node
        to_node = _hidden_sensitivity_gate_for(to_node) or to_node
        return from_node, to_node, edge.to_port, edge.from_port

    def _stored_edge_id_for(self, logical_id: str):
        """The daemon's stored edge id whose GUI-visible form is
        `logical_id`, or None. Needed because a hidden-node-backed edge
        (X -> gate) is stored under a different id (X -> pre)."""
        for stored_id, edge in self.space.edges.items():
            logical = self._logical_edge(edge)
            if logical is None:
                continue
            lf, lt, port, from_port = logical
            if PatchSpace._edge_id(lf, lt, port, from_port) == logical_id:
                return stored_id
        return None

    def _cmd_add_edge(self, cmd: dict) -> dict:
        from_node, to_node = cmd.get("from_node"), cmd.get("to_node")
        to_port = cmd.get("to_port", "in")
        from_port = cmd.get("from_port", "out")
        if not from_node or not to_node:
            return {"status": "error", "message": "from_node and to_node required"}
        careful = False
        with self._lock:
            logical_id = PatchSpace._edge_id(from_node, to_node, to_port, from_port)
            stored_from, stored_to = self._stored_endpoints(from_node, to_node)
            stored_id = PatchSpace._edge_id(
                stored_from, stored_to, to_port, from_port
            )
            if stored_id in self.space.edges:
                return {"status": "ok", "edge_id": logical_id, "already_existed": True}
            try:
                self._store_edge(from_node, to_node, to_port, from_port)
            except (KeyError, ValueError) as exc:
                return {"status": "error", "message": str(exc)}
            careful = isinstance(
                self.space.nodes.get(from_node), _CAREFUL_NODE_TYPES
            ) or isinstance(self.space.nodes.get(to_node), _CAREFUL_NODE_TYPES)
            if not careful:
                self.space.sync()
            self._dirty = True

        if careful:
            # An echo/noise-cancel endpoint publishes its capture/
            # playback streams asynchronously. Wire this edge the
            # careful way - drop/re-make, waiting for the live link - so
            # an edge to a node created this session doesn't end up
            # present-but-silent. Edge-side mirror of _cmd_add_node's
            # careful bring-up.
            self._relink_edge_carefully(
                {
                    "from": from_node,
                    "to": to_node,
                    "to_port": to_port,
                    "from_port": from_port,
                }
            )
        return {"status": "ok", "edge_id": logical_id, "already_existed": False}

    def _cmd_remove_edge(self, cmd: dict) -> dict:
        edge_id = cmd.get("edge_id")
        with self._lock:
            stored_id = (
                edge_id
                if edge_id in self.space.edges
                else self._stored_edge_id_for(edge_id)
            )
            if stored_id is None:
                return {"status": "error", "message": f"Edge {edge_id} not found"}
            self.space.remove_edge(stored_id)
            self.space.sync()
            self._dirty = True
            return {"status": "ok"}

    def _cmd_set_gate(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        with self._lock:
            node = self.space.nodes.get(node_id)
            if not isinstance(node, GateNode):
                return {"status": "error", "message": f"Node {node_id} is not a gate"}
            node.enabled = bool(cmd.get("enabled", True))
            self.space.sync()
            self._dirty = True
            return {"status": "ok"}

    def _cmd_set_volume(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        volume = cmd.get("volume", 1.0)
        with self._lock:
            node = self.space.nodes.get(node_id)
            if not isinstance(node, VolumeProcessNode):
                return {
                    "status": "error",
                    "message": f"Node {node_id} is not a volume node",
                }
            node.set_volume(volume)
            self._dirty = True
            return {"status": "ok", "node_id": node_id, "volume": node.volume}

    def _cmd_set_volume_range(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        with self._lock:
            node = self.space.nodes.get(node_id)
            if not isinstance(node, VolumeProcessNode):
                return {
                    "status": "error",
                    "message": f"Node {node_id} is not a volume node",
                }
            node.set_volume_range(cmd.get("min", 0.0), cmd.get("max", 1.0))
            self._dirty = True
            return {"status": "ok"}

    def _cmd_set_node_property(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        prop = cmd.get("property")
        value = cmd.get("value")
        if not node_id or not prop:
            return {"status": "error", "message": "node_id and property required"}

        with self._lock:
            node = self.space.nodes.get(node_id)
            if node is None:
                return {"status": "error", "message": f"Node {node_id} not found"}

            if prop == "label":
                node.label = value
            elif prop == "output" and isinstance(node, ABSwitchNode):
                node.output = 1 if value else 0
            elif prop in ("pattern", "media_class", "description", "port_type"):
                if hasattr(node, prop):
                    setattr(node, prop, value)
                else:
                    return {
                        "status": "error",
                        "message": f"Node has no {prop!r} property",
                    }
            elif prop == "device_name" and isinstance(node, LiveResolvableNode):
                node.device_name = value
                node.resolve_live(None, None)
                self._try_immediate_resolve(node)
            elif prop == "app_name" and isinstance(node, LiveResolvableNode):
                node.app_name = value
                node.resolve_live(None, None)
                self._try_immediate_resolve(node)
            elif prop == "device_label" and hasattr(node, "device_label"):
                if node.device_label != value:
                    node.device_label = value
                    # The label only becomes the real node.description at
                    # creation time, so the backing must be rebuilt.
                    if isinstance(node, BackedNode):
                        node.teardown_backing()
                        node.ensure_structural()
            elif prop == "vad_threshold" and isinstance(node, NoiseCancelNode):
                try:
                    new_val = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    return {
                        "status": "error",
                        "message": "vad_threshold must be 0..100",
                    }
                node.set_vad_threshold(new_val)
            elif prop == "level" and isinstance(node, SensitivityGateNode):
                try:
                    new_val = max(0.0, min(100.0, float(value)))
                except (TypeError, ValueError):
                    return {"status": "error", "message": "level must be 0..100"}
                node.set_level(new_val)
            elif prop == "sensitivity" and isinstance(node, SensitivityGateNode):
                try:
                    new_val = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    return {"status": "error", "message": "sensitivity must be 0..1"}
                node.sensitivity = new_val
                self._apply_sensitivity(node)
            elif prop == "wet_dry" and isinstance(node, ReverbNode):
                try:
                    new_val = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    return {"status": "error", "message": "wet_dry must be 0..1"}
                if abs(new_val - node.wet_dry) < 1e-9 and node.module_ok():
                    return {"status": "ok"}
                node.wet_dry = new_val
                self._coalesce_reload(node)
            elif prop == "ladspa_plugin" and isinstance(
                node, (NoiseCancelNode, SensitivityGateNode)
            ):
                node.ladspa_plugin = value
                self._coalesce_reload(node)
            elif prop == "ladspa_label" and isinstance(
                node, (NoiseCancelNode, SensitivityGateNode)
            ):
                node.ladspa_label = value
                self._coalesce_reload(node)
            elif prop in ("library_name", "aec_args", "monitor_mode") and isinstance(
                node, EchoCancelNode
            ):
                if prop == "monitor_mode":
                    if not isinstance(value, bool):
                        return {
                            "status": "error",
                            "message": "monitor_mode must be boolean",
                        }
                    node.monitor_mode = value
                elif prop == "library_name":
                    node.library_name = value or EchoCancelNode.DEFAULT_AEC_LIBRARY
                else:
                    node.aec_args = value or ""
                self._coalesce_reload(node)
            elif prop == "volume_locked":
                locked = bool(value)
                target = self._line_volume_target(node)
                if target is not None:
                    target.volume_locked = locked
                    self._mirror_line_volume(target)
                    target.apply_device_settings()
                elif hasattr(node, "volume_locked"):
                    node.volume_locked = locked
                    node.apply_device_settings()
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no volume lock",
                    }
            elif isinstance(node, NormalizeNode) and prop in (
                "boost_db",
                "max_boost_db",
                "ceiling_db",
                "leveling",
                "threshold_db",
                "ratio",
                "attack_ms",
                "release_ms",
                "knee_db",
                "limiter_release_s",
                "ladspa_dir",
            ):
                if prop == "leveling":
                    node.leveling = bool(value)
                elif prop == "ladspa_dir":
                    node.ladspa_dir = value or ""
                else:
                    try:
                        new_val = float(value)
                    except (TypeError, ValueError):
                        return {
                            "status": "error",
                            "message": f"{prop} must be a number",
                        }
                    bounds = {
                        "boost_db": (node.BOOST_MIN_DB, node.BOOST_MAX_DB),
                        "max_boost_db": (node.BOOST_MIN_DB, node.BOOST_MAX_DB),
                        "ceiling_db": (node.CEILING_MIN_DB, node.CEILING_MAX_DB),
                        "threshold_db": (
                            node.THRESHOLD_MIN_DB,
                            node.THRESHOLD_MAX_DB,
                        ),
                        "ratio": (node.RATIO_MIN, node.RATIO_MAX),
                        "attack_ms": (node.ATTACK_MIN_MS, node.ATTACK_MAX_MS),
                        "release_ms": (node.RELEASE_MIN_MS, node.RELEASE_MAX_MS),
                        "knee_db": (node.KNEE_MIN_DB, node.KNEE_MAX_DB),
                        "limiter_release_s": (
                            node.LIMITER_RELEASE_MIN_S,
                            node.LIMITER_RELEASE_MAX_S,
                        ),
                    }
                    lo, hi = bounds[prop]
                    setattr(node, prop, max(lo, min(hi, new_val)))
                # Load-time filter-graph values: debounce one interior
                # reload rather than reloading per drag tick.
                self._coalesce_reload(node)
            else:
                return {
                    "status": "error",
                    "message": f"Unknown property {prop} for node type "
                    f"{CLASS_TO_TYPE.get(type(node), '?')}",
                }
            self.space.sync()
            self._dirty = True
            return {"status": "ok"}

    def _cmd_set_node_layout(self, cmd: dict) -> dict:
        """Persist the GUI's canvas layout: {node_id: {x, y, anchored}}.
        Purely presentational - it never touches the live PipeWire graph;
        it exists so positions and the anchored flag round-trip through
        export/import and the auto-saved last-session cache."""
        layout = cmd.get("layout") or {}
        with self._lock:
            for node_id, props in layout.items():
                node = self.space.nodes.get(node_id)
                if node is None or not isinstance(props, dict):
                    continue
                _apply_layout_attrs(node, props)
            self._dirty = True
        return {"status": "ok"}

    def _cmd_add_group(self, cmd: dict) -> dict:
        group_id = cmd.get("group_id")
        if not group_id:
            return {"status": "error", "message": "group_id required"}
        with self._lock:
            self.groups[group_id] = {
                "id": group_id,
                "label": cmd.get("label", "Group"),
                "color": cmd.get("color", "#3584e4"),
                "nodes": [n for n in cmd.get("nodes", []) if n in self.space.nodes],
            }
            self._dirty = True
        return {"status": "ok", "group_id": group_id}

    def _cmd_set_group(self, cmd: dict) -> dict:
        group_id = cmd.get("group_id")
        with self._lock:
            group = self.groups.get(group_id)
            if group is None:
                return {"status": "error", "message": f"Group {group_id} not found"}
            new_id = cmd.get("new_group_id") or group_id
            if new_id != group_id:
                if new_id in self.groups:
                    return {"status": "error", "message": f"Group {new_id} already exists"}
                self.groups.pop(group_id)
                group["id"] = new_id
                self.groups[new_id] = group
            if "label" in cmd:
                group["label"] = cmd["label"]
            if "color" in cmd:
                group["color"] = cmd["color"]
            if "nodes" in cmd:
                group["nodes"] = [
                    n for n in cmd["nodes"] if n in self.space.nodes
                ]
            self._dirty = True
        return {"status": "ok", "group_id": new_id}

    def _cmd_remove_group(self, cmd: dict) -> dict:
        with self._lock:
            self.groups.pop(cmd.get("group_id"), None)
            self._dirty = True
        return {"status": "ok"}

    def _cmd_set_device_volume(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        try:
            volume = max(0.0, min(1.0, float(cmd.get("volume", 1.0))))
        except (TypeError, ValueError):
            return {"status": "error", "message": "volume must be a number"}
        with self._lock:
            node = self.space.nodes.get(node_id)
            if node is None:
                return {"status": "error", "message": f"Node {node_id} not found"}
            target = self._line_volume_target(node)
            if target is not None:
                # A Speaker/Mic Line slider drives the shared built-in
                # device; mirror onto every sibling line so they agree.
                target.device_volume = volume
                self._mirror_line_volume(target)
            elif hasattr(node, "device_volume"):
                node.device_volume = volume
            else:
                return {
                    "status": "error",
                    "message": f"Node {node_id} has no controllable device",
                }

        # A user drag always takes effect immediately, locked or not -
        # the lock only governs the *continuous* override on the tick.
        apply_node = target if target is not None else node
        if hasattr(apply_node, "apply_device_settings"):
            try:
                apply_node.apply_device_settings(push_volume=True)
            except Exception as exc:
                logger.warning("Applying volume for %r failed: %s", node_id, exc)
        self._dirty = True
        applied = (
            target._volume_backing_node_id() is not None
            if target is not None
            else getattr(node, "live_node_id", None) is not None
        )
        return {
            "status": "ok",
            "node_id": node_id,
            "volume": volume,
            "applied": applied,
        }

    def _cmd_set_device_profile(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        profile_index = cmd.get("profile_index")
        if profile_index is None:
            return {"status": "error", "message": "profile_index required"}
        with self._lock:
            node = self.space.nodes.get(node_id)
            if not hasattr(node, "profile_index"):
                return {
                    "status": "error",
                    "message": f"Node {node_id} has no controllable device",
                }
            node.profile_index = profile_index
            node.profile_description = cmd.get("description", "")
        node.apply_device_settings()
        self._dirty = True
        return {
            "status": "ok",
            "node_id": node_id,
            "applied": node.live_node_id is not None,
        }

    def _cmd_get_hardware_devices(self, cmd: dict) -> dict:
        devices = {"inputs": [], "outputs": []}
        for node_id, node_data in self.graph.nodes().items():
            props = node_data.get("info", {}).get("props", {})
            media_class = props.get("media.class")
            name = props.get("node.name")
            if not name or media_class not in ("Audio/Source", "Audio/Sink"):
                continue
            if props.get("device.id") is None:
                continue
            if props.get("factory.name") == "support.null-audio-sink":
                continue
            entry = {
                "name": name,
                "description": props.get("node.description")
                or props.get("node.nick")
                or name,
                "is_bluetooth": props.get("device.api") == "bluez5",
            }
            (
                devices["inputs"]
                if media_class == "Audio/Source"
                else devices["outputs"]
            ).append(entry)
        return {"status": "ok", "devices": devices}

    def _cmd_get_applications(self, cmd: dict) -> dict:
        applications = {"inputs": [], "outputs": []}
        seen = set()
        for node_id, node_data in self.graph.nodes().items():
            props = node_data.get("info", {}).get("props", {})
            media_class = props.get("media.class")
            if media_class not in ("Stream/Output/Audio", "Stream/Input/Audio"):
                continue
            app_name = props.get("application.name") or props.get("node.name")
            if not app_name:
                continue
            key = (media_class, app_name)
            if key in seen:
                continue
            seen.add(key)
            entry = {"name": app_name}
            (
                applications["outputs"]
                if media_class == "Stream/Output/Audio"
                else applications["inputs"]
            ).append(entry)
        return {"status": "ok", "applications": applications}

    def _cmd_get_device_profiles(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        with self._lock:
            node = self.space.nodes.get(node_id)
            if not isinstance(node, LiveResolvableNode) or node.live_node_id is None:
                return {"status": "ok", "node_id": node_id, "profiles": []}
            live_props = dict(node.live_props)
        device_id = live_props.get("device.id")
        if device_id is None:
            return {"status": "ok", "node_id": node_id, "profiles": []}
        profiles = []
        for obj_id, obj_data in self.graph.all_objects().items():
            if obj_data.get("type") != DEVICE_OBJ_TYPE or obj_id != device_id:
                continue
            params = obj_data.get("info", {}).get("params", {})
            for p in params.get("EnumProfile", []):
                index = p.get("index")
                if index is None:
                    continue
                profiles.append(
                    {
                        "index": index,
                        "description": p.get("description")
                        or p.get("name")
                        or f"Profile {index}",
                    }
                )
            break
        return {"status": "ok", "node_id": node_id, "profiles": profiles}

    def _cmd_connect_ports(self, cmd: dict) -> dict:
        out_port, in_port = cmd.get("output_port"), cmd.get("input_port")
        if not isinstance(out_port, int) or not isinstance(in_port, int):
            return {
                "status": "error",
                "message": "output_port and input_port must be integers",
            }
        try:
            created = self.graph.connect(out_port, in_port)
            return {"status": "ok", "created": created}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def _cmd_disconnect_ports(self, cmd: dict) -> dict:
        out_port, in_port = cmd.get("output_port"), cmd.get("input_port")
        if not isinstance(out_port, int) or not isinstance(in_port, int):
            return {
                "status": "error",
                "message": "output_port and input_port must be integers",
            }
        try:
            removed = self.graph.disconnect(out_port, in_port)
            return {"status": "ok", "removed": removed}
        except Exception as exc:
            return {"status": "error", "message": str(exc)}

    def _cmd_get_nodes(self, cmd: dict) -> dict:
        with self._lock:
            groups = [dict(g) for g in self.groups.values()]
        return {
            "status": "ok",
            "nodes": self._serialize_nodes(),
            "edges": self._serialize_edges(),
            "groups": groups,
        }

    def _cmd_get_graph(self, cmd: dict) -> dict:
        return {"status": "ok", "graph": self._serialize_graph()}

    def _cmd_export_config(self, cmd: dict) -> dict:
        return {"status": "ok", "config": self._build_export_config()}

    def _cmd_reset(self, cmd: dict) -> dict:
        with self._lock:
            for edge_id in list(self.space.edges.keys()):
                self.space.remove_edge(edge_id)
            for node_id in list(self.space.nodes.keys()):
                if node_id in self.space.public_nodes:
                    self.space.remove_node(node_id)
            for node_id in list(self.space.nodes.keys()):
                if _hidden_sensitivity_gate_for(node_id) is not None:
                    self.space.remove_node(node_id)
            self.groups.clear()
            self.space.sync()
            self._dirty = True
            return {"status": "ok", "message": "Graph reset"}

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    def _serialize_nodes(self) -> dict:
        result = {}
        for node_id, node in self.space.nodes.items():
            if node_id not in self.space.public_nodes:
                continue
            node_type = CLASS_TO_TYPE.get(type(node), "unknown")
            data: Dict[str, Any] = {"id": node.id, "type": node_type}
            for attr in _SERIAL_ATTRS:
                if hasattr(node, attr):
                    data[attr] = getattr(node, attr)
            for attr in _LAYOUT_ATTRS:
                value = getattr(node, attr, None)
                if value is not None:
                    data[attr] = value
            if isinstance(node, VolumeProcessNode):
                data["volume"] = node.volume
                data["backing_node_id"] = node.backing_node_id
            if isinstance(node, BackedNode):
                # Same definition of "ready" _bring_node_up polls for on
                # session load - structural pieces + module (if any) all
                # healthy AND every backing confirmed present in the live
                # graph, not merely "the owning process is still running".
                # Surfaced to the GUI so a node that's still coming up (or
                # got dropped by supervision and hasn't been repaired yet)
                # shows as such instead of looking indistinguishable from
                # a fully-wired one.
                data["ready"] = self._node_is_ready(node)
                # Richer than "ready": separates "still coming up" from
                # "actually dead" so the GUI can flag a node whose module
                # exited or whose interior never connected (an
                # acoustically dead effect) with something stronger than
                # the neutral "not connected yet" badge. See _node_health.
                data["health"] = self._node_health(node)
            if isinstance(node, LiveResolvableNode):
                data["connected"] = node.live_node_id is not None
                data["is_bluetooth"] = node.live_props.get("device.api") == "bluez5"
                data["selection_label"] = (
                    node.live_props.get("node.description")
                    or node.live_props.get("node.nick")
                    or getattr(node, "device_name", "")
                    or getattr(node, "app_name", "")
                )
            result[node_id] = data
        return result

    def _serialize_edges(self) -> dict:
        result = {}
        for e in self.space.edges.values():
            logical = self._logical_edge(e)
            if logical is None:
                continue
            from_node, to_node, to_port, from_port = logical
            eid = PatchSpace._edge_id(from_node, to_node, to_port, from_port)
            result[eid] = {
                "id": eid,
                "from_node": from_node,
                "to_node": to_node,
                "to_port": to_port,
                "from_port": from_port,
                # Whether every pair this edge currently needs is a live
                # PipeWire link yet - see PatchSpace.edge_wired. Surfaced
                # so the GUI can show a node as still wiring instead of
                # looking indistinguishable from a fully-connected one
                # (the same idea as the existing "ready" flag on backed
                # nodes, just for the edges plugged into any node).
                "wired": self.space.edge_wired(e.id),
            }
        return result

    def _serialize_graph(self) -> dict:
        nodes = {}
        for node_id, node_data in self.graph.nodes().items():
            props = node_data.get("info", {}).get("props", {})
            nodes[node_id] = {
                "name": props.get("node.name"),
                "description": props.get("node.description") or props.get("node.nick"),
                "media_class": props.get("media.class"),
                "application_name": props.get("application.name"),
            }
        ports = {}
        for port_id, port_data in self.graph.ports().items():
            props = port_data.get("info", {}).get("props", {})
            ports[port_id] = {
                "node_id": props.get("node.id"),
                "name": props.get("port.name"),
                "direction": props.get("port.direction"),
                "channel": props.get("audio.channel"),
            }
        links = []
        for link_data in self.graph.links().values():
            info = link_data.get("info", {})
            links.append(
                {
                    "output_port": info.get("output-port-id"),
                    "input_port": info.get("input-port-id"),
                }
            )
        return {"nodes": nodes, "ports": ports, "links": links}

    def _try_immediate_resolve(self, node) -> None:
        """Resolve an already-present device/app immediately rather than
        waiting for a node_created event that will never come."""
        for live_id, live_data in self.graph.nodes().items():
            props = live_data.get("info", {}).get("props", {})
            if node.matches_live_node(props):
                node.resolve_live(live_id, props)
                return

    # ------------------------------------------------------------------
    # command dispatch
    # ------------------------------------------------------------------

    def handle_command(self, cmd: dict) -> dict:
        command = cmd.get("command")
        try:
            if command == "add_node":
                response = self._cmd_add_node(cmd)
            elif command == "remove_node":
                response = self._cmd_remove_node(cmd)
            elif command == "rename_node":
                response = self._cmd_rename_node(cmd)
            elif command == "add_edge":
                response = self._cmd_add_edge(cmd)
            elif command == "remove_edge":
                response = self._cmd_remove_edge(cmd)
            elif command == "set_gate":
                response = self._cmd_set_gate(cmd)
            elif command == "set_volume":
                response = self._cmd_set_volume(cmd)
            elif command == "set_volume_range":
                response = self._cmd_set_volume_range(cmd)
            elif command == "set_node_property":
                response = self._cmd_set_node_property(cmd)
            elif command == "set_node_layout":
                response = self._cmd_set_node_layout(cmd)
            elif command == "add_group":
                response = self._cmd_add_group(cmd)
            elif command == "set_group":
                response = self._cmd_set_group(cmd)
            elif command == "remove_group":
                response = self._cmd_remove_group(cmd)
            elif command == "set_device_volume":
                response = self._cmd_set_device_volume(cmd)
            elif command == "set_device_profile":
                response = self._cmd_set_device_profile(cmd)
            elif command == "get_hardware_devices":
                response = self._cmd_get_hardware_devices(cmd)
            elif command == "get_applications":
                response = self._cmd_get_applications(cmd)
            elif command == "get_device_profiles":
                response = self._cmd_get_device_profiles(cmd)
            elif command == "get_nodes":
                response = self._cmd_get_nodes(cmd)
            elif command == "get_graph":
                response = self._cmd_get_graph(cmd)
            elif command == "export_config":
                response = self._cmd_export_config(cmd)
            elif command == "load_session":
                response = self._cmd_load_session(cmd)
            elif command == "connect_ports":
                response = self._cmd_connect_ports(cmd)
            elif command == "disconnect_ports":
                response = self._cmd_disconnect_ports(cmd)
            elif command == "reset":
                response = self._cmd_reset(cmd)
            elif command == "ping":
                response = {"status": "ok", "message": "pong"}
            else:
                response = {"status": "error", "message": f"Unknown command: {command}"}
        except Exception as exc:
            logger.exception("Command %s failed", command)
            return {"status": "error", "message": str(exc)}
        return response

    # ------------------------------------------------------------------
    # socket server
    # ------------------------------------------------------------------

    def _socket_server(self) -> None:
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(5)
        os.chmod(SOCKET_PATH, 0o666)
        logger.info("Listening on %s", SOCKET_PATH)
        try:
            while self._running:
                try:
                    client_socket, _ = server.accept()
                    threading.Thread(
                        target=self._handle_client, args=(client_socket,), daemon=True
                    ).start()
                except Exception as exc:
                    if self._running:
                        logger.error("socket accept error: %s", exc)
        finally:
            server.close()
            if os.path.exists(SOCKET_PATH):
                os.unlink(SOCKET_PATH)

    def _handle_client(self, client_socket: socket.socket) -> None:
        self._clients.add(client_socket)
        buffer = ""
        try:
            while self._running:
                data = client_socket.recv(4096)
                if not data:
                    break
                buffer += data.decode("utf-8")
                while "\n" in buffer:
                    line, buffer = buffer.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        cmd = json.loads(line)
                        response = self.handle_command(cmd)
                    except json.JSONDecodeError as exc:
                        response = {
                            "status": "error",
                            "message": f"Invalid JSON: {exc}",
                        }
                    try:
                        client_socket.sendall(
                            (json.dumps(response) + "\n").encode("utf-8")
                        )
                    except OSError:
                        return
        except OSError:
            pass
        finally:
            self._clients.discard(client_socket)
            try:
                client_socket.close()
            except OSError:
                pass


def main():
    daemon = PatchBayDaemon()
    try:
        daemon.start()
    finally:
        daemon.stop()


if __name__ == "__main__":
    main()
