"""
pwnodes.py

The patch graph: a Blender-style node graph of Input / Process / Output
nodes that the daemon drives against a live pwgraph.PipewireGraph.

This module is a ground-up rewrite of the original patchSpace.py with
one idea at its centre: **every real PipeWire object a node owns is
supervised**.  The graph (PatchSpace) reconciles two things on every
tick:

  1. *structure* - the desired user-edited graph: which edges should be
     connected, what the stable sockets of every node are;
  2. *health* - for every backed node, that the processes owning its
     real objects are still alive, restarting anything that died (with
     bounded backoff so a permanently-broken node - a missing plugin,
     say - is retried periodically instead of thrashed every tick).

Why loading is deliberately lazy
--------------------------------
Creating an effect used to mean "load the DSP module right now", and a
module load is the single most failure-prone step in the whole program
(missing plugin, store not yet mounted, a module that rejects its args).
The rewrite decouples the two:

  * the *structural* pieces of a node - the plain null-audio-sink
    adapter sockets and the keepalive streams - are created
    synchronously when the node is added.  These basically cannot fail,
    and they are what user edges attach to, so adding a node gives you
    sockets immediately and never fails because of a DSP problem;
  * the *module* (the actual filter-chain / echo-cancel engine, the
    piece that can fail) is materialised by the supervision tick.  A
    module that fails to load leaves the node healthy-but-silent and is
    retried with backoff; fixing the config (or installing the plugin)
    is picked up on the very next attempt, no restart required.

This is what "loading nodes shouldn't cause as many failures as it
does" means concretely: a load failure now degrades one node's interior,
never the add-node operation, never the whole sync, and never the rest
of the chain.

Effect plumbing
---------------
Every effect is a *sandwich*:

    real source -> [ in-dummy sink ] --link--> [ fx capture ] <-> DSP <-> [ fx playback ] --link--> [ out-dummy sink ] -> real sink

The two dummy sinks are plain, stable, always-running null-audio-sink
adapters - identical objects to every splitter/volume in the graph.
User edges plug into the dummies, never into the DSP module, so a module
reload (config change or crash recovery) only ever swaps the interior
and can never drop or renegotiate a user edge.  The internal links
between dummies and module are re-derived by name on every sync, so they
self-heal across every reload for free.

Echo-cancel is the same sandwich idea with three sockets (mic / probe /
out) and one extra hidden playback drain, because its module really has
four coordinated streams sharing one AEC state.

Node taxonomy
-------------
  * InputNode / OutputNode        - leaves that match external nodes.
  * LiveResolvableNode            - leaf that names an external device
                                    or app; resolves to a live id when
                                    the thing is present.
  * TransparentNode               - pure pass-through (gate / exclude);
                                    contributes no identity of its own.
  * BackedNode                    - owns one or more real PipeWire
                                    objects (see above for the contract).
"""

from __future__ import annotations

import array
import logging
import os
import re
import subprocess
import tempfile
import threading
import time as _time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Set, Tuple

import pwmatch
from pwgraph import PipewireGraph
from pwproc import Backoff, OwnedPwNode, OwnedPwProcess

logger = logging.getLogger(__name__)

NodeId = str
EdgeId = str

# PipeWire appends a numeric suffix (".99") to node.name when another
# object with the same name shows up, so a hardware device can reappear
# under a slightly different name than the one a Device Input/Output
# node stored.  Comparing suffix-stripped base names lets the daemon
# re-adopt the live name instead of silently going dead (no links, no
# volume) until the user re-selects the device.
_NAME_SUFFIX_RE = re.compile(r"\.\d+$")


def _base_node_name(name: str) -> str:
    return _NAME_SUFFIX_RE.sub("", name or "")

# Reserved backing names for the daemon's built-in virtual sink/mic.
# They are ordinary supervised VirtualSpeaker/VirtualMic nodes (added
# hidden, see PatchSpace.add_node(public=...)) so they get exactly the
# same supervision as any user-created device.  GUI convenience nodes
# (patchspace_device / patchspace_mic_device) reference these names.
PATCHSPACE_VIRTUAL_SINK_NAME = "Patch Space"
PATCHSPACE_VIRTUAL_MIC_NAME = "Patch Space Mic"

# How long a single in-flight link may go unconfirmed by the live graph
# before PatchSpace gives up waiting on it and moves to the next one.
# Wiring is deliberately paced one link at a time (see
# PatchSpace._apply_desired_links): creating a whole reconcile's worth
# of ports in one burst races multi-stream effect nodes, whose capture
# and playback ports (echo/noise cancel filter chains) appear
# asynchronously - a burst could attach user edges before the DSP
# sandwich existed and leave the node half-wired.  This bounds the wait
# so one port that vanished before its link landed can't stall the
# reconcile forever.
LINK_CONFIRM_TIMEOUT_S = 3.0


def _run_wpctl(*args, timeout: float = 2.0) -> bool:
    """Best-effort `wpctl <args>`.  Returns whether it succeeded; failures
    are retried on a later tick anyway.  A value that fails to apply now
    (e.g. a Bluetooth profile switch during transport setup) must not be
    recorded as applied, so callers that gate on the result can retry."""
    try:
        result = subprocess.run(
            ["wpctl", *[str(a) for a in args]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0
    except Exception as exc:
        logger.warning("wpctl %s failed: %s", args, exc)
        return False


def _read_wpctl_volume(node_id) -> Optional[float]:
    """Live volume of a device/node via `wpctl get-volume`, or None when
    it can't be read.  Used to keep an *unlocked* slider in step with
    changes made outside Patch Space (pactl/wpctl/a desktop applet) instead
    of fighting them."""
    try:
        result = subprocess.run(
            ["wpctl", "get-volume", str(node_id)],
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    match = re.search(r"volume:\s*([0-9]*\.?[0-9]+)", result.stdout, re.IGNORECASE)
    if match is None:
        return None
    try:
        return max(0.0, min(1.0, float(match.group(1))))
    except ValueError:
        return None


def device_profile_name(device_obj: Optional[dict]) -> Optional[str]:
    """The currently-active profile name of a pipewire Device snapshot
    (``info.params.Profile``), or None when it can't be read.  A device
    stuck on the ``"off"`` profile exports no Audio/Sink or Audio/Source
    node at all, so nothing can be routed to or from it."""
    if not device_obj:
        return None
    params = device_obj.get("info", {}).get("params", {}) or {}
    profile = params.get("Profile")
    if isinstance(profile, list):
        profile = profile[0] if profile else None
    if isinstance(profile, dict):
        return profile.get("name")
    return None


def _profile_available(entry: dict) -> bool:
    avail = entry.get("available")
    return avail is True or str(avail).lower() in ("yes", "true")


def pick_auto_a2dp_profile(
    device_obj: Optional[dict], is_output: bool
) -> Optional[Tuple[int, str]]:
    """The best available A2DP profile for a Bluetooth device, or None.

    Sinks want ``a2dp-sink*`` (high-quality playback); a source node
    falls back to ``headset-head-unit`` (the HFP mic) when the device
    exposes no A2DP source.  Highest ``priority`` wins."""
    if not device_obj:
        return None
    params = device_obj.get("info", {}).get("params", {}) or {}
    profiles = params.get("EnumProfile") or []
    if isinstance(profiles, dict):
        profiles = [profiles]
    wanted = "a2dp-sink" if is_output else "a2dp-source"

    def candidates(substr: str):
        return [
            p for p in profiles
            if _profile_available(p) and substr in (p.get("name") or "")
        ]

    cands = candidates(wanted)
    if not cands and not is_output:
        cands = candidates("headset-head-unit")
    if not cands:
        return None
    best = max(cands, key=lambda p: p.get("priority", 0))
    index = best.get("index")
    name = best.get("name")
    if index is None or not name:
        return None
    return int(index), name


def _db_to_linear(db: float) -> float:
    return 10.0 ** (db / 20.0)


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


_NIX_STORE_CACHE: Dict[Tuple[str, str], Optional[str]] = {}


def _store_ladspa_candidate(prefix: str, rel_path: str) -> Optional[str]:
    """Look up (once, cached) whether /nix/store contains a
    ``<prefix>-*`` output with ``rel_path`` under it - e.g. an
    rnnoise-plugin install.  The store is where `nix develop` /
    `nix profile` put packages, so this is how a plugin a dev shell adds
    becomes discoverable without hardcoding a store hash."""
    key = (prefix, rel_path)
    if key in _NIX_STORE_CACHE:
        return _NIX_STORE_CACHE[key]
    found = None
    try:
        for entry in os.scandir("/nix/store"):
            # Store paths are <hash>-<pkgname>-<version>...; match on the
            # package-name part after the first dash, not the whole path.
            if ("-" + prefix) not in entry.name:
                continue
            candidate = os.path.join(entry.path, rel_path)
            if os.path.isfile(candidate):
                found = candidate
                break
    except OSError:
        pass
    _NIX_STORE_CACHE[key] = found
    return found


# ---------------------------------------------------------------------------
# Node base classes
# ---------------------------------------------------------------------------


class Node:
    """Anything that can sit in the graph."""

    # -- GUI layout state ------------------------------------------------
    # Purely presentational, but owned by the daemon so it round-trips
    # through get_nodes / export / the auto-saved last-session cache:
    # where the node sits on the canvas and whether the force layout
    # pins it.  None means "the GUI hasn't told us yet" (a freshly
    # created node), which lets the GUI keep choosing a default slot
    # instead of snapping everything to (0, 0).
    x: Optional[float] = None
    y: Optional[float] = None
    anchored: Optional[bool] = None

    def __init__(self, node_id: NodeId):
        self.id = node_id
        # True when the daemon created this node from a declarative file.
        # Declarative nodes/edges are never written back to the imperative
        # autosave; they are re-derived from their files on start / rebuild
        # / file change (see main.py's declarative handling).
        self.declarative = False

    def is_transparent(self) -> bool:
        return False

    def port_kind(self, port: str, direction: str) -> str:
        """The kind of signal one of this node's ports carries:
        ``"audio"`` (the default for every ordinary node) or
        ``"boolean"`` (a control signal - see BooleanSourceNode /
        BooleanSplitterNode / BoolControlledMixin).  Only same-kind
        ports may be wired together; boolean edges never become
        PipeWire links, they just drive a node's pass/select state."""
        return "audio"

    # -- serialization support: the config fields this node exposes ----
    def config_fields(self) -> Dict[str, Any]:
        """Canonical dict of this node's user-facing config, used for
        export and for the daemon's set_node_property validation.  The
        GUI reads these same keys back off get_nodes, so keeping one
        source of truth here prevents drift."""
        out: Dict[str, Any] = {}
        for attr in ("pattern", "media_class", "description", "device_name",
                     "app_name", "device_label", "device_volume",
                     "profile_index", "profile_description", "label", "output"):
            if hasattr(self, attr):
                out[attr] = getattr(self, attr)
        return out


class InputNode(Node):
    def source_filters(self) -> List[dict]:
        raise NotImplementedError


class OutputNode(Node):
    def sink_filters(self) -> List[dict]:
        raise NotImplementedError


class BoolControlledMixin:
    """A transparent node whose pass/select state is driven by a boolean
    signal on its ``BOOLEAN_INPUT`` port instead of an inline button.

    ``_bool_effective`` is filled in by the owning PatchSpace on every
    sync: the resolved boolean value of whatever source is wired into
    the input, or ``None`` when nothing is wired (fall back to the
    node's own stored default - so an unwired gate still passes, an
    unwired switcher still picks "a")."""

    BOOLEAN_INPUT = "ctrl"

    def _init_bool_control(self) -> None:
        self._bool_effective: Optional[bool] = None

    def port_kind(self, port: str, direction: str) -> str:
        if direction == "in" and port == self.BOOLEAN_INPUT:
            return "boolean"
        return "audio"

    def effective_bool(self, default: bool) -> bool:
        return self._bool_effective if self._bool_effective is not None else default

    @property
    def bool_driven(self) -> bool:
        """True while a boolean signal is actually wired into this node's
        input (so PatchSpace resolved an effective value).  Serialized so
        the GUI can draw the node's on/off switch non-interactively and
        show the value being driven into it."""
        return self._bool_effective is not None

    @property
    def bool_state(self) -> Optional[bool]:
        """The resolved boolean driving this node, or None when nothing
        is wired (the stored default applies instead)."""
        return self._bool_effective


class TransparentNode(Node):
    def is_transparent(self) -> bool:
        return True

    def gate_open(self) -> bool:
        return True

    def passes_output(self, from_port: str) -> bool:
        """Whether an edge leaving `from_port` should carry audio.  Only
        the Switcher overrides this; every other transparent node has a
        single output, so anything routed through it passes."""
        return True

    def allows_multiple_inputs(self) -> bool:
        """Whether this transparent node may have more than one inbound
        edge.  Ordinary pass-throughs (gate, exclude, switcher) take a
        single upstream; an input switch (InverseSwitcherNode) takes one
        per selectable input."""
        return False

    def select_upstream(self, upstream: List["Edge"]) -> Optional["Edge"]:
        """Which inbound edge feeds this node's output.  A single-input
        transparent node has at most one, so the default is unambiguous;
        InverseSwitcherNode picks by its selected input port."""
        return upstream[0] if upstream else None


class LiveResolvableNode:
    """Mixin for a node that references an external object by a
    persistent identity (device node.name / app application.name).  The
    identity survives the object's absence; resolve_live(None) is normal,
    not an error."""

    def __init__(self):
        self.live_node_id: Optional[int] = None
        self.live_props: dict = {}

    def matches_live_node(self, props: dict) -> bool:
        raise NotImplementedError

    def resolve_live(self, node_id, props) -> None:
        self.live_node_id = node_id
        self.live_props = dict(props) if props else {}
        self._on_live_resolved()

    def _on_live_resolved(self) -> None:
        """Hook: a live device (re)resolved.  Subclasses push any
        config that must be enforced on the live object."""
        if hasattr(self, "apply_device_settings"):
            self.apply_device_settings()


class DeviceControlMixin:
    """Persisted, continuously-re-enforced device volume + profile
    (Bluetooth codec) config.  Values live on the node, so they
    round-trip through export/import; apply_device_settings() re-pushes
    them whenever the device (re)resolves and on every supervision tick,
    so a Bluetooth reconnect resetting the codec gets corrected.

    ``volume_locked`` (default True) gates the volume half of that:
    locked means the Patch Space value is authoritative and re-asserted
    every tick, overriding anything else that changed the device; a
    one-off push still happens on an explicit user drag regardless.
    Unlocked leaves the device's own volume alone and mirrors it back
    (sync_volume_from_live) so the slider follows external changes."""

    def __init__(
        self,
        device_volume: float = 1.0,
        profile_index: Optional[int] = None,
        profile_description: str = "",
        volume_locked: bool = True,
    ):
        self.device_volume = device_volume
        self.profile_index = profile_index
        self.profile_description = profile_description
        self.volume_locked = bool(volume_locked)
        # (device_id, profile_index) last successfully applied - profile
        # changes visibly restart the device, so never re-issue an
        # identical one (see apply_device_settings).
        self._applied_profile: Optional[Tuple[Any, int]] = None

    def apply_device_settings(self, push_volume: Optional[bool] = None) -> None:
        """Re-apply the configured volume/profile.  ``push_volume``
        forces (True) or suppresses (False) the volume push; the default
        (None) pushes only while locked.  Profile is always re-asserted
        when it differs from what was last applied."""
        live_node_id = getattr(self, "live_node_id", None)
        if live_node_id is not None and (
            push_volume is True
            or (push_volume is None and getattr(self, "volume_locked", True))
        ):
            _run_wpctl("set-volume", live_node_id, self.device_volume)
        device_id = getattr(self, "live_props", {}).get("device.id")
        if device_id is None or self.profile_index is None:
            return
        target = (device_id, self.profile_index)
        if self._applied_profile == target:
            return
        if _run_wpctl("set-profile", device_id, self.profile_index):
            self._applied_profile = target

    def sync_volume_from_live(self) -> None:
        """While unlocked, adopt the live volume so the slider reflects
        changes made outside Patch Space instead of fighting them."""
        if getattr(self, "volume_locked", True):
            return
        live_node_id = getattr(self, "live_node_id", None)
        if live_node_id is None:
            return
        volume = _read_wpctl_volume(live_node_id)
        if volume is not None:
            self.device_volume = volume


class BackedNode(Node):
    """A process node that owns real PipeWire objects for as long as it
    exists.

    Contract with PatchSpace's supervision tick:

      * ``structural_ok()`` / ``module_ok()`` report health cheaply;
      * ``ensure_structural()`` creates whatever structural pieces are
        missing (idempotent, name-based) - this is what add_node runs
        synchronously and what the tick re-runs to repair deaths;
      * effects additionally implement ``ensure_module()`` (create the
        DSP module if it never came up) and ``reload_module()`` (swap an
        existing module for a fresh one - config change / crash).

    ``backings`` is the flat list of OwnedPwNode objects the node is
    responsible for.  ``_reload_due`` is set by set_node_property for
    load-time-only options; the tick performs the reload."""

    def __init__(self, node_id: NodeId, backing_node_name: str):
        super().__init__(node_id)
        self.backing_node_name = backing_node_name
        self.backings: List[OwnedPwNode] = []
        self._reload_due: Optional[float] = None
        # Defaults for the shared backing spawn helpers; effect subclasses
        # override them with their real pw-cli command/settle.
        self._pw_cli_command = ("pw-cli",)
        self._settle = 0.3

    # How long a backing gets to show up as a live PipeWire object,
    # once its owning process is confirmed running, before we give up
    # waiting and treat it the same as a crash (see OwnedPwNode.stuck).
    # Deliberately longer than SESSION_LOAD_NODE_TIMEOUT_S's 5s bring-up
    # window so a plugin that's merely slow to initialize on its first
    # load isn't mistaken for one that will never come up - this only
    # fires for a backing supervise() keeps seeing on tick after tick.
    RESOLVE_GRACE_S = 8.0

    # -- identities ------------------------------------------------------

    def input_identity(self, port: str = "in") -> dict:
        return {"name": self.backing_node_name}

    def output_identity(self) -> dict:
        # Exact node.name match ("nodeName") - node.name is unique, a
        # loose "name" substring would select any sibling whose name
        # merely contains ours.
        return {"nodeName": self.backing_node_name}

    def internal_links(self) -> List[Tuple[dict, dict]]:
        return []

    def has_module(self) -> bool:
        return False

    def module_backing(self) -> Optional[OwnedPwNode]:
        return None

    # -- health ----------------------------------------------------------

    def dead_backings(self) -> List[OwnedPwNode]:
        """Process-owning backings that need to be torn down and
        recreated: ones whose process exited on its own, AND ones that
        are still running but have been alive long enough that they
        should have resolved in the live graph by now and never did
        (see OwnedPwNode.stuck) - a stuck backing is functionally dead
        even though nothing crashed."""
        return [
            b
            for b in self.backings
            if b.owns_process and (not b.is_alive or b.stuck(self.RESOLVE_GRACE_S))
        ]

    def structural_ok(self) -> bool:
        raise NotImplementedError

    def module_ok(self) -> bool:
        return True

    # -- lifecycle -------------------------------------------------------

    def ensure_structural(self) -> None:
        raise NotImplementedError

    def ensure_module(self) -> None:
        pass

    def reload_module(self) -> None:
        self.ensure_module()

    def schedule_reload(self) -> None:
        """Mark the interior module for reload on the next supervision
        pass; the daemon debounces the exact timing."""
        self._reload_due = _time.monotonic()

    def owned_backings(self) -> List[OwnedPwNode]:
        """Hand over every backing this node owns and forget them, so the
        caller can destroy them outside this lock.

        A node's *structural* backings are the ones it must keep alive
        (see ``backings``); a subclass may also own ephemeral children it
        deliberately keeps out of that list because a natural exit is not
        a fault - SoundEffectNode's playback streams, which end every
        time a sound finishes and must therefore not count toward
        ``dead_backings()``/readiness.  Everything the node owns has to
        die with it either way, so both the single-node and the batched
        teardown path go through here."""
        backings, self.backings = self.backings, []
        return backings

    def teardown_backing(self) -> None:
        """Destroy every owned backing at once instead of one at a time.

        Each backing is an independent OS-level thing (its own pw-cli/
        pw-cat subprocess, or none) - destroying them has no ordering
        requirement the way *creating* them does, so serializing it was
        pure waste. ``OwnedPwNode.destroy()`` can block for a couple of
        seconds in the worst case (an in-band ``destroy``/``quit`` that
        doesn't land cleanly escalates to SIGTERM then SIGKILL, each with
        its own wait), and a single effect node can own eight-plus
        backings (dummies + their keepalives + the module's own
        streams) - one at a time that's "the whole chain takes forever
        to delete"; concurrently it's bounded by the single slowest
        destroy instead of their sum."""
        backings = self.owned_backings()
        if len(backings) <= 1:
            for owned in backings:
                owned.destroy()
            return
        threads = [threading.Thread(target=b.destroy, daemon=True) for b in backings]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def resolve_backing(self, name: str, node_id: int) -> bool:
        for owned in self.backings:
            if owned.node_id is None and owned.name == name:
                owned.resolve(node_id)
                return True
        return False

    # -- shared spawn helpers ---------------------------------------------

    def _find(self, name: str) -> Optional[OwnedPwNode]:
        for b in self.backings:
            if b.name == name:
                return b
        return None

    def _drop(self, name: str) -> None:
        b = self._find(name)
        if b is not None:
            b.destroy()
            self.backings.remove(b)

    def _prune_dead(self, names: Optional[Set[str]] = None) -> None:
        """Drop (and destroy) process-owning backings whose process died
        or that are stuck (alive but never resolved - see
        OwnedPwNode.stuck), optionally restricted to a name set."""
        for owned in list(self.backings):
            if owned.owns_process and (
                not owned.is_alive or owned.stuck(self.RESOLVE_GRACE_S)
            ):
                if names is None or owned.name in names:
                    owned.destroy()
                    if owned in self.backings:
                        self.backings.remove(owned)

    def _spawn_cli(self, name: str, create_line: str,
                   pw_cli_command=("pw-cli",), settle: float = 0.3) -> Optional[OwnedPwNode]:
        existing = self._find(name)
        if existing is not None:
            return existing
        owned = OwnedPwNode(name, pw_cli_command, settle)
        if not owned.create(create_line):
            logger.error("Creating %r failed for %r", name, self.id)
            return None
        self.backings.append(owned)
        return owned

    def _ensure_null_sink(self, name: str, extra_props: str = "",
                          description: Optional[str] = None,
                          pw_cli_command=("pw-cli",), settle: float = 0.3,
                          drop_keepalive: bool = True,
                          media_class: str = "Audio/Sink") -> Optional[OwnedPwNode]:
        """Create one plain null-audio-sink adapter named ``name`` if it
        is not already among self.backings.  When it has to be created
        the old one is gone, so any keepalive aimed at it is aimed at a
        vanished target and is dropped too (a pw-cat client does not
        re-link on its own).

        ``media_class`` is normally Audio/Sink (a Pulse-visible sink).
        Purely internal plumbing passes ``pwmatch.INTERNAL_MEDIA_CLASS``
        instead, which keeps the same ports and routing while staying
        invisible to pipewire-pulse clients like Discord."""
        existing = self._find(name)
        if existing is not None:
            return existing
        desc = description or name
        config = (
            "factory.name=support.null-audio-sink "
            f'node.name="{name}" '
            f'node.description="{desc}" '
            f"media.class={media_class} "
            "audio.position=[FL,FR]" + (f" {extra_props}" if extra_props else "")
        )
        owned = self._spawn_cli(name, f"create-node adapter {config}",
                                pw_cli_command, settle)
        if owned is None:
            return None
        if drop_keepalive:
            self._drop(f"{name}_keepalive")
        return owned

    # A keepalive is started right after the sink it targets is
    # requested, and the link that attaches it can lose the buffer-
    # allocation race while the target node's ports are still coming up
    # (PipeWire's impl-link sets the link to ERROR with "Buffer
    # allocation failed", which the pw-cat client reports and then
    # exits).  This is a transient start-up race, not a bad command, so
    # retry a few times with a short gap before declaring failure.
    _KEEPALIVE_ATTEMPTS = 3
    _KEEPALIVE_RETRY_DELAY_S = 0.4

    def _create_keepalive(self, name: str, command, pw_cli_command, settle: float,
                          label: str) -> Optional[OwnedPwProcess]:
        last = self._KEEPALIVE_ATTEMPTS - 1
        for attempt in range(self._KEEPALIVE_ATTEMPTS):
            if attempt:
                _time.sleep(self._KEEPALIVE_RETRY_DELAY_S)
            proc = OwnedPwProcess(name, pw_cli_command, settle)
            if proc.create(command, quiet=attempt < last):
                self.backings.append(proc)
                return proc
        logger.error("Keepalive %s %r failed to start for %r", label, name, self.id)
        return None

    def _ensure_feed(self, name: str, target: str,
                     pw_cli_command=("pw-cli",), settle: float = 0.3) -> Optional[OwnedPwNode]:
        """A silent pw-cat --playback stream permanently feeding
        ``target``, so that sink never drops to zero active links (and
        therefore never gets suspended).

        --properties pins node.name to ``name`` - without it pw-cat
        registers under its own default stream name, which is never
        what ``name`` says it is. That mismatch meant node_id_by_name
        could never find this backing, so it never resolved a node_id -
        which made _node_is_ready() (all backings resolved) return
        False forever for every node with a keepalive, working or not,
        and made stuck() (see OwnedPwNode) misdiagnose a perfectly
        healthy keepalive as broken and tear it down on a loop."""
        existing = self._find(name)
        if existing is not None:
            return existing
        command = (
            "pw-cat", "--playback", "--volume", "0", "--target", target,
            "--properties", f'{{ node.name = "{name}" node.description = "{name}" }}',
            "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
            "/dev/zero",
        )
        return self._create_keepalive(name, command, pw_cli_command, settle, "feed")

    def _ensure_drain(self, name: str, target: str,
                     pw_cli_command=("pw-cli",), settle: float = 0.3) -> Optional[OwnedPwNode]:
        """Output-side mirror of _ensure_feed: a silent pw-cat --record
        stream permanently draining ``target`` (a null-audio-sink dummy,
        so its monitor never has zero consumers and it can't be suspended
        out from under the effect).

        ``stream.capture.sink = true`` is what makes --target a *sink*
        actually capture that sink's monitor.  Without it PipeWire can't
        satisfy a record targeting a sink and the stream silently falls
        back to the default source - measured live as every effect's
        ``*_out_keepalive`` tapping ``Patch Space Mic`` instead of its own
        dummy (see VirtualMicNode._spawn_loopback, which relies on the
        same property).  Same --properties reasoning as _ensure_feed
        above."""
        existing = self._find(name)
        if existing is not None:
            return existing
        command = (
            "pw-cat", "--record", "--target", target,
            "--properties",
            f'{{ node.name = "{name}" node.description = "{name}" '
            "stream.capture.sink = true }",
            "--raw", "--format", "s16", "--rate", "48000", "--channels", "2",
            "/dev/null",
        )
        return self._create_keepalive(name, command, pw_cli_command, settle, "drain")


# ---------------------------------------------------------------------------
# Leaves: filters, devices, apps, patchspace conveniences
# ---------------------------------------------------------------------------


class RegexInputNode(InputNode):
    def __init__(self, node_id, pattern: str = ""):
        super().__init__(node_id)
        self.pattern = pattern

    def source_filters(self):
        return [{"nameRegex": self.pattern}] if self.pattern else []


class MediaClassInputNode(InputNode):
    def __init__(self, node_id, media_class: str = ""):
        super().__init__(node_id)
        self.media_class = media_class

    def source_filters(self):
        return [{"mediaClass": self.media_class}] if self.media_class else []


class DescriptionInputNode(InputNode):
    def __init__(self, node_id, description: str = ""):
        super().__init__(node_id)
        self.description = description

    def source_filters(self):
        return [{"description": self.description}] if self.description else []


class RegexOutputNode(OutputNode):
    def __init__(self, node_id, pattern: str = "", port_type: Optional[str] = None):
        super().__init__(node_id)
        self.pattern = pattern
        self.port_type = port_type

    def sink_filters(self):
        return [{"nameRegex": self.pattern, "type": self.port_type}] if self.pattern else []


class MediaClassOutputNode(OutputNode):
    def __init__(self, node_id, media_class: str = "", port_type: Optional[str] = None):
        super().__init__(node_id)
        self.media_class = media_class
        self.port_type = port_type

    def sink_filters(self):
        return [{"mediaClass": self.media_class, "type": self.port_type}] if self.media_class else []


class DescriptionOutputNode(OutputNode):
    def __init__(self, node_id, description: str = "", port_type: Optional[str] = None):
        super().__init__(node_id)
        self.description = description
        self.port_type = port_type

    def sink_filters(self):
        return [{"description": self.description, "type": self.port_type}] if self.description else []


class PatchSpaceDeviceNode(InputNode, OutputNode):
    """Convenience node for the daemon's built-in virtual sink: usable
    as a target (apps route in) via its sink input, and as a source via
    its monitor ports.  Both identities are exact-name because the mic
    plumbing uses node.names that merely start with "Patch Space".

    There can be several of these ("Speaker Line" nodes) at once; they
    all reference the same built-in sink.      ``device_volume`` /
    ``volume_locked`` / ``force_default`` are the shared state, mirrored
    from the built-in device by the daemon (the line node owns no
    backing)."""

    # Keep the built-in sink as the system default output (re-checked
    # every tick) while True.  Mirrored from the built-in device.
    force_default = True

    def __init__(self, node_id, device_volume: float = 1.0,
                 volume_locked: bool = True):
        super().__init__(node_id)
        self.device_volume = max(0.0, min(1.0, float(device_volume)))
        self.volume_locked = bool(volume_locked)
        self.force_default = True

    def source_filters(self):
        return [{"nodeName": PATCHSPACE_VIRTUAL_SINK_NAME}]

    def sink_filters(self):
        return [{"name": PATCHSPACE_VIRTUAL_SINK_NAME}]


class PatchSpaceMicDeviceNode(InputNode, OutputNode):
    """Convenience node for the built-in virtual mic.  Routes in via the
    underlying "{name}_sink", picked up downstream from the loopback's
    Audio/Source (PATCHSPACE_VIRTUAL_MIC_NAME).

    Several may exist at once; they all reference the same built-in mic.
    ``device_volume`` / ``volume_locked`` / ``force_default`` mirror that
    shared device."""

    force_default = True

    def __init__(self, node_id, device_volume: float = 1.0,
                 volume_locked: bool = True):
        super().__init__(node_id)
        self.device_volume = max(0.0, min(1.0, float(device_volume)))
        self.volume_locked = bool(volume_locked)
        self.force_default = True

    def source_filters(self):
        return [{"nodeName": PATCHSPACE_VIRTUAL_MIC_NAME}]

    def sink_filters(self):
        return [{"name": f"{PATCHSPACE_VIRTUAL_MIC_NAME}_sink"}]


class DeviceInputNode(InputNode, LiveResolvableNode, DeviceControlMixin):
    def __init__(self, node_id, device_name: str = "", description: str = "",
                 device_volume: float = 1.0,
                 profile_index: Optional[int] = None,
                 profile_description: str = "",
                 volume_locked: bool = True):
        InputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        DeviceControlMixin.__init__(self, device_volume, profile_index,
                                    profile_description, volume_locked)
        self.device_name = device_name
        self.description = description

    def source_filters(self):
        if not self.device_name:
            return []
        return [{"nodeName": self.device_name, "mediaClass": "Audio/Source"}]

    def matches_live_node(self, props):
        return bool(self.device_name) and props.get("node.name") == self.device_name

    def _on_live_resolved(self):
        if self.live_props:
            self.description = (
                self.live_props.get("node.description")
                or self.live_props.get("node.nick")
                or self.description
            )
        super()._on_live_resolved()


class DeviceOutputNode(OutputNode, LiveResolvableNode, DeviceControlMixin):
    def __init__(self, node_id, device_name: str = "", description: str = "",
                 device_volume: float = 1.0,
                 profile_index: Optional[int] = None,
                 profile_description: str = "",
                 volume_locked: bool = True):
        OutputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        DeviceControlMixin.__init__(self, device_volume, profile_index,
                                    profile_description, volume_locked)
        self.device_name = device_name
        self.description = description

    def sink_filters(self):
        if not self.device_name:
            return []
        return [{"name": self.device_name, "mediaClass": "Audio/Sink"}]

    def matches_live_node(self, props):
        return bool(self.device_name) and props.get("node.name") == self.device_name

    def _on_live_resolved(self):
        if self.live_props:
            self.description = (
                self.live_props.get("node.description")
                or self.live_props.get("node.nick")
                or self.description
            )
        super()._on_live_resolved()


class AppInputNode(InputNode, LiveResolvableNode):
    def __init__(self, node_id, app_name: str = ""):
        InputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        self.app_name = app_name

    def source_filters(self):
        return [{"name": self.app_name, "mediaClass": "Stream/Output/Audio"}] if self.app_name else []

    def matches_live_node(self, props):
        hay = props.get("application.name") or props.get("node.name") or ""
        return bool(self.app_name) and hay == self.app_name


class AppOutputNode(OutputNode, LiveResolvableNode):
    def __init__(self, node_id, app_name: str = ""):
        OutputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        self.app_name = app_name

    def sink_filters(self):
        if not self.app_name:
            return []
        return [
            {"name": self.app_name, "mediaClass": "Stream/Input/Audio"},
            {"description": self.app_name, "mediaClass": "Stream/Input/Audio"},
        ]

    def matches_live_node(self, props):
        hay = props.get("application.name") or props.get("node.name") or ""
        return bool(self.app_name) and hay == self.app_name


# ---------------------------------------------------------------------------
# Transparent nodes
# ---------------------------------------------------------------------------


class GateNode(BoolControlledMixin, TransparentNode):
    """The "should this pass" gate.  Its state is no longer set by an
    inline button: a boolean signal on the ``ctrl`` input opens/closes
    it.  With nothing wired there, it falls back to its stored
    ``enabled`` default.  When closed, sync() treats the node as having
    no source, so every edge downstream is disconnected on the next
    pass."""

    def __init__(self, node_id, enabled: bool = True):
        super().__init__(node_id)
        self.enabled = enabled
        self._init_bool_control()

    def gate_open(self):
        return self.effective_bool(bool(self.enabled))


class ABSwitchNode(BoolControlledMixin, TransparentNode):
    """Shared state for the two switches.  Their two channels are "on"
    and "off": a wired boolean signal selects on (true) / off (false),
    and with nothing wired the stored ``output`` flag is the default
    (1 = "on", 0 = "off").  SwitcherNode sends the chosen channel out of
    one of two outputs; InverseSwitcherNode takes the chosen channel in
    through one of two inputs."""

    OUTPUT_ON = "on"
    OUTPUT_OFF = "off"
    OUTPUTS = (OUTPUT_ON, OUTPUT_OFF)
    # Pre-rename port names, tolerated so an older saved session keeps
    # routing after the A/B -> On/Off rename.
    _LEGACY_PORTS = {"a": OUTPUT_ON, "b": OUTPUT_OFF}

    def __init__(self, node_id, output: int = 0):
        super().__init__(node_id)
        self.output = 1 if output else 0
        self._init_bool_control()

    @classmethod
    def _normalize_port(cls, port: str) -> str:
        return cls._LEGACY_PORTS.get(port, port)

    def active_output(self) -> str:
        # A wired boolean signal drives it (true = "on", false = "off");
        # with nothing wired, the stored ``output`` flag is the default
        # (1 = "on", 0 = "off").
        if self._bool_effective is not None:
            return self.OUTPUT_ON if self._bool_effective else self.OUTPUT_OFF
        return self.OUTPUT_ON if self.output else self.OUTPUT_OFF


class SwitcherNode(ABSwitchNode):
    """A one-in, two-out on/off switch.  Exactly one output - "on" or
    "off" - is live at a time; the other resolves to no source, so
    sync() disconnects everything downstream of it."""

    def passes_output(self, from_port: str) -> bool:
        """Whether audio arriving from the selected output port should
        continue.  A portless legacy edge follows the selected channel
        (with "on" as the first output)."""
        if from_port in ("", None, "out"):
            return self.active_output() == self.OUTPUT_ON
        return self._normalize_port(from_port) == self.active_output()


class InverseSwitcherNode(ABSwitchNode):
    """A two-in, one-out on/off switch - the mirror of SwitcherNode.
    Edges arrive on inputs "on"/"off"; only the one matching the
    selection feeds the single output, the other input is ignored (so
    anything wired to it is left alone but silent)."""

    def allows_multiple_inputs(self) -> bool:
        return True

    def select_upstream(self, upstream: List["Edge"]) -> Optional["Edge"]:
        wanted = self.active_output()
        for edge in upstream:
            if self._normalize_port(edge.to_port) == wanted:
                return edge
        return None


class BooleanSourceNode(Node):
    """On/Off boolean signal source.  Carries a single boolean output
    ("out"); its inline On/Off button flips ``output``.  Wiring it into
    a gate/switcher's boolean input drives that node - nothing on the
    audio graph moves by itself."""

    def __init__(self, node_id, output: int = 0):
        super().__init__(node_id)
        self.output = 1 if output else 0

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean" if direction == "out" else "audio"

    def boolean_value(self) -> bool:
        return bool(self.output)


class BooleanSplitterNode(Node):
    """Fans one boolean signal out to two boolean outputs ("out1", "out2").
    Pure control-plane plumbing: it carries no audio and never becomes a
    PipeWire object, it just forwards the value of whatever source is
    wired into its "in" to everything wired downstream."""

    # Its boolean input port, so PatchSpace._resolve_boolean_input knows
    # which inbound edge to follow (it shares the lookup with the
    # BoolControlledMixin nodes, whose input is "ctrl").
    BOOLEAN_INPUT = "boolean"

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"


class BooleanInvertNode(Node):
    """Boolean NOT.  Its "out" is the negation of whatever boolean
    signal is wired into "in"; with nothing wired it emits no value, so
    anything downstream falls back to its own default."""

    BOOLEAN_INPUT = "boolean"

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"


class BooleanLogicNode(Node):
    """Shared base for the two-input boolean logic gates (AND / OR / XOR).

    Pure control-plane plumbing like the splitter and inverter - no
    audio, no PipeWire object, just a value derived from its boolean
    inputs.  An unwired input contributes nothing, so a gate with a
    single input wired passes that value through (AND/OR/XOR with one
    operand); with nothing wired it emits no value and anything
    downstream keeps its own default."""

    BOOLEAN_INPUTS = ("a", "b")

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"

    def combine(self, values: List[bool]) -> bool:
        raise NotImplementedError


class BooleanAndNode(BooleanLogicNode):
    """Boolean AND: true only when every wired input is true."""

    def combine(self, values: List[bool]) -> bool:
        return all(values)


class BooleanOrNode(BooleanLogicNode):
    """Boolean OR: true when any wired input is true."""

    def combine(self, values: List[bool]) -> bool:
        return any(values)


class BooleanXorNode(BooleanLogicNode):
    """Boolean XOR: true when an odd number of wired inputs are true.

    With the usual two wired inputs that's "exactly one is true"; a
    single wired input passes through unchanged (odd parity), matching
    the AND/OR single-operand behaviour."""

    def combine(self, values: List[bool]) -> bool:
        return sum(1 for v in values if v) % 2 == 1


class WarpInNode(TransparentNode):
    """Publishes the audio feeding its single "in" under ``warp_name``.

    A pure logical alias - no backing and no PipeWire object.  Every
    WarpOutNode with the same name resolves to this node's upstream
    source(s); because it is a TransparentNode it reuses the ordinary
    single-upstream rule and passthrough resolution.  Several WarpInNodes
    sharing a name are summed by the resolver (see _resolve_warp_audio)."""

    def __init__(self, node_id, warp_name: str = ""):
        super().__init__(node_id)
        self.warp_name = warp_name or ""


class WarpOutNode(Node):
    """Reads whatever WarpInNode(s) publish under ``warp_name``: its
    "out" resolves to their upstream audio.  Pure alias, no backing."""

    def __init__(self, node_id, warp_name: str = ""):
        super().__init__(node_id)
        self.warp_name = warp_name or ""


class BooleanWarpInNode(Node):
    """Boolean counterpart of WarpInNode: publishes the boolean signal
    on its "in" under ``warp_name`` in the separate boolean namespace."""

    BOOLEAN_INPUT = "boolean"

    def __init__(self, node_id, warp_name: str = ""):
        super().__init__(node_id)
        self.warp_name = warp_name or ""

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"


class BooleanWarpOutNode(Node):
    """Boolean counterpart of WarpOutNode: its "out" is the boolean
    value published under ``warp_name`` (first matching publisher wins;
    see PatchSpace._resolve_boolean)."""

    def __init__(self, node_id, warp_name: str = ""):
        super().__init__(node_id)
        self.warp_name = warp_name or ""

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"


# ---------------------------------------------------------------------------
# Impulse: momentary trigger wires
# ---------------------------------------------------------------------------
#
# An *impulse* is a momentary event - a bang.  A Button fires one, and a
# node with an impulse input reacts to it (see SoundEffectNode).  Like a
# boolean or a filter wire it is control-plane: it never becomes a
# PipeWire link, and add_edge pairs it only with another impulse port.
#
# Unlike a boolean it has no *value* to resolve on every sync, so nothing
# in `_resolve_boolean` / sync_locked's link pass touches it: an impulse
# is a push, walked once per press by `PatchSpace.pulse()`.  That keeps
# the momentary nature honest - there is no stored state that a poll
# could read, and a node that missed a pulse will not hear it "again"
# on the next supervision tick.


class ButtonNode(Node):
    """A momentary push button: one impulse output and no state at all.

    Pressing it in the GUI sends one ``impulse`` command; the daemon
    walks the impulse edges leaving this node and calls ``on_impulse()``
    on everything it reaches, once per node.  Fan-out is just several
    edges leaving the single output, so one button can fire any number
    of sound effects."""

    def port_kind(self, port: str, direction: str) -> str:
        return "impulse" if direction == "out" else "audio"


class PanelInNode(TransparentNode):
    """A panel input port: external audio arrives on "in" and the panel's
    internal nodes pull it from "out".  Pure logical pass-through (no
    backing).  Unlike other single-input transparent nodes it is a *bus*:
    several upstream edges may feed the same input and their sources are
    summed/mixed downstream."""

    # See PatchSpace._resolve_sources: mix every inbound audio edge rather
    # than passing only the first.
    MIX_INPUTS = True

    def allows_multiple_inputs(self) -> bool:
        return True

    def __init__(self, node_id, port_name: str = "", description: str = ""):
        super().__init__(node_id)
        self.port_name = port_name
        self.description = description


class PanelOutNode(TransparentNode):
    """A panel output port: internal audio arrives on "in" and external
    nodes pull it from "out".  Pure logical pass-through (no backing).  Also
    a mixing bus - several internal edges may feed it."""

    MIX_INPUTS = True

    def allows_multiple_inputs(self) -> bool:
        return True

    def __init__(self, node_id, port_name: str = "", description: str = ""):
        super().__init__(node_id)
        self.port_name = port_name
        self.description = description


class BoolPanelInNode(Node):
    """Boolean counterpart of PanelInNode: a boolean enters from outside
    on "in" and is republished internally on "out".

    ``default_state`` is what the port emits when its external input is
    absent (the panel used standalone with nothing plumbed into this
    input); ``None`` means "no default" (downstream falls back to its own
    default, as before)."""

    BOOLEAN_INPUT = "boolean"
    # Several edges may land on the same boolean port; resolution takes
    # the first wired source (see _resolve_boolean_input).
    ALLOW_MULTIPLE_BOOLEAN = True

    def __init__(self, node_id, port_name: str = "", description: str = "",
                 default_state: Optional[bool] = None):
        super().__init__(node_id)
        self.port_name = port_name
        self.description = description
        self.default_state = default_state

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"


class BoolPanelOutNode(Node):
    """Boolean counterpart of PanelOutNode: internal boolean on "in" is
    republished outside on "out"."""

    BOOLEAN_INPUT = "boolean"
    # Like the audio panel ports, several edges may land on the same
    # boolean port; resolution takes the first wired source (see
    # _resolve_boolean_input).
    ALLOW_MULTIPLE_BOOLEAN = True

    def __init__(self, node_id, port_name: str = "", description: str = ""):
        super().__init__(node_id)
        self.port_name = port_name
        self.description = description

    def port_kind(self, port: str, direction: str) -> str:
        return "boolean"


class ExcludeFilterNode(TransparentNode):
    """Pass-through that narrows whatever is upstream by excluding a
    nameRegex.  Annotates upstream source filters with "exclude" entries
    rather than touching the live graph."""

    def __init__(self, node_id, pattern: str = ""):
        super().__init__(node_id)
        self.pattern = pattern

    def exclude_filter(self) -> Optional[dict]:
        if not self.pattern:
            return None
        return {"nameRegex": self.pattern}


# ---------------------------------------------------------------------------
# Bundles: preset sources, classifiers, filters and terminals
# ---------------------------------------------------------------------------
#
# A *bundle* is a logical set of live PipeWire endpoints travelling on a
# single wire, instead of the one stream an ordinary audio port carries.
# Bundles are typed by side:
#
#   * a *source* bundle holds endpoints audio can be pulled FROM
#     (hardware inputs, app playback streams, sink monitors);
#   * a *sink* bundle holds endpoints audio can be pushed TO
#     (hardware outputs, app recording streams).
#
# A "filter" wire is a *third* kind: a classifier's predicate (a pure
# control-plane value, like a boolean) consumed by a Filter node.  It
# never becomes a PipeWire link.  A fourth, the "impulse" (see
# ButtonNode / SoundEffectNode above), is momentary rather than a level:
# it is a pushed event, not a value anything resolves.
#
# `add_edge` lets a bundle port pair with an audio port either way (a
# single stream is a bundle of one), so "route everything this bundle
# stands for into this sink" is just an edge; boolean and filter wires
# pair only with their own kind.


class AllInputsNode(InputNode):
    """Preset bundle source: every input-side source (hardware capture,
    virtual mics, and app playback streams).  Anchored regexes so the
    match is the exact classes rather than a substring that would also
    swallow internal plumbing."""

    def port_kind(self, port: str, direction: str) -> str:
        return "bundle" if direction == "out" else "audio"

    def bundle_side(self) -> str:
        return "source"

    def source_filters(self):
        return [{"mediaClassRegex": "^Audio/Source$|^Stream/Output/Audio$"}]


class AllAppsNode(InputNode):
    """Preset bundle source: every app playback stream.  App recording
    streams are sinks, so they are reached through All Outputs instead."""

    def port_kind(self, port: str, direction: str) -> str:
        return "bundle" if direction == "out" else "audio"

    def bundle_side(self) -> str:
        return "source"

    def source_filters(self):
        return [{"mediaClassRegex": "^Stream/Output/Audio$"}]


class AllOutputsNode(Node):
    """Preset bundle source of *sink endpoints*: every playback device
    and app recording stream, as a target set to route audio INTO.

    The bundle is a set of endpoints to push audio to, so it is consumed
    by a Filter (to narrow it) and then by a Bundle Output terminal (to
    actually deliver audio)."""

    def port_kind(self, port: str, direction: str) -> str:
        return "bundle" if direction == "out" else "audio"

    def bundle_side(self) -> str:
        return "sink"

    def sink_filters(self):
        return [
            {"mediaClassRegex": "^Audio/Sink$"},
            {"mediaClassRegex": "^Stream/Input/Audio$"},
        ]


class ClassifierNode(Node):
    """A pure control-plane predicate with a single ``filter`` output.

    A classifier carries no audio and owns no backing; it is plugged into
    a Filter node's ``filter`` input, which applies it to the bundle
    flowing through that Filter.  ``invert`` turns any classifier into
    its complement without a second node type."""

    def __init__(self, node_id, invert: bool = False):
        super().__init__(node_id)
        self.invert = bool(invert)

    def port_kind(self, port: str, direction: str) -> str:
        return "filter" if direction == "out" else "audio"

    def matches(self, props: dict, side: str) -> bool:
        """Whether a bundle member (identified by its live props) passes
        this classifier.  ``side`` is "source" or "sink" - the two
        matchers differ (a sink ``name`` is an exact node.name, a source
        ``name`` is a substring of application.name/node.name)."""
        return False

    def classify(self, props: dict, side: str) -> bool:
        return (not self.matches(props, side)) if self.invert else self.matches(props, side)


class RegexClassifierNode(ClassifierNode):
    def __init__(self, node_id, pattern: str = "", invert: bool = False):
        super().__init__(node_id, invert)
        self.pattern = pattern

    def matches(self, props: dict, side: str) -> bool:
        if not self.pattern:
            return False
        if side == "sink":
            return pwmatch.matches_sink_target(
                props.get("_node_id"), props, {"nameRegex": self.pattern}
            )
        return pwmatch.matches_source_filter(props, {"nameRegex": self.pattern})


class MediaClassClassifierNode(ClassifierNode):
    def __init__(self, node_id, media_class: str = "", invert: bool = False):
        super().__init__(node_id, invert)
        self.media_class = media_class

    def matches(self, props: dict, side: str) -> bool:
        if not self.media_class:
            return False
        return pwmatch.matches_source_filter(props, {"mediaClass": self.media_class})


class DescriptionClassifierNode(ClassifierNode):
    def __init__(self, node_id, description: str = "", invert: bool = False):
        super().__init__(node_id, invert)
        self.description = description

    def matches(self, props: dict, side: str) -> bool:
        if not self.description:
            return False
        return pwmatch.matches_source_filter(props, {"description": self.description})


class TitleClassifierNode(ClassifierNode):
    """Classifier that matches a stream's *title*.

    "Title" is PipeWire's ``media.name``: the label a mixer shows for a
    playing app ("YouTube", a track name) - what an app *calls* the thing it
    is playing, as opposed to the node's own ``description`` that
    :class:`DescriptionClassifierNode` matches.  Substring match, like the
    description classifier.  ``invert`` (the node's Exclude switch) turns it
    into "everything except these titles"."""

    def __init__(self, node_id, title: str = "", invert: bool = False):
        super().__init__(node_id, invert)
        self.title = title

    def matches(self, props: dict, side: str) -> bool:
        if not self.title:
            return False
        return pwmatch.matches_source_filter(props, {"mediaName": self.title})


class AppNameClassifierNode(ClassifierNode):
    """Classifier that matches a member's *subprocess* name.

    Substring, case-insensitive, against PipeWire's ``application.name``
    (falling back to ``node.name``, exactly as the daemon's application list
    does) - "Firefox", "Spotify", or the subprocess an app delegates to:
    an Electron app's audio service reports "Chromium input"/"WEBRTC
    VoiceEngine".  The substring counterpart of the Regex classifier's
    ``nameRegex``.  Use :class:`AppClassifierNode` to match the *application*
    those subprocesses belong to."""

    def __init__(self, node_id, app_name: str = "", invert: bool = False):
        super().__init__(node_id, invert)
        self.app_name = app_name

    def matches(self, props: dict, side: str) -> bool:
        if not self.app_name:
            return False
        return pwmatch.matches_source_filter(props, {"name": self.app_name})


class AppClassifierNode(ClassifierNode):
    """Classifier that matches every stream an *application* created.

    The value is an application key (``pwmatch.app_key``): the systemd app
    scope behind the stream's process, i.e. the name the desktop uses for that
    app - "vesktop", "discord", "librewolf".  That is what makes this the
    application-level filter: an app whose audio comes from several
    subprocesses (Electron's audio service, a voice engine, a browser tab
    process in a flatpak) matches on all of them at once, where the Subprocess
    classifier would need one value per name."""

    def __init__(self, node_id, app_key: str = "", invert: bool = False):
        super().__init__(node_id, invert)
        self.app_key = app_key

    def matches(self, props: dict, side: str) -> bool:
        if not self.app_key:
            return False
        return pwmatch.matches_source_filter(props, {"appKey": self.app_key})


class ExternalOnlyClassifierNode(ClassifierNode):
    """Classifier that keeps everything Patch Space doesn't own (real apps
    and hardware), stripping our own built-ins and plumbing.  See
    ``pwmatch.is_patchspace_owned``."""

    def matches(self, props: dict, side: str) -> bool:
        return not pwmatch.is_patchspace_owned(props)


class BundleMergeNode(TransparentNode):
    """Audio (or bundle) in -> one bundle out.

    A junction that collects everything wired into its input into a
    single bundle, so several specific lines can be gathered and then
    filtered/routed as a set.  Its input is a bus: any number of upstream
    edges may land on the one socket and their sources are unioned (the
    same mixing rule the panel ports use)."""

    MIX_INPUTS = True

    def port_kind(self, port: str, direction: str) -> str:
        return "bundle"

    def allows_multiple_inputs(self) -> bool:
        return True


class BundleSplitNode(TransparentNode):
    """Bundle in -> one ordinary audio output per member.

    The output sockets are *dynamic*: the daemon resolves the inbound
    bundle to its live members and reports one port per member (keyed by
    the member's ``node.name``), so the GUI can draw a socket per line.
    An edge leaving a member's socket carries exactly that member's
    stream (resolved by ``nodeName``), so each line can be processed or
    routed on its own; the ordinary routing then links it to whatever
    sink it reaches.  A member that goes away simply resolves to nothing
    until it returns under the same name."""

    def port_kind(self, port: str, direction: str) -> str:
        if direction == "in" and port == "in":
            return "bundle"
        return "audio"


# ---------------------------------------------------------------------------
# Simple backed nodes
# ---------------------------------------------------------------------------


class _SinkVolumeMixin:
    """Volume + lock for a backed node whose backing is a null-audio-sink
    with monitor.channel-volumes (a built-in virtual line such as the
    Patch Space sink/mic).  The backing's wpctl volume is what a Speaker
    Line / Mic Line node's slider drives; the state lives here (the one
    underlying device) and the daemon mirrors it onto each visible line
    node.  Same lock contract as DeviceControlMixin."""

    def _init_sink_volume(self, device_volume: float = 1.0,
                          volume_locked: bool = True) -> None:
        self.device_volume = max(0.0, min(1.0, float(device_volume)))
        self.volume_locked = bool(volume_locked)

    def _volume_backing_node_id(self) -> Optional[int]:
        raise NotImplementedError

    def apply_device_settings(self, push_volume: Optional[bool] = None) -> None:
        node_id = self._volume_backing_node_id()
        if node_id is not None and (
            push_volume is True
            or (push_volume is None and getattr(self, "volume_locked", True))
        ):
            _run_wpctl("set-volume", node_id, self.device_volume)

    def sync_volume_from_live(self) -> None:
        if getattr(self, "volume_locked", True):
            return
        node_id = self._volume_backing_node_id()
        if node_id is None:
            return
        volume = _read_wpctl_volume(node_id)
        if volume is not None:
            self.device_volume = volume


class _SingleSinkNode(BackedNode):
    """A single monitor-enabled null-audio-sink adapter, optionally with
    monitor.channel-volumes so wpctl can drive a per-channel volume.

    ``MEDIA_CLASS`` is the sink's media.class.  The default is a real
    Pulse-visible Audio/Sink (wpctl-controllable volume nodes, virtual
    speakers); SplitterNode overrides it with the engine's internal,
    non-Pulse class so its backing sink never shows up as an audio
    device to clients like Discord."""

    MEDIA_CLASS = "Audio/Sink"

    def __init__(self, node_id, backing_node_name: str,
                 pw_cli_command=("pw-cli",), settle: float = 0.3,
                 extra_props: str = "", description: Optional[str] = None):
        super().__init__(node_id, backing_node_name)
        self._pw_cli_command = pw_cli_command
        self._settle = settle
        self._extra_props = extra_props
        self._description = description

    def structural_ok(self) -> bool:
        b = self._find(self.backing_node_name)
        return b is not None and (
            not b.owns_process or (b.is_alive and not b.stuck(self.RESOLVE_GRACE_S))
        )

    def sink_node_id(self) -> Optional[int]:
        """The live dummy sink's node id, or None while it comes up."""
        b = self._find(self.backing_node_name)
        return b.node_id if b else None

    def ensure_structural(self) -> None:
        self._prune_dead()
        self._ensure_null_sink(self.backing_node_name, self._extra_props,
                               self._description, self._pw_cli_command, self._settle,
                               media_class=self.MEDIA_CLASS)


class SplitterNode(_SingleSinkNode):
    # A splitter is pure fan-out plumbing the user never selects in an
    # app, so its sink stays out of the Pulse device list entirely.
    MEDIA_CLASS = pwmatch.INTERNAL_MEDIA_CLASS

    def __init__(self, node_id, backing_node_name: Optional[str] = None,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name or f"splitter_{node_id}",
                         pw_cli_command, settle, description=f"Splitter: {node_id}")


#: path -> seconds; see probe_duration.  A file doesn't change length.
_DURATION_CACHE: Dict[str, float] = {}

#: How many (min, max) pairs a waveform is reduced to.  Enough for a wide
#: timeline to look like a waveform, small enough to hand to the GUI whole.
PEAK_BUCKETS = 600

def forget_sound(path: str) -> None:
    """Drop a path's cached length and waveform.  For a file that is rewritten
    in place (a Recorder's take): the caches are keyed by path, so the new take
    would otherwise report the old shape."""
    resolved = os.path.expanduser((path or "").strip())
    _DURATION_CACHE.pop(resolved, None)
    _PEAKS_CACHE.pop(resolved, None)


#: path -> peaks; see probe_peaks.  A file's shape doesn't change either.
_PEAKS_CACHE: Dict[str, List[Tuple[float, float]]] = {}


def probe_duration(path: str) -> float:
    """How long an audio file is, in seconds, or 0.0 when it can't be read.

    ffprobe handles every format pw-cat can play (and then some), and this is
    what makes a sound's length *known* rather than discovered when it ends.
    Cached per path; a missing ffprobe just means no length is shown."""
    if not path:
        return 0.0
    resolved = os.path.expanduser(path.strip())
    if resolved in _DURATION_CACHE:
        return _DURATION_CACHE[resolved]
    duration = 0.0
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", resolved],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0:
            duration = max(0.0, float((result.stdout or "").strip() or 0.0))
    except (OSError, subprocess.TimeoutExpired, ValueError) as exc:
        logger.debug("ffprobe couldn't read %r: %s", resolved, exc)
    _DURATION_CACHE[resolved] = duration
    return duration


def probe_peaks(path: str) -> List[Tuple[float, float]]:
    """A file's waveform as up to ``PEAK_BUCKETS`` (min, max) pairs in -1..1.

    Decoded once per path with ffmpeg to raw mono PCM (a low rate is plenty -
    this is the *shape* for the Clip timeline, not the audio), then reduced to
    per-bucket extremes.  Empty when the file can't be read: the GUI draws a
    flat line rather than reporting an error."""
    if not path:
        return []
    resolved = os.path.expanduser(path.strip())
    if resolved in _PEAKS_CACHE:
        return _PEAKS_CACHE[resolved]
    peaks: List[Tuple[float, float]] = []
    try:
        result = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", resolved,
             "-ac", "1", "-ar", "4000", "-f", "s16le", "-"],
            capture_output=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout:
            samples = array.array("h")
            samples.frombytes(result.stdout[: len(result.stdout) // 2 * 2])
            if samples:
                # Ceil, so a long file can't round *up* past the bucket
                # budget (it did: 800 peaks for a 600 budget).
                step = max(1, -(-len(samples) // PEAK_BUCKETS))
                for start in range(0, len(samples), step):
                    chunk = samples[start:start + step]
                    peaks.append((
                        max(-1.0, min(chunk) / 32768.0),
                        min(1.0, max(chunk) / 32768.0),
                    ))
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("ffmpeg couldn't read %r for a waveform: %s", resolved, exc)
    _PEAKS_CACHE[resolved] = peaks
    return peaks


class ClipNode(Node):
    """A range of a sound: a sound in and a sound out.

    It is a *view* of the sound it is given - the node body IS the timeline
    (waveform, a draggable selection, the two timestamps) - and what it passes
    on is the same sound narrowed to ``[start, end]`` seconds.  Because it
    narrows rather than copies, stacking clips intersects their ranges, and
    nothing about the audio is touched until a player fires it."""

    def __init__(self, node_id, start: float = 0.0, end: Optional[float] = None):
        super().__init__(node_id)
        self.start = max(0.0, float(start or 0.0))
        self.end = None if end is None else float(end)

    def port_kind(self, port: str, direction: str) -> str:
        # Both sides carry a sound: in is the file (or a wider clip), out is
        # the range of it this node selects.
        return "sound"


class SoundNode(Node):
    """A sound: a file of known length, carried on a *sound* port.

    It makes no noise and owns no PipeWire object - it is the reference other
    nodes act on.  The Sound Player fires it when an impulse arrives, and the
    Clip node returns a *range* of it, which is why the length matters: every
    node downstream can talk about time in a file that hasn't been opened yet.
    ``path`` is stored exactly as written (usually a home-relative "~/...") and
    expanded when it is actually used, like the sound effect's."""

    def __init__(self, node_id, path: str = ""):
        super().__init__(node_id)
        self.path = path or ""

    def port_kind(self, port: str, direction: str) -> str:
        return "sound" if direction == "out" else "audio"

    @property
    def duration(self) -> float:
        """The sound's length in seconds (0.0 when unknown)."""
        return probe_duration(self.path)


class SoundPlayerNode(_SingleSinkNode):
    """Plays a *sound* when an impulse arrives: a sound input, an impulse
    input, an audio output.

    The sound input carries a file plus the range of it to play (see
    SoundNode / ClipNode); the impulse fires it into a private dummy sink
    whose *monitor* is this node's audio output.  No sound is wired and the
    impulse does nothing - which is why the node reports nothing to play
    rather than playing something stale.

    The dummy is the node's socket, which is the same "the socket is
    stable, the interior is replaceable" rule the effect sandwich
    follows: one impulse spawns one ``pw-cat --playback <path> --target
    <dummy>`` child, the audio lands in the dummy, and every user edge
    wired to the audio out (the dummy's monitor ports) carries it
    unchanged - no per-impulse relinking, and no edge is ever attached
    to a stream that is about to exit.

    ``overlap`` picks the retrigger behaviour: off (the default) stops
    whatever is still playing first, so a pressed button restarts the
    sound instead of stacking takes; on lets impulses stack up and mix in
    the dummy (the node's Stack switch).

    The path comes from the sound upstream of it, stored exactly as the user
    wrote it (usually a home-relative ``~/...``); the expansion happens at
    play time, in ``_start_player``.  A range that doesn't cover the whole
    file is decoded to a temporary WAV with ffmpeg first - pw-cat plays whole
    files only, and that same decode is what makes any format playable.

    The playback children are deliberately kept out of ``backings``: a
    player exits on its own the moment the file ends, and a backing that
    died naturally is exactly what ``dead_backings()`` reports - the node
    would flash "dead" (and its readiness would flap) every time a sound
    finished.  They are handed over by ``owned_backings()`` instead, and
    retired by ``refresh_live()`` on the supervision tick."""

    # The impulse socket.  Its name is "in", so edge ids stay the plain
    # ``button->player`` form; the *kind* is what makes it an impulse wire.
    IMPULSE_INPUT = "impulse"

    #: The socket a sound arrives on (a Sound node, or a Clip narrowing one).
    SOUND_INPUT = "sound"

    # A sound effect is a source the user never selects in an app, so its
    # dummy stays out of the Pulse device list; the monitor ports are
    # still created (and still routable) because the class is an
    # Audio/Sink subclass - see pwmatch.INTERNAL_MEDIA_CLASS.
    MEDIA_CLASS = pwmatch.INTERNAL_MEDIA_CLASS

    # How long a fresh player gets to be confirmed running before we
    # declare the file unplayable.  Short: this blocks the client's
    # command handler, and the two things we are waiting for (the client
    # connecting, or pw-cat rejecting the file) both land well inside it.
    PLAYER_SETTLE_S = 0.1

    def __init__(self, node_id, backing_node_name: str,
                 overlap: bool = False,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle,
                         description=f"Sound Player: {node_id}")
        self.overlap = bool(overlap)
        self._players: List[OwnedPwProcess] = []
        #: name -> temporary WAV a clipped playback is reading (see
        #: _clip_to_temp); removed when its player retires.
        self._temp_files: Dict[str, str] = {}
        # Names are only ever appended to, never reused: a name is what
        # the live-graph lookup keys on, so recycling one would let a
        # lagging removal event tear down the fresh player.
        self._player_seq = 0

    def port_kind(self, port: str, direction: str) -> str:
        if direction == "in":
            if port == self.IMPULSE_INPUT or port == "in":
                return "impulse"
            if port == self.SOUND_INPUT:
                return "sound"
        return "audio"

    # -- playing ---------------------------------------------------------

    def on_impulse(self, sound: Optional[dict] = None) -> None:
        """Play the sound that arrived on the sound input (see the class
        docstring for ``overlap``).  ``sound`` is what the space resolved for
        this node: {"path": ..., "start": ..., "end": ...}."""
        sound = dict(sound or {})
        if not str(sound.get("path") or "").strip():
            logger.warning("Player %r was triggered with no sound wired", self.id)
            return
        self._prune_players()
        if not self.overlap:
            self._stop_players()
        self._start_player(sound)

    def stop(self) -> int:
        """Stop everything this node is playing and return how many streams
        that was - the Stop button on a player, which is the only way to cut a
        long sound short once it has been fired."""
        playing = self.playing
        self._stop_players()
        return playing

    def stop(self) -> int:
        """Stop everything this node is playing and return how many streams that
        was - the Stop button on a player, which is the only way to cut a long
        sound short once it has been fired."""
        playing = self.playing
        self._stop_players()
        return playing

    @property
    def playing(self) -> int:
        """How many playback streams are running right now.  Serialized
        so the GUI can show the node's play count."""
        return sum(1 for p in self._players if p.is_alive)

    def _start_player(self, sound: dict) -> None:
        path = str(sound.get("path") or "").strip()
        start = float(sound.get("start") or 0.0)
        end = sound.get("end")
        end = float(end) if end is not None else None
        name = f"{self.backing_node_name}_playback_{self._player_seq}"
        if start > 0.0 or end is not None:
            clipped = self._clip_to_temp(path, start, end)
            if clipped is None:
                return
            path = clipped
            self._temp_files[name] = clipped
        self._player_seq += 1
        # `~` is expanded here rather than at set time: the stored value
        # stays portable (it round-trips through sessions and panel files
        # as the user wrote it, usually "~/..."), and pw-cat is handed a
        # real path - nothing in that argv is a shell, so a literal `~`
        # would be looked for as a directory of that name.  Only a leading
        # `~`/`~user` expands (os.path.expanduser); `$VARS` deliberately do
        # not, since there is no shell in the chain to be predictable about.
        resolved = os.path.expanduser(path)
        # node.name is pinned so the stream is identifiable (and reads as
        # patchspace-owned plumbing - see pwmatch.is_patchspace_owned); the
        # *file path* is deliberately not put into node.description,
        # which is a SPA-JSON string where a quote or brace in a filename
        # would break the command.
        command = (
            "pw-cat", "--playback", "--target", self.backing_node_name,
            "--properties", f'{{ node.name = "{name}" }}',
            resolved,
        )
        proc = OwnedPwProcess(name, self._pw_cli_command, self.PLAYER_SETTLE_S)
        if not proc.create(command, quiet=True):
            logger.warning(
                "Player %r could not play %r into %r (missing file, an "
                "unsupported format, or the node isn't up yet)",
                self.id, resolved, self.backing_node_name,
            )
            self._drop_temp(name)
            return
        self._players.append(proc)

    def _clip_to_temp(self, path: str, start: float, end: Optional[float]) -> Optional[str]:
        """Decode just the selected part of a file to a temporary WAV.

        pw-cat plays whole files, so a Clip's range has to be materialised
        before it is played.  ffmpeg also decodes the formats pw-cat can't
        guess, and the file is deleted the moment its player retires."""
        resolved = os.path.expanduser(path)
        try:
            handle = tempfile.NamedTemporaryFile(
                prefix="patchspace_clip_", suffix=".wav", delete=False
            )
            handle.close()
        except OSError as exc:
            logger.warning("Couldn't make a clip file: %s", exc)
            return None
        command = ["ffmpeg", "-nostdin", "-v", "error"]
        if start > 0.0:
            command += ["-ss", f"{start:.3f}"]
        command += ["-i", resolved]
        if end is not None:
            command += ["-t", f"{max(0.0, end - start):.3f}"]
        command += ["-y", handle.name]
        try:
            result = subprocess.run(command, capture_output=True, text=True,
                                    timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Couldn't cut %r to a clip: %s", resolved, exc)
            result = None
        if (result is None or result.returncode != 0
                or not os.path.exists(handle.name)
                or os.path.getsize(handle.name) == 0):
            os.path.exists(handle.name) and os.unlink(handle.name)
            return None
        return handle.name

    def _drop_temp(self, name: str) -> None:
        """Delete a clip's temporary file once its player is gone - the file
        exists only for that one playback."""
        path = self._temp_files.pop(name, "")
        if path and os.path.exists(path):
            os.unlink(path)

    def _prune_players(self) -> None:
        """Forget players whose file has ended (and any clip file they were
        reading)."""
        for proc in list(self._players):
            if proc.is_alive:
                continue
            self._players.remove(proc)
            proc.destroy()
            self._drop_temp(proc.name)

    def _stop_players(self) -> None:
        for proc in list(self._players):
            proc.destroy()
            self._players.remove(proc)
            self._drop_temp(proc.name)

    def refresh_live(self) -> None:
        """Per-supervision-tick hook (see PatchSpace.supervise): retire
        the playback children whose file has ended, so the count the GUI
        shows can't drift and a long-lived session can't accumulate
        finished processes."""
        self._prune_players()

    # -- lifecycle -------------------------------------------------------

    def owned_backings(self) -> List[OwnedPwNode]:
        """The dummy sink *and* any playback still running.  Handing the
        players over here (rather than keeping them in ``backings``) is
        what keeps them out of readiness/health accounting while still
        making them die with the node - see BackedNode.owned_backings."""
        players, self._players = self._players, []
        return super().owned_backings() + players


class VirtualSpeakerNode(_SinkVolumeMixin, _SingleSinkNode):
    def __init__(self, node_id, backing_node_name: str, device_label: str = "",
                 device_volume: float = 1.0, volume_locked: bool = True,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle,
                         extra_props="monitor.channel-volumes=1",
                         description=device_label or backing_node_name)
        self.device_label = device_label
        self._init_sink_volume(device_volume, volume_locked)

    def _volume_backing_node_id(self) -> Optional[int]:
        b = self._find(self.backing_node_name)
        return b.node_id if b else None

    def config_fields(self):
        out = super().config_fields()
        out["backing_node_name"] = self.backing_node_name
        return out


class BundleToAudioNode(_SingleSinkNode):
    """Bundle in -> single audio stream out, summed through this node's own sink.

    The bundle's members are mixed into a private internal null sink and the
    node's *output* is that sink's monitor, so the wire downstream never moves:
    changing which members the bundle holds re-links the upstream side only,
    where before the downstream was re-pointed at the new members every time
    (tearing down and re-initialising whatever it fed).  Straight from a bundle
    into an ordinary audio input means the same thing; this node makes the
    conversion explicit and gives it one stable socket.

    The dummy uses the internal non-Pulse media class, so it never shows up as
    a device in apps (same rule as Bundle Output's and the Splitter's)."""

    MEDIA_CLASS = pwmatch.INTERNAL_MEDIA_CLASS

    def __init__(self, node_id, backing_node_name: Optional[str] = None,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        _SingleSinkNode.__init__(
            self, node_id, backing_node_name or f"bundle_audio_{node_id}",
            pw_cli_command, settle,
            description=f"Bundle audio: {node_id}",
        )

    def port_kind(self, port: str, direction: str) -> str:
        if direction == "in" and port in ("bundle", "in"):
            return "bundle"
        return "audio"


class FilterNode(_SingleSinkNode):
    """Bundle in + one or more classifiers in -> filtered bundle out.

    The bundle input carries a set of endpoints; each ``filterN`` input
    carries a classifier's predicate.  The node resolves the incoming
    bundle to its exact live members and keeps the ones *every* wired
    classifier matches (AND - adding classifiers narrows, exactly like
    chaining Filter nodes).  With no classifier wired the bundle passes
    through unchanged; a *wired but empty* classifier matches nothing,
    mirroring the legacy leaves an empty pattern used to match nothing.

    The filter inputs are dynamic: the daemon reports one per wired
    classifier plus a spare (``filter_input_ports``), so plugging into
    the spare grows another and one node can hold an arbitrary number of
    classifiers.

    ``exclude`` is the node's Include/Exclude switch.  Off (Include, the
    default) the node keeps what matches; on (Exclude) it keeps everything
    *except* what matches - the same predicate, negated, so one node covers
    "only these" and "everything but these".

    The node sums what it keeps into its own private internal sink and its
    output *is* that sink's monitor (see PatchSpace._filter_links), so a member
    the switch drops simply stops being fed here: it disappears from this chain
    and nowhere else, and the wire downstream never moves.  (Silencing the app
    globally was wrong - the same stream may well be routed by another part of
    the graph, or meant to keep playing normally.)"""

    MEDIA_CLASS = pwmatch.INTERNAL_MEDIA_CLASS

    def __init__(self, node_id, exclude: bool = False,
                 backing_node_name: Optional[str] = None,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        _SingleSinkNode.__init__(
            self, node_id, backing_node_name or f"filter_{node_id}",
            pw_cli_command, settle, description=f"Filter: {node_id}",
        )
        self.exclude = bool(exclude)

    def port_kind(self, port: str, direction: str) -> str:
        if direction == "in" and port.startswith("filter"):
            return "filter"
        return "bundle"

    def passes_output(self, from_port: str) -> bool:
        return True


def _run_pw_link(output_port: str, input_port: str) -> bool:
    """Create one link.  "Already linked" counts as success: pw-link exits
    non-zero for a link that is already there, and callers here want the link
    to *exist*, not to have made it themselves (see pwgraph's note)."""
    try:
        res = subprocess.run(
            ["pw-link", output_port, input_port],
            capture_output=True, text=True, timeout=3.0,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    if res.returncode == 0:
        return True
    return "already linked" in (res.stderr or "").lower()


def _recorder_input_ports(name: str) -> List[Tuple[str, str]]:
    """The input ports a recording client exposes, as
    ``(channel suffix, port id)`` - ``("_FL", "x_recording:input_FL")``.

    Empty until pw-cat has actually started, which is why the linking that
    uses it retries."""
    try:
        out = subprocess.run(
            ["pw-link", "-i"], capture_output=True, text=True, timeout=2.0
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    prefix = f"{name}:input"
    return [
        (line.strip()[len(prefix):], line.strip())
        for line in out.splitlines()
        if line.strip().startswith(prefix)
        and line.strip()[len(prefix):]
    ]


class RecorderNode(_SingleSinkNode):
    """Records what is wired into it: an audio input, and a *sound* out.

    The input is summed into the node's own private internal sink (every node
    gets a sink - see the rule of thumb), and recording taps that sink's monitor
    with one ``pw-cat --record`` child, so what is captured is exactly what the
    node was fed and nothing else.  Stopping finalises the file, and the node's
    sound output then points at it - ready for a Clip or a Sound Player.

    Record always starts a *fresh* take: the file is deleted first and written
    to the same path every time, so the node's output identity never changes and
    "record" simply overwrites.  The waveform and length caches for that path
    are dropped on stop, since the file behind them just changed."""

    MEDIA_CLASS = pwmatch.INTERNAL_MEDIA_CLASS

    #: Where takes land.  One fixed file per node, so a new take overwrites the
    #: old one rather than piling up.
    RECORD_DIR = os.path.join(
        os.path.expanduser("~/.local/share/patchspace"), "recordings"
    )
    RECORD_RATE = 48000
    RECORD_CHANNELS = 2
    RECORD_FORMAT = "s16"

    def __init__(self, node_id, backing_node_name: Optional[str] = None,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        _SingleSinkNode.__init__(
            self, node_id, backing_node_name or f"recorder_{node_id}",
            pw_cli_command, settle, description=f"Recorder: {node_id}",
        )
        self._recorder: Optional[OwnedPwProcess] = None

    def port_kind(self, port: str, direction: str) -> str:
        return "sound" if direction == "out" else "audio"

    @property
    def take_path(self) -> str:
        """The file this node records to (the same one every take)."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(self.id))
        return os.path.join(self.RECORD_DIR, f"{safe}.wav")

    @property
    def recording(self) -> bool:
        return self._recorder is not None and self._recorder.is_alive

    @property
    def duration(self) -> float:
        """The take's length in seconds (0.0 while recording or when empty)."""
        return probe_duration(self.take_path) if not self.recording else 0.0

    def start_take(self) -> bool:
        """Start a take, overwriting the previous one.  False if it wouldn't
        start (no dummy sink yet, no program to record with)."""
        self._prune_recorder()
        if self.recording:
            return True
        path = self.take_path
        try:
            os.makedirs(self.RECORD_DIR, exist_ok=True)
            if os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("Couldn't clear %r for recording: %s", path, exc)
            return False
        name = f"{self.backing_node_name}_recording"
        # No ``--target``: pw-cat falls back to the *default source* when the
        # target doesn't resolve to something it can capture from, and that
        # silently recorded the microphone instead of this node's input (the
        # sink is not a source, so the name lookup found nothing to bind to).
        # Autoconnect is off for the same reason - the take reads this node's
        # monitor, and only this node's monitor, because we link it below.
        command = (
            "pw-cat", "--record",
            "--format", self.RECORD_FORMAT, "--rate", str(self.RECORD_RATE),
            "--channels", str(self.RECORD_CHANNELS),
            "--properties",
            f'{{ node.name = "{name}" node.autoconnect = false }}',
            path,
        )
        proc = OwnedPwProcess(name, self._pw_cli_command, 0.1)
        if not proc.create(command, quiet=True):
            logger.warning(
                "Recorder %r couldn't start recording from %r",
                self.id, self.backing_node_name,
            )
            return False
        self._recorder = proc
        if not self._link_to_monitor(name):
            # Nothing to read from: without the link this take would capture
            # silence at best, and something else at worst.  Say so instead
            # of reporting a recording that isn't one.
            logger.warning(
                "Recorder %r couldn't link its take to %r's monitor",
                self.id, self.backing_node_name,
            )
            self.stop_take()
            return False
        return True

    def _link_to_monitor(self, name: str) -> bool:
        """Read this node's own sink: link the take's stream to its monitor.

        Retried - the pw-cat client's ports appear a moment after it starts -
        and each channel the client exposes is linked to the matching monitor
        channel.  ``pw-link`` reports "already linked" for a link that is
        there, which is a success for us."""
        for _ in range(12):
            ports = _recorder_input_ports(name)
            links = [
                (f"{self.backing_node_name}:monitor{ch}", port)
                for ch, port in ports
            ]
            if links and all(_run_pw_link(out, inp) for out, inp in links):
                return True
            _time.sleep(0.05)
        return False

    def stop_take(self) -> bool:
        """Finish the take.  True when something was actually recording."""
        was = self.recording
        proc, self._recorder = self._recorder, None
        if proc is not None:
            proc.destroy()
        if was:
            # The file behind these caches just changed: a re-record under the
            # same path would otherwise keep its old waveform and length.
            forget_sound(self.take_path)
        return was

    def _prune_recorder(self) -> None:
        """A recorder that exited on its own (disk full, the sink went away)
        is not a fault - let the node read as idle."""
        if self._recorder is not None and not self._recorder.is_alive:
            self._recorder = None

    def refresh_live(self) -> None:
        self._prune_recorder()


class BundleOutputNode(OutputNode, _SingleSinkNode):
    """Terminal for a *sink* bundle: audio in + a set of targets.

    The ``in`` audio port (which may itself be fed by a source bundle)
    is summed into this node's own private internal null sink, then that
    sink's monitor feeds every sink the ``bundle`` input resolves to - so
    one coherent source reaches several outputs (N+M links instead of
    N*M) and every sink sees a single stable link.  See
    PatchSpace._bundle_output_links; the ordinary OutputNode edge
    resolution is skipped for this node.

    The dummy uses the internal non-Pulse media class, so it never shows
    up as a device in apps."""

    MEDIA_CLASS = pwmatch.INTERNAL_MEDIA_CLASS

    def __init__(self, node_id, backing_node_name: str,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        _SingleSinkNode.__init__(
            self, node_id, backing_node_name, pw_cli_command, settle,
            description=f"Bundle output: {node_id}",
        )

    def port_kind(self, port: str, direction: str) -> str:
        if direction == "in" and port == "bundle":
            return "bundle"
        return "audio"


class VolumeProcessNode(_SingleSinkNode):
    """A volume slider backed by a channel-volume-enabled null sink -
    the real object whose per-channel volume wpctl drives."""

    def __init__(self, node_id, backing_node_name: str,
                 initial_volume: float = 1.0,
                 volume_min: float = 0.0, volume_max: float = 1.0,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle,
                         extra_props="monitor.channel-volumes=1",
                         description=backing_node_name)
        self.volume = max(0.0, min(1.0, initial_volume))
        self.volume_min = volume_min
        self.volume_max = volume_max
        self._volume_applied_to: Optional[int] = None

    @property
    def backing_node_id(self) -> Optional[int]:
        b = self._find(self.backing_node_name)
        return b.node_id if b else None

    def _apply_volume(self) -> None:
        b = self._find(self.backing_node_name)
        if b is None or b.node_id is None:
            return
        if b.node_id == self._volume_applied_to:
            return
        actual = self.volume_min + (self.volume_max - self.volume_min) * self.volume
        logger.info("Volume for %s set to %.2f (fraction %.2f)",
                    self.backing_node_name, actual, self.volume)
        # wpctl silently clamps to 1.0 (100%) unless told otherwise -
        # without an explicit -l/--limit, any `actual` above 1.0 (a
        # boosted volume_max, e.g. the hidden pre-gain node the daemon
        # brackets a Sensitivity gate with - see main.py's
        # _ensure_sensitivity_internals) would just get capped back
        # down to 100% and the boost would never actually apply. The
        # limit tracks volume_max so a node configured for boost can
        # reach it, while an ordinary 0..1 node (limit 1.0) behaves
        # exactly as before.
        limit = max(1.0, self.volume_max)
        _run_wpctl("set-volume", b.node_id, actual, "-l", f"{limit:.3f}")
        self._volume_applied_to = b.node_id

    def set_volume(self, fraction: float) -> None:
        self.volume = max(0.0, min(1.0, fraction))
        self._volume_applied_to = None
        self._apply_volume()

    def set_volume_range(self, vmin: float, vmax: float) -> None:
        self.volume_min = vmin
        self.volume_max = vmax
        self._volume_applied_to = None
        self._apply_volume()

    def refresh_live(self) -> None:
        self._apply_volume()

    def config_fields(self):
        out = super().config_fields()
        out["backing_node_name"] = self.backing_node_name
        out["initial_volume"] = self.volume
        out["volume_min"] = self.volume_min
        out["volume_max"] = self.volume_max
        return out


class VirtualMicNode(_SinkVolumeMixin, BackedNode):
    """A user-named virtual microphone: a null-audio-sink everything is
    fed into, a standalone pw-loopback republishing its monitor as a real
    Audio/Source (that is the object apps pick as a mic - a bare sink
    monitor isn't offered as a microphone), and a permanent silent
    keepalive feeding the sink so the mic never reads as idle."""

    def __init__(self, node_id, backing_node_name: str, device_label: str = "",
                 device_volume: float = 1.0, volume_locked: bool = True,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name)
        self.device_label = device_label
        self._pw_cli_command = pw_cli_command
        self._settle = settle
        self._init_sink_volume(device_volume, volume_locked)

    def _volume_backing_node_id(self) -> Optional[int]:
        # Control the loopback's published Audio/Source, not the inner
        # null sink: this PipeWire build does not apply the sink's
        # monitor.channel-volumes to the monitor the loopback captures,
        # so a sink-volume change is inaudible on the mic (measured).
        # The source node's own volume does gate what apps record.
        b = self._find(self._loopback_name)
        return b.node_id if b else None

    @property
    def _sink_name(self) -> str:
        return f"{self.backing_node_name}_sink"

    @property
    def _loopback_name(self) -> str:
        return self.backing_node_name

    @property
    def _keepalive_name(self) -> str:
        return f"{self.backing_node_name}_keepalive"

    def input_identity(self, port: str = "in") -> dict:
        return {"name": self._sink_name}

    def output_identity(self) -> dict:
        return {"nodeName": self._loopback_name}

    def structural_ok(self) -> bool:
        for name in (self._sink_name, self._loopback_name, self._keepalive_name):
            b = self._find(name)
            if b is None:
                return False
            if b.owns_process and not b.is_alive:
                return False
        return True

    def ensure_structural(self) -> None:
        self._prune_dead()
        if self._find(self._sink_name) is None:
            desc = self.device_label or self.backing_node_name
            config = (
                "factory.name=support.null-audio-sink "
                f'node.name="{self._sink_name}" '
                f'node.description="{desc} (input)" '
                "media.class=Audio/Sink "
                "audio.position=[FL,FR] "
                "monitor.channel-volumes=1"
            )
            sink = self._spawn_cli(self._sink_name, f"create-node adapter {config}",
                                   self._pw_cli_command, self._settle)
            if sink is None:
                return
            self._drop(self._keepalive_name)  # retarget on next ensure
        if self._find(self._loopback_name) is None:
            self._spawn_loopback()
        self._ensure_feed(self._keepalive_name, self._sink_name,
                          self._pw_cli_command, self._settle)

    def _spawn_loopback(self) -> None:
        # Deliberately pw-loopback (argv list, no shell quoting to get
        # wrong) rather than a pw-cli load-module with a hand-escaped
        # nested SPA-JSON props blob.  --playback-props/--capture-props
        # are the property flags; -P/-C would be *target device names*.
        desc = self.device_label or self.backing_node_name
        capture_name = f"{self.backing_node_name}_capture"
        playback_props = (
            f'{{ node.name = "{self._loopback_name}" '
            f'node.description = "{desc}" '
            "media.class = Audio/Source }"
        )
        capture_props = (
            f'{{ node.name = "{capture_name}" '
            f'target.object = "{self._sink_name}" '
            "stream.capture.sink = true "
            "audio.position = [ FL FR ] }"
        )
        owned = OwnedPwProcess(self._loopback_name, self._pw_cli_command, self._settle)
        command = (
            "pw-loopback",
            "--playback-props", playback_props,
            "--capture-props", capture_props,
        )
        if owned.create(command):
            self.backings.append(owned)
        else:
            logger.error("Virtual mic loopback creation failed for %r", self.id)

    def config_fields(self):
        out = super().config_fields()
        out["backing_node_name"] = self.backing_node_name
        return out


# ---------------------------------------------------------------------------
# Effects (real DSP sandwiches)
# ---------------------------------------------------------------------------


class _ChainEffect(BackedNode):
    """Shared plumbing for an effect whose real DSP is a filter-chain
    module sandwiched between two stable dummy sinks:

        [ {name}_in ] -> link -> [ {name} (capture) <-> DSP <-> {name}_fx_out ] -> link -> [ {name}_out ]

    User edges plug into the dummies (never the module), and the two
    internal links are re-derived by name every sync - so reloading the
    interior (option change, crash, plugin fix) never touches a user
    edge.  The input dummy is permanently fed and the output dummy
    permanently drained so neither side can ever be suspended for having
    zero active links.

    Subclasses provide the module command (``_module_command()``) and
    any live controls.
    """

    def __init__(self, node_id, backing_node_name: str,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name)
        self._pw_cli_command = pw_cli_command
        self._settle = settle

    # -- naming ----------------------------------------------------------

    @property
    def _capture_name(self) -> str:
        # The module's capture (sink) stream = primary/owning backing.
        return self.backing_node_name

    @property
    def _playback_name(self) -> str:
        return f"{self.backing_node_name}_fx_out"

    @property
    def _input_dummy_name(self) -> str:
        return f"{self.backing_node_name}_in"

    @property
    def _output_dummy_name(self) -> str:
        return f"{self.backing_node_name}_out"

    @property
    def _feed_name(self) -> str:
        return f"{self._input_dummy_name}_keepalive"

    @property
    def _drain_name(self) -> str:
        return f"{self._output_dummy_name}_keepalive"

    # -- identities / internal plumbing ----------------------------------

    def input_identity(self, port: str = "in") -> dict:
        return {"name": self._input_dummy_name}

    def output_identity(self) -> dict:
        return {"nodeName": self._output_dummy_name}

    def internal_links(self):
        return [
            ({"nodeName": self._input_dummy_name}, {"name": self._capture_name}),
            ({"nodeName": self._playback_name}, {"name": self._output_dummy_name}),
        ]

    # -- health ----------------------------------------------------------

    def has_module(self) -> bool:
        return True

    def module_backing(self) -> Optional[OwnedPwNode]:
        return self._find(self._capture_name)

    def module_ok(self) -> bool:
        b = self.module_backing()
        return b is not None and b.owns_process and b.is_alive and not b.stuck(
            self.RESOLVE_GRACE_S
        )

    def structural_ok(self) -> bool:
        for name in (self._input_dummy_name, self._output_dummy_name,
                     self._feed_name, self._drain_name):
            b = self._find(name)
            if b is None:
                return False
            if b.owns_process and (not b.is_alive or b.stuck(self.RESOLVE_GRACE_S)):
                return False
        return True

    # -- module command ---------------------------------------------------

    def _module_command(self) -> Optional[str]:
        raise NotImplementedError

    def _module_command_args(self) -> str:
        raise NotImplementedError

    def _spawn_module(self) -> Optional[OwnedPwNode]:
        capture_name = self._capture_name
        playback_name = self._playback_name
        if self._find(capture_name) is not None:
            return self._find(capture_name)
        command = "load-module libpipewire-module-filter-chain { " + self._module_command_args() + " }"
        owned = OwnedPwNode(capture_name, self._pw_cli_command, self._settle)
        if not owned.create(command):
            logger.error("%s module creation failed for %r", type(self).__name__, self.id)
            return None
        self.backings.append(owned)
        self.backings.append(OwnedPwNode(playback_name))
        return owned

    def _module_children(self) -> Set[str]:
        return {self._capture_name, self._playback_name}

    # -- lifecycle --------------------------------------------------------

    def ensure_structural(self) -> None:
        structural_names = {self._input_dummy_name, self._output_dummy_name,
                            self._feed_name, self._drain_name}
        self._prune_dead(structural_names)
        # The two dummy sinks are internal plumbing the user never picks
        # in an app, so they use the engine's non-Pulse media class -
        # Discord/Chromium never enumerate them (the effect module's own
        # capture/playback streams keep their real Audio/Sink/Source
        # classes).
        self._ensure_null_sink(
            self._input_dummy_name,
            description=f"{self.id} input",
            pw_cli_command=self._pw_cli_command,
            settle=self._settle,
            media_class=pwmatch.INTERNAL_MEDIA_CLASS,
        )
        self._ensure_null_sink(
            self._output_dummy_name,
            description=f"{self.id} output",
            pw_cli_command=self._pw_cli_command,
            settle=self._settle,
            media_class=pwmatch.INTERNAL_MEDIA_CLASS,
        )
        self._ensure_feed(self._feed_name, self._input_dummy_name,
                          self._pw_cli_command, self._settle)
        self._ensure_drain(self._drain_name, self._output_dummy_name,
                           self._pw_cli_command, self._settle)

    def ensure_module(self) -> None:
        """Spawn the module if it is missing, its process died, or it's
        stuck (alive but never resolved a live node - see
        OwnedPwNode.stuck; this is what actually catches an LV2/LADSPA
        plugin that fails to instantiate without pw-cli's phantom-
        creation guard noticing).

        The output drain is deliberately NOT touched here.  It targets
        the stable out-dummy sink, not the module, and its record stream
        keeps consuming that dummy's monitor straight through an interior
        reload.  Dropping and re-creating it on every spawn used to open
        a window in which the out-dummy had no consumer at all; a
        timing-sensitive module (RNNoise) then had nothing to run into
        and stalled silent, so whether the chain survived depended on
        user wiring order (output wired first worked, input first died).
        Echo Cancel never dropped its out drain and never showed the bug.
        If the drain process itself dies, ensure_structural() replaces it
        like any other structural backing."""
        cap = self._find(self._capture_name)
        fresh = cap is None or not (cap.owns_process and cap.is_alive) or cap.stuck(
            self.RESOLVE_GRACE_S
        )
        if fresh:
            for owned in list(self.backings):
                if owned.name in self._module_children():
                    owned.destroy()
                    if owned in self.backings:
                        self.backings.remove(owned)
            self._spawn_module()
        self.ensure_structural()

    def reload_module(self) -> None:
        """Swap only the interior: drop the module's own backings, then
        rebuild.  The dummies and their feed/drain keepalives are
        untouched, so user edges never drop and the out side never loses
        its consumer across the reload."""
        drop_names = set(self._module_children())
        for owned in list(self.backings):
            if owned.name in drop_names:
                owned.destroy()
                if owned in self.backings:
                    self.backings.remove(owned)
        self.ensure_module()

    # teardown_backing() is inherited from BackedNode (parallel destroy
    # of every owned backing) - _ChainEffect used to shadow it with an
    # identical one-at-a-time loop; removed so there's exactly one
    # implementation to keep in sync with.


def _ladspa_search_paths():
    """Non-/usr locations to probe for a LADSPA plugin, beyond
    LADSPA_PATH and a hardcoded /usr candidate list: a user nix
    profile, the system profile, and the current user's per-user
    profile.  Shared by every LADSPA-backed node (NoiseCancelNode,
    SensitivityGateNode, ...) so a distro that keeps plugins out of
    /usr (NixOS) is handled once, not per-node."""
    bases = [
        os.path.expanduser("~/.nix-profile/lib/ladspa"),
        os.path.expanduser("~/.nix-profile/lib64/ladspa"),
        "/run/current-system/sw/lib/ladspa",
    ]
    user = os.environ.get("USER", "")
    if user:
        bases.append(f"/etc/profiles/per-user/{user}/lib/ladspa")
    return bases


class NoiseCancelNode(_ChainEffect):
    """RNNoise LADSPA denoiser (librnnoise_ladspa.so,
    "noise_suppressor_stereo").  ``vad_threshold`` is a live LADSPA
    control pushed straight down the module's own pw-cli session -
    no reload on every slider tick.

    ``noise_suppressor_stereo`` (not ``_mono``): the node's capture and
    playback streams are stereo ``[ FL FR ]``, and placing the *mono*
    variant between them made the module collapse/drop a channel - the
    "AI Noise Cancel is inconsistent / kills the audio" report.  The
    plugin ships both labels (rnnoise-plugin >= 1.10); the stereo one
    keeps both channels intact.  A session saved with an explicit
    ``ladspa_label`` override still wins, so nothing silently changes
    for anyone who picked the mono label on purpose.

    Its filter graph names the plugin's audio ports explicitly (``inputs``
    / ``outputs``) instead of relying on filter-chain's auto-wiring.  The
    stereo plugin is 2-in/2-out; auto-wiring it to the 2-channel
    capture/playback produced a module that loaded but carried no audio,
    leaving the node a dead insert in the chain.  NormalizeNode - the
    other LADSPA effect - always named its ports and never showed this.
    The mono legacy label keeps its single ``Input``/``Output`` names.

    ensure_module() locates the plugin by probing the standard
    non-/usr install locations: LADSPA_PATH (what the shell/daemon
    exports), a user nix profile, the system profile, and a scan of
    /nix/store.  Without this the module silently fails to load on a
    distro that keeps plugins out of /usr (NixOS) - which takes the
    whole node's audio with it."""

    LABEL = "noise_suppressor_stereo"
    # Older rnnoise-ladspa builds only ship the mono label; configs that
    # explicitly set ladspa_label still override this.
    LEGACY_MONO_LABEL = "noise_suppressor_mono"
    _CANDIDATES = (
        "/usr/lib/ladspa/librnnoise_ladspa.so",
        "/usr/lib/x86_64-linux-gnu/ladspa/librnnoise_ladspa.so",
        "/usr/lib64/ladspa/librnnoise_ladspa.so",
        "/usr/lib/ladspa/rnnoise_ladspa.so",
    )

    def __init__(self, node_id, backing_node_name: str,
                 vad_threshold: float = 50.0,
                 ladspa_plugin: str = "", ladspa_label: str = "",
                 method: str = "rnnoise",
                 pw_cli_command=("pw-cli",), settle: float = 0.3, **_ignored):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle)
        self.vad_threshold = max(0.0, min(100.0, vad_threshold))
        self.ladspa_plugin = ladspa_plugin
        self.ladspa_label = ladspa_label
        self._control_applied_to: Optional[int] = None

    @classmethod
    def _plugin_candidates(cls):
        rel = "librnnoise_ladspa.so"
        candidates = list(cls._CANDIDATES)
        for base in _ladspa_search_paths():
            candidates.append(os.path.join(base, rel))
        for entry in os.environ.get("LADSPA_PATH", "").split(":"):
            entry = entry.strip()
            if entry:
                candidates.append(os.path.join(entry, rel))
        store = _store_ladspa_candidate("rnnoise-plugin", f"lib/ladspa/{rel}")
        if store:
            candidates.append(store)
        return candidates

    def _resolve_plugin(self):
        """(plugin path, label) for the filter graph: the explicit config
        override wins; otherwise the first candidate that actually exists
        on this machine; otherwise the first candidate anyway so a load
        failure at least names what was tried."""
        if self.ladspa_plugin and self.ladspa_label:
            return self.ladspa_plugin, self.ladspa_label
        for path in self._plugin_candidates():
            if path and os.path.isfile(path):
                return path, self.LABEL
        return self._CANDIDATES[0], self.LABEL

    def _module_command_args(self) -> str:
        plugin, label = self._resolve_plugin()
        node_name = f"{self.backing_node_name}_plugin"
        # Name the plugin's audio ports explicitly instead of relying on
        # filter-chain's auto-wiring.  This is the only LADSPA effect left
        # that did not (NormalizeNode always has), and the stereo RNNoise
        # variant is 2-in/2-out: auto-wiring a 2-channel capture/playback
        # onto it produced a module that loaded but moved no audio, so the
        # node sat in the chain as a dead insert - the "AI Noise Cancel
        # breaks the chain" report.  The mono legacy label has one port
        # pair, so keep its names for an explicit override to mono.
        if label == self.LEGACY_MONO_LABEL:
            inputs = f'"{node_name}:Input"'
            outputs = f'"{node_name}:Output"'
        else:
            inputs = f'"{node_name}:Input (L)" "{node_name}:Input (R)"'
            outputs = f'"{node_name}:Output (L)" "{node_name}:Output (R)"'
        return (
            f'node.description = "{self.id}" '
            "filter.graph = { nodes = [ { "
            "type = ladspa "
            f"name = {node_name} "
            f"plugin = {plugin} "
            f"label = {label} "
            f'control = {{ "VAD Threshold (%)" = {self.vad_threshold:.2f} }} '
            "} ] "
            f"inputs = [ {inputs} ] "
            f"outputs = [ {outputs} ] }} "
            "capture.props = { "
            f'node.name = "{self._capture_name}" '
            f'node.description = "{self._capture_name}" '
            "media.class = Audio/Sink "
            "audio.position = [ FL FR ] } "
            "playback.props = { "
            f'node.name = "{self._playback_name}" '
            f'node.description = "{self._playback_name}" '
            "media.class = Audio/Source "
            "audio.position = [ FL FR ] }"
        )

    def _apply_control(self) -> None:
        mod = self.module_backing()
        if mod is None or mod.node_id is None:
            return
        if mod.node_id == self._control_applied_to:
            return
        mod.set_param("Props", f'{{ params = [ "VAD Threshold (%)" {self.vad_threshold:.2f} ] }}')
        self._control_applied_to = mod.node_id

    def set_vad_threshold(self, value: float) -> None:
        self.vad_threshold = max(0.0, min(100.0, value))
        self._control_applied_to = None
        self._apply_control()

    def refresh_live(self) -> None:
        self._apply_control()


class ReverbNode(_ChainEffect):
    """Stereo reverb built on Calf Studio Gear's LV2 "Calf Reverb".

    This is the one effect here that is LV2 rather than LADSPA.  LV2
    plugins are located *by URI* through ``LV2_PATH`` (which the dev
    shell exports - see flake.nix), not by an absolute ``.so`` path, so
    there is no plugin-path probing like the LADSPA effects need.  The
    previous implementation pointed at ``/usr/lib/ladspa/caps.so``
    (absent on NixOS), used a control name CAPS never had (``dry/wet``
    instead of ``blend``), and picked the mono-in CAPS Plate which has
    no wet/dry at all.

    ``wet_dry`` (0..1) crossfades the plugin's wet ``amount`` against
    its ``dry`` level - a real wet/dry for a normal insert effect.  The
    reverb character (decay, room size, damping, ...) lives in the
    Settings dialog; every control is a load-time filter-graph value, so
    a change schedules an interior-only module reload (the dummies keep
    every user edge attached)."""

    DEFAULT_URI = "http://calf.sourceforge.net/plugins/Reverb"

    DECAY_MIN_S, DECAY_MAX_S = 0.4, 15.0
    ROOM_MIN, ROOM_MAX = 0.0, 5.0
    DIFFUSION_MIN, DIFFUSION_MAX = 0.0, 1.0
    DAMP_MIN_HZ, DAMP_MAX_HZ = 2000.0, 20000.0
    PREDELAY_MIN_MS, PREDELAY_MAX_MS = 0.0, 500.0

    def __init__(self, node_id, backing_node_name: str,
                 wet_dry: float = 0.3,
                 decay_time: float = 1.5, room_size: float = 2.0,
                 diffusion: float = 0.5, hf_damp: float = 5000.0,
                 predelay: float = 0.0, plugin_uri: str = "",
                 pw_cli_command=("pw-cli",), settle: float = 0.3, **_ignored):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle)
        self.plugin_uri = plugin_uri or self.DEFAULT_URI
        self.wet_dry = _clamp(wet_dry, 0.0, 1.0)
        self.decay_time = _clamp(decay_time, self.DECAY_MIN_S, self.DECAY_MAX_S)
        self.room_size = _clamp(room_size, self.ROOM_MIN, self.ROOM_MAX)
        self.diffusion = _clamp(
            diffusion, self.DIFFUSION_MIN, self.DIFFUSION_MAX
        )
        self.hf_damp = _clamp(hf_damp, self.DAMP_MIN_HZ, self.DAMP_MAX_HZ)
        self.predelay = _clamp(
            predelay, self.PREDELAY_MIN_MS, self.PREDELAY_MAX_MS
        )

    def _module_command_args(self) -> str:
        # true wet/dry crossfade: wet amount up as the dry level drops.
        wet = self.wet_dry
        dry = 1.0 - self.wet_dry
        return (
            f'node.description = "{self.id}" '
            "filter.graph = { nodes = [ { "
            "type = lv2 "
            f"name = {self.backing_node_name}_plugin "
            f'plugin = "{self.plugin_uri}" '
            "control = { "
            f'"amount" = {wet:.4f} '
            f'"dry" = {dry:.4f} '
            f'"decay_time" = {self.decay_time:.4f} '
            f'"room_size" = {self.room_size:.4f} '
            f'"diffusion" = {self.diffusion:.4f} '
            f'"hf_damp" = {self.hf_damp:.2f} '
            f'"predelay" = {self.predelay:.2f} '
            "} } ] } "
            "capture.props = { "
            f'node.name = "{self._capture_name}" '
            f'node.description = "{self._capture_name}" '
            "media.class = Audio/Sink "
            "audio.position = [ FL FR ] } "
            "playback.props = { "
            f'node.name = "{self._playback_name}" '
            f'node.description = "{self._playback_name}" '
            "media.class = Audio/Source "
            "audio.position = [ FL FR ] }"
        )


class NormalizeNode(_ChainEffect):
    """Loudness normalization / auto-gain that can't blast on resume.

    Two swh-plugins LADSPA stages in one linked stereo filter-chain:

        in -> sc4 (leveling compressor) -> fastLookaheadLimiter -> out

    ``fastLookaheadLimiter`` is a lookahead brickwall limiter: it sees a
    few ms ahead, so a high input gain can lift quiet, far-from-mic
    speech without the momentary blast a feedback AGC produces when
    speech resumes after silence (the classic "wait, then talk"
    earrape).  ``sc4`` ahead of it is a *downward* compressor - it only
    ever reduces gain, so it never winds up during silence - and it
    tames loud, close speech before the limiter has to.

    Unlike the mono effects (rnnoise/gate), these are stereo plugins, so
    the graph names its ports and links explicitly rather than relying
    on filter-chain's per-channel auto-duplication.

    All controls are load-time filter-graph values: changing one
    schedules an interior-only module reload (the dummies keep every
    user edge attached), the same proven pattern ReverbNode uses.  The
    live filter-chain ``Props`` set-param path is deliberately not
    relied on - see SensitivityGateNode's caveat."""

    # swh-plugins shared objects/labels (already a daemon dependency for
    # SensitivityGateNode's gate_1410.so).
    SC4_FILE = "sc4_1882.so"
    SC4_LABEL = "sc4"
    LIMITER_FILE = "fast_lookahead_limiter_1913.so"
    LIMITER_LABEL = "fastLookaheadLimiter"

    # Control ranges, clamped on the way in so a bad saved config or a
    # mistyped Settings value can't ask for something absurd.
    BOOST_MIN_DB, BOOST_MAX_DB = 0.0, 30.0
    CEILING_MIN_DB, CEILING_MAX_DB = -20.0, 0.0
    THRESHOLD_MIN_DB, THRESHOLD_MAX_DB = -60.0, 0.0
    RATIO_MIN, RATIO_MAX = 1.0, 20.0
    ATTACK_MIN_MS, ATTACK_MAX_MS = 0.1, 200.0
    RELEASE_MIN_MS, RELEASE_MAX_MS = 10.0, 2000.0
    KNEE_MIN_DB, KNEE_MAX_DB = 0.0, 24.0
    LIMITER_RELEASE_MIN_S, LIMITER_RELEASE_MAX_S = 0.01, 5.0

    def __init__(self, node_id, backing_node_name: str,
                 boost_db: float = 15.0, max_boost_db: float = 24.0,
                 ceiling_db: float = -1.0, leveling: bool = True,
                 threshold_db: float = -35.0, ratio: float = 4.0,
                 attack_ms: float = 10.0, release_ms: float = 400.0,
                 knee_db: float = 6.0, limiter_release_s: float = 0.5,
                 ladspa_dir: str = "",
                 pw_cli_command=("pw-cli",), settle: float = 0.3, **_ignored):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle)
        self.boost_db = _clamp(boost_db, self.BOOST_MIN_DB, self.BOOST_MAX_DB)
        self.max_boost_db = _clamp(
            max_boost_db, self.BOOST_MIN_DB, self.BOOST_MAX_DB
        )
        self.ceiling_db = _clamp(
            ceiling_db, self.CEILING_MIN_DB, self.CEILING_MAX_DB
        )
        self.leveling = bool(leveling)
        self.threshold_db = _clamp(
            threshold_db, self.THRESHOLD_MIN_DB, self.THRESHOLD_MAX_DB
        )
        self.ratio = _clamp(ratio, self.RATIO_MIN, self.RATIO_MAX)
        self.attack_ms = _clamp(attack_ms, self.ATTACK_MIN_MS, self.ATTACK_MAX_MS)
        self.release_ms = _clamp(
            release_ms, self.RELEASE_MIN_MS, self.RELEASE_MAX_MS
        )
        self.knee_db = _clamp(knee_db, self.KNEE_MIN_DB, self.KNEE_MAX_DB)
        self.limiter_release_s = _clamp(
            limiter_release_s,
            self.LIMITER_RELEASE_MIN_S,
            self.LIMITER_RELEASE_MAX_S,
        )
        self.ladspa_dir = ladspa_dir or ""

    def effective_boost_db(self) -> float:
        """The gain actually handed to the limiter: ``boost_db`` capped
        by the user's ``max_boost_db`` ceiling."""
        return min(self.boost_db, self.max_boost_db)

    @classmethod
    def _candidate_paths(cls, rel: str):
        candidates = [
            f"/usr/lib/ladspa/{rel}",
            f"/usr/lib/x86_64-linux-gnu/ladspa/{rel}",
            f"/usr/lib64/ladspa/{rel}",
        ]
        for base in _ladspa_search_paths():
            candidates.append(os.path.join(base, rel))
        for entry in os.environ.get("LADSPA_PATH", "").split(":"):
            entry = entry.strip()
            if entry:
                candidates.append(os.path.join(entry, rel))
        store = _store_ladspa_candidate("swh-plugins", f"lib/ladspa/{rel}")
        if store:
            candidates.append(store)
        return candidates

    def _resolve_plugins(self):
        """(sc4_path, limiter_path).  A ``ladspa_dir`` override wins when
        it actually holds both files; otherwise the standard swh install
        dirs / LADSPA_PATH / nix store are probed.  A miss falls back to
        the first candidate so a load failure at least names what was
        tried."""
        if self.ladspa_dir:
            sc4 = os.path.join(self.ladspa_dir, self.SC4_FILE)
            lim = os.path.join(self.ladspa_dir, self.LIMITER_FILE)
            if os.path.isfile(sc4) and os.path.isfile(lim):
                return sc4, lim
        sc4_candidates = self._candidate_paths(self.SC4_FILE)
        lim_candidates = self._candidate_paths(self.LIMITER_FILE)
        sc4 = next(
            (p for p in sc4_candidates if p and os.path.isfile(p)), sc4_candidates[0]
        )
        lim = next(
            (p for p in lim_candidates if p and os.path.isfile(p)),
            lim_candidates[0],
        )
        return sc4, lim

    def _module_command_args(self) -> str:
        comp = f"{self.backing_node_name}_comp"
        lim = f"{self.backing_node_name}_lim"
        sc4_path, lim_path = self._resolve_plugins()

        nodes = "[ "
        if self.leveling:
            nodes += (
                "{ type = ladspa "
                f"name = {comp} plugin = {sc4_path} label = {self.SC4_LABEL} "
                "control = { "
                '"RMS/peak" = 1.0 '
                f'"Attack time (ms)" = {self.attack_ms:.2f} '
                f'"Release time (ms)" = {self.release_ms:.2f} '
                f'"Threshold level (dB)" = {self.threshold_db:.2f} '
                f'"Ratio (1:n)" = {self.ratio:.2f} '
                f'"Knee radius (dB)" = {self.knee_db:.2f} '
                '"Makeup gain (dB)" = 0.0 '
                "} } "
            )
        nodes += (
            "{ type = ladspa "
            f"name = {lim} plugin = {lim_path} label = {self.LIMITER_LABEL} "
            "control = { "
            f'"Input gain (dB)" = {self.effective_boost_db():.2f} '
            f'"Limit (dB)" = {self.ceiling_db:.2f} '
            f'"Release time (s)" = {self.limiter_release_s:.3f} '
            "} } "
            "]"
        )

        if self.leveling:
            graph = (
                f"filter.graph = {{ nodes = {nodes} "
                "links = [ "
                f'{{ output = "{comp}:Left output" input = "{lim}:Input 1" }} '
                f'{{ output = "{comp}:Right output" input = "{lim}:Input 2" }} '
                "] "
                f'inputs = [ "{comp}:Left input" "{comp}:Right input" ] '
                f'outputs = [ "{lim}:Output 1" "{lim}:Output 2" ] }} '
            )
        else:
            graph = (
                f"filter.graph = {{ nodes = {nodes} "
                f'inputs = [ "{lim}:Input 1" "{lim}:Input 2" ] '
                f'outputs = [ "{lim}:Output 1" "{lim}:Output 2" ] }} '
            )

        return (
            f'node.description = "{self.id}" '
            + graph
            + "capture.props = { "
            f'node.name = "{self._capture_name}" '
            f'node.description = "{self._capture_name}" '
            "media.class = Audio/Sink "
            "audio.position = [ FL FR ] } "
            "playback.props = { "
            f'node.name = "{self._playback_name}" '
            f'node.description = "{self._playback_name}" '
            "media.class = Audio/Source "
            "audio.position = [ FL FR ] }"
        )


class SensitivityGateNode(_ChainEffect):
    """A voice-activity gate, currently Calf Studio Gear's LV2 "Gate"
    (label ``gate``) - a real downward expander.

    READ THIS FIRST IF SENSITIVITY BREAKS AGAIN.  This node has been
    through several designs, each of which failed in a specific,
    reproducible way; they are recorded here so the same dead ends
    aren't re-tried:

    1. Gain-staging around a fixed swh ``gate_1410`` LADSPA gate (the
       previous design).  The hidden pre/post ``VolumeProcessNode``s the
       daemon brackets the gate with were supposed to make the slider
       "more/less sensitive" by boosting/attenuating the signal ahead of
       a fixed threshold, the post node undoing the gain so loudness
       stayed constant.  It never worked here: the pre node's
       ``monitor.channel-volumes`` volume is **not applied through the
       filter-chain's capture stream** (measured: the gate's input level
       does not change with the pre node's volume), so the only audible
       effect of moving the slider was the post node cutting output -
       sliding sensitivity *up* made the audio *quieter*, and at the low
       end it went silent.  Do not re-introduce a pre-gain stage that
       relies on a null-sink monitor volume feeding a ``_ChainEffect``;
       verify with a tone whether the gain actually reaches the module
       before trusting it.

    2. Live ``set_param("Props", ...)`` of the gate's own threshold via
       the daemon's persistent pw-cli session.  A *fresh* interactive
       ``pw-cli`` session's ``set-param <id> Props '{ params = [
       "threshold" X ] }'`` does move the running gate, but the same
       command written to the pw-cli session that loaded the module
       (``OwnedPwNode.set_param``) does **not** take effect in practice.
       This is why ``threshold`` is treated as a load-time filter-graph
       control now: ``set_sensitivity``/``set_level`` just record the
       value and the daemon schedules a debounced *interior reload*
       (main.py's ``_coalesce_reload``, the same proven path ReverbNode
       uses for ``wet_dry``), so the module is rebuilt with the new
       threshold with a brief gap after a drag settles - no live
       set-param involved.

    The hidden pre/post ``VolumeProcessNode``s are kept (created by
    main.py's ``_ensure_sensitivity_internals``) purely as unity
    pass-throughs so the existing edge routing, serialization and saved
    sessions don't change.  The gate itself is now the only thing that
    gates.

    3. Separately, those hidden pre/post nodes used to be torn down and
    rebuilt in a loop ("live object disappeared while alive" /
    "structural backing ... degraded") whenever a big session loaded:
    the load held the daemon lock while creating every node, so the
    graph's node-created callbacks couldn't resolve a hidden node until
    after ``RESOLVE_GRACE_S`` had already elapsed, and the first
    supervision tick judged it stuck.  ``_load_session`` now stages every
    backed node (including these hidden ones) until its own bring-up
    turn; see ``test_load_stages_hidden_nodes_so_a_mid_load_tick_cannot_
    prune_them``.  If that thrash reappears, check that staging, not the
    gate.

    Calf's Gate is stereo 2-in/2-out and LV2, found by URI via LV2_PATH
    like ReverbNode (no plugin-path probing; needs calf in the daemon's
    environment).  ``sensitivity`` (0..1) is the inline slider; it maps
    onto ``level`` (0..100), where higher sensitivity = a lower (easier
    to cross) threshold."""

    LV2_URI = "http://calf.sourceforge.net/plugins/Gate"

    # Defaults for Calf Gate's tuning controls.  They used to be baked in
    # as class constants; they are now per-node values the Settings dialog
    # exposes.  Like `sensitivity`/`level` these are load-time
    # filter-graph controls, so changing one schedules the same debounced
    # interior reload (main.py's _coalesce_reload).
    RATIO = 4.0
    ATTACK_MS = 5.0
    # Long default hold so the gate doesn't chatter shut between words;
    # 2000 ms is Calf's own release maximum.
    RELEASE_MS = 2000.0
    KNEE = 6.0
    MAKEUP = 1.0
    # How far the closed gate ducks the signal.  Calf's own default is
    # -24 dB, which leaves obvious background bleed; -96 dB is its
    # minimum gain (1.5849e-05 linear) and effectively silence, so an
    # inactive gate actually gets out of the way.
    RANGE_DB = -96.0

    RATIO_MIN, RATIO_MAX = 1.0, 20.0
    ATTACK_MIN_MS, ATTACK_MAX_MS = 0.0, 200.0
    RELEASE_MIN_MS, RELEASE_MAX_MS = 0.0, 2000.0
    KNEE_MIN, KNEE_MAX = 0.0, 12.0
    MAKEUP_MIN, MAKEUP_MAX = 0.0, 10.0
    # 20*log10(1.5849e-05) ~= -96 dB, Calf's own `range` minimum.
    RANGE_DB_MIN, RANGE_DB_MAX = -96.0, 0.0

    # level 0 -> most sensitive (opens on a whisper), level 100 -> least
    # sensitive (needs a loud, close voice).
    THRESHOLD_DB_AT_LEVEL_0 = -45.0
    THRESHOLD_DB_AT_LEVEL_100 = -15.0
    DEFAULT_LEVEL = 25.0

    def __init__(self, node_id, backing_node_name: str,
                 level: Optional[float] = None,
                 sensitivity: Optional[float] = None,
                 lv2_uri: str = "",
                 ratio: Optional[float] = None,
                 attack_ms: Optional[float] = None,
                 release_ms: Optional[float] = None,
                 knee_db: Optional[float] = None,
                 makeup: Optional[float] = None,
                 range_db: Optional[float] = None,
                 pw_cli_command=("pw-cli",), settle: float = 0.3, **_ignored):
        super().__init__(node_id, backing_node_name, pw_cli_command, settle)
        self.lv2_uri = lv2_uri or ""
        # `sensitivity` (the inline 0..1 slider) is the single source of
        # truth; `level` is just its inverse on a 0..100 scale.  Derive
        # whichever one wasn't supplied so a saved config carrying only
        # the legacy `level` (or only `sensitivity`) round-trips, and a
        # brand-new node can never come up with the two disagreeing -
        # which previously showed an empty slider next to "25" in
        # Settings (level defaulted to 25 while sensitivity defaulted to
        # 0.0, even though 0.0 implies level 100).
        if sensitivity is None:
            if level is None:
                level = self.DEFAULT_LEVEL
            self.set_level(level)
        else:
            self.set_sensitivity(sensitivity)
        self.ratio = _clamp(
            self.RATIO if ratio is None else ratio,
            self.RATIO_MIN, self.RATIO_MAX,
        )
        self.attack_ms = _clamp(
            self.ATTACK_MS if attack_ms is None else attack_ms,
            self.ATTACK_MIN_MS, self.ATTACK_MAX_MS,
        )
        self.release_ms = _clamp(
            self.RELEASE_MS if release_ms is None else release_ms,
            self.RELEASE_MIN_MS, self.RELEASE_MAX_MS,
        )
        self.knee_db = _clamp(
            self.KNEE if knee_db is None else knee_db,
            self.KNEE_MIN, self.KNEE_MAX,
        )
        self.makeup = _clamp(
            self.MAKEUP if makeup is None else makeup,
            self.MAKEUP_MIN, self.MAKEUP_MAX,
        )
        self.range_db = _clamp(
            self.RANGE_DB if range_db is None else range_db,
            self.RANGE_DB_MIN, self.RANGE_DB_MAX,
        )

    @classmethod
    def level_to_threshold_linear(cls, level: float) -> float:
        """0-100 level -> Calf's linear amplitude ``threshold`` port
        (its "threshold" is linear despite being labelled dBFS)."""
        db = cls.THRESHOLD_DB_AT_LEVEL_0 + (
            cls.THRESHOLD_DB_AT_LEVEL_100 - cls.THRESHOLD_DB_AT_LEVEL_0
        ) * (_clamp(level, 0.0, 100.0) / 100.0)
        return 10.0 ** (db / 20.0)

    @classmethod
    def sensitivity_to_level(cls, sensitivity: float) -> float:
        return (1.0 - _clamp(sensitivity, 0.0, 1.0)) * 100.0

    def _threshold_linear(self) -> float:
        return self.level_to_threshold_linear(self.level)

    def _module_command_args(self) -> str:
        uri = self.lv2_uri or self.LV2_URI
        threshold = self._threshold_linear()
        return (
            f'node.description = "{self.id}" '
            "filter.graph = { nodes = [ { "
            "type = lv2 "
            f"name = {self.backing_node_name}_plugin "
            f'plugin = "{uri}" '
            "control = { "
            f'"threshold" = {threshold:.6f} '
            f'"range" = {10.0 ** (self.range_db / 20.0):.8f} '
            f'"ratio" = {self.ratio:.2f} '
            f'"attack" = {self.attack_ms:.2f} '
            f'"release" = {self.release_ms:.2f} '
            f'"knee" = {self.knee_db:.4f} '
            f'"makeup" = {self.makeup:.2f} '
            "} } ] } "
            "capture.props = { "
            f'node.name = "{self._capture_name}" '
            f'node.description = "{self._capture_name}" '
            "media.class = Audio/Sink "
            "audio.position = [ FL FR ] } "
            "playback.props = { "
            f'node.name = "{self._playback_name}" '
            f'node.description = "{self._playback_name}" '
            "media.class = Audio/Source "
            "audio.position = [ FL FR ] }"
        )

    # ``threshold`` is a load-time filter-graph control (the live
    # filter-chain set-param path does not reliably reach the plugin
    # through the daemon's own pw-cli session), so changing sensitivity
    # schedules an interior-only module reload via the daemon's
    # _coalesce_reload - the dummies keep every user edge attached, so
    # only a brief gap is heard after a drag settles.
    def set_level(self, value: float) -> None:
        self.level = _clamp(value, 0.0, 100.0)
        self.sensitivity = 1.0 - self.level / 100.0

    def set_sensitivity(self, value: float) -> None:
        self.sensitivity = _clamp(value, 0.0, 1.0)
        self.level = self.sensitivity_to_level(self.sensitivity)

    def refresh_live(self) -> None:
        """No live control to push.

        The gate's threshold/tuning are baked into the filter graph at
        load time, and the hidden pre/post volume nodes are unity
        pass-throughs (see main.py's _apply_sensitivity), so there is
        nothing to re-apply here.  The method exists only because the
        shared supervision path calls it on every resolved backing
        (pwnodes._on_backing_resolved) - without it that call raised
        AttributeError, aborting the rest of the node's supervision step
        (the "Bring-up step ... has no attribute 'refresh_live'" warnings
        in the daemon log)."""
        return None


class EchoCancelNode(BackedNode):
    """PipeWire's own libpipewire-module-echo-cancel (WebRTC AEC),
    exposed as three stable dummy sockets sharing one module:

        mic   -> [ mic_in ]  --link->  module capture
        probe -> [ probe_in ] --link->  module sink
        module source --link-> [ out ]  (what consumers pick up)

    The module's hidden playback stream is drained into a node-owned
    throwaway sink so the AEC graph stays scheduled without ever
    doubling the reference into the user's speakers.  ``monitor_mode``
    makes the module auto-capture the default sink instead of waiting
    for a manual probe feed.

    ``library_name`` / ``aec_args`` / ``monitor_mode`` are load-time
    module options; changing any schedules an interior-only reload."""

    DEFAULT_AEC_LIBRARY = "aec/libspa-aec-webrtc"

    def __init__(self, node_id, backing_node_name: str,
                 library_name: str = "", aec_args: str = "",
                 monitor_mode: bool = False,
                 pw_cli_command=("pw-cli",), settle: float = 0.3):
        super().__init__(node_id, backing_node_name)
        self.library_name = library_name or self.DEFAULT_AEC_LIBRARY
        self.aec_args = aec_args
        self.monitor_mode = bool(monitor_mode)
        self._pw_cli_command = pw_cli_command
        self._settle = settle

    # -- naming ----------------------------------------------------------

    @property
    def _mic_name(self) -> str:
        return self.backing_node_name

    @property
    def _probe_name(self) -> str:
        return f"{self.backing_node_name}_probe"

    @property
    def _source_name(self) -> str:
        return f"{self.backing_node_name}_fx_out"

    @property
    def _playback_name(self) -> str:
        return f"{self.backing_node_name}_playback"

    @property
    def _playback_sink_name(self) -> str:
        return f"{self.backing_node_name}_playback_sink"

    @property
    def _mic_dummy_name(self) -> str:
        return f"{self.backing_node_name}_mic_in"

    @property
    def _probe_dummy_name(self) -> str:
        return f"{self.backing_node_name}_probe_in"

    @property
    def _out_dummy_name(self) -> str:
        return f"{self.backing_node_name}_out"

    def _kl(self, base: str) -> str:
        return f"{base}_keepalive"

    # -- identities / plumbing -------------------------------------------

    def input_identity(self, port: str = "in") -> dict:
        if port == "probe":
            return {"name": self._probe_dummy_name}
        return {"name": self._mic_dummy_name}

    def output_identity(self) -> dict:
        return {"nodeName": self._out_dummy_name}

    def internal_links(self):
        links = [
            ({"nodeName": self._mic_dummy_name}, {"name": self._mic_name}),
            ({"nodeName": self._probe_dummy_name}, {"name": self._probe_name}),
            ({"nodeName": self._source_name}, {"name": self._out_dummy_name}),
        ]
        if not self.monitor_mode:
            links.append(
                ({"nodeName": self._playback_name}, {"name": self._playback_sink_name})
            )
        return links

    # -- health ----------------------------------------------------------

    def has_module(self) -> bool:
        return True

    def module_backing(self) -> Optional[OwnedPwNode]:
        return self._find(self._mic_name)

    def module_ok(self) -> bool:
        b = self.module_backing()
        return b is not None and b.owns_process and b.is_alive and not b.stuck(
            self.RESOLVE_GRACE_S
        )

    def structural_ok(self) -> bool:
        need = {self._mic_dummy_name, self._probe_dummy_name, self._out_dummy_name,
                self._kl(self._mic_dummy_name), self._kl(self._out_dummy_name)}
        if not self.monitor_mode:
            need |= {self._probe_dummy_name, self._playback_sink_name,
                     self._kl(self._probe_dummy_name), self._kl(self._playback_sink_name)}
        for name in need:
            b = self._find(name)
            if b is None:
                return False
            if b.owns_process and (not b.is_alive or b.stuck(self.RESOLVE_GRACE_S)):
                return False
        return True

    # -- module -----------------------------------------------------------

    def _stream_props(self, name: str, autoconnect: bool = False) -> str:
        extra = "" if autoconnect else " node.autoconnect = false"
        return f'{{ node.name = "{name}" node.description = "{name}"{extra} }}'

    def _spawn_module(self) -> Optional[OwnedPwNode]:
        if self._find(self._mic_name) is not None:
            return self._find(self._mic_name)
        parts = [f"library.name = {self.library_name}"]
        if self.monitor_mode:
            parts.append("monitor.mode = true")
        if (self.aec_args or "").strip():
            parts.append(f"aec.args = {{ {self.aec_args} }}")
        parts += [
            f"capture.props = {self._stream_props(self._mic_name)}",
            f"sink.props = {self._stream_props(self._probe_name, autoconnect=self.monitor_mode)}",
            f"source.props = {self._stream_props(self._source_name)}",
            f"playback.props = {self._stream_props(self._playback_name)}",
        ]
        command = "load-module libpipewire-module-echo-cancel { " + " ".join(parts) + " }"
        owned = OwnedPwNode(self._mic_name, self._pw_cli_command, self._settle)
        if not owned.create(command):
            logger.error("Echo-cancel module creation failed for %r", self.id)
            return None
        self.backings.append(owned)
        self.backings.append(OwnedPwNode(self._probe_name))
        self.backings.append(OwnedPwNode(self._source_name))
        if not self.monitor_mode:
            self.backings.append(OwnedPwNode(self._playback_name))
        return owned

    def _module_children(self) -> Set[str]:
        names = {self._mic_name, self._probe_name, self._source_name}
        if not self.monitor_mode:
            names.add(self._playback_name)
        return names

    # Echo Cancel keeps its out-dummy drain created once and left alone -
    # restarting it was measured to lose the AEC's output entirely.  Its
    # one module-stream tap (the playback-sink drain) is re-pointed by
    # ensure_module below, which covers both the fresh-spawn and reload
    # paths.  _ChainEffect now follows the same "never tear down the out
    # drain" rule; do not add a re-point step back for either.

    # -- lifecycle --------------------------------------------------------

    def ensure_structural(self) -> None:
        kls = self._kl
        structural = {self._mic_dummy_name, self._probe_dummy_name,
                      self._out_dummy_name, self._playback_sink_name,
                      kls(self._mic_dummy_name), kls(self._probe_dummy_name),
                      kls(self._out_dummy_name), kls(self._playback_sink_name)}
        self._prune_dead(structural)
        self._ensure_null_sink(self._mic_dummy_name, description=f"{self.id} mic",
                               pw_cli_command=self._pw_cli_command, settle=self._settle)
        self._ensure_null_sink(self._probe_dummy_name, description=f"{self.id} probe",
                               pw_cli_command=self._pw_cli_command, settle=self._settle)
        self._ensure_null_sink(self._out_dummy_name, description=f"{self.id} out",
                               pw_cli_command=self._pw_cli_command, settle=self._settle)
        if not self.monitor_mode:
            self._ensure_null_sink(self._playback_sink_name,
                                   description=f"{self.id} playback",
                                   pw_cli_command=self._pw_cli_command, settle=self._settle)
        self._ensure_feed(kls(self._mic_dummy_name), self._mic_dummy_name,
                          self._pw_cli_command, self._settle)
        if self.monitor_mode:
            # monitor.mode auto-captures the default sink - a manual
            # probe feed would be a redundant second one, so make sure a
            # stale one from an earlier non-monitor config is gone.
            self._drop(kls(self._probe_dummy_name))
        else:
            self._ensure_feed(kls(self._probe_dummy_name), self._probe_dummy_name,
                              self._pw_cli_command, self._settle)
        self._ensure_drain(kls(self._out_dummy_name), self._out_dummy_name,
                           self._pw_cli_command, self._settle)
        if not self.monitor_mode:
            self._ensure_drain(kls(self._playback_sink_name), self._playback_sink_name,
                               self._pw_cli_command, self._settle)

    def ensure_module(self) -> None:
        mic = self._find(self._mic_name)
        fresh = mic is None or not (mic.owns_process and mic.is_alive) or mic.stuck(
            self.RESOLVE_GRACE_S
        )
        if fresh:
            for owned in list(self.backings):
                if owned.name in self._module_children():
                    owned.destroy()
                    if owned in self.backings:
                        self.backings.remove(owned)
            self._spawn_module()
        mod = self._find(self._mic_name)
        if fresh and mod is not None and mod.is_alive and not self.monitor_mode:
            # The playback-sink drain taps the old module's playback
            # stream; a fresh module needs a fresh tap.
            self._drop(self._kl(self._playback_sink_name))
        self.ensure_structural()

    def reload_module(self) -> None:
        """Interior-only reload: drop the module's own streams plus the
        mode-dependent extras (playback sink + its drain), then rebuild
        everything to the current config.  The mic/probe/out dummies and
        their keepalives are untouched, so user edges never drop."""
        drop_names = self._module_children() | {
            self._playback_sink_name, self._kl(self._playback_sink_name),
        }
        for owned in list(self.backings):
            if owned.name in drop_names:
                owned.destroy()
                if owned in self.backings:
                    self.backings.remove(owned)
        self.ensure_module()

    # teardown_backing() is inherited from BackedNode (parallel destroy)
    # - see the note on _ChainEffect.ensure_module above.


class LightNoiseCancelNode(EchoCancelNode):
    """A mic-only take on EchoCancelNode: identical
    libpipewire-module-echo-cancel backing and identical plumbing (mic/
    probe/out dummies, their keepalives, the module's own capture/
    playback streams), but the GUI exposes only the ``mic`` input - no
    user-visible ``probe`` socket - so it reads as a plain one-in/one-out
    noise suppressor rather than an echo canceller.

    The module still creates its probe stream internally (and the probe
    dummy is still fed), since libpipewire-module-echo-cancel always
    does; it simply isn't surfaced as a connectable port. Biasing the
    WebRTC plugin toward noise suppression over echo cancellation is a
    matter of the ``aec_args`` Settings value."""

    # The module is still the echo-cancel one, so the probe dummy must
    # keep existing and being fed - see ensure_structural on the base.
    pass


# ---------------------------------------------------------------------------
# The graph
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    id: EdgeId
    from_node: NodeId
    to_node: NodeId
    to_port: str = "in"
    # Which of the source node's output sockets this edge leaves from.
    # "out" is the single-output default every node type had until the
    # Switcher; its two outputs are named "a"/"b" (see SwitcherNode).
    from_port: str = "out"
    # True when this edge came from a declarative file (or was created by
    # the GUI as part of one).  Imperative edges are autosaved; declarative
    # ones are re-derived from their file.
    declarative: bool = False


@dataclass
class _DesiredLinks:
    """Per-edge bookkeeping: the exact (output, input) port pairs the
    last sync connected, plus a monotonic timestamp of the last time the
    edge was observed healthy (used to spot edges that silently stopped
    carrying audio)."""
    pairs: Set[Tuple[int, int]] = field(default_factory=set)


class PatchSpace:
    """Owns the node graph, drives the live PipeWire graph to match it,
    and supervises every backed node.

    ``sync()`` is the structural reconciliation pass - run after any
    edit.  ``supervise()`` is the health pass - run periodically by the
    daemon - which repairs dead backings, performs due interior reloads,
    re-enforces device/effect settings and then syncs again."""

    def __init__(self, graph: PipewireGraph, repair_gate: Optional[Backoff] = None):
        self.graph = graph
        self._lock = threading.RLock()

        self.nodes: Dict[NodeId, Node] = {}
        self.public_nodes: Set[NodeId] = set()
        self.edges: Dict[EdgeId, Edge] = {}
        self._edges_into: Dict[NodeId, List[Edge]] = {}
        self._edges_out_of: Dict[NodeId, List[Edge]] = {}

        # edge id -> desired pairs connected as of the last sync.
        self._edge_links: Dict[EdgeId, _DesiredLinks] = {}

        # Paced wire-up (see _apply_desired_links): pairs whose connect
        # has been issued but not yet confirmed by the live graph,
        # keyed by the pair itself -> (edge_id, issued_at). Unlike the
        # old single-slot design, more than one pair can be in flight
        # at once - only a candidate pair that shares a port with one
        # already in flight is held back (see _apply_desired_links),
        # so one slow-to-confirm link can no longer stall every other,
        # unrelated edge in the graph. Any pair whose edge was removed
        # while its connect was still in flight is queued for a
        # best-effort disconnect so it can't land orphaned.
        self._inflight_links: Dict[Tuple[int, int], Tuple[EdgeId, float]] = {}
        self._orphan_disconnects: Set[Tuple[int, int]] = set()

        # Per-pair backoff for links the live graph refuses to confirm.
        # ``pw-link`` can return success for a link it creates and
        # immediately drops (a Bluetooth sink that rejects the source's
        # format, a port that vanishes mid-negotiation), and without this
        # such a pair was re-issued every LINK_CONFIRM_TIMEOUT_S forever.
        # A confirmed pair clears its entry; a timed-out one backs off
        # exponentially (1s -> 30s), and removing the edge forgets it so a
        # deliberate re-add retries immediately.
        self._link_gate = Backoff(initial_s=1.0, max_s=30.0)

        # Per-(node, stage) repair backoff - see supervise().
        self._repair_gate = repair_gate or Backoff(initial_s=1.0, max_s=30.0)

        self.on_change: List[Any] = []
        self._graph_loaded = False

        # Node ids a caller (currently only _load_session) is bringing up
        # one at a time itself - see stage()/unstage(). supervise() skips
        # these entirely so the periodic tick can never race a staged
        # node's own deliberately-throttled bring-up by kicking off its
        # module load early. Nodes not in here are supervised as normal;
        # this is additive, not a replacement for the regular health pass.
        self._staging: Set[NodeId] = set()

        # Boolean warp names we've already warned about having more than
        # one publisher (see _boolean_warp_publisher) - without this the
        # warning would repeat every supervision tick.
        self._warned_bool_warps: Set[str] = set()

    # ------------------------------------------------------------------
    # staged bring-up (see _load_session in main.py)
    # ------------------------------------------------------------------

    def stage(self, node_ids) -> None:
        """Mark node ids as being brought up by someone else's own
        sequenced logic, so supervise() leaves them alone until
        unstage() is called - see the _staging docstring above."""
        with self._lock:
            self._staging.update(node_ids)

    def unstage(self, node_id: NodeId) -> None:
        """Release a node back to normal periodic supervision - call
        this once its dedicated bring-up is done, whether it succeeded
        or timed out, so it doesn't get skipped forever."""
        with self._lock:
            self._staging.discard(node_id)

    # ------------------------------------------------------------------
    # graph editing
    # ------------------------------------------------------------------

    def add_node(self, node: Node, public: bool = True) -> NodeId:
        with self._lock:
            self.nodes[node.id] = node
            if public:
                self.public_nodes.add(node.id)
            # Structural pieces are created synchronously so the node has
            # working sockets immediately.  DSP modules are materialised
            # lazily by supervise() (see the module docstring for why).
            if isinstance(node, BackedNode):
                try:
                    node.ensure_structural()
                except Exception as exc:
                    logger.warning("Failed to create structural backing for %r: %s",
                                   node.id, exc)
                # Kick module creation promptly rather than waiting for
                # the next tick.
                self._wake()
            return node.id

    def remove_node(self, node_id: NodeId) -> None:
        with self._lock:
            node = self.nodes.pop(node_id, None)
            if node is None:
                return
            self.public_nodes.discard(node_id)
            for edge in list(self._edges_into.get(node_id, [])) + list(
                self._edges_out_of.get(node_id, [])
            ):
                self._remove_edge_locked(edge.id)
            if isinstance(node, BackedNode):
                try:
                    node.teardown_backing()
                except Exception as exc:
                    logger.warning("Tearing down %r failed: %s", node_id, exc)
            for key in ((node_id, "structural"), (node_id, "module")):
                self._repair_gate.forget(key)

    def detach_nodes(self, node_ids) -> List[OwnedPwNode]:
        """Remove several nodes from the model in one pass and return
        every backing they owned, WITHOUT destroying anything.

        The caller destroys the returned backings - and must do so
        *outside* the daemon lock and in parallel.  `remove_node` tears a
        node's backings down (concurrently within the node) but nodes are
        removed one at a time, so deleting a graph full of effects costs
        the *sum* of each node's slowest process exit; batching them (and
        destroying all at once) costs only the single slowest.  Splitting
        the model mutation from the destruction also lets the slow part
        run without holding the daemon lock."""
        doomed: List[OwnedPwNode] = []
        with self._lock:
            for node_id in list(node_ids):
                node = self.nodes.pop(node_id, None)
                if node is None:
                    continue
                self.public_nodes.discard(node_id)
                for edge in list(self._edges_into.get(node_id, [])) + list(
                    self._edges_out_of.get(node_id, [])
                ):
                    self._remove_edge_locked(edge.id)
                if isinstance(node, BackedNode):
                    doomed.extend(node.owned_backings())
                for key in ((node_id, "structural"), (node_id, "module")):
                    self._repair_gate.forget(key)
        return doomed

    def add_edge(
        self,
        from_node: NodeId,
        to_node: NodeId,
        to_port: str = "in",
        from_port: str = "out",
        declarative: bool = False,
    ) -> EdgeId:
        with self._lock:
            if from_node not in self.nodes or to_node not in self.nodes:
                raise KeyError("both endpoints must already be added")
            target = self.nodes[to_node]
            source = self.nodes[from_node]
            from_kind = source.port_kind(from_port, "out")
            to_kind = target.port_kind(to_port, "in")
            # Boolean, filter and impulse wires are control-plane and
            # pair only with their own kind.  Audio and bundle ports mix
            # freely either way: a bundle is a set of audio streams, and
            # one stream is a bundle of one (see the bundle nodes).
            if (from_kind == "boolean") != (to_kind == "boolean"):
                raise ValueError(
                    f"cannot connect a {from_kind} output to a {to_kind} input"
                )
            if (from_kind == "filter") != (to_kind == "filter"):
                raise ValueError(
                    f"cannot connect a {from_kind} output to a {to_kind} input"
                )
            if (from_kind == "impulse") != (to_kind == "impulse"):
                raise ValueError(
                    f"cannot connect a {from_kind} output to a {to_kind} input"
                )
            if (from_kind == "sound") != (to_kind == "sound"):
                raise ValueError(
                    f"cannot connect a {from_kind} output to a {to_kind} input"
                )
            if from_kind not in (
                "boolean", "filter", "impulse", "sound", "bundle", "audio"
            ):
                raise ValueError(f"unknown port kind {from_kind!r}")
            if to_kind in ("boolean", "impulse"):
                # A boolean input is usually a single control signal, not a
                # mixable bus: exactly one source may drive it.  Panel
                # boolean ports opt out (ALLOW_MULTIPLE_BOOLEAN) so several
                # sources can be offered; the first wired one wins.  An
                # impulse input is the same - one button drives it.
                if getattr(target, "ALLOW_MULTIPLE_BOOLEAN", False):
                    pass
                else:
                    for existing in self._edges_into.get(to_node, []):
                        if existing.to_port == to_port:
                            raise ValueError(
                                f"{to_node}.{to_port} is already driven"
                            )
            elif to_kind == "sound":
                # A sound input takes exactly one sound: it is a reference to
                # a file (plus a range), not something to mix.
                for existing in self._edges_into.get(to_node, []):
                    if existing.to_port == to_port and self._edge_is_sound(existing):
                        raise ValueError(f"{to_node}.{to_port} is already driven")
            elif to_kind == "filter":
                # A classifier input is a single control slot, separate
                # from the node's bundle/audio upstream (a Filter node's
                # "filter" input).
                for existing in self._edges_into.get(to_node, []):
                    if existing.to_port == to_port and self._edge_is_filter(existing):
                        raise ValueError(f"{to_node}.{to_port} is already driven")
            elif (
                target.is_transparent()
                and not target.allows_multiple_inputs()
                and self._edges_into.get(to_node)
            ):
                # Only count bundle/audio upstream edges - a gate's
                # boolean "ctrl" edge, a Filter's "filter" edge and a
                # sound effect's impulse edge must not look like a second
                # audio/bundle input.
                audio_into = [
                    e
                    for e in self._edges_into.get(to_node, [])
                    if not self._edge_is_control(e)
                ]
                if audio_into:
                    raise ValueError(
                        f"{to_node} is a transparent node and already has "
                        "an upstream edge"
                    )
            edge_id = self._edge_id(from_node, to_node, to_port, from_port)
            self.edges[edge_id] = Edge(
                edge_id, from_node, to_node, to_port, from_port, declarative
            )
            self._edges_into.setdefault(to_node, []).append(self.edges[edge_id])
            self._edges_out_of.setdefault(from_node, []).append(self.edges[edge_id])
            return edge_id

    def remove_edge(self, edge_id: EdgeId) -> None:
        with self._lock:
            self._remove_edge_locked(edge_id)

    def set_edge_declarative(self, edge_id: EdgeId, declarative: bool) -> None:
        """Flip an existing edge's declarative flag in place.

        Used by a live declarative ownership move: the edge keeps its id,
        its port pairs and its ``_edge_links`` bookkeeping, so nothing is
        disconnected - unlike remove+add, which would drop and re-make
        the live link (an audible blip).  Edge is frozen, so swap in a
        copy and fix up the adjacency lists' references."""
        with self._lock:
            edge = self.edges.get(edge_id)
            if edge is None or edge.declarative == declarative:
                return
            new_edge = replace(edge, declarative=declarative)
            self.edges[edge_id] = new_edge
            for lst in (
                self._edges_into.get(new_edge.to_node, []),
                self._edges_out_of.get(new_edge.from_node, []),
            ):
                for i, existing in enumerate(lst):
                    if existing is edge:
                        lst[i] = new_edge

    def rename_node(self, old_id: NodeId, new_id: NodeId) -> None:
        with self._lock:
            if old_id == new_id:
                return
            if old_id not in self.nodes:
                raise KeyError(f"no such node {old_id!r}")
            if new_id in self.nodes:
                raise ValueError(f"node id {new_id!r} already in use")
            node = self.nodes.pop(old_id)
            node.id = new_id
            self.nodes[new_id] = node
            if old_id in self.public_nodes:
                self.public_nodes.discard(old_id)
                self.public_nodes.add(new_id)
            for key in ((old_id, "structural"), (old_id, "module")):
                self._repair_gate.forget(key)
            affected = list(self._edges_into.pop(old_id, [])) + list(
                self._edges_out_of.pop(old_id, [])
            )
            for old_edge in affected:
                self.edges.pop(old_edge.id, None)
                self._edge_links.pop(old_edge.id, None)
                if old_edge.to_node == old_id:
                    other_list = self._edges_out_of
                    other_id = old_edge.from_node
                else:
                    other_list = self._edges_into
                    other_id = old_edge.to_node
                other_list.get(other_id, []).remove(old_edge)
                new_from = new_id if old_edge.from_node == old_id else old_edge.from_node
                new_to = new_id if old_edge.to_node == old_id else old_edge.to_node
                new_edge = Edge(
                    self._edge_id(
                        new_from, new_to, old_edge.to_port, old_edge.from_port
                    ),
                    new_from,
                    new_to,
                    old_edge.to_port,
                    old_edge.from_port,
                )
                self.edges[new_edge.id] = new_edge
                self._edges_into.setdefault(new_to, []).append(new_edge)
                self._edges_out_of.setdefault(new_from, []).append(new_edge)

            # Preserve the node's own module interior across the rename.
            # Its synthetic internal-link bookkeeping is id-derived
            # (``__internal__:<node_id>:<i>``, see sync_locked).  Without
            # re-keying, the next sync() treats the old keys as stale,
            # disconnects the whole capture/playback sandwich and re-makes
            # it - and that momentary window where the out side has no
            # consumer is exactly what stalls timing-sensitive modules
            # (RNNoise; see the NoiseCancelNode docstring).  The pairs are
            # name/identity-based, so they stay valid across the rename;
            # if any identity did depend on the id, sync() corrects it.
            old_prefix = f"__internal__:{old_id}:"
            new_prefix = f"__internal__:{new_id}:"
            for link_id in [
                k for k in self._edge_links if k.startswith(old_prefix)
            ]:
                self._edge_links[new_prefix + link_id[len(old_prefix):]] = (
                    self._edge_links.pop(link_id)
                )
            for pair, (link_id, issued_at) in list(self._inflight_links.items()):
                if link_id.startswith(old_prefix):
                    self._inflight_links[pair] = (
                        new_prefix + link_id[len(old_prefix):],
                        issued_at,
                    )

    @staticmethod
    def _edge_id(
        from_node: NodeId,
        to_node: NodeId,
        to_port: str = "in",
        from_port: str = "out",
    ) -> EdgeId:
        """Stable id for an edge.  The common single-in/single-out case
        keeps the historical ``a->b`` form (so existing persisted edges
        are untouched); a named target port appends ``:port`` and a named
        source port appends ``@port``, keeping the two unambiguous."""
        base = f"{from_node}->{to_node}"
        if to_port != "in":
            base += f":{to_port}"
        if from_port != "out":
            base += f"@{from_port}"
        return base

    def _remove_edge_locked(self, edge_id: EdgeId) -> None:
        edge = self.edges.pop(edge_id, None)
        if edge is None:
            return
        # Note: _edge_links is deliberately left intact here - the next
        # sync() disconnects the edge's stale pairs and then drops the
        # bookkeeping, which is what actually unlinks the audio.
        if edge.to_node in self._edges_into:
            try:
                self._edges_into[edge.to_node].remove(edge)
            except ValueError:
                pass
        if edge.from_node in self._edges_out_of:
            try:
                self._edges_out_of[edge.from_node].remove(edge)
            except ValueError:
                pass
        # If any of this edge's links were still waiting to be
        # confirmed, stop waiting on them and remember the pairs so
        # that if the connect does land after all, the next sync
        # unlinks it.
        for pair, (inflight_edge_id, _issued_at) in list(self._inflight_links.items()):
            if inflight_edge_id == edge_id:
                self._orphan_disconnects.add(pair)
                del self._inflight_links[pair]

    def pulse(self, node_id: NodeId) -> List[NodeId]:
        """Fire an impulse from ``node_id``'s impulse output(s).

        Every impulse input reachable along impulse edges gets exactly one
        ``on_impulse()``, and the list of nodes that fired is returned.
        Fan-out is free (any number of edges may leave one output); a node
        reached by two paths is still triggered once, and a cycle
        terminates.

        This is a *push*, deliberately outside sync_locked's
        resolve-then-link pass: an impulse has no value to sample, so
        there is nothing for a later tick to re-derive (a node that was
        not running when the pulse happened does not get a delayed one).

        The walk itself runs under the lock, but the triggers do not: a
        play spawns a child process and must not stall the supervision
        tick or the graph's own event thread."""
        with self._lock:
            reached = self._impulse_targets(node_id)
        fired: List[NodeId] = []
        for node in reached:
            try:
                if isinstance(node, SoundPlayerNode):
                    # What to play is a graph question (the sound wired into
                    # it), answered here - at the moment of firing.
                    node.on_impulse(self.resolve_sound(node.id))
                else:
                    node.on_impulse()
            except Exception as exc:
                logger.warning("Impulse handler on %r failed: %s", node.id, exc)
            fired.append(node.id)
        return fired

    def _impulse_targets(self, node_id: NodeId) -> List[Any]:
        """Every node reachable from ``node_id`` along impulse edges, in
        walk order, each once - see pulse()."""
        targets = []
        seen: Set[NodeId] = {node_id}
        stack = [node_id]
        while stack:
            current = stack.pop()
            source = self.nodes.get(current)
            if source is None:
                continue
            for edge in self._edges_out_of.get(current, []):
                if source.port_kind(edge.from_port, "out") != "impulse":
                    continue
                if edge.to_node in seen:
                    continue
                seen.add(edge.to_node)
                target = self.nodes.get(edge.to_node)
                if target is None:
                    continue
                if callable(getattr(target, "on_impulse", None)):
                    targets.append(target)
                stack.append(edge.to_node)
        return targets

    # ------------------------------------------------------------------
    # resolving what feeds an edge
    # ------------------------------------------------------------------

    def _edge_is_boolean(self, edge: "Edge") -> bool:
        """Whether `edge` carries a boolean control signal rather than
        audio.  Either endpoint being a boolean port is enough; add_edge
        refuses mixed-kind edges, so the two always agree."""
        src = self.nodes.get(edge.from_node)
        if src is not None and src.port_kind(edge.from_port, "out") == "boolean":
            return True
        dst = self.nodes.get(edge.to_node)
        return dst is not None and dst.port_kind(edge.to_port, "in") == "boolean"

    def _edge_is_filter(self, edge: "Edge") -> bool:
        """Whether `edge` carries a classifier predicate (a Filter node's
        "filter" input) rather than audio/bundle.  add_edge refuses to
        pair a filter port with anything else, so checking either end is
        enough."""
        src = self.nodes.get(edge.from_node)
        if src is not None and src.port_kind(edge.from_port, "out") == "filter":
            return True
        dst = self.nodes.get(edge.to_node)
        return dst is not None and dst.port_kind(edge.to_port, "in") == "filter"

    def _edge_is_sound(self, edge: "Edge") -> bool:
        """Whether `edge` carries a *sound* (a file plus a range - see
        SoundNode): a reference, not audio, so like an impulse it never
        becomes a PipeWire link."""
        src = self.nodes.get(edge.from_node)
        dst = self.nodes.get(edge.to_node)
        if src is not None and src.port_kind(edge.from_port, "out") == "sound":
            return True
        return dst is not None and dst.port_kind(edge.to_port, "in") == "sound"

    def _edge_is_impulse(self, edge: "Edge") -> bool:
        """Whether `edge` is a momentary impulse wire rather than audio.
        Same either-end check as the boolean/filter helpers above."""
        src = self.nodes.get(edge.from_node)
        if src is not None and src.port_kind(edge.from_port, "out") == "impulse":
            return True
        dst = self.nodes.get(edge.to_node)
        return dst is not None and dst.port_kind(edge.to_port, "in") == "impulse"

    def _edge_is_control(self, edge: "Edge") -> bool:
        """Whether `edge` is control-plane - a boolean value, a classifier
        predicate or an impulse - and therefore never a PipeWire link.

        Everywhere that walks a node's inbound edges looking for its
        *audio* upstream must filter on this, or a gate's "ctrl" edge, a
        Filter's "filter" edge or a sound effect's impulse edge would look
        like a second audio input (and sync_locked would try to resolve
        audio out of a node that carries none)."""
        return (
            self._edge_is_boolean(edge)
            or self._edge_is_filter(edge)
            or self._edge_is_sound(edge)
            or self._edge_is_impulse(edge)
        )

    def _refresh_boolean_states(self) -> None:
        """Resolve every bool-controlled node's effective state from the
        boolean signal wired into it (or None when nothing is wired, so
        the node keeps its own stored default).  Called at the top of
        every sync, before audio sources are resolved."""
        for node in self.nodes.values():
            if isinstance(node, BoolControlledMixin):
                node._bool_effective = self._resolve_boolean_input(node.id, set())

    def _resolve_boolean_input(self, node_id: NodeId,
                               seen: Set[NodeId]) -> Optional[bool]:
        node = self.nodes.get(node_id)
        port = getattr(node, "BOOLEAN_INPUT", None)
        if port is None:
            return None
        edges = self._edges_into.get(node_id, [])
        for edge in edges:
            if edge.to_port == port:
                return self._resolve_boolean(edge.from_node, edge.from_port, seen)
        # A saved session may still name this input "in", from before ports
        # were named after what they carry.  Only as a fallback: another port's
        # edge must never be mistaken for this one.
        for edge in edges:
            if edge.to_port == "in" and node.port_kind("in", "in") == "boolean":
                return self._resolve_boolean(edge.from_node, edge.from_port, seen)
        return None

    def _resolve_boolean_inputs(self, node_id: NodeId,
                                seen: Set[NodeId]) -> List[bool]:
        """Resolve every *wired* input of a multi-input logic gate, in
        port order.  Unwired inputs are skipped rather than treated as a
        value, so a gate with a single input wired acts as a pass-through
        (the caller combines whatever it gets)."""
        node = self.nodes.get(node_id)
        values: List[bool] = []
        edges = self._edges_into.get(node_id, [])
        legacy = node.port_kind("in", "in") == "boolean"
        for port in getattr(node, "BOOLEAN_INPUTS", ()):
            wanted = [port] + (["in"] if legacy else [])
            for edge in edges:
                if edge.to_port not in wanted:
                    continue
                value = self._resolve_boolean(edge.from_node, edge.from_port, seen)
                if value is not None:
                    values.append(value)
                break
        return values

    def _resolve_boolean(self, node_id: NodeId, from_port: str,
                         seen: Set[NodeId]) -> Optional[bool]:
        if node_id in seen:
            # A boolean feedback loop has no stable value; treat it as
            # unwired rather than recursing forever.
            return None
        seen = seen | {node_id}
        node = self.nodes.get(node_id)
        if isinstance(node, BooleanSourceNode):
            return node.boolean_value()
        if isinstance(node, BooleanSplitterNode):
            return self._resolve_boolean_input(node_id, seen)
        if isinstance(node, BooleanInvertNode):
            value = self._resolve_boolean_input(node_id, seen)
            return None if value is None else (not value)
        if isinstance(node, BooleanLogicNode):
            values = self._resolve_boolean_inputs(node_id, seen)
            return node.combine(values) if values else None
        if isinstance(node, (BoolPanelInNode, BoolPanelOutNode)):
            # Panel boolean ports relay their input.  A panel *input* with
            # nothing wired from outside falls back to its configured
            # default state (if any) rather than emitting nothing.
            value = self._resolve_boolean_input(node_id, seen)
            if value is None and isinstance(node, BoolPanelInNode):
                return getattr(node, "default_state", None)
            return value
        if isinstance(node, BooleanWarpOutNode):
            name = getattr(node, "warp_name", "")
            if not name:
                return None
            key = f"warp:{name}"
            if key in seen:
                return None
            seen = seen | {key}
            publisher = self._boolean_warp_publisher(name)
            if publisher is None:
                return None
            return self._resolve_boolean_input(publisher.id, seen)
        return None

    def _boolean_warp_publisher(self, name: str) -> Optional["BooleanWarpInNode"]:
        """The BooleanWarpInNode that publishes `name`.  Boolean warps
        are a separate namespace from audio warps and can't be summed,
        so if several share a name the first one (stable node order)
        wins and the rest are reported once."""
        matches = [
            node
            for node in self.nodes.values()
            if isinstance(node, BooleanWarpInNode) and node.warp_name == name
        ]
        if not matches:
            return None
        if len(matches) > 1 and name not in self._warned_bool_warps:
            self._warned_bool_warps.add(name)
            logger.warning(
                "Boolean warp %r has %d publishers; using %r",
                name, len(matches), matches[0].id,
            )
        return matches[0]

    def _resolve_sources(
        self, node_id: NodeId, from_port: str = "out",
        seen: Optional[Set[Any]] = None,
    ) -> List[dict]:
        node = self.nodes.get(node_id)
        if node is None:
            return []
        # Path-local cycle guard: a warp loop (or any transparent loop)
        # resolves to no signal instead of recursing forever.
        if seen is None:
            seen = set()
        if node_id in seen:
            return []
        seen = seen | {node_id}
        if isinstance(node, InputNode):
            return node.source_filters()
        if isinstance(node, BackedNode):
            return [node.output_identity()]
        if isinstance(node, WarpOutNode):
            return self._resolve_warp_audio(node, seen)
        if isinstance(node, TransparentNode):
            if isinstance(node, BundleSplitNode):
                # Each dynamic output socket carries exactly one member of
                # the inbound bundle, keyed by that member's node.name (the
                # port name the GUI was told about).  A name that no longer
                # exists simply resolves to nothing.
                if from_port in ("", None, "in", "out"):
                    return []
                return [{"nodeName": from_port}]
            if not node.gate_open():
                return []
            # A switcher only passes the output its button has selected;
            # the other output resolves to nothing, which makes sync()
            # tear down everything wired to it.
            if not node.passes_output(from_port):
                return []
            upstream = self._edges_into.get(node_id, [])
            # Boolean control edges (a gate's "ctrl", a switcher's
            # "ctrl"), a Filter's classifier edge and an impulse edge land
            # here too, but must never be mistaken for the node's
            # bundle/audio upstream.
            upstream = [e for e in upstream if not self._edge_is_control(e)]
            if not upstream:
                return []
            # A mixing transparent node (a panel input/output bus) sums
            # every inbound audio edge instead of selecting just one.
            if getattr(node, "MIX_INPUTS", False):
                mixed: List[dict] = []
                for e in upstream:
                    mixed.extend(
                        self._resolve_sources(e.from_node, e.from_port, seen)
                    )
                return mixed
            # A single-input transparent node has exactly one inbound
            # edge; InverseSwitcherNode has one per input and picks the
            # one its button selected (see select_upstream).
            chosen = node.select_upstream(upstream)
            if chosen is None:
                return []
            sources = self._resolve_sources(
                chosen.from_node, chosen.from_port, seen
            )
            if isinstance(node, FilterNode):
                # Narrow the upstream bundle by the classifier wired into
                # this node's "filter" input, as exact live ids so a chain
                # of Filters intersects.
                ids = pwmatch.find_source_nodes(self.graph, sources)
                ids = self._apply_classifier(node, "source", ids)
                return [{"id": i} for i in ids]
            if isinstance(node, ExcludeFilterNode):
                exclude = node.exclude_filter()
                if exclude is not None:
                    sources = [
                        {**f, "exclude": [*f.get("exclude", []), exclude]}
                        for f in sources
                    ]
            return sources
        return []

    def resolve_sound(self, node_id: NodeId, port: str = "sound",
                      seen: Optional[Set[NodeId]] = None) -> Optional[dict]:
        """The sound reaching ``node_id``'s sound input, as
        ``{"path", "start", "end"}`` - or None when nothing sound-like is
        wired there.

        A Sound node is the source; anything transparent in between passes it
        through, and (once a Clip is in the path) narrows the range.  Called at
        impulse time rather than on a sync: a sound has no value to sample, so
        it is resolved exactly when it is about to be played."""
        if seen is None:
            seen = set()
        if node_id in seen:
            return None
        seen.add(node_id)
        wanted = [(e) for e in self.edges.values()
                  if e.to_node == node_id and e.to_port == port]
        if not wanted:
            # A caller asking under an old port name ("in", before ports were
            # named after what they carry) still finds the sound, as long as
            # the edge is of the right *kind*.
            wanted = [
                e for e in self.edges.values()
                if e.to_node == node_id
                and self.nodes.get(e.from_node) is not None
                and self.nodes[e.from_node].port_kind(e.from_port, "out") == "sound"
            ]
        if not wanted:
            return None
        upstream = wanted
        source = self.nodes.get(upstream[0].from_node)
        if source is None:
            return None
        if isinstance(source, SoundNode):
            return {"path": source.path, "start": 0.0, "end": None}
        if isinstance(source, RecorderNode):
            # A recorder's "file" is its take (and it only exists once one has
            # been made).
            return {"path": source.take_path, "start": 0.0, "end": None}
        if isinstance(source, ClipNode):
            # A clip narrows whatever reaches it, in the *incoming* file's own
            # time base - so clips stacked behind one another intersect.
            upstream = self.resolve_sound(source.id, "sound", seen)
            if upstream is None:
                return None
            base = float(upstream.get("start") or 0.0)
            ceiling = upstream.get("end")
            start = base + source.start
            end = None if source.end is None else base + source.end
            if ceiling is not None:
                end = float(ceiling) if end is None else min(end, float(ceiling))
            return {"path": upstream["path"], "start": start, "end": end}
        # Anything else between a Sound and the player hands it on unchanged.
        return self.resolve_sound(source.id, port, seen)

    def _resolve_warp_audio(self, warp_out: "WarpOutNode",
                            seen: Set[Any]) -> List[dict]:
        """Everything published under ``warp_out.warp_name``.  Audio
        warps are a *mix*: every matching WarpInNode contributes its
        upstream source filters, and sync_locked links all of them into
        the downstream input ports (PipeWire sums multiple links into
        one port)."""
        name = getattr(warp_out, "warp_name", "")
        if not name:
            return []
        key = f"warp:{name}"
        if key in seen:
            return []
        seen = seen | {key}
        sources: List[dict] = []
        for node in self.nodes.values():
            if isinstance(node, WarpInNode) and node.warp_name == name:
                sources.extend(self._resolve_sources(node.id, "in", seen))
        return sources

    # ------------------------------------------------------------------
    # bundles / classifiers
    # ------------------------------------------------------------------

    def _classifiers_for(self, filter_node_id: NodeId) -> List["ClassifierNode"]:
        """Every ClassifierNode wired into a Filter node's filter inputs,
        in port order.  Empty when nothing is plugged in (the bundle then
        passes through unchanged)."""
        by_port: Dict[str, "ClassifierNode"] = {}
        for edge in self._edges_into.get(filter_node_id, []):
            if not self._edge_is_filter(edge):
                continue
            classifier = self.nodes.get(edge.from_node)
            if isinstance(classifier, ClassifierNode):
                by_port[edge.to_port] = classifier
        return [by_port[port] for port in sorted(by_port)]

    def _apply_classifier(self, filter_node: "FilterNode", side: str,
                          ids: List[int]) -> List[int]:
        """Keep the `ids` a Filter node's classifiers all match (AND).  No
        classifier wired => the bundle passes through; an empty classifier
        matches nothing.  With the node's Include/Exclude switch on Exclude,
        keep the complement instead: everything that does *not* match."""
        classifiers = self._classifiers_for(filter_node.id)
        if not classifiers:
            return list(ids)
        exclude = bool(getattr(filter_node, "exclude", False))
        kept: List[int] = []
        live = self.graph.nodes()
        for node_id in ids:
            props = dict(
                (live.get(node_id) or {}).get("info", {}).get("props", {})
            )
            props["_node_id"] = node_id
            matched = all(c.classify(props, side) for c in classifiers)
            if matched != exclude:
                kept.append(node_id)
        return kept

    def bundle_side(self, node_id: NodeId,
                    seen: Optional[Set[NodeId]] = None) -> Optional[str]:
        """Which side a bundle endpoint produces: "source" (audio can be
        pulled from its members) or "sink" (audio can be pushed to them),
        or None when `node_id` is not a bundle endpoint."""
        node = self.nodes.get(node_id)
        if isinstance(node, (AllInputsNode, AllAppsNode)):
            return "source"
        if isinstance(node, AllOutputsNode):
            return "sink"
        if isinstance(node, BundleOutputNode):
            return "sink"
        if isinstance(node, FilterNode):
            if seen is None:
                seen = set()
            if node_id in seen:
                return None
            seen = seen | {node_id}
            chosen = self._bundle_upstream(node_id)
            return self.bundle_side(chosen.from_node, seen) if chosen else None
        return None

    def _bundle_upstream(self, node_id: NodeId) -> Optional["Edge"]:
        for edge in self._edges_into.get(node_id, []):
            if not self._edge_is_boolean(edge) and not self._edge_is_filter(edge):
                return edge
        return None

    def filter_input_ports(self, node_id: NodeId) -> List[str]:
        """The dynamic ``filterN`` input sockets a Filter node shows: one
        per wired classifier, plus one spare.  Same growth contract as a
        Merge Bundle's inputs (a bare legacy ``filter`` port counts as
        the first)."""
        node = self.nodes.get(node_id)
        if not isinstance(node, FilterNode):
            return []
        used = [
            e for e in self._edges_into.get(node_id, [])
            if self._edge_is_filter(e)
        ]
        max_index = 0
        for edge in used:
            port = edge.to_port or ""
            if port == "filter":
                max_index = max(max_index, 1)
            elif port.startswith("filter") and port[6:].isdigit():
                max_index = max(max_index, int(port[6:]))
        count = max(len(used), max_index) + 1
        return [f"filter{i}" for i in range(1, count + 1)]

    def bundle_input_ports(self, node_id: NodeId) -> List[str]:
        """The dynamic input sockets a Bundle merge node shows: one per
        inbound line, plus one spare.  Plugging into the spare makes the
        next socket appear on the following poll, so the node grows a
        socket per connection instead of being a single hidden bus.
        Names are stable (``in1``, ``in2``, ...) because edges reference
        them by name."""
        node = self.nodes.get(node_id)
        if not isinstance(node, BundleMergeNode):
            return []
        used = [
            e
            for e in self._edges_into.get(node_id, [])
            if not self._edge_is_boolean(e) and not self._edge_is_filter(e)
        ]
        max_index = 0
        for edge in used:
            port = edge.to_port or ""
            if port.startswith("in") and port[2:].isdigit():
                max_index = max(max_index, int(port[2:]))
        count = max(len(used), max_index) + 1
        return [f"in{i}" for i in range(1, count + 1)]

    def _bundle_member_ids(self, node_id: NodeId,
                           seen: Optional[Set[Any]] = None) -> List[int]:
        """The live source ids behind a bundle endpoint, as ints.

        A Filter is resolved through to what reaches it and then narrowed by
        its classifiers: it is a *backed* node, so its own output is its
        monitor, which is not a live source - resolving it the generic way
        found no members at all, and a Split on the far side of a filter
        showed no lines."""
        if seen is None:
            seen = set()
        if node_id in seen:
            return []
        seen = seen | {node_id}
        node = self.nodes.get(node_id)
        if isinstance(node, FilterNode):
            upstream = self._bundle_upstream(node_id)
            if upstream is None:
                return []
            return self._apply_classifier(
                node, "source",
                self._bundle_member_ids(upstream.from_node, seen),
            )
        return pwmatch.find_source_nodes(
            self.graph, self._resolve_sources(node_id, "out")
        )

    def bundle_members(
        self, node_id: NodeId, seen: Optional[Set[Any]] = None
    ) -> List[dict]:
        """The live members a bundle endpoint stands for, as
        ``[{"port": node.name, "label": readable}]``.

        Used to give a Bundle Split node one output socket per line.  A
        Split resolves through to its upstream bundle; any other bundle
        endpoint (All Inputs, a Filter chain, a Bundle junction) resolves
        to its concrete source ids here."""
        # The guard has to come *before* the Split branch below: a Split
        # resolves through its upstream, which may itself be a Split, and
        # recursing from there without a `seen` to carry would never stop.
        if seen is None:
            seen = set()
        if node_id in seen:
            return []
        seen = seen | {node_id}
        node = self.nodes.get(node_id)
        if isinstance(node, BundleSplitNode):
            chosen = self._bundle_upstream(node_id)
            return self.bundle_members(chosen.from_node, seen) if chosen else []
        live = self.graph.nodes()
        members: List[dict] = []
        # A fresh `seen` for the helper: this node is *already* in the caller's
        # (added above), and handing that in would read as a cycle.  The helper
        # keeps its own guard for the filters it walks through.
        for member_id in self._bundle_member_ids(node_id):
            props = (live.get(member_id) or {}).get("info", {}).get("props", {})
            name = props.get("node.name") or ""
            if not name:
                continue
            members.append({
                "port": name,
                "label": (
                    props.get("application.name")
                    or props.get("node.description")
                    or props.get("node.nick")
                    or name
                ),
            })
        return members

    def _resolve_bundle_targets(
        self, node_id: NodeId, seen: Optional[Set[Any]] = None
    ) -> List[dict]:
        """The sink-target filter list a sink-side bundle resolves to.

        Mirrors ``_resolve_sources`` for the other direction: preset All
        Outputs yields every playback/recording sink, and a Filter narrows
        it to the sinks its classifier matches (as exact ids)."""
        node = self.nodes.get(node_id)
        if node is None:
            return []
        if seen is None:
            seen = set()
        if node_id in seen:
            return []
        seen = seen | {node_id}
        if isinstance(node, AllOutputsNode):
            return node.sink_filters()
        if isinstance(node, FilterNode):
            chosen = self._bundle_upstream(node_id)
            if chosen is None:
                return []
            targets = self._resolve_bundle_targets(chosen.from_node, seen)
            ids: List[int] = []
            for filt in targets:
                ids.extend(pwmatch.find_target_nodes(self.graph, filt))
            ids = self._apply_classifier(node, "sink", ids)
            return [{"id": i} for i in ids]
        return []

    def _filter_links(self, node: "FilterNode") -> Dict[EdgeId, Set[Tuple[int, int]]]:
        """Desired links for a Filter: the members its classifiers keep, summed
        into the node's own dummy sink.

        Excluding a member therefore drops it from *this* chain only - it stops
        being fed into the dummy, and everything downstream (which reads the
        dummy's monitor) never has to change."""
        sum_id = f"__internal__:{node.id}:sum"
        dummy_id = node.sink_node_id()
        if dummy_id is None:
            # The dummy hasn't resolved yet: nothing to connect or tear down.
            return {sum_id: set()}
        sources: List[dict] = []
        for edge in self._edges_into.get(node.id, []):
            if self._edge_is_boolean(edge) or self._edge_is_filter(edge):
                continue
            sources.extend(self._resolve_sources(edge.from_node, edge.from_port))
        ids = pwmatch.find_source_nodes(self.graph, sources)
        ids = self._apply_classifier(node, "source", ids)
        summed: Set[Tuple[int, int]] = set()
        for src_id in ids:
            summed |= pwmatch.resolve_channel_pairs(self.graph, src_id, dummy_id)
        return {sum_id: summed}

    def _bundle_output_links(
        self, node: "BundleOutputNode"
    ) -> Dict[EdgeId, Set[Tuple[int, int]]]:
        """Desired links for a Bundle Output terminal, in two stages:
        every source feeding its audio "in" is summed into the node's own
        dummy sink (``:sum``), and the dummy's monitor feeds every sink
        its ``bundle`` input resolves to (``:feed``).  Both stages are
        internal links, so the ordinary teardown/backoff bookkeeping
        keeps them healthy."""
        graph = self.graph
        sum_id = f"__internal__:{node.id}:sum"
        feed_id = f"__internal__:{node.id}:feed"
        dummy_id = node.sink_node_id()
        if dummy_id is None:
            # The dummy hasn't resolved yet: nothing to connect, and
            # nothing to tear down either (empty desired sets).
            return {sum_id: set(), feed_id: set()}
        sources: List[dict] = []
        targets: List[dict] = []
        for edge in self._edges_into.get(node.id, []):
            if self._edge_is_boolean(edge) or self._edge_is_filter(edge):
                continue
            if edge.to_port == "bundle":
                targets.extend(self._resolve_bundle_targets(edge.from_node))
            else:
                sources.extend(
                    self._resolve_sources(edge.from_node, edge.from_port)
                )
        summed: Set[Tuple[int, int]] = set()
        for src_id in pwmatch.find_source_nodes(graph, sources):
            summed |= pwmatch.resolve_channel_pairs(graph, src_id, dummy_id)
        feed: Set[Tuple[int, int]] = set()
        for filt in targets:
            for tgt_id in pwmatch.find_target_nodes(graph, filt):
                feed |= pwmatch.resolve_channel_pairs(
                    graph, dummy_id, tgt_id, filt.get("type")
                )
        return {sum_id: summed, feed_id: feed}

    # ------------------------------------------------------------------
    # supervision
    # ------------------------------------------------------------------

    def _wake(self) -> None:
        for cb in self.on_change:
            try:
                cb()
            except Exception:
                logger.exception("wake callback failed")

    def _sync_device_bindings(self) -> None:
        """Keep Device Input/Output nodes pointed at the live hardware
        object.

        Two failures this repairs without the user having to re-select
        the device in the GUI:

          * a node whose stored ``device_name`` is no longer in the graph
            but reappeared under a suffixed sibling (PipeWire's
            ``.99``-style duplicate suffix) - the stored name is updated
            to the live one, so sync() links it again, and
          * a node whose ``live_node_id`` is missing or stale (a
            node-created event was missed) - it is re-bound so device
            volume/profile keep being enforced.

        Runs every supervise() tick, before sync()."""
        graph = self.graph
        live = graph.nodes()
        by_base = {}
        for live_id, data in live.items():
            props = data.get("info", {}).get("props", {})
            media_class = props.get("media.class")
            name = props.get("node.name")
            if name and media_class in ("Audio/Sink", "Audio/Source"):
                by_base.setdefault(
                    (media_class, _base_node_name(name)), (live_id, props, name)
                )
        if not by_base:
            return
        for node in list(self.nodes.values()):
            if not isinstance(node, (DeviceInputNode, DeviceOutputNode)):
                continue
            name = getattr(node, "device_name", "")
            if not name:
                continue
            live_id = graph.node_id_by_name(name)
            if live_id is None:
                media_class = (
                    "Audio/Source"
                    if isinstance(node, DeviceInputNode)
                    else "Audio/Sink"
                )
                found = by_base.get((media_class, _base_node_name(name)))
                if found is None:
                    continue
                live_id, props, live_name = found
                if live_name != name:
                    logger.info(
                        "Device %r re-enumerated as %r - re-binding %r",
                        name, live_name, node.id,
                    )
                    node.device_name = live_name
                node.resolve_live(live_id, props)
            elif node.live_node_id != live_id:
                props = live.get(live_id, {}).get("info", {}).get("props", {})
                node.resolve_live(live_id, props)

    def supervise(self) -> None:
        """Health pass.  Called by the daemon's tick thread under no
        lock held by the caller."""
        with self._lock:
            for node in list(self.nodes.values()):
                if not isinstance(node, BackedNode):
                    continue
                if node.id in self._staging:
                    # Being brought up by _load_session's own one-at-a-
                    # time loop right now - touching it here would race
                    # that loop's deliberate throttling (see the _staging
                    # docstring in __init__). It gets a normal repair
                    # pass again the moment it's unstage()'d.
                    continue
                try:
                    self._supervise_node(node)
                except Exception as exc:
                    logger.warning("Supervision of %r failed: %s", node.id, exc)

            # Re-bind device nodes whose hardware object changed/returned
            # under a different name, before re-applying their settings
            # and recomputing links.
            self._sync_device_bindings()

            # Re-enforce device/effect settings that must continuously
            # match their configured values.
            for node in list(self.nodes.values()):
                if node.id in self._staging:
                    # Same staging contract as the health loop above: a
                    # node mid-bring-up may be about to have its module
                    # re-spawned, so don't push settings at the current
                    # interior; _on_backing_resolved() re-applies them to
                    # the fresh one.
                    continue
                try:
                    if hasattr(node, "apply_device_settings"):
                        node.apply_device_settings()
                    if hasattr(node, "refresh_live"):
                        node.refresh_live()
                except Exception as exc:
                    logger.warning("Settings re-apply for %r failed: %s", node.id, exc)

            self.sync_locked()

    def _supervise_node(self, node: BackedNode) -> None:
        # -- structural health --------------------------------------------
        s_key = (node.id, "structural")
        if not node.structural_ok():
            if self._repair_gate.ready(s_key):
                logger.warning("Structural backing of %r (%s) degraded - repairing",
                               node.id, type(node).__name__)
                try:
                    node.ensure_structural()
                except Exception as exc:
                    logger.warning("Structural repair of %r failed: %s", node.id, exc)
                if node.structural_ok():
                    self._repair_gate.record_success(s_key)
                else:
                    self._repair_gate.record_failure(s_key)
        else:
            self._repair_gate.record_success(s_key)

        # -- module health / interior reload --------------------------------
        if node.has_module():
            m_key = (node.id, "module")
            now = _time.monotonic()
            reload_due = node._reload_due is not None and now >= node._reload_due
            if reload_due:
                node._reload_due = None
            module = node.module_backing()
            module_crashed = (
                module is not None and module.owns_process and not module.is_alive
            )

            if reload_due or module_crashed or not node.module_ok():
                if self._repair_gate.ready(m_key):
                    logger.info(
                        "Reloading %s module for %r (%s)",
                        "crashed" if module_crashed else ("configured" if reload_due else "missing"),
                        node.id, type(node).__name__,
                    )
                    try:
                        if module_crashed or reload_due:
                            node.reload_module()
                        else:
                            node.ensure_module()
                    except Exception as exc:
                        logger.warning("Module repair of %r failed: %s", node.id, exc)
                    if node.module_ok():
                        self._repair_gate.record_success(m_key)
                    else:
                        self._repair_gate.record_failure(m_key)
            else:
                self._repair_gate.record_success(m_key)

        # -- (re)resolve any backing whose graph id we don't know -----------
        self._resolve_backings(node)

    def _resolve_backings(self, node: BackedNode) -> None:
        for owned in node.backings:
            if owned.node_id is not None:
                continue
            found = self.graph.node_id_by_name(owned.name)
            if found is not None:
                owned.resolve(found)
                self._on_backing_resolved(node, owned)

    def _on_backing_resolved(self, node: BackedNode, owned: OwnedPwNode) -> None:
        """A backing object's id became known (first appearance or after
        a recreate) - give the node a chance to push anything that must
        live on the object."""
        if isinstance(node, VolumeProcessNode):
            node.refresh_live()
        elif isinstance(node, (NoiseCancelNode, SensitivityGateNode)):
            node.refresh_live()

    def handle_node_removed(self, removed_id: int,
                            removed_props: Optional[dict] = None) -> None:
        """A live object disappeared.  If it is one of our backings whose
        owning process is *still alive*, the object was destroyed out
        from under us (or a reload raced) - drop the owner so the next
        supervise() respawns it.

        ``removed_props`` (the removed node's last-known snapshot) guards
        against PipeWire node-id reuse: when provided, the removed
        object's ``node.name`` must match the backing's name, so a stale
        id that has since been handed to an unrelated node can't tear
        down the wrong backing."""
        removed_name = None
        if removed_props:
            removed_name = (
                removed_props.get("info", {}).get("props", {}).get("node.name")
            )
        with self._lock:
            for node in list(self.nodes.values()):
                if not isinstance(node, BackedNode):
                    continue
                for owned in list(node.backings):
                    if owned.node_id != removed_id:
                        continue
                    if removed_name is not None and removed_name != owned.name:
                        # The id was reused by another object; this is
                        # not our backing dying. Forget the stale id so
                        # the backing re-resolves instead of being torn
                        # down.
                        owned.node_id = None
                        continue
                    if owned.owns_process and owned.is_alive:
                        resolved_at = getattr(owned, "_resolved_at", None)
                        if (
                            resolved_at is not None
                            and (_time.monotonic() - resolved_at) < 3.0
                        ):
                            # Resolved moments ago: this is almost always
                            # a lagging removal event for a recycled id
                            # (the load-time reap frees an id, PipeWire
                            # hands it to our fresh node, then the old
                            # removal lands). Clear the id and let it
                            # re-resolve rather than destroy the backing.
                            owned.node_id = None
                            continue
                        logger.warning(
                            "Live object for %r (%s) disappeared while its "
                            "process was alive - restarting backing",
                            node.id, owned.name,
                        )
                        owned.destroy()
                        if owned in node.backings:
                            node.backings.remove(owned)

    # ------------------------------------------------------------------
    # structural reconciliation (sync)
    # ------------------------------------------------------------------

    def sync(self) -> None:
        with self._lock:
            self.sync_locked()

    def sync_locked(self) -> None:
        if not self._graph_loaded:
            return
        # Resolve boolean control signals first: a gate/switcher's state
        # (and therefore which audio edges resolve to a source) depends
        # on them.
        self._refresh_boolean_states()
        graph = self.graph
        desired: Dict[EdgeId, Set[Tuple[int, int]]] = {}
        unresolved: Set[EdgeId] = set()

        for node_id, node in self.nodes.items():
            is_output = isinstance(node, OutputNode)
            is_backed = isinstance(node, BackedNode)
            if not (is_output or is_backed):
                continue
            if node_id in self._staging:
                # A node a caller (_load_session) is bringing up
                # one-at-a-time right now.  Its module can be destroyed
                # and re-spawned mid-pass by ensure_module() (the "fresh
                # spawn" path drops/re-points the interior and the output
                # drain), so deriving or committing links for it here
                # would wire against streams that are about to vanish.
                # Leave whatever is currently attached alone by marking
                # its links unresolved (the teardown loop below skips
                # those), derive nothing into `desired`, and let the
                # post-load supervise() - run once every node is
                # unstage()'d - do the one deterministic wire-up.
                for edge in list(self._edges_into.get(node_id, [])):
                    unresolved.add(edge.id)
                if is_backed:
                    prefix = f"__internal__:{node_id}:"
                    unresolved.update(
                        k for k in self._edge_links if k.startswith(prefix)
                    )
                continue
            if isinstance(node, FilterNode):
                # Its kept members are summed into its own dummy; its output is
                # that dummy's monitor, so the downstream never moves.
                desired.update(self._filter_links(node))
                continue
            if isinstance(node, BundleOutputNode):
                # The terminal's "in" (audio) + "bundle" inputs are wired
                # by one custom pass through its own dummy, not by the
                # ordinary per-edge source->sink resolution.
                try:
                    desired.update(self._bundle_output_links(node))
                except Exception as exc:
                    logger.warning(
                        "Failed to compute bundle-output links for %s "
                        "(leaving current links in place): %s", node_id, exc,
                    )
                    prefix = f"__internal__:{node_id}:"
                    unresolved.update(
                        k for k in self._edge_links if k.startswith(prefix)
                    )
                    for edge in list(self._edges_into.get(node_id, [])):
                        unresolved.add(edge.id)
                continue
            for edge in list(self._edges_into.get(node_id, [])):
                if self._edge_is_control(edge):
                    # A boolean value, a classifier predicate or an
                    # impulse is not audio: there is nothing here to
                    # resolve into links.  (A sound effect's only input is
                    # an impulse, so without this every press-drag would
                    # also try to wire the button into the dummy sink.)
                    continue
                try:
                    sink_filters = (
                        node.sink_filters()
                        if is_output
                        else [node.input_identity(edge.to_port)]
                    )
                    sources = self._resolve_sources(
                        edge.from_node, edge.from_port
                    )
                    if not sources:
                        desired[edge.id] = set()
                        continue
                    pairs: Set[Tuple[int, int]] = set()
                    for src_id in pwmatch.find_source_nodes(graph, sources):
                        for filt in sink_filters:
                            for tgt_id in pwmatch.find_target_nodes(graph, filt):
                                pairs |= pwmatch.resolve_channel_pairs(
                                    graph, src_id, tgt_id, filt.get("type")
                                )
                    desired[edge.id] = pairs
                except Exception as exc:
                    logger.warning(
                        "Failed to compute desired links for edge %s (leaving "
                        "current links in place): %s", edge.id, exc,
                    )
                    unresolved.add(edge.id)

            if is_backed:
                try:
                    for i, (src_ident, sink_ident) in enumerate(node.internal_links()):
                        link_id = f"__internal__:{node_id}:{i}"
                        pairs = set()
                        for src_id in pwmatch.find_source_nodes(graph, [src_ident]):
                            for tgt_id in pwmatch.find_target_nodes(graph, sink_ident):
                                pairs |= pwmatch.resolve_channel_pairs(
                                    graph, src_id, tgt_id, sink_ident.get("type")
                                )
                        desired[link_id] = pairs
                except Exception as exc:
                    logger.warning(
                        "Failed to compute internal links for node %s (leaving "
                        "current links in place): %s", node_id, exc,
                    )
                    prefix = f"__internal__:{node_id}:"
                    unresolved.update(k for k in self._edge_links if k.startswith(prefix))

        # Tear down stale pairs first.  (The _edge_links entry for an
        # edge that no longer exists is kept until this loop so its
        # current links are disconnected here; it is deleted below.)
        for edge_id, entry in list(self._edge_links.items()):
            if edge_id in unresolved:
                continue
            new_pairs = desired.get(edge_id, set())
            for pair in entry.pairs - new_pairs:
                try:
                    graph.disconnect(*pair)
                except Exception as exc:
                    logger.debug("disconnect %s for %s failed (already gone?): %s",
                                 pair, edge_id, exc)
                # No longer desired: forget any backoff so a future re-add
                # of this exact pair is retried immediately.
                self._link_gate.forget(pair)
            if edge_id not in desired:
                del self._edge_links[edge_id]

        # Then create whatever is missing - one link at a time, in a
        # fixed order, each waited on before the next (see
        # _apply_desired_links for why).
        self._apply_desired_links(desired)

    def _apply_desired_links(self, desired: Dict[EdgeId, Set[Tuple[int, int]]]) -> None:
        """Create the links in `desired` that aren't live yet.

        The old code fired every missing ``pw-link`` back-to-back in one
        pass.  That races multi-stream effect nodes: an echo/noise-cancel
        filter chain's capture and playback ports show up asynchronously,
        and wiring its interior plus every user edge in a single burst
        could attach an edge to a dummy before the DSP sandwich existed,
        leaving the node half-connected.

        A later fix addressed that by issuing exactly one connect per
        call, globally, and refusing to issue another until the live
        graph confirmed the first. That closed the race but overshot:
        the one-connect gate was graph-wide, not node-local, so a single
        pair that took a moment to show up in the live snapshot stalled
        *every other, unrelated* pending link too - on a big session
        import (many edges wired in one batch, see main.py's
        _load_session) that could leave whole device outputs silent for
        seconds at a time while completely independent parts of the
        graph sat waiting on one slow confirmation.

        This version keeps the *reason* for pacing (never attach an edge
        to a dummy before its node's own interior is wired) but narrows
        the gate to what actually needs it: a candidate pair is held
        back only if one of its two ports is already used by another
        pair still waiting on confirmation (self._inflight_links) - i.e.
        only connects that could race the *same* node's own in-flight
        wiring are serialized against each other. Pairs touching
        entirely different nodes proceed in the same pass instead of
        queuing up behind whatever happens to be slow this tick.

        Entries are still keyed off the live snapshot, so a link dropped
        out from under us (external ``pw-cli`` destroy, module reload) is
        re-made on a later pass."""
        graph = self.graph
        live = graph.linked_pairs()
        now = _time.monotonic()

        # Retire any best-effort disconnect queued when an edge was
        # removed while its connect was still in flight.
        for pair in list(self._orphan_disconnects):
            try:
                graph.disconnect(*pair)
            except Exception as exc:
                logger.debug("orphan disconnect %s failed: %s", pair, exc)
            self._orphan_disconnects.discard(pair)
            self._link_gate.forget(pair)
            live.discard(pair)

        # Reconcile in-flight pairs against the live snapshot: confirmed
        # ones are done, timed-out ones are logged and dropped (they'll
        # be re-attempted below like any other missing pair, and won't
        # block anything else in the meantime).
        busy_ports: Set[int] = set()
        for pair, (edge_id, issued_at) in list(self._inflight_links.items()):
            if pair in live:
                del self._inflight_links[pair]
                self._link_gate.record_success(pair)
                continue
            if now - issued_at >= LINK_CONFIRM_TIMEOUT_S:
                del self._inflight_links[pair]
                self._link_gate.record_failure(pair)
                logger.warning(
                    "Link %s for %s was not confirmed by the live graph "
                    "within %.1fs - backing off (next retry in %.1fs)",
                    pair,
                    edge_id,
                    LINK_CONFIRM_TIMEOUT_S,
                    self._link_gate.next_attempt_in(pair),
                )
                continue
            busy_ports.add(pair[0])
            busy_ports.add(pair[1])

        # Record the full desired set on every edge (teardown accounting),
        # then build the ordered work list.  Internal DSP links go first
        # so an effect's sandwich is whole before user edges attach to
        # its dummies; everything else follows in a stable order.
        internal: List[Tuple[EdgeId, Tuple[int, int]]] = []
        external: List[Tuple[EdgeId, Tuple[int, int]]] = []
        seen: Set[Tuple[int, int]] = set()
        for edge_id in sorted(desired):
            pairs = desired[edge_id]
            entry = self._edge_links.setdefault(edge_id, _DesiredLinks())
            entry.pairs = pairs
            bucket = internal if edge_id.startswith("__internal__:") else external
            for pair in sorted(pairs):
                if pair in live or pair in seen:
                    continue
                seen.add(pair)
                bucket.append((edge_id, pair))

        for edge_id, pair in internal + external:
            if pair in graph.linked_pairs():
                continue
            if pair[0] in busy_ports or pair[1] in busy_ports:
                # One of this pair's ports already has an unconfirmed
                # connect outstanding - wait for that to settle before
                # racing another link onto the same port.
                continue
            if not self._link_gate.ready(pair):
                # A previous connect to this pair returned but the link
                # never showed up; don't re-issue it every pass.
                continue
            try:
                graph.connect(*pair)
            except Exception as exc:
                logger.warning("connect %s for %s failed: %s", pair, edge_id, exc)
                self._link_gate.record_failure(pair)
                continue
            logger.info(
                "Wiring %s -> %s for %s", pair[0], pair[1], edge_id
            )
            if pair in graph.linked_pairs():
                # Confirmed synchronously - keep going, no need to wait.
                self._link_gate.record_success(pair)
                continue
            # A real, asynchronous graph: remember this link and let
            # later pairs in this same pass proceed as long as they
            # don't touch the same ports.
            self._inflight_links[pair] = (edge_id, _time.monotonic())
            busy_ports.add(pair[0])
            busy_ports.add(pair[1])

    # ------------------------------------------------------------------
    # per-edge wiring status (for the GUI - see main.py's _serialize_edges)
    # ------------------------------------------------------------------

    def edge_wired(self, edge_id: EdgeId) -> bool:
        """Whether every pair the last sync decided `edge_id` needs is
        actually live right now. An edge with nothing desired (a gated/
        switched-off path, or one not computed yet) counts as wired -
        there is nothing pending for it to show as stuck."""
        entry = self._edge_links.get(edge_id)
        if entry is None or not entry.pairs:
            return True
        return entry.pairs.issubset(self.graph.linked_pairs())

    def drop_edge_links(self, edge_id: EdgeId) -> None:
        """Forget and disconnect an edge's live pairs so the next sync
        re-derives and re-creates them from scratch.

        sync() only fills in *missing* links; a link that exists but is
        dead (e.g. a downstream device link created before its source
        effect's module had settled) is left alone forever. Session load
        uses this to emulate the manual unplug/replug that fixes such a
        link: drop the pairs here, then let the next sync_locked()
        reconnect them in the right order."""
        with self._lock:
            entry = self._edge_links.pop(edge_id, None)
            # Any connect still in flight for this edge is void too.
            for pair, (inflight_edge_id, _issued_at) in list(
                self._inflight_links.items()
            ):
                if inflight_edge_id == edge_id:
                    del self._inflight_links[pair]
            if entry is None:
                return
            for pair in entry.pairs:
                self._link_gate.forget(pair)
                try:
                    self.graph.disconnect(*pair)
                except Exception as exc:
                    logger.debug(
                        "relink disconnect %s for %s failed: %s",
                        pair, edge_id, exc,
                    )

    def node_internals_wired(self, node_id: NodeId) -> bool:
        """Whether every internal link a BackedNode's own module needs
        is currently live.

        User-facing edges can all be wired while an effect's interior
        sandwich (dummy -> module capture, module playback -> dummy - see
        EchoCancelNode / _ChainEffect.internal_links) never connected:
        the node is then structurally present and its edges look fine,
        but no audio actually passes through it - acoustically dead.
        That is exactly the Echo/Noise-Cancel failure mode this exposes.

        Non-backed nodes and backed nodes with no internal links count
        as wired. An internal link with an empty desired set does *not*
        count: it means the module's stream never resolved, which is the
        dead case rather than the trivially-wired one."""
        node = self.nodes.get(node_id)
        if not isinstance(node, BackedNode):
            return True
        links = node.internal_links()
        if not links:
            return True
        live = self.graph.linked_pairs()
        for i in range(len(links)):
            entry = self._edge_links.get(f"__internal__:{node_id}:{i}")
            if entry is None or not entry.pairs or not entry.pairs.issubset(live):
                return False
        return True

    def mark_graph_loaded(self) -> None:
        self._graph_loaded = True
