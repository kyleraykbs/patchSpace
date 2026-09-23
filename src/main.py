#!/usr/bin/env python3
"""
main.py - Patch Space daemon with a Unix-socket API.

Layout
------
  * a pwgraph.PipewireGraph keeps a live model of the PipeWire graph;
  * a pwnodes.PatchSpace owns the user's node graph, reconciles edges,
    and supervises every backed node (restarting anything that dies,
    with backoff for anything that keeps failing);
  * a single tick thread runs PatchSpace.supervise() every ~0.5s (and
    immediately when a mutating command nudges it);
  * a Unix socket (``$XDG_RUNTIME_DIR/patchspace.sock`` by default, so it
    lives somewhere the user owns - ``/tmp/patchspace.sock`` only as the
    fallback for a session with no runtime dir; ``--socket`` or
    ``$PATCHSPACE_SOCKET`` moves it) serves the JSON command API the GUI
    (gui/) and the CLI scripts speak.

The daemon's own built-in virtual sink ("Patch Space") and virtual mic
("Patch Space Mic") are ordinary hidden VirtualSpeaker/VirtualMic nodes
added to the PatchSpace - they get exactly the same supervision as
user-created devices, and their default-device status is captured
before they are created and restored on shutdown.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import threading
import time
from collections import deque
from typing import Any, Dict, Optional

import pwgraph
import pwmatch
import pwnodes
from pwgraph import PipewireGraph
from pwproc import Backoff, Ticker
from pwnodes import (
    PatchSpace,
    Node,
    BackedNode,
    LiveResolvableNode,
    BoolControlledMixin,
    GateNode,
    ABSwitchNode,
    SwitcherNode,
    InverseSwitcherNode,
    ExcludeFilterNode,
    BooleanSourceNode,
    BooleanSplitterNode,
    BooleanInvertNode,
    BooleanAndNode,
    BooleanOrNode,
    BooleanXorNode,
    WarpInNode,
    WarpOutNode,
    BooleanWarpInNode,
    BooleanWarpOutNode,
    PanelInNode,
    PanelOutNode,
    BoolPanelInNode,
    BoolPanelOutNode,
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
    PatchSpaceDeviceNode,
    PatchSpaceMicDeviceNode,
    RegexInputNode,
    RegexOutputNode,
    MediaClassInputNode,
    MediaClassOutputNode,
    AppClassifierNode,
    AppNameClassifierNode,
    TitleClassifierNode,
    DescriptionInputNode,
    DescriptionOutputNode,
    AllInputsNode,
    AllOutputsNode,
    AllAppsNode,
    ClassifierNode,
    RegexClassifierNode,
    MediaClassClassifierNode,
    DescriptionClassifierNode,
    ExternalOnlyClassifierNode,
    FilterNode,
    BundleToAudioNode,
    BundleMergeNode,
    BundleSplitNode,
    BundleOutputNode,
    SplitterNode,
    ClipNode,
    RecorderNode,
    SoundNode,
    SoundPlayerNode,
    ButtonNode,
    PATCHSPACE_VIRTUAL_SINK_NAME,
    PATCHSPACE_VIRTUAL_MIC_NAME,
    device_profile_name,
    pick_auto_a2dp_profile,
    _run_wpctl,
)

import session_repair
import panels

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

# Recent log output, kept in memory so the GUI's log console (get_logs)
# can display it without the daemon having to write a file.  Bounded so
# a chatty daemon can't grow it without limit.
_LOG_RING_MAX = 1000
_log_buffer: "deque[tuple[int, str]]" = deque(maxlen=_LOG_RING_MAX)
_log_seq = 0


class _LogRingHandler(logging.Handler):
    """Appends every record (from any logger that propagates to root) to
    the bounded in-memory ring the get_logs command serves."""

    def emit(self, record: logging.LogRecord) -> None:
        global _log_seq
        try:
            text = self.format(record)
        except Exception:
            return
        # logging calls emit under the handler's own lock, so this
        # counter can't interleave between records.
        _log_seq += 1
        _log_buffer.append((_log_seq, text))


_log_ring_installed = False


def _install_log_ring() -> None:
    """Attach the in-memory ring handler to the root logger exactly once,
    so every daemon entrypoint (main() or a direct start()) captures its
    log output for the GUI's get_logs console."""
    global _log_ring_installed
    if _log_ring_installed:
        return
    handler = _LogRingHandler()
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S")
    )
    logging.getLogger().addHandler(handler)
    _log_ring_installed = True

# The command-API socket.  Overridable so more than one daemon can exist on
# a machine (a second user, a test instance, or a service whose socket
# belongs in $XDG_RUNTIME_DIR) - otherwise the single-instance guard below
# makes the second one refuse to start.  Same env-var convention as the
# panel dirs/root panel below; `--socket` overrides it.
#
# The pre-rename `PATCHBAY_*` names are still read (below and in the two
# clients): a session that is already running started its daemon with them in
# its environment, and a user service written before the rename still sets
# them.  They are read, never written.
def _default_socket_path() -> str:
    """Where the daemon listens when nothing says otherwise.

    `$XDG_RUNTIME_DIR` (a per-user directory the user owns) rather than a
    fixed name in `/tmp`: /tmp is world-writable and sticky, so a socket
    there left behind by another user - a root-owned one is enough - cannot
    be unlinked by us, and the daemon used to carry on without a socket and
    sweep the live daemon's objects.  The /tmp name is only the fallback for
    a session with no runtime dir, and the GUI resolves this the same way
    (gui/constants.py) - a test asserts the two agree.
    """
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and os.path.isdir(runtime):
        return os.path.join(runtime, "patchspace.sock")
    return "/tmp/patchspace.sock"


SOCKET_PATH = (
    os.environ.get("PATCHSPACE_SOCKET")
    or os.environ.get("PATCHBAY_SOCKET")
    or _default_socket_path()
)
SESSION_CACHE_PATH = os.path.expanduser("~/.cache/patchspace/last_session.json")

# The GUI's canvas background opacity, when the deployment configures one (the
# module sets this on the *unit*, so a locally started daemon reads it from the
# environment like the socket and panel dirs - and it reaches a client that was
# started by a launcher with a stale session environment, which a session
# variable alone would not).  `None` (unset or unusable) means "no preference".
def _env_canvas_opacity():
    raw = os.environ.get("PATCHSPACE_CANVAS_OPACITY")
    if not raw:
        return None
    try:
        return max(0.0, min(1.0, float(raw)))
    except ValueError:
        logger.warning("Ignoring PATCHSPACE_CANVAS_OPACITY=%r: expected 0..1", raw)
        return None


CANVAS_OPACITY = _env_canvas_opacity()

# How often the tick rescans the panel directories for changes.
PANEL_POLL_S = 1.5

# The PipeWire graph monitor is a child process (`pw-dump -m`).  It exits by
# itself when PipeWire goes away - most often because PipeWire was restarted,
# or because this daemon was started before PipeWire was listening at all - so
# the death is reported to the supervisor (see _on_graph_monitor_error) which
# restarts it with backoff.  A monitor that dies again within
# MONITOR_RESTART_GRACE_S of a restart is treated as "the restart didn't
# stick" and backs off, rather than being respawned every tick.
MONITOR_RESTART_GRACE_S = 5.0

# Panels: each non-root panel is one file, referenced by stem.  The
# daemon takes a list of directories to search (later shadows earlier);
# a directory is writable unless it is a read-only source like a Nix
# store path.  The root panel is the session autosave file.
DEFAULT_PANEL_DIR = os.environ.get(
    "PATCHSPACE_PANEL_DIR",
    os.environ.get("PATCHBAY_PANEL_DIR")
    or os.path.expanduser("~/.local/share/patchspace/panels"),
)
DEFAULT_ROOT_PANEL = os.environ.get(
    "PATCHSPACE_ROOT_PANEL",
    os.environ.get("PATCHBAY_ROOT_PANEL") or SESSION_CACHE_PATH,
)

# Where this project kept its panels and its session before the rename
# (PatchBay -> Patch Space).  See _migrate_legacy_paths: the old contents are
# *copied* into the new locations, once, so a machine that has been running
# the old code finds its graph where it always was - and the old files are
# left alone rather than moved out from under a second checkout.
LEGACY_PANEL_DIR = os.path.expanduser("~/.local/share/patchbay/panels")
LEGACY_SESSION_CACHE = os.path.expanduser("~/.cache/patchbay/last_session.json")


def _copy_missing_files(src_dir: str, dst_dir: str) -> int:
    """Copy every panel file under `src_dir` that `dst_dir` doesn't have.

    Returns how many were copied.  Never overwrites and never deletes: the
    destination wins on a name clash (it is the canonical location)."""
    copied = 0
    for path in panels.list_files(src_dir):
        stem = panels.file_stem(path)
        target = os.path.join(dst_dir, stem + panels.PANEL_SUFFIX)
        if os.path.exists(target):
            continue
        try:
            os.makedirs(dst_dir, exist_ok=True)
            shutil.copy2(path, target)
            copied += 1
        except OSError as exc:
            logger.warning("Could not migrate panel %r: %s", path, exc)
    return copied



# How often the default-device "force" check runs (each check shells out
# to wpctl to read the live default, so it's throttled well below the
# tick rate).
DEFAULT_CHECK_INTERVAL_S = 2.0


SUPERVISE_INTERVAL_S = 0.5
RELOAD_DEBOUNCE_S = 0.35
DEVICE_OBJ_TYPE = "PipeWire:Interface:Device"

# How often to re-read the live default sink/source and put the built-in
# virtual devices back if something else claimed them.  Promotion used to
# be one-shot (only when the built-in's id first resolved), so a
# newly-plugged device or another app could silently take over the
# default source - apps then recorded a hardware source instead of the
# processed Mic Line, with everything looking healthy.  Throttled because
# each check shells out to wpctl.
DEFAULT_CHECK_INTERVAL_S = 2.0

# How long a staged session load will wait for a single backed/effect
# node to come all the way up (structural + module + every backing
# resolved in the live graph - see PatchSpaceDaemon._node_is_ready)
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
    ReverbNode,
)


NODE_TYPE_REGISTRY: Dict[str, type] = {
    "regex_input": RegexInputNode,
    "media_class_input": MediaClassInputNode,
    "description_input": DescriptionInputNode,
    "regex_output": RegexOutputNode,
    "media_class_output": MediaClassOutputNode,
    "description_output": DescriptionOutputNode,
    # Bundles: preset sources, classifiers, filters and terminals.
    "all_inputs": AllInputsNode,
    "all_outputs": AllOutputsNode,
    "all_apps": AllAppsNode,
    "regex_classifier": RegexClassifierNode,
    "media_class_classifier": MediaClassClassifierNode,
    "description_classifier": DescriptionClassifierNode,
    "title_classifier": TitleClassifierNode,
    "app_name_classifier": AppNameClassifierNode,
    "app_classifier": AppClassifierNode,
    "external_only_classifier": ExternalOnlyClassifierNode,
    "filter": FilterNode,
    "bundle": BundleMergeNode,
    "bundle_split": BundleSplitNode,
    "bundle_to_audio": BundleToAudioNode,
    "bundle_output": BundleOutputNode,
    "splitter": SplitterNode,
    "button": ButtonNode,
    "sound": SoundNode,
    "recorder": RecorderNode,
    "clip": ClipNode,
    "sound_player": SoundPlayerNode,
    "gate": GateNode,
    "switcher": SwitcherNode,
    "inverse_switcher": InverseSwitcherNode,
    "exclude_filter": ExcludeFilterNode,
    "boolean_switch": BooleanSourceNode,
    "boolean_splitter": BooleanSplitterNode,
    "boolean_invert": BooleanInvertNode,
    "boolean_and": BooleanAndNode,
    "boolean_or": BooleanOrNode,
    "boolean_xor": BooleanXorNode,
    "warp_in": WarpInNode,
    "warp_out": WarpOutNode,
    "bool_warp_in": BooleanWarpInNode,
    "bool_warp_out": BooleanWarpOutNode,
    "panel_in": PanelInNode,
    "panel_out": PanelOutNode,
    "bool_panel_in": BoolPanelInNode,
    "bool_panel_out": BoolPanelOutNode,
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
    "patchspace_device": PatchSpaceDeviceNode,
    "patchspace_mic_device": PatchSpaceMicDeviceNode,
    "virtual_speaker": VirtualSpeakerNode,
    "virtual_mic": VirtualMicNode,
}
CLASS_TO_TYPE = {cls: key for key, cls in NODE_TYPE_REGISTRY.items()}

# Node type keys this project used before the rename (PatchBay -> Patch
# Space).  Registered as aliases *after* CLASS_TO_TYPE so a panel or session
# written under the old key still loads (the node keeps working), while
# everything this daemon reports/serializes uses the canonical key - i.e. an
# old file is read, then re-exported in the new spelling.
for _legacy_key, _canonical_key in {
    "patchbay_device": "patchspace_device",
    "patchbay_mic_device": "patchspace_mic_device",
}.items():
    NODE_TYPE_REGISTRY[_legacy_key] = NODE_TYPE_REGISTRY[_canonical_key]

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
    "app_key",
    "start",
    "end",
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
    "lv2_uri",
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
    "makeup",
    "range_db",
    "limiter_release_s",
    "ladspa_dir",
    "warp_name",
    "port_name",
    "default_state",
    "plugin_uri",
    "decay_time",
    "room_size",
    "diffusion",
    "hf_damp",
    "predelay",
    "force_default",
    "invert",
    "title",
    "exclude",
    "declarative",
    "path",
    "overlap",
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


class PatchSpaceDaemon:
    def __init__(
        self,
        panel_dirs: Optional[List[tuple]] = None,
        root_panel_path: Optional[str] = None,
    ):
        self.graph = PipewireGraph(pw_cli_command=("pw-cli",))
        #: The listening socket (bound by `_bind_socket`, served by
        #: `_serve_clients`); None until then, and cleared on teardown.
        self._server: Optional[socket.socket] = None
        self.space = PatchSpace(self.graph)
        self._lock = self.space._lock

        self._running = False
        self._clients: set[socket.socket] = set()

        self._ticker: Optional[Ticker] = None
        self._reload_wake_timer: Optional[threading.Timer] = None
        self._dirty = False

        # Panels: first-class nested containers.  Each non-root panel is
        # one file; the root panel is the session autosave.  `panel_dirs`
        # is a list of (path, writable) searched in order (later wins).
        self.panel_dirs: List[tuple] = (
            list(panel_dirs) if panel_dirs else [(DEFAULT_PANEL_DIR, True)]
        )
        self.root_panel_path = root_panel_path or DEFAULT_ROOT_PANEL
        # panel_id -> Panel (includes the root under "").
        self.panels: Dict[str, panels.Panel] = {}
        # Frozen copy of each read-only panel's file state, re-applied on
        # reload/restart and on reset_panel.
        self._readonly_snapshots: Dict[str, panels.Panel] = {}
        # Last committed file state for *every* panel: parameter edits
        # outside a panel's edit mode are not written back (they're served
        # from this snapshot), and entering edit mode refreshes to it.
        self._panel_snapshots: Dict[str, panels.Panel] = {}
        # Panels currently in edit mode (parameter changes are persisted).
        self._edit_panels: set = set()
        self._panel_mtimes: Dict[str, float] = {}
        self._panel_poll_at = 0.0
        self._panel_reloading = False
        self._panel_lock = threading.Lock()

        # True while any heavy (re)build runs: the startup session load, a
        # panel reload, a rebuild, or a GUI-issued session load.  Surfaced
        # to the GUI via get_nodes so it can show its loading overlay - the
        # GUI has no command-side signal for loads it didn't issue.
        # A counter so overlapping loads don't clear the flag early.
        self._startup_loading = False
        self._loading_count = 0
        self._loading_lock = threading.Lock()

        # GUI-only node groups: id -> {id, label, color, nodes:[node_id]}.
        # Pure canvas annotations (like x/y/anchored) - they never touch
        # the audio graph, they just persist through export/import.
        self.groups: Dict[str, Dict[str, Any]] = {}

        # Builtin hidden virtual devices (see module docstring).
        self.builtin_sink: Optional[VirtualSpeakerNode] = None
        self.builtin_mic: Optional[VirtualMicNode] = None
        # Device nodes we've already warned about having no live audio
        # object, so the "device can't be routed" message isn't repeated
        # every tick (see _ensure_device_profiles).
        self._warned_unroutable: set = set()
        self._prev_default_sink_id: Optional[int] = None
        self._prev_default_source_id: Optional[int] = None
        self._set_default_sink_id: Optional[int] = None
        self._set_default_source_id: Optional[int] = None
        # Throttle for the force-default re-check (see _assert_defaults).
        self._default_check_at: float = 0.0

        self.graph.on_node_created(self._on_node_created)
        self.graph.on_node_removed(self._on_node_removed)
        self.graph.on_initial_sync(self._on_initial_sync)
        # Watch the monitor process itself.  Without this, a `pw-dump -m`
        # that exits (PipeWire restart, or started before PipeWire listened)
        # leaves the daemon holding a graph model that no longer exists -
        # and silently doing nothing about it.
        self.graph.on_error = self._on_graph_monitor_error
        self._graph_monitor_error: Optional[str] = None
        self._graph_monitor_restarted_at: float = 0.0
        self._graph_monitor_backoff = Backoff(initial_s=1.0, max_s=30.0)
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
        "patchspace_",
        "echo_cancel_node_",
        "light_noise_cancel_node_",
        "noise_cancel_node_",
        "reverb_node_",
        "normalize_node_",
        "volume_node_",
        "volume_mute_",
        "virtual_speaker_node_",
        "virtual_mic_node_",
        "splitter_",
    )

    def _startup_stale_markers(self) -> list:
        """Every name a previous patchspace run may have left live objects
        under: the built-in virtual devices, this project's known backing
        prefixes, and the backing_node_names recorded in the last-session
        cache (the exact names a re-import will reuse, so orphaned copies
        must not survive)."""
        markers = list(self._OWNED_PREFIXES) + [
            PATCHSPACE_VIRTUAL_SINK_NAME,
            PATCHSPACE_VIRTUAL_MIC_NAME,
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
        # owned_sweep also reaps project plumbing whose backing name is
        # not in the prefix list / session cache (an imported or old
        # config's ``fx``/``nc_a`` style backings), derived from the live
        # graph's reserved Internal media class and *_keepalive names.
        stale = self.graph.reap_stale_for_names(markers, owned_sweep=True)
        logger.info("Startup cleanup swept %d stale object(s)", stale)

    @staticmethod
    def _another_daemon_running(timeout: float = 0.5) -> bool:
        """Whether another daemon is already serving SOCKET_PATH.

        A successful AF_UNIX connect proves a live listener (the kernel
        refuses with ECONNREFUSED for a leftover socket file whose owner
        is gone), so a crashed run's stale socket does not block a fresh
        start."""
        if not os.path.exists(SOCKET_PATH):
            return False
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        probe.settimeout(timeout)
        try:
            probe.connect(SOCKET_PATH)
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def start(self) -> None:
        # Single-instance guard.  A second daemon would unlink the live
        # socket in _socket_server and bind its own, leaving the first
        # running headless while BOTH create their own copies of the
        # built-in devices and session backings - duplicate PipeWire
        # objects and a split graph.  Refuse to start if another daemon
        # is already answering on the socket (a stale socket file from a
        # crashed run is not a listener, so that still starts normally).
        if self._another_daemon_running():
            logger.error(
                "Another Patch Space daemon is already listening on %s - "
                "refusing to start a second instance",
                SOCKET_PATH,
            )
            return

        self._running = True

        # Capture our own log output for the GUI's get_logs console
        # regardless of how the daemon was launched.
        _install_log_ring()

        # Mark the whole start-up sequence as a heavy load *before* binding
        # the socket, so the GUI can raise its loading overlay while the
        # crash-recovery sweep destroys orphaned helper processes and the
        # saved session comes back up.  The socket is bound first (below)
        # precisely so get_nodes can report this to the GUI.
        self._begin_heavy_load()

        # Bind the socket immediately so the GUI connects (and sees
        # loading=true) while the slow work below runs.  The overlay blocks
        # canvas interaction until the load finishes.
        #
        # Binding is synchronous and fatal on purpose.  It used to happen on
        # the server thread, so a socket that could not be replaced (a
        # root-owned leftover in sticky /tmp, say) killed only that thread:
        # the daemon carried on headless - while its start-up sweep tore
        # down the live daemon's objects, and both instances rebuilt the
        # user's nodes.  A daemon nobody can talk to must not run.
        try:
            self._bind_socket()
        except OSError as exc:
            logger.error(
                "Cannot serve %s (%s).  If that path is a leftover socket "
                "owned by someone else, remove it or point PATCHSPACE_SOCKET "
                "at a path you own; refusing to start rather than running "
                "without a socket.",
                SOCKET_PATH,
                exc,
            )
            self._running = False
            return
        socket_thread = threading.Thread(target=self._serve_clients, daemon=True)
        socket_thread.start()

        # Clear anything a previous (uncleanly-killed) run left behind
        # before we request fresh objects.  This is the slow part when a
        # crashed run left a pile of orphaned pw-cli/pw-cat helpers - and it
        # raises AnotherDaemonRunning (refusing to touch them) when the
        # objects belong to a daemon that is still alive.
        try:
            self._cleanup_stale_objects()
        except pwgraph.AnotherDaemonRunning as exc:
            logger.error("%s - refusing to start a second instance", exc)
            self._close_socket()
            self._running = False
            return

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

        self.started = True

        # Snapshot the panel files up front so the tick's change watcher
        # doesn't fire a reload on top of the startup load (which refreshes
        # this snapshot again when it finishes).
        self._panel_mtimes = panels.snapshot(self._panel_dir_paths())

        # Load the saved session on a background thread.  Bringing up a
        # session's backed effect nodes is slow (up to
        # SESSION_LOAD_NODE_TIMEOUT_S each, sequentially); the GUI's
        # periodic get_nodes poll picks them up as they land.  This thread
        # owns the end of the start-up heavy-load counter opened above.
        threading.Thread(target=self._startup_load_thread, daemon=True).start()

        logger.info("Patch Space daemon running. Press Ctrl+C to exit.")
        logger.info(f"Connect via: socat - UNIX-CONNECT:{SOCKET_PATH}")
        try:
            while self._running:
                time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("\nShutting down...")
        finally:
            self._running = False

    #: True once `start` got past the single-instance probe, bound its
    #: socket and claimed the graph - i.e. the daemon is actually serving.
    #: `start` returning without this means it refused (see `main`).
    started: bool = False

    def stop(self) -> None:
        self._running = False
        # Make sure a start-up that aborted part-way can't leave the
        # loading flag stuck on for any lingering get_nodes poll.
        with self._loading_lock:
            self._loading_count = 0
            self._startup_loading = False
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
                "__patchspace_builtin_sink__",
                PATCHSPACE_VIRTUAL_SINK_NAME,
                device_label="Patch Space Virtual Sink",
            )
            mic = VirtualMicNode(
                "__patchspace_builtin_mic__",
                PATCHSPACE_VIRTUAL_MIC_NAME,
                device_label="Patch Space Mic",
            )
            sink.force_default = True
            mic.force_default = True
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
                if b.name == PATCHSPACE_VIRTUAL_MIC_NAME:
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
        if isinstance(node, PatchSpaceDeviceNode):
            return self.builtin_sink
        if isinstance(node, PatchSpaceMicDeviceNode):
            return self.builtin_mic
        return None

    def _line_nodes_for(self, target) -> list:
        if target is self.builtin_sink:
            cls = PatchSpaceDeviceNode
        elif target is self.builtin_mic:
            cls = PatchSpaceMicDeviceNode
        else:
            return []
        return [n for n in self.space.nodes.values() if isinstance(n, cls)]

    def _mirror_line_volume(self, target) -> None:
        if target is None:
            return
        for line in self._line_nodes_for(target):
            line.device_volume = target.device_volume
            line.volume_locked = target.volume_locked
            line.force_default = getattr(target, "force_default", True)

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
        if "force_default" in config:
            target.force_default = bool(config["force_default"])
        if "device_volume" in config:
            try:
                target.device_volume = max(
                    0.0, min(1.0, float(config["device_volume"]))
                )
            except (TypeError, ValueError):
                pass
        self._mirror_line_volume(target)

    def _apply_node_config(self, node, config: dict) -> None:
        """Standard "re-adopt an existing node from a config" fixups.

        Shared by ``_cmd_add_node`` and ``_load_session`` so a replayed
        config behaves identically however it arrives.  Applies params
        (guarding the per-type needs: ``level`` must go through
        ``set_level`` so the clamp + live re-push happen, volumes through
        their setters), seeds the shared line volume, and re-applies
        device settings."""
        for key, value in config.items():
            if key == "level" and isinstance(node, SensitivityGateNode):
                continue
            if hasattr(node, key):
                setattr(node, key, value)
        self._adopt_line_volume(node, config)
        if isinstance(node, VolumeProcessNode):
            if "initial_volume" in config:
                node.set_volume(config["initial_volume"])
            if "volume_min" in config or "volume_max" in config:
                node.set_volume_range(
                    getattr(node, "volume_min", 0.0),
                    getattr(node, "volume_max", 1.0),
                )
        if isinstance(node, SensitivityGateNode):
            new_level = config.get("level")
            if new_level is not None and new_level != getattr(node, "level", None):
                node.set_level(new_level)
            else:
                node.refresh_live()
        if hasattr(node, "apply_device_settings"):
            node.apply_device_settings()

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

    def _ensure_device_profiles(self) -> None:
        """Keep Bluetooth device profiles usable, and warn when a
        configured hardware device can't appear in the graph at all.

        A connected BlueZ device whose profile is ``off`` exports no
        Audio/Sink or Audio/Source node, so every edge to/from it can
        never wire and any stream targeting it dies with "Buffer
        allocation failed" (measured live with a TOZO headset).  When the
        node didn't pin a profile, pick the best available A2DP one and
        apply it - and keep re-asserting via the node's normal
        apply_device_settings() so a reconnect that resets the profile is
        corrected.  When no profile can be applied, say so once per node,
        loudly, instead of letting it look like a routing bug."""
        devices: Dict[Any, dict] = {}
        for obj in self.graph.all_objects().values():
            if obj.get("type") == "PipeWire:Interface:Device":
                devices[obj.get("id")] = obj

        now = time.monotonic()
        for node in list(self.space.nodes.values()):
            if not isinstance(node, (DeviceInputNode, DeviceOutputNode)):
                continue
            name = getattr(node, "device_name", "")
            if not name:
                continue
            device_id = getattr(node, "live_props", {}).get("device.id")
            device_obj = devices.get(device_id) if device_id is not None else None
            profile = device_profile_name(device_obj)
            resolved = getattr(node, "live_node_id", None) is not None

            if resolved and profile != "off":
                self._warned_unroutable.discard(node.id)
                continue

            # Unroutable: the profile is off (or the device has no live
            # node at all).  If the profile is off, get it back on - with
            # the pinned profile when one is set, otherwise by picking the
            # best available A2DP profile.
            if profile == "off" and now >= getattr(node, "_auto_profile_at", 0.0):
                node._auto_profile_at = now + 5.0
                index = node.profile_index
                pname = node.profile_description
                if index is None:
                    picked = pick_auto_a2dp_profile(
                        device_obj, isinstance(node, DeviceOutputNode)
                    )
                    if picked is not None:
                        index, pname = picked
                        logger.warning(
                            "Bluetooth device %r has no active profile - "
                            "applying %s", name, pname,
                        )
                elif index is not None:
                    logger.info(
                        "Re-applying pinned profile %s on Bluetooth device %r"
                        " (it was off)",
                        pname or index, name,
                    )
                if index is not None and _run_wpctl(
                    "set-profile", device_id, index
                ):
                    # Pin auto-picks so the normal tick keeps re-asserting
                    # them and a saved session remembers the choice.
                    node.profile_index = index
                    if pname:
                        node.profile_description = pname
                    node._applied_profile = (device_id, index)
                    continue

            if node.id not in self._warned_unroutable:
                self._warned_unroutable.add(node.id)
                logger.warning(
                    "Hardware device %r has no live audio node%s - edges "
                    "to/from it can't be wired until it is available",
                    name,
                    " (Bluetooth profile is off)" if profile == "off" else "",
                )

    def _assert_defaults(self) -> None:
        """Promote the builtins to system default output/input.

        Two modes, per builtin (the Speaker/Mic Line node's "force"
        button):

        * **force on** (default): *constantly* re-check the live default
          and put it back on Patch Space if something moved it - throttled by
          ``DEFAULT_CHECK_INTERVAL_S`` so we're not spawning ``wpctl`` on
          every tick.
        * **force off**: the old once-per-resolved-id behaviour - promote
          a newly-created builtin, then leave the user's choice alone.

        The id is only remembered after ``wpctl set-default`` actually
        *succeeds*: at start-up the built-in sink can be resolved a tick
        or two before WirePlumber will accept it as default (or before
        ``wpctl`` can see it), and recording the id up front on a failed
        attempt meant the promotion was never retried."""
        now = time.monotonic()
        if now < self._default_check_at:
            return
        self._default_check_at = now + DEFAULT_CHECK_INTERVAL_S

        sink_id, mic_id = self._builtin_resolved_ids()
        if sink_id is not None:
            force = getattr(self.builtin_sink, "force_default", True)
            if force:
                if self._read_default_id("@DEFAULT_AUDIO_SINK@") != sink_id:
                    if self._set_default("@DEFAULT_AUDIO_SINK@", sink_id):
                        self._set_default_sink_id = sink_id
            elif sink_id != self._set_default_sink_id:
                if self._set_default("@DEFAULT_AUDIO_SINK@", sink_id):
                    self._set_default_sink_id = sink_id
                else:
                    self._set_default_sink_id = None
        if mic_id is not None:
            force = getattr(self.builtin_mic, "force_default", True)
            if force:
                if self._read_default_id("@DEFAULT_AUDIO_SOURCE@") != mic_id:
                    if self._set_default("@DEFAULT_AUDIO_SOURCE@", mic_id):
                        self._set_default_source_id = mic_id
            elif mic_id != self._set_default_source_id:
                if self._set_default("@DEFAULT_AUDIO_SOURCE@", mic_id):
                    self._set_default_source_id = mic_id
                else:
                    self._set_default_source_id = None

    @staticmethod
    def _set_default(token: str, node_id: int) -> bool:
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
                return True
            logger.warning(
                "wpctl set-default %s failed: %s",
                node_id,
                (result.stderr or result.stdout).strip(),
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("wpctl set-default %s failed: %s", node_id, exc)
        return False

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

    def _on_graph_monitor_error(self, exc: Exception) -> None:
        """The monitor process died (its reader thread reports it here).

        Called from that thread, so this only records the fact and wakes the
        supervisor; restarting is the tick's job.  A death shortly after a
        restart means the restart didn't stick (PipeWire still not there), so
        it counts as a failure and the next attempt backs off.
        """
        message = f"{type(exc).__name__}: {exc}"
        fresh = self._graph_monitor_error is None
        if fresh:
            logger.warning(
                "PipeWire monitor died (%s); the supervisor will restart it",
                message,
            )
        self._graph_monitor_error = message
        if (
            self._graph_monitor_restarted_at
            and time.monotonic() - self._graph_monitor_restarted_at
            < MONITOR_RESTART_GRACE_S
        ):
            self._graph_monitor_backoff.record_failure("monitor")
        else:
            self._graph_monitor_backoff.record_success("monitor")
        self._wake_ticker()

    def _restart_graph_monitor(self) -> None:
        """Restart a dead monitor, at most once per backoff interval.

        A fresh monitor sends a full dump, which fires the initial-sync
        callbacks and repopulates the graph model; the normal supervision
        pass then recreates whatever vanished with the old PipeWire.
        """
        if self._graph_monitor_error is None or not self._graph_monitor_backoff.ready(
            "monitor"
        ):
            return
        try:
            self.graph.stop()
            self.graph.start()
        except Exception as exc:
            self._graph_monitor_backoff.record_failure("monitor")
            logger.warning("Could not restart the PipeWire monitor: %s", exc)
            return
        self._graph_monitor_restarted_at = time.monotonic()
        self._graph_monitor_error = None
        logger.info("PipeWire monitor restarted; re-syncing the graph model")

    def _tick(self) -> None:
        if not self._running:
            return
        try:
            self._restart_graph_monitor()
            self.space.supervise()
            self._assert_defaults()
            self._enforce_volume_locks()
            self._ensure_device_profiles()
        except Exception:
            logger.exception("supervision tick failed")
        # Never autosave while a declarative reload is mid-flight: it
        # briefly removes all declarative nodes, so an imperative export
        # taken then would drop every edge that crosses into a declarative
        # node (declarative -> hardware output, say) and, if the daemon is
        # killed before the next save, that loss is permanent.  Keep
        # `_dirty` set and save once the reload has finished.
        if self._dirty and not self._panel_reloading:
            self._dirty = False
            self._auto_export_session()
        self._poll_panels()

    # ------------------------------------------------------------------
    # declarative node files
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # panels
    # ------------------------------------------------------------------

    def _panel_dir_paths(self) -> List[str]:
        seen: List[str] = []
        for path, _writable in self.panel_dirs:
            if path and path not in seen:
                seen.append(path)
        return seen

    def _panel_dir_writable(self) -> Dict[str, bool]:
        out: Dict[str, bool] = {}
        for path, writable in self.panel_dirs:
            if path:
                out[path] = writable  # later dirs win
        return out

    def _migrate_legacy_paths(self) -> None:
        """Adopt whatever the pre-rename paths (PatchBay) still hold: the
        panel directory and the session cache.

        Copied, not moved, and only into a location that is empty or missing
        the file - so it is idempotent, it can never clobber a panel the
        user made under the new name, and the old files stay put (a second
        checkout or an old daemon may still be using them)."""
        target_dir = next(
            (d for d, w in self.panel_dirs if d and w), None
        )
        if (
            target_dir
            and os.path.realpath(target_dir) != os.path.realpath(LEGACY_PANEL_DIR)
            and os.path.isdir(LEGACY_PANEL_DIR)
        ):
            copied = _copy_missing_files(LEGACY_PANEL_DIR, target_dir)
            if copied:
                logger.info(
                    "Migrated %d panel file(s) from %s to %s",
                    copied, LEGACY_PANEL_DIR, target_dir,
                )
        if (
            self.root_panel_path == SESSION_CACHE_PATH
            and not os.path.exists(SESSION_CACHE_PATH)
            and os.path.exists(LEGACY_SESSION_CACHE)
        ):
            try:
                os.makedirs(os.path.dirname(SESSION_CACHE_PATH), exist_ok=True)
                shutil.copy2(LEGACY_SESSION_CACHE, SESSION_CACHE_PATH)
                logger.info(
                    "Migrated the session cache %s -> %s",
                    LEGACY_SESSION_CACHE, SESSION_CACHE_PATH,
                )
            except OSError as exc:
                logger.warning("Could not migrate the session cache: %s", exc)

    def _load_panels_tree(self) -> Dict[str, panels.Panel]:
        """Load the root panel + every reachable panel, migrating the
        legacy shapes in place (a bare session cache, and any old
        declarative files sitting in the panel dirs)."""
        self._migrate_legacy_paths()
        root_path = self.root_panel_path
        dirs = self._panel_dir_paths()
        raw = panels.read_file(root_path) if root_path else None
        if raw is not None and raw.get("type") != panels.TYPE_PANEL:
            logger.info("Migrating legacy session cache %r to a root panel", root_path)
            panels.write_file(root_path, panels.migrate_legacy_session(raw))

        tree = panels.load_tree(root_path, dirs, self._panel_dir_writable())

        # Migrate old declarative files that aren't referenced yet: make
        # each a top-level panel and move its namespaced nodes out of the
        # root config (the file is now the source of truth).
        root = tree.get(panels.ROOT_ID)
        if root is not None:
            referenced = set(root.child_stems().values())
            adopted = []
            for directory in dirs:
                for path in panels.list_files(directory):
                    stem = panels.file_stem(path)
                    if stem in referenced:
                        continue
                    raw = panels.read_file(path)
                    # Only files that opt in are spawned at the root;
                    # everything else comes up only when referenced as a
                    # sub-panel (nested panels always auto-load).
                    if not (isinstance(raw, dict) and raw.get("auto_load")):
                        continue
                    referenced.add(stem)
                    adopted.append(stem)
            if adopted:
                # Add the references to a *freshly read* root and write that,
                # so the file on disk is "what we just loaded, plus these
                # children".  Adding them to the tree loaded above and then
                # writing a reload of the file discarded both halves - the
                # added references lived only in the in-memory root, and the
                # reload (which cannot see them: they were never written) is
                # what got written back - so an auto_load panel in a
                # read-only directory was never actually adopted.
                tree = panels.load_tree(root_path, dirs, self._panel_dir_writable())
                fresh = tree[panels.ROOT_ID]
                for stem in adopted:
                    fresh.config.setdefault("panels", []).append(
                        panels.child_ref(stem, stem)
                    )
                # Drop root nodes that now belong to a migrated panel.
                fresh.config["nodes"] = {
                    nid: cfg
                    for nid, cfg in (fresh.config.get("nodes") or {}).items()
                    if panels.panel_of(nid) == panels.ROOT_ID
                }
                panels.write_file(root_path, fresh)
                tree = panels.load_tree(root_path, dirs, self._panel_dir_writable())
        self._place_unplaced_panels(tree)
        return tree

    def _place_unplaced_panels(self, tree: Dict[str, panels.Panel]) -> None:
        """Drop a top-level panel that has no placement at all *beside* the
        panels that do, instead of leaving it at the origin.

        This is what makes a declarative panel appear next to the graph the
        user has arranged rather than in its own empty corner of the canvas: a
        Nix-generated panel carries no coordinates (the module's placement
        defaults are unset), so it is placed to the right of the placed
        panels, top-aligned with the topmost one.  The placement is marked on
        the panel, which is a session value - so it is remembered, not
        recomputed on every start, and a panel the user then drags simply
        stops being unplaced.

        Nested panels keep their parent's frame and are left alone, and a
        graph with nothing placed yet is left alone too (the next start
        places it, once there is something to sit beside)."""
        root = tree.get(panels.ROOT_ID)
        if root is None:
            return
        placed = [
            panel for pid, panel in tree.items()
            if pid != panels.ROOT_ID
            and panel.placed
            and (panel.parent or panels.ROOT_ID) == panels.ROOT_ID
        ]
        if not placed:
            return
        right = max(panel.x + panel.w for panel in placed)
        top = min(panel.y for panel in placed)
        for pid, panel in tree.items():
            if pid == panels.ROOT_ID or panel.placed:
                continue
            if (panel.parent or panels.ROOT_ID) != panels.ROOT_ID:
                continue
            panel.x, panel.y = right + panels.PLACE_GAP, top
            panel.placed = True
            logger.info(
                "Placed unpositioned panel %r at (%.0f, %.0f), beside the "
                "panels already on the graph", pid, panel.x, panel.y,
            )
            right = panel.x + panel.w
            self._dirty = True

    @staticmethod
    def _panel_origin(tree: Dict[str, panels.Panel], panel_id: str) -> tuple:
        """Absolute (x, y) of a panel's top-left, by folding ancestors.

        Node runtime coordinates are absolute, so serialization converts
        to panel-relative by subtracting this and loading adds it back."""
        x = y = 0.0
        pid = panel_id
        guard = 0
        while pid and pid in tree and guard < 64:
            panel = tree[pid]
            x += panel.x
            y += panel.y
            pid = panel.parent or ""
            guard += 1
        return x, y

    def _flatten_panels(self, tree: Dict[str, panels.Panel],
                        only_panels: Optional[set] = None) -> dict:
        """Panels -> one load-ready config with fully-qualified node ids
        and absolute node coordinates.  ``only_panels`` restricts the
        emitted nodes/edges/groups (the whole ``tree`` is still used for
        origin folding)."""
        nodes: Dict[str, dict] = {}
        edges: List[dict] = []
        groups: List[dict] = []
        for pid, panel in tree.items():
            if only_panels is not None and pid not in only_panels:
                continue
            ox, oy = self._panel_origin(tree, pid)
            for local, cfg in panel.nodes.items():
                qid = panels.make_id(pid, local)
                params = dict(cfg.get("params") or {})
                if params.get("x") is not None:
                    params["x"] = float(params["x"]) + ox
                if params.get("y") is not None:
                    params["y"] = float(params["y"]) + oy
                params["declarative"] = pid != panels.ROOT_ID
                nodes[qid] = {"type": cfg.get("type"), "params": params}
            for e in panel.edges:
                src, dst = e.get("from"), e.get("to")
                if not src or not dst:
                    continue
                entry = {
                    "from": panels.make_id(pid, src),
                    "to": panels.make_id(pid, dst),
                    "declarative": pid != panels.ROOT_ID,
                }
                if e.get("to_port"):
                    entry["to_port"] = e["to_port"]
                if e.get("from_port"):
                    entry["from_port"] = e["from_port"]
                edges.append(entry)
            for g in panel.groups:
                gid = g.get("id")
                if not gid:
                    continue
                groups.append(
                    {
                        "id": panels.make_id(pid, gid),
                        "label": g.get("label", "Group"),
                        "color": g.get("color"),
                        "declarative": pid != panels.ROOT_ID,
                        "nodes": [
                            panels.make_id(pid, m) for m in g.get("nodes") or []
                        ],
                    }
                )
        return {"nodes": nodes, "edges": edges, "groups": groups}

    def _install_panels(self, tree: Dict[str, panels.Panel]) -> None:
        """Adopt a loaded panel tree, freeze the read-only panels'
        snapshots and the committed file state used for edit-mode
        gating."""
        self.panels = tree
        self._readonly_snapshots = {
            pid: p for pid, p in tree.items() if p.is_readonly
        }
        self._panel_snapshots = dict(tree)
        self._edit_panels &= set(tree)

    def _load_new_placement(self, root_id: str) -> None:
        """Bring up just one newly-added placement subtree, without tearing
        down the rest of the graph (unlike a full reload)."""
        tree = self._load_panels_tree()
        self._install_panels(tree)
        new_ids = {
            pid for pid in tree
            if pid == root_id or pid.startswith(root_id + panels.NAMESPACE_SEP)
        }
        if not new_ids:
            return
        config = self._flatten_panels(tree, only_panels=new_ids)
        # Incremental placement of a live file's sibling: load it in the
        # file's own schema (no migration) so it matches the placements
        # already in the graph; a full session load migrates them all.
        self._load_session(config, declarative=True, migrate=False)
        self._dirty = True
        self._wake_ticker()

    def _startup_load_panels(self) -> None:
        tree = self._load_panels_tree()
        self._install_panels(tree)
        config = self._flatten_panels(tree)
        if not config["nodes"]:
            self._panel_mtimes = panels.snapshot(self._panel_dir_paths())
            return
        logger.info(
            "Auto-loading %d panel(s), %d node(s)",
            max(0, len(tree) - 1), len(config["nodes"]),
        )
        # Full load from disk: bring any legacy node shapes forward.
        self._load_session(config, declarative=False, migrate=True)
        self._panel_mtimes = panels.snapshot(self._panel_dir_paths())
        self._dirty = True

    def _build_panels_from_space(self, imperative_only: bool = False,
                                 use_snapshots: bool = True,
                                 freeze_params: bool = True
                                 ) -> Dict[str, panels.Panel]:
        """Serialize the live graph into a panel tree.  Read-only panels
        keep their frozen snapshot (runtime edits are not persisted);
        read-write panels and the root are rebuilt from live state.
        ``use_snapshots=False`` rebuilds read-only panels from live state
        too.  ``freeze_params=False`` also disables the edit-mode parameter
        freezing (used by copy/save-as-json and clone, which want the
        current live state)."""
        out: Dict[str, panels.Panel] = {}

        def ensure(pid: str) -> panels.Panel:
            if pid in out:
                return out[pid]
            meta = self.panels.get(pid)
            if meta is not None:
                panel = panels.Panel(
                    id=pid, parent=meta.parent, label=meta.label, color=meta.color,
                    mode=meta.mode, x=meta.x, y=meta.y, w=meta.w, h=meta.h,
                    anchored=meta.anchored, path=meta.path, writable=meta.writable,
                    stem=meta.stem, auto_load=meta.auto_load,
                    config={"nodes": {}, "edges": [],
                            "panels": list(meta.child_entries), "groups": []},
                )
            else:
                panel = panels.Panel(
                    id=pid, parent=panels.parent_panel(pid),
                    label=panels.local_of(pid) if pid else "root",
                    color=panels.DEFAULT_COLOR, mode=panels.MODE_RW,
                    config={"nodes": {}, "edges": [], "panels": [], "groups": []},
                )
            # Read-only panels are not rebuilt from live state (unless the
            # caller explicitly asks for the current state).
            if use_snapshots and panel.is_readonly and pid in self._readonly_snapshots:
                out[pid] = self._readonly_snapshots[pid]
                return out[pid]
            out[pid] = panel
            return panel

        ensure(panels.ROOT_ID)
        # Preserve existing panels even if empty.
        for pid in self.panels:
            ensure(pid)

        with self._lock:
            for nid, node in self.space.nodes.items():
                if nid not in self.space.public_nodes:
                    continue
                if imperative_only and getattr(node, "declarative", False):
                    continue
                pid = panels.panel_of(nid)
                panel = ensure(pid)
                entry = self._export_node_params(nid, node)
                inner = entry["params"]
                ox, oy = self._panel_origin(out, pid)
                if inner.get("x") is not None:
                    inner["x"] = float(inner["x"]) - ox
                if inner.get("y") is not None:
                    inner["y"] = float(inner["y"]) - oy
                # Outside a panel's edit mode, write the file's committed
                # parameter values (only layout stays live), so runtime
                # knob/switch tweaks aren't persisted until you edit.
                if (
                    freeze_params
                    and pid not in self._edit_panels
                    and pid != panels.ROOT_ID
                ):
                    snap = self._panel_snapshots.get(pid)
                    snap_node = snap.nodes.get(panels.local_of(nid)) if snap else None
                    if snap_node:
                        frozen = dict(snap_node.get("params") or {})
                        for key in ("x", "y", "anchored"):
                            if key in inner:
                                frozen[key] = inner[key]
                        entry["params"] = frozen
                panel.config["nodes"][panels.local_of(nid)] = entry
            for edge in self.space.edges.values():
                logical = self._logical_edge(edge)
                if logical is None:
                    continue
                if imperative_only and getattr(edge, "declarative", False):
                    continue
                f, t, tp, fp = logical
                owner = panels.edge_owner(f, t)
                panel = ensure(owner)
                entry = {
                    "from": panels.relative_id(owner, f),
                    "to": panels.relative_id(owner, t),
                }
                if tp != "in":
                    entry["to_port"] = tp
                if fp != "out":
                    entry["from_port"] = fp
                panel.config["edges"].append(entry)
            for gid, g in self.groups.items():
                if imperative_only and g.get("declarative"):
                    continue
                members = list(g.get("nodes") or [])
                if not members:
                    continue
                owner = panels.panel_of(members[0])
                for m in members[1:]:
                    owner = panels.lca(owner, panels.panel_of(m))
                panel = ensure(owner)
                panel.config["groups"].append(
                    {
                        "id": panels.relative_id(owner, gid),
                        "label": g.get("label", "Group"),
                        "color": g.get("color"),
                        "nodes": [panels.relative_id(owner, m) for m in members],
                    }
                )
        # Each placement's geometry lives in its *parent's* child reference
        # (the panel file is shared), so rewrite the refs from the child
        # panels we just built.
        for pid, panel in out.items():
            if not panel.config.get("panels"):
                continue
            refs = []
            for entry in panel.child_entries:
                name = panels.child_name(entry)
                stem = panels.child_stem(entry)
                child = out.get(panels.make_id(pid, name))
                if child is not None:
                    refs.append(
                        panels.child_ref(name, stem, child.placement())
                    )
                else:
                    refs.append(entry)
            panel.config["panels"] = refs
        return out

    def _write_panels(self) -> None:
        """Write the root panel and every writable read-write panel."""
        tree = self._build_panels_from_space()
        canon = self._canonical_by_stem()
        root = tree.get(panels.ROOT_ID)
        if root is not None:
            panels.write_file(self.root_panel_path, root)
        written = set()
        for pid, panel in tree.items():
            if pid == panels.ROOT_ID:
                continue
            if not panel.writable or panel.is_readonly or not panel.path:
                continue
            # Placements of the same file share it; write it once, from the
            # placement that actually changed.
            if panel.path in written:
                continue
            written.add(panel.path)
            write_panel = tree.get(canon.get(panel.stem or "", pid), panel)
            panels.write_file(panel.path, write_panel)
        # A panel in edit mode (or one whose change was just written as the
        # canonical placement) has committed; make that the frozen state.
        for pid in list(self._edit_panels):
            if pid in tree:
                self._panel_snapshots[pid] = tree[pid]
        for pid in canon.values():
            if pid in tree:
                self._panel_snapshots[pid] = tree[pid]
        self._sync_placements(canon)
        self._panel_mtimes = panels.snapshot(self._panel_dir_paths())

    def _placement_differs(self, snap, live, include_params, positions=True):
        """Whether ``live`` differs from committed ``snap`` in a way that
        should sync to the file's other placements.  ``positions=False``
        ignores node x/y, leaving only structural/parameter differences
        (which are the ones that need a sibling rebuild)."""
        if set(snap.nodes) != set(live.nodes):
            return True
        if snap.children != live.children:
            return True
        snap_edges = {
            (e.get("from"), e.get("to"), e.get("to_port", "in"),
             e.get("from_port", "out"))
            for e in snap.edges
        }
        live_edges = {
            (e.get("from"), e.get("to"), e.get("to_port", "in"),
             e.get("from_port", "out"))
            for e in live.edges
        }
        if snap_edges != live_edges:
            return True
        if [g.get("id") for g in snap.groups] != [
            g.get("id") for g in live.groups
        ]:
            return True
        for local, cfg in live.nodes.items():
            other = snap.nodes.get(local)
            if other is None:
                return True
            lp = cfg.get("params") or {}
            sp = other.get("params") or {}
            if positions:
                for key in ("x", "y"):
                    lv, sv = lp.get(key), sp.get(key)
                    if lv is not None and sv is not None and abs(lv - sv) > 1e-6:
                        return True
            if include_params:
                for key in set(lp) | set(sp):
                    if key in ("x", "y", "anchored"):
                        continue
                    if lp.get(key) != sp.get(key):
                        return True
        return False

    def _plan_placement_groups(self):
        groups: Dict[str, list] = {}
        for pid, p in self.panels.items():
            if pid == panels.ROOT_ID or not p.stem:
                continue
            groups.setdefault(p.stem, []).append(pid)
        return groups

    def _canonical_by_stem(self) -> Dict[str, str]:
        """{stem: placement id} for stems with several placements, picking
        the placement whose live state diverged from its snapshot (the one
        that changed); empty when nothing needs syncing."""
        groups = self._plan_placement_groups()
        if not any(len(v) > 1 for v in groups.values()):
            return {}
        live_all = self._build_panels_from_space(
            use_snapshots=False, freeze_params=False
        )
        canon: Dict[str, str] = {}
        for stem, pids in groups.items():
            if len(pids) < 2:
                continue
            for pid in pids:
                snap = self._panel_snapshots.get(pid)
                live = live_all.get(pid)
                if snap is not None and live is not None and self._placement_differs(
                    snap, live, pid in self._edit_panels
                ):
                    canon[stem] = pid
                    break
        return canon

    def _sync_placements(self, canon: Optional[Dict[str, str]] = None) -> None:
        """Fan a changed placement out to the file's other placements:
        positions are copied live (cheap), structural/parameter changes
        revert the siblings to the changed placement's config."""
        if canon is None:
            canon = self._canonical_by_stem()
        if not canon:
            return
        groups = self._plan_placement_groups()
        live_all = self._build_panels_from_space(
            use_snapshots=False, freeze_params=False
        )
        for stem, pid in canon.items():
            source = live_all.get(pid)
            if source is None:
                continue
            off_src = self._panel_origin(self.panels, pid)
            for other in groups.get(stem, []):
                if other == pid:
                    continue
                # Cheap position sync.
                off_other = self._panel_origin(self.panels, other)
                for local, cfg in source.nodes.items():
                    node = self.space.nodes.get(panels.make_id(other, local))
                    params = cfg.get("params") or {}
                    if node is None or params.get("x") is None:
                        continue
                    node.x = off_other[0] + float(params["x"])
                    node.y = off_other[1] + float(params["y"])
                snap = self._panel_snapshots.get(other)
                other_panel = self.panels.get(other)
                if snap is None or other_panel is None:
                    continue
                # Keep the sibling's snapshot in step for the copied
                # positions, so it doesn't look "changed" next time.
                for local, cfg in source.nodes.items():
                    sc = snap.nodes.get(local)
                    params = cfg.get("params") or {}
                    if sc is None or params.get("x") is None:
                        continue
                    sp = sc.setdefault("params", {})
                    sp["x"] = float(params["x"])
                    sp["y"] = float(params["y"])
                structural = self._placement_differs(
                    snap, source, include_params=False, positions=False
                )
                params_differ = self._placement_differs(
                    snap, source, include_params=True, positions=False
                )
                if not structural and not params_differ:
                    continue
                adapted = panels.Panel(
                    id=other_panel.id, parent=other_panel.parent,
                    label=other_panel.label, color=other_panel.color,
                    mode=other_panel.mode, x=other_panel.x, y=other_panel.y,
                    w=other_panel.w, h=other_panel.h,
                    anchored=other_panel.anchored, path=other_panel.path,
                    writable=other_panel.writable, stem=other_panel.stem,
                    auto_load=other_panel.auto_load,
                    config={
                        "nodes": dict(source.config.get("nodes") or {}),
                        "edges": list(source.config.get("edges") or []),
                        "panels": list(source.child_entries),
                        "groups": list(source.config.get("groups") or []),
                    },
                )
                self._panel_snapshots[other] = adapted
                if structural:
                    # Membership/edges differ: rebuild the sibling subtree.
                    self._revert_panel(other, {other: adapted})
                else:
                    # Parameters only: apply in place (no node reload).
                    self._apply_panel_params(other, source)

    def _cmd_list_panels(self, cmd: dict) -> dict:
        files = []
        for pid, panel in sorted(self.panels.items()):
            if pid == panels.ROOT_ID:
                continue
            files.append({
                "id": pid,
                "name": panels.local_of(pid),
                "label": panel.label,
                "color": panel.color,
                "mode": panel.mode,
                "readonly": panel.is_readonly,
                "writable": panel.writable,
                "auto_load": panel.auto_load,
                "stem": panel.stem,
                "path": panel.path,
                "directory": os.path.dirname(panel.path) if panel.path else None,
                "nodes": [panels.make_id(pid, n) for n in panel.nodes],
                "children": panel.child_ids(),
            })
        return {
            "status": "ok",
            "root": self.root_panel_path,
            "directories": [
                {"path": p, "writable": w} for p, w in self.panel_dirs
            ],
            "files": files,
        }

    def _panel_slug(self, name: str) -> str:
        """A filesystem-safe panel stem from a display name."""
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", (name or "").strip()).strip("_")
        return stem or f"panel_{int(time.time() * 1000)}"

    def _cmd_create_panel(self, cmd: dict) -> dict:
        """Create a new panel (a file) and move the selection into it.

        When ``parent_id`` names a panel (the selection's own panel), the
        new panel is nested inside it; otherwise it is created at the
        root.  This is the panel form of the old "Declare" action."""
        name = (cmd.get("name") or "").strip()
        if not name:
            return {"status": "error", "message": "name required"}
        stem = self._panel_slug(name)
        parent_id = cmd.get("parent_id", "") or panels.ROOT_ID
        parent = self.panels.get(parent_id)
        if parent is None:
            if parent_id != panels.ROOT_ID:
                return {"status": "error", "message": f"no panel {parent_id!r}"}
            # A daemon that hasn't loaded a root file yet still needs one to
            # own top-level children.
            parent = panels.Panel(
                id=panels.ROOT_ID, parent=None, label="root",
                color=panels.DEFAULT_COLOR, mode=panels.MODE_RW,
                path=self.root_panel_path, writable=True,
                config={"nodes": {}, "edges": [], "panels": [], "groups": []},
            )
            self.panels[panels.ROOT_ID] = parent
        # A unique local name among the parent's children.
        local = stem
        n = 2
        while local in parent.children:
            local = f"{stem}_{n}"
            n += 1
        new_panel_id = panels.make_id(parent_id, local)
        if new_panel_id in self.panels:
            return {"status": "error", "message": f"panel {new_panel_id!r} exists"}
        target_dir = next(
            (d for d, w in self.panel_dirs if d and w), None
        )
        if target_dir is None:
            return {"status": "error", "message": "no writable panel directory"}
        path = os.path.join(target_dir, local + panels.PANEL_SUFFIX)
        node_ids = list(cmd.get("node_ids") or [])
        moved: List[str] = []
        px, py = self._panel_origin(self.panels, parent_id)
        with self._lock:
            for nid in node_ids:
                if nid not in self.space.nodes:
                    continue
                new_id = panels.make_id(new_panel_id, panels.local_of(nid))
                if new_id in self.space.nodes:
                    continue
                try:
                    self._rename_owned_node(nid, new_id)
                    moved.append(new_id)
                except (KeyError, ValueError) as exc:
                    logger.warning("create_panel move %r failed: %s", nid, exc)
            mode = panels.MODE_RO if cmd.get("readonly") else panels.MODE_RW
            self.panels[new_panel_id] = panels.Panel(
                id=new_panel_id, parent=parent_id,
                label=cmd.get("label") or name,
                color=cmd.get("color") or panels.DEFAULT_COLOR,
                mode=mode, path=path, writable=True, stem=local,
                # New panels start pinned/paused so the layout doesn't shove
                # them around the moment they're created (the panel header's
                # physics toggle re-arms them).
                anchored=True,
                x=float(cmd.get("x", 0.0) or 0.0) - px,
                y=float(cmd.get("y", 0.0) or 0.0) - py,
                w=max(panels.MIN_W, float(cmd.get("w", panels.DEFAULT_W) or 0.0)),
                h=max(panels.MIN_H, float(cmd.get("h", panels.DEFAULT_H) or 0.0)),
                config={"nodes": {}, "edges": [], "panels": [], "groups": []},
            )
            if local not in parent.children:
                parent.config.setdefault("panels", []).append(
                    panels.child_ref(local, local)
                )
        self._standardize_nodes(moved)
        # Refresh the in-memory tree from live state so list_panels (and a
        # later reset) sees the just-moved nodes as members immediately.
        self._install_panels(self._build_panels_from_space())
        self._write_panels()
        self._dirty = True
        self._wake_ticker()
        return {
            "status": "ok", "panel_id": new_panel_id,
            "moved": moved, "path": path,
        }

    def _cmd_delete_panel(self, cmd: dict) -> dict:
        """Delete a panel file.

        ``keep_nodes`` (default False) removes just the panel container and
        moves its direct nodes up into the parent panel (re-qualified);
        otherwise the panel's whole subtree - nodes and child panels - is
        removed with it."""
        panel_id = cmd.get("panel_id", "")
        panel = self.panels.get(panel_id)
        if not panel_id or panel is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        if panel.is_readonly or not panel.writable:
            return {"status": "error", "message": f"panel {panel_id!r} is read-only"}
        keep_nodes = bool(cmd.get("keep_nodes"))
        parent_id = panel.parent or ""
        prefix = panel_id + panels.NAMESPACE_SEP
        with self._lock:
            if keep_nodes:
                if panel.children:
                    return {
                        "status": "error",
                        "message": "panel has child panels; delete them first",
                    }
                for nid in [
                    n for n in self.space.nodes
                    if panels.panel_of(n) == panel_id
                ]:
                    new_id = panels.make_id(parent_id, panels.local_of(nid))
                    if new_id == nid or new_id in self.space.nodes:
                        continue
                    try:
                        self._rename_owned_node(nid, new_id)
                    except (KeyError, ValueError) as exc:
                        logger.warning(
                            "delete_panel keep %r -> %r failed: %s",
                            nid, new_id, exc,
                        )
                self.panels.pop(panel_id, None)
            else:
                for nid in [
                    n for n in self.space.nodes
                    if n == panel_id or n.startswith(prefix)
                ]:
                    self.space.remove_node(nid)
                for pid in [
                    p for p in self.panels
                    if p == panel_id or p.startswith(prefix)
                ]:
                    self.panels.pop(pid, None)
            parent = self.panels.get(parent_id)
            if parent is not None:
                local = panels.local_of(panel_id)
                parent.config["panels"] = [
                    s for s in parent.config.get("panels", [])
                    if panels.child_name(s) != local
                ]
        others = [
            p for p in self.panels.values()
            if p.id != panel_id and p.stem and p.stem == panel.stem
        ]
        if not others and panel.path and os.path.isfile(panel.path):
            try:
                os.remove(panel.path)
            except OSError as exc:
                logger.warning("Could not delete panel file %r: %s", panel.path, exc)
        # Refresh the in-memory tree so list_panels reflects the moved or
        # removed nodes immediately.
        self._install_panels(self._build_panels_from_space())
        self._write_panels()
        self._dirty = True
        self._wake_ticker()
        return {"status": "ok", "panel_id": panel_id, "kept_nodes": keep_nodes}

    def _cmd_delete_panel_file(self, cmd: dict) -> dict:
        """Delete a panel *file* (the backend) from the side view.

        Unlike ``delete_panel`` (one placement, with a keep-nodes choice),
        this removes the file itself, so every loaded placement of it goes
        too - along with each placement's subtree (nested child panels)."""
        stem = (cmd.get("stem") or "").strip()
        if not stem:
            return {"status": "error", "message": "stem required"}
        path, writable = self._panel_file_path(stem)
        if not path:
            return {"status": "error", "message": f"no panel file {stem!r}"}
        if not writable:
            return {"status": "error", "message": f"panel file {stem!r} is read-only"}
        placements = [
            pid for pid, panel in self.panels.items()
            if pid != panels.ROOT_ID and panel.stem == stem
        ]
        with self._lock:
            # A placement's subtree includes its nested child panels; gather
            # every doomed panel id before mutating anything.
            doomed = set()
            for pid in placements:
                prefix = pid + panels.NAMESPACE_SEP
                for p in self.panels:
                    if p == pid or p.startswith(prefix):
                        doomed.add(p)
            for pid in doomed:
                prefix = pid + panels.NAMESPACE_SEP
                for nid in [
                    n for n in self.space.nodes
                    if n == pid or n.startswith(prefix)
                ]:
                    self.space.remove_node(nid)
            for pid in placements:
                parent = self.panels.get(self.panels[pid].parent or "")
                if parent is None:
                    continue
                local = panels.local_of(pid)
                parent.config["panels"] = [
                    s for s in parent.config.get("panels", [])
                    if panels.child_name(s) != local
                ]
            for pid in doomed:
                self.panels.pop(pid, None)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("Could not delete panel file %r: %s", path, exc)
            return {"status": "error", "message": f"could not delete {path}: {exc}"}
        self._prune_empty_panel_dirs(os.path.dirname(path))
        self._install_panels(self._build_panels_from_space())
        self._write_panels()
        self._dirty = True
        self._wake_ticker()
        return {"status": "ok", "stem": stem, "removed": sorted(placements)}

    def _prune_empty_panel_dirs(self, start: str) -> None:
        """Remove now-empty sub-folders left after deleting a panel file,
        walking up but never past a configured panel directory."""
        start = os.path.abspath(start)
        bases = [
            os.path.abspath(d) for d, _w in self.panel_dirs if d
        ]
        cur = start
        while True:
            base = next(
                (b for b in bases if cur == b or cur.startswith(b + os.sep)),
                None,
            )
            if base is None or cur == base:
                return
            try:
                os.rmdir(cur)
            except OSError:
                return
            cur = os.path.dirname(cur)

    def _cmd_edit_panel(self, cmd: dict) -> dict:
        """Change a writable panel's display label and/or color."""
        panel_id = cmd.get("panel_id", "")
        panel = self.panels.get(panel_id)
        if not panel_id or panel is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        if panel.is_readonly or not panel.writable:
            return {"status": "error", "message": f"panel {panel_id!r} is read-only"}
        label = (cmd.get("label") or "").strip()
        if label:
            panel.label = label
        color = (cmd.get("color") or "").strip()
        if color:
            panel.color = color
        if cmd.get("auto_load") is not None:
            panel.auto_load = bool(cmd["auto_load"])
        self._write_panels()
        self._dirty = True
        self._wake_ticker()
        return {
            "status": "ok",
            "panel_id": panel_id,
            "label": panel.label,
            "color": panel.color,
        }

    def _cmd_export_panel(self, cmd: dict) -> dict:
        """Return a panel's *current* file payload (live state, even for a
        read-only panel) for the GUI's copy/save actions."""
        panel_id = cmd.get("panel_id", "")
        if panel_id not in self.panels:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        tree = self._build_panels_from_space(use_snapshots=False, freeze_params=False)
        panel = tree.get(panel_id)
        if panel is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        return {
            "status": "ok",
            "panel_id": panel_id,
            "payload": panels.build_payload(panel),
        }

    def _cmd_clone_panel(self, cmd: dict) -> dict:
        """Duplicate a panel (its current live nodes/edges/groups) into a
        new panel file, offset slightly, then reload so the copy is live."""
        panel_id = cmd.get("panel_id", "")
        src = self.panels.get(panel_id)
        if panel_id == "" or src is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        name = (cmd.get("name") or "").strip()
        if not name:
            return {"status": "error", "message": "name required"}
        stem = self._panel_slug(name)
        if stem in self.panels:
            return {"status": "error", "message": f"panel {stem!r} already exists"}
        src_dir = None
        if src.path and src.writable and not src.is_readonly:
            src_dir = os.path.dirname(src.path)
        target_dir = src_dir if src_dir and os.path.isdir(src_dir) else next(
            (d for d, w in self.panel_dirs if d and w), None
        )
        if target_dir is None:
            return {"status": "error", "message": "no writable panel directory"}
        path = os.path.join(target_dir, stem + panels.PANEL_SUFFIX)
        live = self._build_panels_from_space(use_snapshots=False, freeze_params=False).get(panel_id)
        if live is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        clone = panels.Panel(
            id=stem, parent=src.parent, label=name, color=src.color,
            mode=panels.MODE_RW, x=src.x + 40.0, y=src.y + 40.0,
            w=src.w, h=src.h, path=path, writable=True,
            auto_load=src.auto_load,
            # A fresh clone starts pinned/paused like a newly created panel.
            anchored=True,
            config={
                "nodes": dict(live.config.get("nodes") or {}),
                "edges": list(live.config.get("edges") or []),
                "panels": [],
                "groups": list(live.config.get("groups") or []),
            },
        )
        panels.write_file(path, clone)
        # Persist the parent's child list so the reload finds the clone.
        parent = self.panels.get(src.parent or "")
        if parent is not None:
            if stem not in parent.children:
                parent.config.setdefault("panels", []).append(
                    panels.child_ref(stem, stem)
                )
            if parent.path:
                parent_now = self._build_panels_from_space(
                    use_snapshots=False
                ).get(parent.id)
                if parent_now is not None:
                    if stem not in parent_now.children:
                        parent_now.config.setdefault("panels", []).append(
                            panels.child_ref(stem, stem)
                        )
                    panels.write_file(parent.path, parent_now)
        # Bring the clone's nodes up (without reloading everything).
        self._load_new_placement(stem)
        return {"status": "ok", "panel_id": stem, "path": path}

    def _panel_file_path(self, stem: str):
        """Locate a panel file by stem across the load dirs (later dirs
        shadow earlier).  Returns (path, writable) or (None, False)."""
        found = None
        for directory, writable in self.panel_dirs:
            if not directory:
                continue
            for path in panels.list_files(directory):
                if panels.file_stem(path) == stem:
                    found = (path, writable)
        return found if found else (None, False)

    def _cmd_list_panel_files(self, cmd: dict) -> dict:
        """The panel *files* (backends), de-duplicated by stem; the side
        view lists these and each can be placed and auto-loaded."""
        by_stem = {}
        for directory, writable in self.panel_dirs:
            if not directory:
                continue
            for path in panels.list_files(directory):
                stem = panels.file_stem(path)
                raw = panels.read_file(path) or {}
                config = panels.config_from_raw(raw)
                children = [
                    panels.child_name(c)
                    for c in (config.get("panels") or [])
                ]
                rel = os.path.relpath(path, directory)
                by_stem[stem] = {
                    "stem": stem,
                    "label": raw.get("label") or stem,
                    "color": raw.get("color") or panels.DEFAULT_COLOR,
                    "mode": panels.mode_from_raw(raw),
                    "auto_load": bool(raw.get("auto_load")),
                    "path": path,
                    # Sub-folder relative to the panel directory ("" for a
                    # top-level file); the side view groups rows by it.
                    "folder": os.path.dirname(rel),
                    "writable": writable,
                    "node_count": len(config.get("nodes") or {}),
                    "children": children,
                }
        return {
            "status": "ok",
            "panel_files": True,
            "files": [by_stem[s] for s in sorted(by_stem)],
        }

    def _cmd_set_panel_file_autoload(self, cmd: dict) -> dict:
        """Toggle a panel file's auto-load flag (writes the file)."""
        stem = (cmd.get("stem") or "").strip()
        path, writable = self._panel_file_path(stem)
        if not path:
            return {"status": "error", "message": f"no panel file {stem!r}"}
        if not writable:
            return {"status": "error", "message": f"panel file {stem!r} is read-only"}
        raw = panels.read_file(path) or {}
        raw["auto_load"] = bool(cmd.get("enabled"))
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(raw, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        # Reflect it on any loaded placement of the same file.
        for p in self.panels.values():
            if p.stem == stem:
                p.auto_load = bool(cmd.get("enabled"))
        self._panel_mtimes = panels.snapshot(self._panel_dir_paths())
        return {"status": "ok", "stem": stem, "auto_load": bool(cmd.get("enabled"))}

    def _cmd_place_panel(self, cmd: dict) -> dict:
        """Add a placement of a panel file under ``parent_id`` (root by
        default), then reload so its nodes come up."""
        stem = (cmd.get("stem") or "").strip()
        parent_id = cmd.get("parent_id", "")
        if not stem:
            return {"status": "error", "message": "stem required"}
        path, writable = self._panel_file_path(stem)
        if not path:
            return {"status": "error", "message": f"no panel file {stem!r}"}
        parent = self.panels.get(parent_id)
        if parent is None:
            return {"status": "error", "message": f"no panel {parent_id!r}"}
        name = (cmd.get("name") or stem).strip() or stem
        placement = {
            "x": float(cmd.get("x", 0.0) or 0.0),
            "y": float(cmd.get("y", 0.0) or 0.0),
            "w": max(panels.MIN_W, float(cmd.get("w", panels.DEFAULT_W) or 0.0)),
            "h": max(panels.MIN_H, float(cmd.get("h", panels.DEFAULT_H) or 0.0)),
            # Pinned/paused by default so a freshly placed panel holds still.
            "anchored": bool(cmd.get("anchored", True)),
        }
        with self._lock:
            existing = set(parent.children)
            base = panels.file_stem(name) or stem
            local = base
            n = 2
            while local in existing:
                local = f"{base}_{n}"
                n += 1
            ref = panels.child_ref(local, stem, placement)
            parent.config.setdefault("panels", []).append(ref)
            # Persist the parent file so the reload finds the new child.
            if parent.path:
                parent_now = self._build_panels_from_space().get(parent.id)
                if parent_now is not None:
                    if local not in parent_now.children:
                        parent_now.config.setdefault("panels", []).append(ref)
                    panels.write_file(parent.path, parent_now)
        self._load_new_placement(panels.make_id(parent_id, local))
        return {"status": "ok", "stem": stem, "name": local, "parent_id": parent_id}

    def _cmd_remove_panel_placement(self, cmd: dict) -> dict:
        """Remove this placement (and its nodes) from the workspace, but
        keep the panel file."""
        panel_id = cmd.get("panel_id", "")
        panel = self.panels.get(panel_id)
        if not panel_id or panel_id == panels.ROOT_ID or panel is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        parent_id = panel.parent or ""
        prefix = panel_id + panels.NAMESPACE_SEP
        with self._lock:
            for nid in [
                n for n in list(self.space.nodes)
                if n == panel_id or n.startswith(prefix)
            ]:
                self.space.remove_node(nid)
            for pid in [
                p for p in list(self.panels)
                if p == panel_id or p.startswith(prefix)
            ]:
                self.panels.pop(pid, None)
            for store in (self._readonly_snapshots, self._panel_snapshots):
                for pid in [
                    p for p in list(store)
                    if p == panel_id or p.startswith(prefix)
                ]:
                    store.pop(pid, None)
            self._edit_panels = {
                p for p in self._edit_panels
                if p != panel_id and not p.startswith(prefix)
            }
            parent = self.panels.get(parent_id)
            if parent is not None:
                local = panels.local_of(panel_id)
                parent.config["panels"] = [
                    s for s in parent.config.get("panels", [])
                    if panels.child_name(s) != local
                ]
        self._write_panels()
        self._dirty = True
        self._wake_ticker()
        return {"status": "ok", "panel_id": panel_id}

    def _cmd_reload_panels(self, cmd: dict) -> dict:
        with self._panel_lock:
            self._panel_reloading = True
            self._begin_heavy_load()
            try:
                tree = self._load_panels_tree()
                self._install_panels(tree)
                config = self._flatten_panels(tree)
                self._remove_panels_from_space()
                # Full reload from disk: migrate any legacy nodes.
                result = self._load_session(config, declarative=True, migrate=True)
                result["panels"] = len(tree) - 1
                self._dirty = True
                self._wake_ticker()
                return result
            finally:
                self._panel_reloading = False
                self._end_heavy_load()
                self._panel_mtimes = panels.snapshot(self._panel_dir_paths())

    def _cmd_set_panel_layout(self, cmd: dict) -> dict:
        """Set a panel's placement (x/y/w/h/anchored).

        Moving the panel also shifts its subtree's *nodes* by the same
        delta so the panel keeps its contents (the GUI sends matching
        absolute positions afterwards, so this is idempotent).  Doing it
        here keeps node-relative-to-panel stable even if an autosave lands
        before the GUI's node layout arrives, which otherwise looked like a
        content change and rebuilt the file's other placements."""
        panel_id = cmd.get("panel_id", "")
        panel = self.panels.get(panel_id)
        if panel is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        old_x, old_y = panel.x, panel.y
        if cmd.get("x") is not None:
            panel.x = float(cmd["x"])
        if cmd.get("y") is not None:
            panel.y = float(cmd["y"])
        if cmd.get("w") is not None:
            panel.w = max(panels.MIN_W, float(cmd["w"]))
        if cmd.get("h") is not None:
            panel.h = max(panels.MIN_H, float(cmd["h"]))
        if cmd.get("anchored") is not None:
            panel.anchored = bool(cmd["anchored"])
        dx, dy = panel.x - old_x, panel.y - old_y
        if dx or dy:
            prefix = panel_id + panels.NAMESPACE_SEP
            with self._lock:
                for nid, node in self.space.nodes.items():
                    if nid != panel_id and not nid.startswith(prefix):
                        continue
                    if getattr(node, "x", None) is not None:
                        node.x += dx
                    if getattr(node, "y", None) is not None:
                        node.y += dy
        self._dirty = True
        self._wake_ticker()
        return {"status": "ok", "panel_id": panel_id}

    @staticmethod
    def _in_panel_subtree(node_id: str, panel_id: str) -> bool:
        return node_id == panel_id or node_id.startswith(
            panel_id + panels.NAMESPACE_SEP
        )

    def _revert_panel(self, panel_id: str, snapshots) -> bool:
        """Revert one panel's subtree to a snapshot: membership, positions,
        edges, groups and params.  Edges that merely cross into the panel
        (owned by an ancestor, hence imperative) are preserved and
        re-added."""
        with self._panel_lock:
            self._panel_reloading = True
            self._begin_heavy_load()
            try:
                subtree = {
                    pid: p for pid, p in snapshots.items()
                    if pid == panel_id
                    or pid.startswith(panel_id + panels.NAMESPACE_SEP)
                }
                if panel_id not in subtree:
                    return False
                imperative = [
                    e for e in self._build_export_config(imperative_only=True)["edges"]
                    if self._in_panel_subtree(e.get("from", ""), panel_id)
                    or self._in_panel_subtree(e.get("to", ""), panel_id)
                ]
                with self._lock:
                    for nid in [
                        n for n in self.space.nodes
                        if self._in_panel_subtree(n, panel_id)
                    ]:
                        self.space.remove_node(nid)
                    for gid in list(self.groups):
                        members = self.groups[gid].get("nodes") or []
                        if members and all(
                            self._in_panel_subtree(m, panel_id) for m in members
                        ):
                            self.groups.pop(gid, None)
                    for pid, p in subtree.items():
                        self.panels[pid] = p
                config = self._flatten_panels(
                    {**self.panels, **subtree}, only_panels=set(subtree)
                )
                # Rebuilding one placement's subtree to match its sibling
                # keeps the file's own schema (see _load_new_placement).
                self._load_session(config, declarative=True, migrate=False)
                self._store_imperative_edges(imperative)
                self._dirty = True
                self._wake_ticker()
                return True
            except Exception:
                logger.exception("revert panel %r failed", panel_id)
                return False
            finally:
                self._panel_reloading = False
                self._end_heavy_load()

    def _cmd_reset_panel(self, cmd: dict) -> dict:
        """Revert one read-only panel's subtree to its file snapshot."""
        panel_id = cmd.get("panel_id", "")
        if panel_id not in self._readonly_snapshots:
            return {"status": "error", "message": f"no read-only panel {panel_id!r}"}
        if not self._revert_panel(panel_id, self._readonly_snapshots):
            return {"status": "error", "message": f"reset {panel_id!r} failed"}
        return {"status": "ok", "panel_id": panel_id, "reverted": True}

    def _refresh_panel_params_from_file(self, panel_id: str) -> bool:
        """Re-apply a panel file's parameter values to its live nodes
        *without* recreating them.

        Entering edit mode only needs to discard runtime knob/switch tweaks
        and restore the file's values; membership, edges and layout already
        track the file, so a full node teardown/rebuild (``_revert_panel``)
        is unnecessary - and was re-loading every effect each time."""
        panel = self.panels.get(panel_id)
        if panel is None or not panel.path:
            return False
        raw = panels.read_file(panel.path)
        if raw is None:
            return False
        cfg = panels.config_from_raw(raw)
        ox, oy = self._panel_origin(self.panels, panel_id)
        with self._lock:
            for local, ncfg in (cfg.get("nodes") or {}).items():
                node = self.space.nodes.get(panels.make_id(panel_id, local))
                if node is None:
                    continue
                params = dict(ncfg.get("params") or {})
                if params.get("x") is not None:
                    params["x"] = float(params["x"]) + ox
                if params.get("y") is not None:
                    params["y"] = float(params["y"]) + oy
                self._apply_node_config(node, params)
            self.space.sync()
        return True

    def _apply_panel_params(self, panel_id: str, source) -> None:
        """Apply another placement's parameter values to this placement's
        live nodes in place (no reload) - used to sync an edit across
        placements of the same file."""
        with self._lock:
            for local, cfg in (source.config.get("nodes") or {}).items():
                node = self.space.nodes.get(panels.make_id(panel_id, local))
                if node is None:
                    continue
                params = dict(cfg.get("params") or {})
                for key in ("x", "y", "anchored"):
                    params.pop(key, None)
                self._apply_node_config(node, params)
            self.space.sync()

    def _cmd_set_panel_edit_mode(self, cmd: dict) -> dict:
        """Enter/leave a panel's edit mode.

        Entering refreshes the panel to the file state, then parameter
        changes are written back to the file while editing."""
        panel_id = cmd.get("panel_id", "")
        panel = self.panels.get(panel_id)
        if panel is None or panel_id == panels.ROOT_ID:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        enabled = bool(cmd.get("enabled"))
        if enabled:
            if panel.is_readonly:
                return {
                    "status": "error",
                    "message": f"panel {panel_id!r} is read-only",
                }
            if not self._refresh_panel_params_from_file(panel_id):
                return {"status": "error", "message": f"refresh {panel_id!r} failed"}
            self._edit_panels.add(panel_id)
        else:
            self._edit_panels.discard(panel_id)
        self._dirty = True
        self._wake_ticker()
        return {"status": "ok", "panel_id": panel_id, "edit_mode": enabled}

    def _cmd_move_panel(self, cmd: dict) -> dict:
        """Nest one panel inside another (``parent_id``, "" = root).

        The moved panel keeps its absolute placement; every panel in its
        subtree is re-keyed under the new path and every node id is
        re-qualified (edges are rebuilt by rename, groups re-pointed), so
        arbitrary nesting depth round-trips through the files."""
        panel_id = cmd.get("panel_id", "")
        new_parent = cmd.get("parent_id", "")
        panel = self.panels.get(panel_id)
        if not panel_id or panel_id == panels.ROOT_ID or panel is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        if panel.is_readonly or not panel.writable:
            return {"status": "error", "message": f"panel {panel_id!r} is read-only"}
        if new_parent:
            target = self.panels.get(new_parent)
            if target is None:
                return {"status": "error", "message": f"no panel {new_parent!r}"}
            if target.is_readonly or not target.writable:
                return {
                    "status": "error",
                    "message": f"panel {new_parent!r} is read-only",
                }
        if new_parent == panel_id or (
            new_parent
            and new_parent.startswith(panel_id + panels.NAMESPACE_SEP)
        ):
            return {"status": "error", "message": "cannot nest a panel in itself"}
        old_parent = panel.parent or ""
        if old_parent == new_parent:
            return {"status": "ok", "panel_id": panel_id, "parent_id": new_parent}
        prefix = panel_id + panels.NAMESPACE_SEP
        with self._lock:
            subtree = [
                p for p in self.panels
                if p == panel_id or p.startswith(prefix)
            ]
            new_root = panels.make_id(new_parent, panels.local_of(panel_id))
            id_map = {p: new_root + p[len(panel_id):] for p in subtree}
            for old, new in id_map.items():
                if new in self.panels and new not in subtree:
                    return {"status": "error", "message": f"{new!r} already exists"}
            old_abs = self._panel_origin(self.panels, panel_id)
            new_abs = (
                self._panel_origin(self.panels, new_parent)
                if new_parent else (0.0, 0.0)
            )
            panel.x = old_abs[0] - new_abs[0]
            panel.y = old_abs[1] - new_abs[1]
            # Re-qualify every node in the subtree (rebuilds their edges).
            for nid in [
                n for n in list(self.space.nodes)
                if panels.panel_of(n) in id_map
            ]:
                new_nid = panels.make_id(
                    id_map[panels.panel_of(nid)], panels.local_of(nid)
                )
                self._rename_owned_node(nid, new_nid)
            # Re-key the panels (and their snapshots).
            moved = {}
            for p in subtree:
                obj = self.panels.pop(p)
                obj.id = id_map[p]
                obj.parent = new_parent if p == panel_id else id_map.get(
                    obj.parent, new_parent
                )
                moved[obj.id] = obj
            self.panels.update(moved)
            for store in (self._readonly_snapshots, self._panel_snapshots):
                for old, new in id_map.items():
                    if old in store:
                        store[new] = store.pop(old)
            self._edit_panels = {id_map.get(p, p) for p in self._edit_panels}
            local = panels.local_of(panel_id)
            ref = panels.child_ref(local, panel.stem or local)
            op = self.panels.get(old_parent)
            if op is not None:
                op.config["panels"] = [
                    s for s in op.config.get("panels", [])
                    if panels.child_name(s) != local
                ]
            np_ = self.panels.get(new_parent)
            if np_ is not None and local not in np_.children:
                np_.config.setdefault("panels", []).append(ref)
        self._install_panels(self._build_panels_from_space())
        self._write_panels()
        self._dirty = True
        self._wake_ticker()
        return {"status": "ok", "panel_id": id_map[panel_id], "parent_id": new_parent}

    def _cmd_move_nodes(self, cmd: dict) -> dict:
        """Reparent a selection into ``panel_id``.  Node ids are
        re-qualified to the new panel, which re-homes every incident edge
        to its new least-common-ancestor automatically.  Refused (for the
        GUI to snap back) when the node's current panel or the target is
        read-only."""
        panel_id = cmd.get("panel_id", "")
        node_ids = list(cmd.get("node_ids") or [])
        target = self.panels.get(panel_id)
        if target is None:
            return {"status": "error", "message": f"no panel {panel_id!r}"}
        moved, refused = [], []
        with self._lock:
            for nid in node_ids:
                if nid not in self.space.nodes:
                    continue
                cur = panels.panel_of(nid)
                if cur == panel_id:
                    continue
                cur_panel = self.panels.get(cur)
                if (cur_panel is not None and cur_panel.is_readonly) or target.is_readonly:
                    refused.append(nid)
                    continue
                new_id = panels.make_id(panel_id, panels.local_of(nid))
                if new_id in self.space.nodes:
                    refused.append(nid)
                    continue
                try:
                    self._rename_owned_node(nid, new_id)
                    moved.append(new_id)
                except (KeyError, ValueError) as exc:
                    logger.warning("move %r -> %r failed: %s", nid, new_id, exc)
                    refused.append(nid)
        self._standardize_nodes(moved)
        if moved:
            # Keep the in-memory tree in step with the renames so
            # list_panels and reset_panel see the new membership.
            self._install_panels(self._build_panels_from_space())
            self._dirty = True
            self._wake_ticker()
        return {"status": "ok", "moved": moved, "refused": refused}

    def _poll_panels(self) -> None:
        """Cheap mtime scan on the tick; reload when a panel file is
        added, removed or touched."""
        if self._panel_reloading:
            return
        now = time.monotonic()
        if now < self._panel_poll_at:
            return
        self._panel_poll_at = now + PANEL_POLL_S
        state = panels.snapshot(self._panel_dir_paths())
        if state != self._panel_mtimes:
            self._panel_mtimes = state
            self._cmd_reload_panels({})

    def _begin_heavy_load(self) -> None:
        with self._loading_lock:
            self._loading_count += 1
            self._startup_loading = True
        self._wake_ticker()

    def _end_heavy_load(self) -> None:
        with self._loading_lock:
            self._loading_count = max(0, self._loading_count - 1)
            self._startup_loading = self._loading_count > 0

    def _startup_load_thread(self) -> None:
        """Run the start-up session load off the socket thread.  Never
        lets a bad session take the daemon down: the socket is already
        serving, so failures are logged and the GUI stays connected.

        `start()` opens the heavy-load counter (covering the orphan sweep)
        before this thread exists; this thread closes it when the session
        load finishes."""
        try:
            self._load_startup_sessions()
        except Exception:
            logger.exception("Start-up session load failed")
        finally:
            self._end_heavy_load()

    def _load_startup_sessions(self) -> None:
        """Boot the graph from the panel tree (root autosave + panel
        files), migrating the legacy session/declarative shapes."""
        self._startup_load_panels()

    def _remove_panels_from_space(self) -> None:
        """Drop every declarative node/edge/group so the files can be
        re-applied cleanly.  Imperative edges that touched declarative
        nodes are captured first (by the caller) and re-added after."""
        with self._lock:
            for edge_id in [
                eid
                for eid, e in self.space.edges.items()
                if getattr(e, "declarative", False)
            ]:
                self.space.remove_edge(edge_id)
            for node_id in [
                nid
                for nid, n in self.space.nodes.items()
                if getattr(n, "declarative", False)
            ]:
                self.space.remove_node(node_id)
            for group_id in [
                gid for gid, g in self.groups.items() if g.get("declarative")
            ]:
                self.groups.pop(group_id, None)

    def _store_imperative_edges(self, edges) -> None:
        with self._lock:
            for edge in edges:
                if edge.get("from") not in self.space.nodes:
                    continue
                if edge.get("to") not in self.space.nodes:
                    continue
                try:
                    self._store_session_edge(edge)
                except (KeyError, ValueError) as exc:
                    logger.warning("Could not restore imperative edge %r: %s", edge, exc)
            self.space.supervise()

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

    def _on_node_removed(self, node_id: int, node_data: Optional[dict] = None) -> None:
        with self._lock:
            changed = False
            for node in self.space.nodes.values():
                if (
                    isinstance(node, LiveResolvableNode)
                    and node.live_node_id == node_id
                ):
                    node.resolve_live(None, None)
                    changed = True
            self.space.handle_node_removed(node_id, node_data)
        if changed:
            self.space.sync()

    # ------------------------------------------------------------------
    # session persistence
    # ------------------------------------------------------------------

    def _build_export_config(self, imperative_only: bool = False) -> dict:
        with self._lock:
            nodes = {}
            for node_id, node in self.space.nodes.items():
                if node_id not in self.space.public_nodes:
                    continue
                if imperative_only and getattr(node, "declarative", False):
                    continue
                node_type = CLASS_TO_TYPE.get(type(node), "unknown")
                params = {}
                for attr in _SERIAL_ATTRS:
                    # The declarative flag is provenance, not a setting:
                    # it is reported by get_nodes so the GUI can tag the
                    # node, but never written into an exported session.
                    if attr == "declarative":
                        continue
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
                if imperative_only and getattr(e, "declarative", False):
                    continue
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
        groups = [
            dict(g)
            for g in self.groups.values()
            if not (imperative_only and g.get("declarative"))
        ]
        for g in groups:
            g.pop("declarative", None)
        return {"nodes": nodes, "edges": edges, "groups": groups}

    def _auto_export_session(self) -> None:
        try:
            # Panels are the persistence unit now: the root panel (and
            # every writable read-write panel) is rewritten from live
            # state; read-only panels keep their frozen snapshot so a
            # runtime edit is reverted on the next boot.
            self._write_panels()
        except OSError as exc:
            logger.warning("Failed to auto-save panels: %s", exc)

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
        if not self.space._graph_loaded:
            # Nothing is wired yet (tests, or a daemon before its first
            # graph snapshot); polling readiness here would just burn the
            # full timeout and block the caller.
            return True
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
            self._store_edge(
                from_node, to_node, to_port, from_port,
                declarative=bool(edge.get("declarative")),
            )
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

    def _load_session(self, config: dict, declarative: bool = False,
                      migrate: bool = False) -> dict:
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
        # Self-heal the config first: a saved session can carry legacy
        # switch port names or edges to nodes that no longer exist.  Only
        # the lossless fixes run here (no group collapsing, no orphan
        # pruning) - see session_repair.repair.
        repaired = session_repair.repair(
            config, known_types=set(NODE_TYPE_REGISTRY), dedupe_groups=False,
            migrate=migrate,
        )
        if repaired.fixes:
            logger.info(
                "Session repair applied %d fix(es) before load:", len(repaired.fixes)
            )
            for fix in repaired.fixes:
                logger.info("  - %s", fix)
        config = repaired.config

        nodes_cfg = config.get("nodes", {}) or {}
        edges_cfg = config.get("edges", []) or []
        groups_cfg = config.get("groups", []) or []

        created, updated, node_failures = [], [], []
        backed_ids: List[str] = []
        # Backed nodes needing the dedicated second, per-input pass
        # (see _CAREFUL_NODE_TYPES), in creation order.
        finicky_ids: List[str] = []

        # One batched reap for everything this config could collide with,
        # done *outside* the lock (and with reap_stale_for_names waiting
        # for the graph to drop the objects) so the daemon's node-removed
        # callbacks can drain before we create same-named replacements.
        # Creating before the removals are processed lets PipeWire reuse
        # the freed node id and a lagging removal then tears down the new
        # backing - the "live object disappeared while alive" thrash that
        # took out the hidden sensitivity pre/post nodes.
        #
        # Only nodes this load will actually *create* are swept.  A node
        # already in the space keeps its live objects: the loop below
        # re-adopts the existing node object unchanged, so reaping its
        # backing would kill the owning process of a perfectly healthy
        # running node and leave it structurally present but silent.
        # (Re-importing the current session is the common case: every
        # node is "existing".)
        backing_names = [
            (node_cfg.get("params") or {}).get("backing_node_name")
            for node_id, node_cfg in nodes_cfg.items()
            if node_id not in self.space.nodes
        ]
        backing_names = [b for b in backing_names if b]
        if backing_names:
            self.graph.reap_stale_for_names(backing_names)

        with self._lock:
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
                    existing.declarative = bool(
                        params.get("declarative", declarative)
                    )
                    # Shared re-adopt fixups - identical to _cmd_add_node's
                    # re-apply (see _apply_node_config).
                    self._apply_node_config(existing, params)
                    if isinstance(existing, SensitivityGateNode):
                        backed_ids.extend(self._ensure_sensitivity_internals(existing))
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
                node.declarative = bool(params.get("declarative", declarative))
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
        # Stage every backed node - including the hidden sensitivity
        # pre/post that _ensure_sensitivity_internals just created, which
        # are not in the config's to_stage list - until its own bring-up
        # turn below.  The node-creation loop above holds the daemon lock
        # for as long as it takes to build every node's structural
        # pieces, and while it's held the graph's node-created callbacks
        # can't resolve any backing; a hidden pre/post created near the
        # start of a big load then looks "stuck" (past RESOLVE_GRACE_S)
        # the moment the tick first runs and gets torn down and rebuilt -
        # the pre/post thrash.  Staging keeps the tick off them until the
        # load's own bring-up loop resolves them.
        if backed_ids:
            self.space.stage(backed_ids)

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
                    "declarative": bool(raw.get("declarative", declarative)),
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
            self._begin_heavy_load()
            try:
                # Explicit user session load/import: migrate legacy nodes.
                self._load_session(config, migrate=True)
            except Exception:
                logger.exception("Background session load failed")
            finally:
                self._end_heavy_load()

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
    # declarative node commands
    # ------------------------------------------------------------------

    def _export_node_params(self, node_id: str, node: Node) -> dict:
        """One node's serialized params, same shape as _build_export_config
        but without the declarative provenance marker (the file's name is
        the provenance in a declarative file)."""
        node_type = CLASS_TO_TYPE.get(type(node), "unknown")
        params: Dict[str, Any] = {}
        for attr in _SERIAL_ATTRS:
            # Provenance and the live helper's name are not settings: the
            # file's stem names the node, and the daemon derives a fresh
            # backing name from the namespaced id.  Reusing the imperative
            # node's backing name would collide with it.
            if attr in ("declarative", "backing_node_name"):
                continue
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
        return {"type": node_type, "params": params}

    def _standardize_nodes(self, node_ids) -> None:
        """Apply the *standard* per-node setup + careful bring-up to a set
        of nodes, whichever code path they entered the graph through.

        Historically the per-node fixups (sensitivity internals, live
        resolve, device settings, and - crucially - the staged careful
        bring-up for finicky effects) were duplicated inline in
        ``_cmd_add_node`` and ``_load_session``, while the declarative
        live move just called ``supervise()``.  So a node moved into/out
        of a file skipped fixes the other paths apply, which is what made
        declarative moves unstable (a finicky effect could be left
        half-wired).  Everything now funnels through here."""
        finicky = []
        for node_id in node_ids:
            with self._lock:
                node = self.space.nodes.get(node_id)
                if node is None:
                    continue
                if isinstance(node, SensitivityGateNode):
                    self._ensure_sensitivity_internals(node)
                if isinstance(node, LiveResolvableNode):
                    self._try_immediate_resolve(node)
                if hasattr(node, "apply_device_settings"):
                    try:
                        node.apply_device_settings()
                    except Exception as exc:
                        logger.warning(
                            "Settings re-apply for %r failed: %s", node_id, exc
                        )
                if hasattr(node, "refresh_live"):
                    try:
                        node.refresh_live()
                    except Exception as exc:
                        logger.warning(
                            "Live refresh for %r failed: %s", node_id, exc
                        )
            if isinstance(node, _CAREFUL_NODE_TYPES):
                finicky.append(node)
        # Same dedicated per-node bring-up (_load_session's careful pass)
        # so a moved finicky effect's interior is confirmed live.
        for node in finicky:
            self._careful_bring_up(node)
        with self._lock:
            self.space.supervise()

    def _rename_owned_node(self, old_id: str, new_id: str) -> None:
        """Rename a node and any hidden companion whose id embeds its own
        (a Sensitivity gate's pre/post pass-throughs).  rename_node()
        rebuilds the incident edges, so this stays live - no teardown.
        Group membership is re-pointed too, so a selected set dragged into
        another panel keeps the groups it was part of."""
        hidden = [
            (prefix + old_id, prefix + new_id)
            for prefix in (_SENS_PRE_PREFIX, _SENS_POST_PREFIX)
            if (prefix + old_id) in self.space.nodes
        ]
        self.space.rename_node(old_id, new_id)
        for h_old, h_new in hidden:
            if h_old in self.space.nodes and h_new not in self.space.nodes:
                self.space.rename_node(h_old, h_new)
        for group in self.groups.values():
            members = group.get("nodes")
            if members:
                group["nodes"] = [
                    new_id if n == old_id else n for n in members
                ]

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
        # Declarative ids are namespaced ("file::node"), and the namespace
        # separator is not valid in a PipeWire node.name, so sanitise the
        # generated default.  Imperative ids are already safe and pass
        # through unchanged.
        backing = g("backing_node_name") or "patchspace_" + re.sub(
            r"[^A-Za-z0-9_.-]", "_", node_id
        )

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
        if cls in (AllInputsNode, AllOutputsNode, AllAppsNode):
            return cls(node_id)
        if cls is RegexClassifierNode:
            return cls(node_id, g("pattern", ""), g("invert", False))
        if cls is MediaClassClassifierNode:
            return cls(node_id, g("media_class", ""), g("invert", False))
        if cls is DescriptionClassifierNode:
            return cls(node_id, g("description", ""), g("invert", False))
        if cls is TitleClassifierNode:
            return cls(node_id, g("title", ""), g("invert", False))
        if cls is AppNameClassifierNode:
            return cls(node_id, g("app_name", ""), g("invert", False))
        if cls is AppClassifierNode:
            return cls(node_id, g("app_key", ""), g("invert", False))
        if cls is ExternalOnlyClassifierNode:
            return cls(node_id, g("invert", False))
        if cls is FilterNode:
            return cls(node_id, g("exclude", False))
        if cls in (BundleMergeNode, BundleSplitNode):
            return cls(node_id)
        if cls is BundleToAudioNode:
            return cls(node_id, backing)
        if cls is BundleOutputNode:
            return cls(node_id, backing)
        if cls is SplitterNode:
            return cls(node_id, backing)
        if cls is ButtonNode:
            return cls(node_id)
        if cls is SoundPlayerNode:
            return cls(node_id, backing, g("overlap", False))
        if cls is ClipNode:
            return cls(node_id, g("start", 0.0), g("end"))
        if cls is RecorderNode:
            return cls(node_id, backing)
        if cls is SoundNode:
            return cls(node_id, g("path", ""))
        if cls is GateNode:
            return cls(node_id, g("enabled", True))
        if cls in (SwitcherNode, InverseSwitcherNode):
            return cls(node_id, g("output", 0))
        if cls is BooleanSourceNode:
            return cls(node_id, g("output", 0))
        if cls is BooleanSplitterNode:
            return cls(node_id)
        if cls is BooleanInvertNode:
            return cls(node_id)
        if cls in (BooleanAndNode, BooleanOrNode, BooleanXorNode):
            return cls(node_id)
        if cls in (WarpInNode, WarpOutNode, BooleanWarpInNode, BooleanWarpOutNode):
            return cls(node_id, g("warp_name", ""))
        if cls in (PanelInNode, PanelOutNode, BoolPanelInNode, BoolPanelOutNode):
            return cls(node_id, g("port_name", ""), g("description", ""))
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
        if cls in (EchoCancelNode, LightNoiseCancelNode):
            return cls(
                node_id,
                backing,
                g("library_name", ""),
                g("aec_args", ""),
                g("monitor_mode", False),
            )
        if cls is SensitivityGateNode:
            return cls(
                node_id,
                backing,
                # None means "derive from the other" - see the
                # SensitivityGateNode constructor.  Both are absent for a
                # freshly GUI-created node, and both are present (and
                # consistent) for one loaded from a saved session.
                level=g("level", None),
                sensitivity=g("sensitivity", None),
                lv2_uri=g("lv2_uri", ""),
                ratio=g("ratio", None),
                attack_ms=g("attack_ms", None),
                release_ms=g("release_ms", None),
                knee_db=g("knee_db", None),
                makeup=g("makeup", None),
                range_db=g("range_db", None),
            )
        if cls is ReverbNode:
            return cls(
                node_id,
                backing,
                wet_dry=g("wet_dry", 0.3),
                decay_time=g("decay_time", 1.5),
                room_size=g("room_size", 2.0),
                diffusion=g("diffusion", 0.5),
                hf_damp=g("hf_damp", 5000.0),
                predelay=g("predelay", 0.0),
                plugin_uri=g("plugin_uri", ""),
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
        if cls is PatchSpaceDeviceNode:
            return cls(node_id, g("device_volume", 1.0), g("volume_locked", True))
        if cls is PatchSpaceMicDeviceNode:
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
                # Idempotent re-apply through the shared fixups (see
                # _apply_node_config) so the add and load paths can't drift.
                self._apply_node_config(existing, config)
                if isinstance(existing, SensitivityGateNode):
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
            self._wake_ticker()
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
            # Unity pass-through (the real gate now moves its own live
            # threshold - see SensitivityGateNode); kept only so the
            # routing/serialization plumbing and saved sessions don't
            # change.
            pre = VolumeProcessNode(
                pre_id,
                f"patchspace_{pre_id}",
                initial_volume=1.0,
                volume_min=1.0,
                volume_max=1.0,
            )
            self.space.add_node(pre, public=False)
            created.append(pre_id)
        if post_id not in self.space.nodes:
            post = VolumeProcessNode(
                post_id,
                f"patchspace_{post_id}",
                initial_volume=1.0,
                volume_min=1.0,
                volume_max=1.0,
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
        """The gate's threshold is a load-time filter-graph control now
        (Calf LV2 Gate, see SensitivityGateNode), so the hidden pre/post
        VolumeProcessNodes are pure unity pass-throughs - kept only so
        the routing/serialization plumbing and saved sessions don't
        change.  No-op (never raises) until both hidden nodes exist."""
        pre = self.space.nodes.get(_sens_pre_id(gate.id))
        post = self.space.nodes.get(_sens_post_id(gate.id))
        if isinstance(pre, VolumeProcessNode) and isinstance(post, VolumeProcessNode):
            pre_span = pre.volume_max - pre.volume_min
            if pre_span > 1e-9:
                pre.set_volume((1.0 - pre.volume_min) / pre_span)
            post_span = post.volume_max - post.volume_min
            if post_span > 1e-9:
                post.set_volume((1.0 - post.volume_min) / post_span)

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
        declarative: bool = False,
    ) -> str:
        stored_from, stored_to = self._stored_endpoints(from_node, to_node)
        return self.space.add_edge(
            stored_from, stored_to, to_port, from_port, declarative=declarative
        )

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

    def _cmd_record(self, cmd: dict) -> dict:
        """Start or finish a Recorder's take.

        A take is always fresh: recording deletes the node's file first and
        writes the same path, so the node's output identity never changes and
        "record" simply overwrites."""
        node_id = cmd.get("node_id")
        if not node_id:
            return {"status": "error", "message": "node_id required"}
        with self._lock:
            node = self.space.nodes.get(node_id)
            if not isinstance(node, RecorderNode):
                return {
                    "status": "error",
                    "message": f"Node {node_id} is not a recorder",
                }
            if cmd.get("recording", True):
                started = node.start()
                if not started:
                    return {
                        "status": "error",
                        "message": f"Recorder {node_id} could not start "
                                   "(its sink is not up yet?)",
                    }
            else:
                node.stop()
        return {"status": "ok", "node_id": node_id,
                "recording": node.recording, "path": node.take_path}

    def _cmd_stop_sound(self, cmd: dict) -> dict:
        """Stop whatever a Sound Player is playing (its Stop button).

        A player's children exit on their own when the file ends, so this is
        for cutting one short - the count it returns is what the GUI was
        showing, so a stop that had nothing to stop is visible as a 0."""
        node_id = cmd.get("node_id")
        if not node_id:
            return {"status": "error", "message": "node_id required"}
        node = self.space.nodes.get(node_id)
        if not isinstance(node, SoundPlayerNode):
            return {
                "status": "error",
                "message": f"Node {node_id} is not a sound player",
            }
        with self._lock:
            stopped = node.stop()
        return {"status": "ok", "stopped": stopped}

    def _cmd_stop_sound(self, cmd: dict) -> dict:
        """Stop whatever a Sound Player is playing (its Stop button).

        A player's children exit on their own when the file ends, so this is
        for cutting one short - the count it returns is what the GUI was
        showing, so a stop with nothing to stop reads as a 0."""
        node_id = cmd.get("node_id")
        if not node_id:
            return {"status": "error", "message": "node_id required"}
        node = self.space.nodes.get(node_id)
        if not isinstance(node, SoundPlayerNode):
            return {
                "status": "error",
                "message": f"Node {node_id} is not a sound player",
            }
        with self._lock:
            stopped = node.stop()
        return {"status": "ok", "stopped": stopped}

    def _cmd_impulse(self, cmd: dict) -> dict:
        """Fire one impulse out of a Button node (see PatchSpace.pulse).

        The button carries no state, so there is nothing to store: the
        command walks the impulse edges leaving the node and triggers
        every node it reaches.  The reply names them so a press that
        reached nothing is visible in the log instead of being silent."""
        node_id = cmd.get("node_id")
        if not node_id:
            return {"status": "error", "message": "node_id required"}
        node = self.space.nodes.get(node_id)
        if isinstance(node, SoundPlayerNode):
            # A player's own Play face fires *that* node (it has no impulse
            # output to pulse - the wire would be what triggers it).
            with self._lock:
                node.on_impulse(self.space.resolve_sound(node_id))
            return {"status": "ok", "fired": [node_id]}
        if not isinstance(node, ButtonNode):
            return {
                "status": "error",
                "message": f"Node {node_id} is not a button",
            }
        fired = self.space.pulse(node_id)
        if not fired:
            logger.info("Impulse from %r reached no impulse inputs", node_id)
        return {"status": "ok", "fired": fired}

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
            elif prop == "output" and isinstance(
                node, (ABSwitchNode, BooleanSourceNode)
            ):
                node.output = 1 if value else 0
            elif prop in (
                "pattern",
                "media_class",
                "description",
                "title",
                "port_type",
                "warp_name",
                "port_name",
                "default_state",
                "path",
            ):
                if hasattr(node, prop):
                    setattr(node, prop, value)
                else:
                    return {
                        "status": "error",
                        "message": f"Node has no {prop!r} property",
                    }
            elif prop in ("start", "end") and isinstance(node, ClipNode):
                # A Clip's selection, dragged on its timeline or typed into one
                # of its boxes.  ``end = None`` means "to the end of the sound",
                # which is what the box shows until it is moved.  Without this
                # branch the daemon rejected the property, so the GUI's own
                # value was overwritten by the stale one on the next poll and
                # the selection snapped straight back.
                if prop == "end" and value is None:
                    node.end = None
                else:
                    try:
                        seconds = max(0.0, float(value))
                    except (TypeError, ValueError):
                        return {
                            "status": "error",
                            "message": f"{prop} must be a number of seconds",
                        }
                    setattr(node, prop, seconds)
            elif prop == "overlap" and isinstance(node, SoundPlayerNode):
                # Retrigger behaviour (the node's Stack switch): off
                # (default) restarts the sound, on lets impulses stack
                # (see SoundPlayerNode).
                node.overlap = bool(value)
            elif prop == "invert" and isinstance(node, ClassifierNode):
                node.invert = bool(value)
            elif prop == "exclude" and isinstance(node, FilterNode):
                # The Filter node's Include/Exclude switch: on = keep
                # everything the title box / classifiers do *not* match.
                node.exclude = bool(value)
            elif prop == "force_default" and self._line_volume_target(node) is not None:
                # The line nodes share the built-in's force flag; set it on
                # the built-in and mirror it back onto every line node.
                target = self._line_volume_target(node)
                target.force_default = bool(value)
                self._mirror_line_volume(target)
                # Apply immediately rather than waiting for the throttle.
                self._default_check_at = 0.0
            elif prop == "device_name" and isinstance(node, LiveResolvableNode):
                node.device_name = value
                node.resolve_live(None, None)
                self._try_immediate_resolve(node)
            elif prop == "app_key" and isinstance(node, AppClassifierNode):
                # The Application classifier's picker value (an app key, see
                # pwmatch.app_key).
                node.app_key = value
            elif prop == "app_name" and isinstance(node, AppNameClassifierNode):
                # The Application classifier's picker value (the live app
                # node's own app_name is the branch below).
                node.app_name = value
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
                self._coalesce_reload(node)
            elif prop == "sensitivity" and isinstance(node, SensitivityGateNode):
                try:
                    new_val = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    return {"status": "error", "message": "sensitivity must be 0..1"}
                node.set_sensitivity(new_val)
                self._coalesce_reload(node)
            elif prop == "lv2_uri" and isinstance(node, SensitivityGateNode):
                node.lv2_uri = value or ""
                self._coalesce_reload(node)
            elif isinstance(node, SensitivityGateNode) and prop in (
                "ratio",
                "attack_ms",
                "release_ms",
                "knee_db",
                "makeup",
                "range_db",
            ):
                # Calf Gate tuning.  These are load-time filter-graph
                # controls too, so (like sensitivity/level above) a change
                # is clamped and then schedules a debounced interior
                # reload - a live set_param doesn't reliably reach the
                # plugin through the daemon's pw-cli session.
                try:
                    new_val = float(value)
                except (TypeError, ValueError):
                    return {
                        "status": "error",
                        "message": f"{prop} must be a number",
                    }
                lo, hi = {
                    "ratio": (node.RATIO_MIN, node.RATIO_MAX),
                    "attack_ms": (node.ATTACK_MIN_MS, node.ATTACK_MAX_MS),
                    "release_ms": (node.RELEASE_MIN_MS, node.RELEASE_MAX_MS),
                    "knee_db": (node.KNEE_MIN, node.KNEE_MAX),
                    "makeup": (node.MAKEUP_MIN, node.MAKEUP_MAX),
                    "range_db": (node.RANGE_DB_MIN, node.RANGE_DB_MAX),
                }[prop]
                setattr(node, prop, max(lo, min(hi, new_val)))
                self._coalesce_reload(node)
            elif prop == "wet_dry" and isinstance(node, ReverbNode):
                try:
                    new_val = max(0.0, min(1.0, float(value)))
                except (TypeError, ValueError):
                    return {"status": "error", "message": "wet_dry must be 0..1"}
                if abs(new_val - node.wet_dry) < 1e-9 and node.module_ok():
                    return {"status": "ok"}
                node.wet_dry = new_val
                self._coalesce_reload(node)
            elif isinstance(node, ReverbNode) and prop in (
                "plugin_uri",
                "decay_time",
                "room_size",
                "diffusion",
                "hf_damp",
                "predelay",
            ):
                if prop == "plugin_uri":
                    node.plugin_uri = value or ReverbNode.DEFAULT_URI
                else:
                    try:
                        new_val = float(value)
                    except (TypeError, ValueError):
                        return {
                            "status": "error",
                            "message": f"{prop} must be a number",
                        }
                    bounds = {
                        "decay_time": (node.DECAY_MIN_S, node.DECAY_MAX_S),
                        "room_size": (node.ROOM_MIN, node.ROOM_MAX),
                        "diffusion": (node.DIFFUSION_MIN, node.DIFFUSION_MAX),
                        "hf_damp": (node.DAMP_MIN_HZ, node.DAMP_MAX_HZ),
                        "predelay": (
                            node.PREDELAY_MIN_MS,
                            node.PREDELAY_MAX_MS,
                        ),
                    }
                    lo, hi = bounds[prop]
                    setattr(node, prop, max(lo, min(hi, new_val)))
                self._coalesce_reload(node)
            elif prop == "ladspa_plugin" and isinstance(node, NoiseCancelNode):
                node.ladspa_plugin = value
                self._coalesce_reload(node)
            elif prop == "ladspa_label" and isinstance(node, NoiseCancelNode):
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

    def _duplicate_group_id(self, nodes, exclude_gid=None):
        """Id of an existing group whose (live) member set is exactly
        ``nodes``, or None.  Groups may overlap freely; only an identical
        membership set is disallowed."""
        wanted = frozenset(n for n in nodes if n in self.space.nodes)
        for gid, group in self.groups.items():
            if gid == exclude_gid:
                continue
            if frozenset(group.get("nodes", ())) == wanted:
                return gid
        return None

    def _cmd_add_group(self, cmd: dict) -> dict:
        group_id = cmd.get("group_id")
        if not group_id:
            return {"status": "error", "message": "group_id required"}
        nodes = [n for n in cmd.get("nodes", []) if n in self.space.nodes]
        with self._lock:
            if self._duplicate_group_id(nodes) is not None:
                return {
                    "status": "error",
                    "message": "a group with exactly these nodes already exists",
                }
            self.groups[group_id] = {
                "id": group_id,
                "label": cmd.get("label", "Group"),
                "color": cmd.get("color", "#3584e4"),
                "nodes": nodes,
            }
            self._dirty = True
        return {"status": "ok", "group_id": group_id}

    def _cmd_set_group(self, cmd: dict) -> dict:
        group_id = cmd.get("group_id")
        with self._lock:
            group = self.groups.get(group_id)
            if group is None:
                return {"status": "error", "message": f"Group {group_id} not found"}
            if "nodes" in cmd:
                nodes = [
                    n for n in cmd["nodes"] if n in self.space.nodes
                ]
                if self._duplicate_group_id(nodes, exclude_gid=group_id) is not None:
                    return {
                        "status": "error",
                        "message": "another group already has exactly these nodes",
                    }
            else:
                nodes = None
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
            if nodes is not None:
                group["nodes"] = nodes
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
        # Serialize *under* the lock: the supervision tick creates/removes
        # nodes (e.g. a sensitivity gate's hidden pre/post pair) while the
        # GUI polls, and iterating space.nodes outside the lock raised
        # "dictionary changed size during iteration".  It is an RLock, so
        # nesting the serialize helpers' own acquisitions is fine.
        with self._lock:
            groups = [dict(g) for g in self.groups.values()]
            return {
                "status": "ok",
                "nodes": self._serialize_nodes(),
                "edges": self._serialize_edges(),
                "groups": groups,
                "panels": self._serialize_panels(),
                "loading": self._startup_loading,
                # The deployment's UI preference, so a client does not have to
                # be launched with the session environment for it to apply.
                "canvas_opacity": CANVAS_OPACITY,
            }

    def _cmd_get_graph(self, cmd: dict) -> dict:
        return {"status": "ok", "graph": self._serialize_graph()}

    def _cmd_get_titles(self, cmd: dict) -> dict:
        """The titles of the live streams (``media.name``) for the Title
        classifier's picker: what a player reports it is playing, which is
        exactly what that classifier matches.  Streams only - a device's
        media.name is its description, not a title."""
        titles = set()
        for node_data in self.graph.nodes().values():
            props = node_data.get("info", {}).get("props", {})
            if not str(props.get("media.class") or "").startswith("Stream/"):
                continue
            title = str(props.get("media.name") or "").strip()
            if title:
                titles.add(title)
        return {"status": "ok", "titles": sorted(titles, key=str.lower)}

    def _cmd_get_peaks(self, cmd: dict) -> dict:
        """The waveform (and length) of the sound reaching a Clip node.

        The GUI draws the timeline from this, so it is asked for when the clip's
        source changes rather than shipped in every poll.  An unwired clip, or
        a file that can't be read, answers with empty peaks - the timeline then
        draws a flat line."""
        node_id = cmd.get("node_id")
        with self._lock:
            node = self.space.nodes.get(node_id)
            if isinstance(node, RecorderNode):
                # A recorder's waveform is of its own take.
                path = node.take_path
                return {
                    "status": "ok", "node_id": node_id, "path": path,
                    "duration": pwnodes.probe_duration(path),
                    "peaks": pwnodes.probe_peaks(path),
                }
            if not isinstance(node, ClipNode):
                return {"status": "ok", "node_id": node_id, "path": "",
                        "duration": 0.0, "peaks": []}
            sound = self.space.resolve_sound(node_id, "sound")
        path = str((sound or {}).get("path") or "")
        return {
            "status": "ok",
            "node_id": node_id,
            "path": path,
            "duration": pwnodes.probe_duration(path),
            "peaks": pwnodes.probe_peaks(path),
        }

    def _cmd_get_apps(self, cmd: dict) -> dict:
        """The applications behind the live audio streams, as the desktop names
        them (``pwmatch.app_key``), for the Application classifier's picker.
        Patch Space's own streams are left out - they are plumbing, not apps."""
        keys = set()
        for node_data in self.graph.nodes().values():
            props = node_data.get("info", {}).get("props", {})
            if not str(props.get("media.class") or "").startswith("Stream/"):
                continue
            if pwmatch.is_patchspace_owned(props):
                continue
            key = pwmatch.app_key(props)
            if key:
                keys.add(key)
        return {"status": "ok", "apps": sorted(keys, key=str.lower)}

    def _cmd_get_logs(self, cmd: dict) -> dict:
        """Recent daemon log lines for the GUI console.  `since` is the
        last sequence number the caller has seen; lines with a higher
        seq are returned.  A `last_seq` lower than `since` means the
        daemon restarted, so the caller should reset."""
        try:
            since = int(cmd.get("since", 0) or 0)
        except (TypeError, ValueError):
            since = 0
        snapshot = list(_log_buffer)
        lines = [{"seq": seq, "text": text} for seq, text in snapshot if seq > since]
        last_seq = snapshot[-1][0] if snapshot else 0
        return {"status": "ok", "lines": lines, "last_seq": last_seq}

    def _cmd_export_config(self, cmd: dict) -> dict:
        return {"status": "ok", "config": self._build_export_config()}

    @staticmethod
    def _destroy_backings(backings) -> None:
        """Terminate/park every backing concurrently.

        Each ``OwnedPwNode.destroy`` can block for a couple of seconds
        waiting for its process to exit before escalating to SIGTERM then
        SIGKILL, and the naive teardown did that per node in sequence, so
        deleting a graph with several effects took the *sum* of those
        waits.  Destroying every backing at once bounds it by the single
        slowest process.  Called outside the daemon lock."""
        if not backings:
            return
        threads = [
            threading.Thread(target=b.destroy, daemon=True) for b in backings
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _teardown_public_graph(self) -> None:
        """Remove every user-visible node (and the hidden sensitivity
        children that go with a gate) plus all edges and groups, leaving
        the daemon's built-in virtual devices untouched.  Shared by reset
        and rebuild.

        The model mutation happens under the lock; the slow process
        teardown happens after it, in parallel and *without* the lock, so
        the supervision tick and other commands keep flowing while the
        old processes drain."""
        with self._lock:
            doomed_ids = [
                nid for nid in self.space.nodes if nid in self.space.public_nodes
            ]
            doomed_ids += [
                nid
                for nid in self.space.nodes
                if _hidden_sensitivity_gate_for(nid) is not None
            ]
            backings = self.space.detach_nodes(doomed_ids)
            self.groups.clear()
            self.space.sync()
        self._destroy_backings(backings)

    def _cmd_reset(self, cmd: dict) -> dict:
        self._teardown_public_graph()
        with self._lock:
            self._dirty = True
        return {"status": "ok", "message": "Graph reset"}

    def _cmd_rebuild(self, cmd: dict) -> dict:
        """Tear the PatchSpace down and rebuild it exactly as it is now -
        a user-facing "turn it off and on again" for when a node's live
        routing has gone wrong.  The current graph is captured first (so
        the rebuild is identical, layout/anchors/groups included), then
        every public node is removed (backings destroyed in parallel,
        outside the lock) and the captured config is staged back in.
        Staged on a background thread for the same reason load_session is
        (see _cmd_load_session): a multi-effect rebuild can take seconds,
        and the socket protocol is one-in-flight.  The GUI's get_nodes
        poll observes the nodes land, so the reply just says the rebuild
        started."""
        # Panel nodes are deliberately left out of the captured config: a
        # rebuild should re-derive them from their files (that is the whole
        # point), not replay the possibly-edited in-memory copies.  They are
        # brought back by _startup_load_panels() below.
        config = self._build_export_config(imperative_only=True)
        self._teardown_public_graph()
        with self._lock:
            self._dirty = True

        def _run():
            self._begin_heavy_load()
            try:
                self._load_session(config)
                # Second pass: re-derive panel nodes from disk, then wire the
                # imperative edges that target them.
                self._startup_load_panels()
            except Exception:
                logger.exception("Background rebuild failed")
            finally:
                self._end_heavy_load()

        threading.Thread(target=_run, daemon=True).start()
        return {"status": "ok", "started": True}

    def _cmd_shutdown(self, cmd: dict) -> dict:
        """Ask the daemon to exit cleanly.  Runs on the client-handler
        thread; both the socket accept loop and start()'s main loop watch
        self._running, so flipping it here begins teardown.  The reply is
        written by the caller before its socket closes, so a client can
        wait for this response rather than race the daemon's exit."""
        self._running = False
        return {"status": "ok", "message": "Shutting down"}

    def _cmd_validate_session(self, cmd: dict) -> dict:
        """Validate the current session, and optionally repair it.

        Dry run by default: returns every structural problem found
        (unknown node types, dangling edges, invalid/legacy ports,
        duplicate edges/lines, overlapping groups).  With ``apply: true``
        the repaired config is torn down and rebuilt exactly as
        ``rebuild`` does, so the live graph matches the fixed document
        (a plain re-load only adds/updates - it can't drop a node or edge
        the repair removed).  ``collapse_duplicate_lines: true`` also
        folds redundant built-in line nodes, keeping the busiest one.
        """
        config = self._build_export_config()
        result = session_repair.repair(
            config,
            known_types=set(NODE_TYPE_REGISTRY),
            collapse_duplicate_lines=bool(cmd.get("collapse_duplicate_lines", False)),
            drop_orphans=bool(cmd.get("drop_orphans", False)),
            dedupe_groups=bool(cmd.get("dedupe_groups", False)),
        )
        payload = {
            "status": "ok",
            "issues": [
                {
                    "severity": issue.severity,
                    "code": issue.code,
                    "where": issue.where,
                    "message": issue.message,
                }
                for issue in result.issues
            ],
            "fixes": result.fixes,
            "applied": False,
        }
        if cmd.get("apply") and result.fixes:
            fixed = result.config
            self._teardown_public_graph()
            with self._lock:
                self._dirty = True

            def _run():
                try:
                    self._load_session(fixed)
                except Exception:
                    logger.exception("Background session repair failed")

            threading.Thread(target=_run, daemon=True).start()
            payload["applied"] = True
        return payload

    # ------------------------------------------------------------------
    # serialization
    # ------------------------------------------------------------------

    def _serialize_panels(self) -> list:
        """Panel metadata for get_nodes.  Panel placement is stored on the
        daemon; a panel's on-canvas bounds are derived by the GUI from its
        member nodes (the box auto-fits, with an explicit min w/h)."""
        with self._lock:
            return [
                {
                    "id": pid,
                    "parent": panel.parent or "",
                    "label": panel.label,
                    "color": panel.color,
                    "mode": panel.mode,
                    "readonly": panel.is_readonly,
                    "writable": panel.writable,
                    "x": panel.x,
                    "y": panel.y,
                    "w": panel.w,
                    "h": panel.h,
                    "anchored": panel.anchored,
                    "stem": panel.stem,
                    "auto_load": panel.auto_load,
                    "edit_mode": pid in self._edit_panels,
                    "path": panel.path,
                    "children": panel.child_ids(),
                }
                for pid, panel in self.panels.items()
            ]

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
            if isinstance(node, BoolControlledMixin):
                # A gate/switcher whose boolean "ctrl" input is wired has
                # its state set by that signal, not its own stored
                # default.  Surface both so the GUI can draw the on/off
                # switch read-only (white) showing the value actually in
                # effect on the node.
                data["bool_driven"] = node.bool_driven
                data["bool_state"] = node.bool_state
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
            if isinstance(node, SoundPlayerNode):
                data["playing"] = node.playing
            if isinstance(node, ClipNode):
                # What the clip is showing: the GUI re-asks for the waveform
                # (get_peaks) whenever this changes.
                sound = self.space.resolve_sound(node_id, "sound") or {}
                data["source_path"] = str(sound.get("path") or "")
                # A clip's own times are seconds into *what it is given*, so
                # the timeline needs that sound's start to place them on the
                # file's waveform.  Zero unless clips are stacked.
                data["source_start"] = float(sound.get("start") or 0.0)
                data["duration"] = pwnodes.probe_duration(sound.get("path") or "")
            if isinstance(node, RecorderNode):
                # What the GUI shows: whether a take is running, and the file
                # (and length) of the last one, for its waveform.
                data["recording"] = node.recording
                data["source_path"] = node.take_path
                data["duration"] = node.duration
            if isinstance(node, SoundNode):
                # The file's length, so the node can show it and the Clip
                # timeline knows how much sound there is to select from.
                data["duration"] = node.duration
            if isinstance(node, FilterNode):
                # One dynamic filter socket per wired classifier plus a
                # spare, so a single Filter can hold many classifiers.
                data["filter_inputs"] = self.space.filter_input_ports(node_id)
            if isinstance(node, BundleMergeNode):
                # One dynamic input socket per plugged-in line plus a
                # spare, so the GUI grows a socket each time one is used.
                data["bundle_inputs"] = self.space.bundle_input_ports(node_id)
            if isinstance(node, BundleSplitNode):
                # One dynamic output socket per live member, so the GUI can
                # draw a line per stream (port key = the member's node.name;
                # label is what to show beside it).
                data["bundle_members"] = self.space.bundle_members(node_id)
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
            elif command == "record":
                response = self._cmd_record(cmd)
            elif command == "stop_sound":
                response = self._cmd_stop_sound(cmd)
            elif command == "stop_sound":
                response = self._cmd_stop_sound(cmd)
            elif command == "impulse":
                response = self._cmd_impulse(cmd)
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
            elif command == "get_peaks":
                response = self._cmd_get_peaks(cmd)
            elif command == "get_apps":
                response = self._cmd_get_apps(cmd)
            elif command == "get_titles":
                response = self._cmd_get_titles(cmd)
            elif command == "get_logs":
                response = self._cmd_get_logs(cmd)
            elif command == "export_config":
                response = self._cmd_export_config(cmd)
            elif command == "load_session":
                response = self._cmd_load_session(cmd)
            elif command == "reload_panels":
                response = self._cmd_reload_panels(cmd)
            elif command == "list_panels":
                response = self._cmd_list_panels(cmd)
            elif command == "set_panel_layout":
                response = self._cmd_set_panel_layout(cmd)
            elif command == "reset_panel":
                response = self._cmd_reset_panel(cmd)
            elif command == "set_panel_edit_mode":
                response = self._cmd_set_panel_edit_mode(cmd)
            elif command == "move_nodes":
                response = self._cmd_move_nodes(cmd)
            elif command == "move_panel":
                response = self._cmd_move_panel(cmd)
            elif command == "create_panel":
                response = self._cmd_create_panel(cmd)
            elif command == "delete_panel":
                response = self._cmd_delete_panel(cmd)
            elif command == "delete_panel_file":
                response = self._cmd_delete_panel_file(cmd)
            elif command == "edit_panel":
                response = self._cmd_edit_panel(cmd)
            elif command == "export_panel":
                response = self._cmd_export_panel(cmd)
            elif command == "clone_panel":
                response = self._cmd_clone_panel(cmd)
            elif command == "list_panel_files":
                response = self._cmd_list_panel_files(cmd)
            elif command == "set_panel_file_autoload":
                response = self._cmd_set_panel_file_autoload(cmd)
            elif command == "place_panel":
                response = self._cmd_place_panel(cmd)
            elif command == "remove_panel_placement":
                response = self._cmd_remove_panel_placement(cmd)
            elif command == "connect_ports":
                response = self._cmd_connect_ports(cmd)
            elif command == "disconnect_ports":
                response = self._cmd_disconnect_ports(cmd)
            elif command == "reset":
                response = self._cmd_reset(cmd)
            elif command == "rebuild":
                response = self._cmd_rebuild(cmd)
            elif command == "shutdown":
                response = self._cmd_shutdown(cmd)
            elif command in ("validate_session", "repair_session"):
                response = self._cmd_validate_session(cmd)
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

    def _bind_socket(self) -> None:
        """Replace any stale socket file and listen on SOCKET_PATH.

        Raises OSError if the path cannot be taken (see `start`).  A stale
        file whose owner is gone is safe to unlink; a *live* listener is not
        - `start`'s single-instance probe has already refused that case."""
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(5)
        os.chmod(SOCKET_PATH, 0o666)
        self._server = server
        logger.info("Listening on %s", SOCKET_PATH)

    def _close_socket(self) -> None:
        server = self._server
        self._server = None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        if os.path.exists(SOCKET_PATH):
            try:
                os.unlink(SOCKET_PATH)
            except OSError:
                pass

    def _serve_clients(self) -> None:
        server = self._server
        if server is None:
            return
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
            self._close_socket()

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
    import argparse

    # SOCKET_PATH is read here (as the flag's default) and reassigned below,
    # so the global declaration has to come first in this scope.
    global SOCKET_PATH

    parser = argparse.ArgumentParser(
        prog="patchspace-daemon", description="Patch Space daemon"
    )
    parser.add_argument(
        "--socket",
        default=SOCKET_PATH,
        metavar="PATH",
        help="Unix socket to serve the command API on (default: "
        "$PATCHSPACE_SOCKET, else $XDG_RUNTIME_DIR/patchspace.sock, else "
        "/tmp/patchspace.sock)",
    )
    parser.add_argument(
        "--panel-dir",
        action="append",
        default=None,
        metavar="PATH[:rw|:ro]",
        help="Directory to load panel files from (repeatable; later dirs "
        "shadow earlier).  Suffix :ro marks it unwritable.",
    )
    parser.add_argument(
        "--root-panel",
        default=DEFAULT_ROOT_PANEL,
        help="Path of the root panel file (session autosave).",
    )
    args = parser.parse_args()

    # The server and the single-instance guard read SOCKET_PATH straight off
    # the module global, so the flag lands there (declared above).
    SOCKET_PATH = args.socket

    panel_dirs = None
    if args.panel_dir:
        panel_dirs = []
        for spec in args.panel_dir:
            path, _, mode = spec.rpartition(":")
            if mode not in ("rw", "ro") or not path:
                path, mode = spec, "rw"
            panel_dirs.append((path, mode == "rw"))

    _install_log_ring()
    daemon = PatchSpaceDaemon(
        panel_dirs=panel_dirs,
        root_panel_path=args.root_panel,
    )
    try:
        daemon.start()
    finally:
        daemon.stop()
    # `start` returns without ever running when it refused (another daemon
    # owns the graph, or the socket cannot be served).  Exit non-zero so the
    # supervisor reports it instead of calling the exit a success.
    if not daemon.started:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
