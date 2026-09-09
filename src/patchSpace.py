"""
patchspace.py

Node-graph layer on top of pwgraph.PipewireGraph. Lets you build a
Blender-style graph of Input / Process / Output nodes and drives the
live PipeWire graph to match it, re-syncing whenever the graph is
edited.

This used to compile down into pwroute.RuleRouter rules and let that
layer's rule/cache reconciliation do the actual connecting. That
added a second source of truth (RuleRouter's _routed_cache /
_rule_connections) on top of PatchSpace's own idea of what should be
connected, and the two could drift apart - which is what made closing
a gate or tearing down a volume node an unreliable way to actually
disconnect the audio. PatchSpace now talks to PipewireGraph directly
(see sync() below) and is the only thing that decides what its own
edges are connected to; pwroute.RuleRouter is still available
separately for manually-authored rules (e.g. via the CLI), but
PatchSpace no longer depends on it at all.

Node design
-----------
Every node in the graph falls into one of four behavioral categories,
not enforced by inheritance but by which methods are implemented:

  * INPUT nodes implement source_filters() -> list[dict], each entry
    usable as a pwmatch source filter. They have no upstream.

  * OUTPUT nodes implement sink_filters() -> list[dict], each entry
    usable as a pwmatch sink filter. They have no downstream.

  * TRANSPARENT process nodes (SplitterNode, GateNode) contribute no
    identity of their own and are skipped over when resolving "what's
    really upstream of this edge". A transparent node may have at
    most one upstream edge (enforced by PatchSpace.add_edge) -
    "splitting" happens for free downstream, since any node's output
    can already feed several edges.

  * BACKED process nodes (VolumeProcessNode) materialize one or more
    real PipeWire nodes for as long as they exist, and expose
    input_identity()/output_identity() so pwmatch can route into/out
    of them. A backed node owns its real objects as a list of
    pw_owned.OwnedPwNode (`self.backings`) rather than a single id/
    process pair - so a node type that needs more than one real
    PipeWire object (a future multi-stage effect, say) is just a
    matter of appending more OwnedPwNode instances, not inventing a
    new lifecycle pattern.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

import pwmatch
from pwgraph import PipewireGraph
from pw_owned import OwnedPwNode, OwnedPwProcess

logger = logging.getLogger(__name__)

NodeId = str
EdgeId = str

# Name of the daemon's own virtual sink, created by
# pwgraph.PipewireGraph(virtual_sink_name=...) at startup (see
# main.py). Duplicated here rather than imported from main.py for the
# same reason SOCKET_PATH is duplicated between constants.py and
# patchbay_cli.py - this module shouldn't depend on the daemon's entry
# point. Keep this in sync with the virtual_sink_name= argument main.py
# passes to PipewireGraph if it's ever changed.
PATCHBAY_VIRTUAL_SINK_NAME = "PatchBay"

# Name of the daemon's own virtual microphone - the input-side mirror
# of PATCHBAY_VIRTUAL_SINK_NAME, created by
# pwgraph.PipewireGraph(virtual_mic_name=...) at startup (see
# main.py). Unlike the virtual sink (a single null-audio-sink),
# PATCHBAY_VIRTUAL_MIC_NAME names the visible Audio/Source - a
# pw-loopback process republishing an internal "{name}_sink"'s
# monitor - since that's the object other apps actually pick as a
# microphone; see pwgraph.PipewireGraph._create_virtual_mic() and
# VirtualMicNode below, which uses the exact same three-object
# pattern for a user-created (rather than built-in) virtual mic. Keep
# this in sync with the virtual_mic_name= argument main.py passes to
# PipewireGraph if it's ever changed.
PATCHBAY_VIRTUAL_MIC_NAME = "PatchBay Mic"


def _run_wpctl(*args, timeout: float = 2.0) -> bool:
    """Best-effort `wpctl <args>`, swallowing failures the same way the
    rest of this module already treats a device that may not currently
    be present - the caller doesn't need to know or care which; a
    value that fails to apply now gets tried again on the next resolve
    or safety-sync tick regardless (see DeviceControlMixin below)."""
    try:
        subprocess.run(
            ["wpctl", *[str(a) for a in args]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return True
    except Exception as e:
        logger.warning("wpctl %s failed: %s", args, e)
        return False


# ---------------------------------------------------------------------
# Node base classes
# ---------------------------------------------------------------------


class Node:
    def __init__(self, node_id: NodeId):
        self.id = node_id

    def is_transparent(self) -> bool:
        return False


class InputNode(Node):
    def source_filters(self) -> List[dict]:
        raise NotImplementedError


class OutputNode(Node):
    def sink_filters(self) -> List[dict]:
        raise NotImplementedError


class TransparentNode(Node):
    """A process node with no PipeWire backing of its own."""

    def is_transparent(self) -> bool:
        return True

    def gate_open(self) -> bool:
        """False if this node currently blocks signal passing through it."""
        return True


class BackedNode(Node):
    """
    A process node materialized as one or more real, named PipeWire
    nodes, owned for as long as this node exists in the graph.

    `backing_node_name` remains the single "primary" name used for
    input_identity()/output_identity() (i.e. what an edge routes
    into/out of) - that's the one real object every existing node
    type needs. `backings` is the full list of OwnedPwNode this node
    is responsible for creating and tearing down; a subclass that
    needs additional real objects beyond the primary one just appends
    more to this list in ensure_backing().
    """

    def __init__(self, node_id: NodeId, backing_node_name: str):
        super().__init__(node_id)
        self.backing_node_name = backing_node_name
        self.backings: List[OwnedPwNode] = []

    def input_identity(self, port: str = "in") -> dict:
        # `port` distinguishes between a multi-input node type's
        # several named inputs (see Edge.to_port's comment and
        # PatchSpace.sync(), which is what actually passes a non-
        # default value through here) - every node type except
        # EchoCancelNode has exactly one input and ignores it, same
        # as before this parameter existed.
        #
        # Targets are matched by pwmatch.matches_sink_target, where
        # "name" is already an EXACT node.name comparison.
        return {"name": self.backing_node_name}

    def output_identity(self) -> dict:
        # Sources are matched by pwmatch.matches_source_filter, where
        # "name" is a SUBSTRING match. node.name is a unique identifier,
        # so identity-by-node.name must go through the exact "nodeName"
        # key instead - a loose "name" here would also select any other
        # live node whose name merely contains ours (e.g. a virtual
        # mic's internal "{name}_sink" sibling).
        return {"nodeName": self.backing_node_name}

    def ensure_backing(self) -> None:
        """Create the real PipeWire object(s) if not already up."""
        raise NotImplementedError

    def teardown_backing(self) -> None:
        for owned in self.backings:
            owned.destroy()
        self.backings.clear()

    def dead_backings(self) -> List[OwnedPwNode]:
        """The subset of self.backings whose own process has exited
        WITHOUT our teardown_backing() asking it to. A BackedNode's
        real PipeWire objects are owned by those processes (a pw-cli
        connection owns everything it created - see pw_owned.py), so
        an entry here means part or all of this node's live backing is
        gone even though the PatchSpace node is still in the graph:
        e.g. a crashed LADSPA plugin takes the pw-cli process hosting
        its filter-chain module down, and the server then tears the
        module's streams down with that connection.

        Name-only placeholders (the sibling-stream OwnedPwNodes a
        filter-chain/echo-cancel module appends for resolve_backing()
        to watch) own no process and never appear here - their fate is
        tied to the module owner's, so only that owner's liveness needs
        polling."""
        return [b for b in self.backings if b.owns_process and not b.is_alive]

    def resolve_backing(self, name: str, node_id: int) -> bool:
        """Called by the daemon once a real node with `name` shows up
        in the live graph, so any OwnedPwNode still waiting to learn
        its id can pick it up. Returns True if one of ours matched."""
        for owned in self.backings:
            if owned.node_id is None and owned.name == name:
                owned.resolve(node_id)
                return True
        return False

    def internal_links(self) -> List[Tuple[dict, dict]]:
        """(source_identity, sink_identity) pairs this node needs wired
        between its OWN backing objects, independent of any
        PatchSpace edge the user drew. Default: none - almost every
        node type is a single real object (or a capture/playback pair
        already fully described by input_identity()/output_identity())
        with nothing private to connect.

        A composite node with more than one internal audio stage
        overrides this so PatchSpace.sync() keeps that private plumbing
        linked using the exact same self-healing connect/disconnect
        diffing it already runs for user-drawn edges (each pair gets a
        synthetic edge id, `__internal__:{node_id}:{i}`, so a stale
        internal link is torn down and a missing one reconnected on
        every sync() - including the 2-second safety-sync tick - with
        zero extra bookkeeping). Identity dicts use the same shape as
        input_identity()/output_identity() (matched via
        pwmatch.find_source_nodes / pwmatch.find_target_nodes)."""
        return []

    def reload_backing(self) -> None:
        """Recreate this node's backing from scratch (teardown then
        ensure). A node type whose user-facing sockets are stable
        adapters wrapped around a rebuildable interior (see
        _FilterChainNode) overrides this to reload ONLY the interior,
        so a reload never drops the edges the user has drawn on the
        stable part."""
        self.teardown_backing()
        self.ensure_backing()


class LiveResolvableNode:
    """
    Mixin for a node that references an existing, externally-owned
    PipeWire object by a persistent identity (a device's node.name, or
    an app's application.name) rather than creating/owning one itself
    - contrast with BackedNode, which owns real objects it created.

    Because the identity survives the underlying object's absence
    (unplugged hardware, a closed app), a node using this mixin stays
    in PatchSpace's graph and keeps its configured identity even when
    nothing currently matches it - resolve_live(None, None) is normal,
    not an error. `live_node_id` is exactly analogous to
    BackedNode.backing_node_id: the real graph id of whatever
    currently satisfies this node's identity, used only to point
    property-control commands (set_volume, set_profile) at the right
    live object. It is NEVER used for matching in source_filters()/
    sink_filters() - those always match by identity - which is why
    routing through this node keeps working the instant the device
    reappears, without needing this resolution step at all.
    """

    def __init__(self):
        self.live_node_id: Optional[int] = None
        self.live_props: dict = {}

    def matches_live_node(self, props: dict) -> bool:
        raise NotImplementedError

    def resolve_live(self, node_id: Optional[int], props: Optional[dict]) -> None:
        self.live_node_id = node_id
        self.live_props = dict(props) if props else {}


class DeviceControlMixin:
    """
    Adds persisted, continuously-reapplied hardware-device settings -
    output/input volume, and profile (i.e. Bluetooth codec choice) -
    to a LiveResolvableNode-based device node.

    These are node CONFIG, not one-shot live actions: once set (via
    main.py's set_device_volume / set_device_profile commands) they
    live on the node itself, so they show up in get_nodes/get_state
    and round-trip through export_config/apply_config exactly like
    device_name already does. They're also *enforced*, not just
    applied once - apply_device_settings() runs again every time this
    device resolves to a live object (plugged back in, or the daemon
    starting up with it already present - see the resolve_live()
    overrides on DeviceInputNode/DeviceOutputNode below) and again on
    every safety-sync tick (main.py's _safety_sync), so a Bluetooth
    reconnect resetting the codec to its default profile, or anything
    else nudging the volume, gets pushed back to the configured value
    within one tick instead of silently sticking.
    """

    def __init__(
        self,
        device_volume: float = 1.0,
        profile_index: Optional[int] = None,
        profile_description: str = "",
    ):
        self.device_volume = device_volume
        # None means "no profile preference configured" - leave
        # whatever profile the device is already on alone.
        self.profile_index = profile_index
        self.profile_description = profile_description
        # (device_id, profile_index) we last successfully pushed via
        # wpctl - see apply_device_settings() below. None until the
        # first successful push.
        self._applied_profile: Optional[Tuple[Any, int]] = None

    def apply_device_settings(self) -> None:
        """No-op for whichever of volume/profile isn't currently
        resolvable (device unplugged, or no profile preference set) -
        safe to call unconditionally from a resolve callback or the
        safety-sync loop without checking connection state first.

        The profile half of this is guarded against re-doing work
        that's already done, unlike the volume half: `wpctl
        set-profile` isn't a soft, in-place update the way
        `set-volume` is - PipeWire tears the device's node(s) down and
        recreates them to apply a profile change, i.e. it visibly
        "restarts" the device. main.py calls this method
        unconditionally from *both* the 2-second safety-sync loop and
        every resolve_live() (so a Bluetooth reconnect that reset the
        codec gets corrected quickly) - without tracking what's
        already been applied, that combination re-issues
        `set-profile` over and over forever the moment any profile
        preference is configured: pick a codec once, and the device
        restarts every 2 seconds indefinitely (each restart triggers
        a fresh resolve, which reapplies the profile, which restarts
        it again). Tracking (device_id, profile_index) here means the
        wpctl call only fires when the target actually differs from
        what we last successfully pushed - once when a codec is first
        picked, again if it's changed, and again if the device
        reappears as a genuinely different object (new device_id) -
        never on a tick where nothing changed."""
        live_node_id = getattr(self, "live_node_id", None)
        if live_node_id is not None:
            _run_wpctl("set-volume", live_node_id, self.device_volume)

        device_id = getattr(self, "live_props", {}).get("device.id")
        if device_id is None or self.profile_index is None:
            return

        target = (device_id, self.profile_index)
        if self._applied_profile == target:
            return  # already on the configured profile - nothing to do

        if _run_wpctl("set-profile", device_id, self.profile_index):
            self._applied_profile = target


class DeviceInputNode(InputNode, LiveResolvableNode, DeviceControlMixin):
    """A specific hardware capture device (mic, line-in, Bluetooth
    input), selected from a live list at add-time but matched
    thereafter by node.name - so it stays in the graph, keeps its
    selection, and reconnects automatically if the device is
    unplugged and replugged."""

    def __init__(
        self,
        node_id: NodeId,
        device_name: str = "",
        description: str = "",
        device_volume: float = 1.0,
        profile_index: Optional[int] = None,
        profile_description: str = "",
    ):
        InputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        DeviceControlMixin.__init__(
            self, device_volume, profile_index, profile_description
        )
        self.device_name = device_name  # matched against node.name (exact)
        self.description = description  # display only; refreshed on resolve

    def source_filters(self) -> List[dict]:
        if not self.device_name:
            return []
        # nodeName (exact) - device_name is a specific hardware node's
        # node.name, and the substring "name" key would also select any
        # other live source whose node.name merely contains it.
        return [{"nodeName": self.device_name, "mediaClass": "Audio/Source"}]

    def matches_live_node(self, props: dict) -> bool:
        return bool(self.device_name) and props.get("node.name") == self.device_name

    def resolve_live(self, node_id, props):
        super().resolve_live(node_id, props)
        if props:
            self.description = (
                props.get("node.description")
                or props.get("node.nick")
                or self.description
            )
        if node_id is not None:
            self.apply_device_settings()


class DeviceOutputNode(OutputNode, LiveResolvableNode, DeviceControlMixin):
    """Mirror of DeviceInputNode for a specific hardware playback
    device (speakers, headphones, Bluetooth output)."""

    def __init__(
        self,
        node_id: NodeId,
        device_name: str = "",
        description: str = "",
        device_volume: float = 1.0,
        profile_index: Optional[int] = None,
        profile_description: str = "",
    ):
        OutputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        DeviceControlMixin.__init__(
            self, device_volume, profile_index, profile_description
        )
        self.device_name = device_name
        self.description = description

    def sink_filters(self) -> List[dict]:
        if not self.device_name:
            return []
        return [{"name": self.device_name, "mediaClass": "Audio/Sink"}]

    def matches_live_node(self, props: dict) -> bool:
        return bool(self.device_name) and props.get("node.name") == self.device_name

    def resolve_live(self, node_id, props):
        super().resolve_live(node_id, props)
        if props:
            self.description = (
                props.get("node.description")
                or props.get("node.nick")
                or self.description
            )
        if node_id is not None:
            self.apply_device_settings()


class AppInputNode(InputNode, LiveResolvableNode):
    """A specific running application's playback stream, selected by
    application.name (e.g. 'Firefox', 'Spotify'). Persists even while
    the app is closed - matching resumes automatically next time an
    app with that name launches."""

    def __init__(self, node_id: NodeId, app_name: str = ""):
        InputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        self.app_name = app_name

    def source_filters(self) -> List[dict]:
        if not self.app_name:
            return []
        return [{"name": self.app_name, "mediaClass": "Stream/Output/Audio"}]

    def matches_live_node(self, props: dict) -> bool:
        haystack = props.get("application.name") or props.get("node.name") or ""
        return bool(self.app_name) and haystack == self.app_name


class AppOutputNode(OutputNode, LiveResolvableNode):
    """Mirror of AppInputNode, for routing INTO a specific app's
    capture stream (e.g. a mic routed into one particular
    conferencing app). Matches by both exact node.name and a
    description substring (sink matching in pwmatch only checks
    node.name, and many apps' capture-stream node.name isn't their
    app name) - two filter entries OR together, so either is enough."""

    def __init__(self, node_id: NodeId, app_name: str = ""):
        OutputNode.__init__(self, node_id)
        LiveResolvableNode.__init__(self)
        self.app_name = app_name

    def sink_filters(self) -> List[dict]:
        if not self.app_name:
            return []
        return [
            {"name": self.app_name, "mediaClass": "Stream/Input/Audio"},
            {"description": self.app_name, "mediaClass": "Stream/Input/Audio"},
        ]

    def matches_live_node(self, props: dict) -> bool:
        haystack = props.get("application.name") or props.get("node.name") or ""
        return bool(self.app_name) and haystack == self.app_name


# ---------------------------------------------------------------------
# Input nodes
# ---------------------------------------------------------------------


class PatchBayDeviceNode(InputNode, OutputNode):
    """
    A no-config convenience node pointing directly at the daemon's own
    built-in virtual sink (PATCHBAY_VIRTUAL_SINK_NAME) - the thing a
    user would otherwise have to reconstruct by hand with a
    Description Output filter matching its sink name AND a separate
    Description Input filter matching the same name's monitor ports.

    It inherits both InputNode and OutputNode rather than picking one:
    the underlying object is a real Audio/Sink, which - like any
    sink - has input ports apps can be routed into (sink_filters(),
    used as a target) and monitor ports usable as a live source
    (source_filters(), used as an origin). PatchSpace's engine treats
    these two roles as independent isinstance() checks (see
    PatchSpace._resolve_sources and PatchSpace.sync()), so a node
    implementing both filter methods is automatically usable as either
    end of an edge - no PatchSpace/engine changes needed for this to
    work.

    source_filters() deliberately matches its node EXACTLY (nodeName,
    not the substring "name" key): the daemon's own virtual microphone
    plumbing uses node.names that merely *start with* this sink's name
    ("PatchBay Mic", "PatchBay Mic_sink", "PatchBay Mic_capture"), and
    a loose substring filter on "PatchBay" would let this node select
    those too - silently routing the live mic through whatever this
    node feeds. See pwmatch.matches_source_filter for the distinction.
    """

    def source_filters(self) -> List[dict]:
        return [{"nodeName": PATCHBAY_VIRTUAL_SINK_NAME}]

    def sink_filters(self) -> List[dict]:
        return [{"name": PATCHBAY_VIRTUAL_SINK_NAME}]


class PatchBayMicDeviceNode(InputNode, OutputNode):
    """
    No-config convenience node pointing at the daemon's own built-in
    virtual microphone (PATCHBAY_VIRTUAL_MIC_NAME) - the input-side
    mirror of PatchBayDeviceNode above. The daemon creates this
    virtual mic once at startup and points the system's default input
    device at it (see pwgraph.PipewireGraph's virtual_mic_name=/
    virtual_mic_set_default=), so apps that just use "the default
    mic" pick up whatever's routed through this node in the
    PatchSpace graph, without the user reselecting an input device
    anywhere.

    Same "inherit both InputNode and OutputNode" shape as
    PatchBayDeviceNode, for the same reason: real audio (an actual
    hardware mic, or anything else) routes IN via sink_filters()
    targeting the virtual mic's underlying "{name}_sink", and
    whatever's flowing through it is picked up downstream via
    source_filters() targeting the loopback's visible Audio/Source
    (PATCHBAY_VIRTUAL_MIC_NAME itself) - the same two independent
    isinstance() checks PatchSpace's engine already handles for
    PatchBayDeviceNode (PatchSpace._resolve_sources / sync()), so this
    node type needed no engine changes either.
    """

    def source_filters(self) -> List[dict]:
        # Exact node.name identity - this node's visible object is the
        # loopback Audio/Source whose node.name is exactly
        # PATCHBAY_VIRTUAL_MIC_NAME. A substring "name" here would also
        # select the virtual mic's own internal "{name}_sink" monitor.
        return [{"nodeName": PATCHBAY_VIRTUAL_MIC_NAME}]

    def sink_filters(self) -> List[dict]:
        return [{"name": f"{PATCHBAY_VIRTUAL_MIC_NAME}_sink"}]


class RegexInputNode(InputNode):
    """Matches live nodes by nameRegex."""

    def __init__(self, node_id: NodeId, pattern: str):
        super().__init__(node_id)
        self.pattern = pattern

    def source_filters(self) -> List[dict]:
        return [{"nameRegex": self.pattern}]


class MediaClassInputNode(InputNode):
    """e.g. every hardware microphone: media_class='Audio/Source'."""

    def __init__(self, node_id: NodeId, media_class: str):
        super().__init__(node_id)
        self.media_class = media_class

    def source_filters(self) -> List[dict]:
        return [{"mediaClass": self.media_class}]


class DescriptionInputNode(InputNode):
    def __init__(self, node_id: NodeId, description: str):
        super().__init__(node_id)
        self.description = description

    def source_filters(self) -> List[dict]:
        return [{"description": self.description}]


# ---------------------------------------------------------------------
# Output nodes (mirror of the input nodes above)
# ---------------------------------------------------------------------


class RegexOutputNode(OutputNode):
    def __init__(self, node_id: NodeId, pattern: str, port_type: Optional[str] = None):
        super().__init__(node_id)
        self.pattern = pattern
        self.port_type = port_type

    def sink_filters(self) -> List[dict]:
        return [{"nameRegex": self.pattern, "type": self.port_type}]


class MediaClassOutputNode(OutputNode):
    def __init__(
        self, node_id: NodeId, media_class: str, port_type: Optional[str] = None
    ):
        super().__init__(node_id)
        self.media_class = media_class
        self.port_type = port_type

    def sink_filters(self) -> List[dict]:
        return [{"mediaClass": self.media_class, "type": self.port_type}]


class DescriptionOutputNode(OutputNode):
    def __init__(
        self, node_id: NodeId, description: str, port_type: Optional[str] = None
    ):
        super().__init__(node_id)
        self.description = description
        self.port_type = port_type

    def sink_filters(self) -> List[dict]:
        return [{"description": self.description, "type": self.port_type}]


# ---------------------------------------------------------------------
# Transparent process nodes
# ---------------------------------------------------------------------


class SplitterNode(BackedNode):
    """Physical splitter: audio in -> null sink -> monitor ports -> multiple outputs."""

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: Optional[str] = None,
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name or f"splitter_{node_id}")
        self._pw_cli_command = pw_cli_command
        self._pw_cli_settle = pw_cli_settle

    def input_identity(self, port: str = "in") -> dict:
        return {"name": self.backing_node_name}

    def output_identity(self) -> dict:
        # Exact node.name identity - see BackedNode.output_identity.
        return {"nodeName": self.backing_node_name}

    def ensure_backing(self) -> None:
        if self.backings:
            return

        config = (
            "factory.name=support.null-audio-sink "
            f'node.name="{self.backing_node_name}" '
            f'node.description="Splitter: {self.id}" '
            "media.class=Audio/Sink "
            "audio.position=[FL,FR]"
        )
        command = f"create-node adapter {config}"
        logger.info("Creating splitter node with: %s", command)

        owned = OwnedPwNode(
            self.backing_node_name, self._pw_cli_command, self._pw_cli_settle
        )
        if owned.create(command):
            self.backings.append(owned)
        else:
            logger.error("Splitter node creation failed for %r", self.id)


class GateNode(TransparentNode):
    """
    The "Should Play" checkbox. When disabled, sync() treats this
    node as having no source at all, so every edge downstream of it
    (all the way to the nearest real sink) is disconnected on the
    very next sync() - not merely "not re-created next time something
    else changes". See PatchSpace.sync().
    """

    def __init__(self, node_id: NodeId, enabled: bool = True):
        super().__init__(node_id)
        self.enabled = enabled

    def gate_open(self) -> bool:
        return self.enabled


class ExcludeFilterNode(TransparentNode):
    """
    "Filter out" node: a transparent pass-through, exactly like
    GateNode/SplitterNode (one upstream edge, contributes no identity
    of its own - see the module docstring's TRANSPARENT category),
    except it also narrows whatever's upstream by a nameRegex it
    excludes rather than requires.

    It does this by annotating each of the upstream source filter
    dicts with an extra "exclude" entry (see
    PatchSpace._resolve_sources and pwmatch.matches_source_filter) -
    it never touches the live graph itself. Chaining several of these
    in a row (e.g. "all apps" -> exclude "Discord" -> exclude "OBS")
    appends one more "exclude" entry each time, so the final match is
    "upstream AND NOT filter1 AND NOT filter2 AND ...", which is
    exactly the "get everything, then knock out what I don't want"
    chain this node type exists for.
    """

    def __init__(self, node_id: NodeId, pattern: str = ""):
        super().__init__(node_id)
        self.pattern = pattern

    def exclude_filter(self) -> Optional[dict]:
        """The pwmatch filter dict this node excludes, or None while
        unconfigured (an empty pattern would otherwise compile to a
        regex that matches everything, silently excluding every
        source - better to just contribute nothing until a pattern is
        set)."""
        if not self.pattern:
            return None
        return {"nameRegex": self.pattern}


# ---------------------------------------------------------------------
# Example backed process node: a volume slider
# ---------------------------------------------------------------------


class VolumeProcessNode(BackedNode):
    """
    A volume slider (or, driven 0.0/1.0 by the GUI's "mute switch"
    node type, an on/off gate that actually attenuates real audio
    rather than just rerouting it), backed by a
    libpipewire-module-filter-chain node running the builtin "volume"
    plugin.
    """

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        initial_volume: float = 1.0,
        volume_min: float = 0.0,
        volume_max: float = 1.0,
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name)
        self.volume = initial_volume  # fraction (0-1)
        self.volume_min = volume_min
        self.volume_max = volume_max
        self._pw_cli_command = pw_cli_command
        self._pw_cli_settle = pw_cli_settle

    @property
    def backing_node_id(self) -> Optional[int]:
        """The real graph id of our one backing object, once
        resolved - kept as a property (rather than a plain attribute)
        so callers/serializers written against the old single-id API
        keep working unchanged."""
        return self.backings[0].node_id if self.backings else None

    def ensure_backing(self) -> None:
        if self.backings:
            return

        # Create a null sink with monitor.channel-volumes enabled.
        config = (
            "factory.name=support.null-audio-sink "
            f'node.name="{self.backing_node_name}" '
            f'node.description="{self.backing_node_name}" '
            "media.class=Audio/Sink "
            "audio.position=[FL,FR] "
            "monitor.channel-volumes=1"
        )
        command = f"create-node adapter {config}"
        logger.info("Creating volume node with: %s", command)

        owned = OwnedPwNode(
            self.backing_node_name, self._pw_cli_command, self._pw_cli_settle
        )
        if owned.create(command):
            self.backings.append(owned)
        else:
            logger.error("Volume node creation failed for %r", self.id)

    def set_volume(self, fraction: float) -> None:
        """Set volume as a fraction (0-1) mapped to [volume_min, volume_max]."""
        self.volume = max(0.0, min(1.0, fraction))
        if not self.backings or self.backings[0].node_id is None:
            return

        # Compute actual volume in the configured range
        actual = self.volume_min + (self.volume_max - self.volume_min) * self.volume
        logger.info(
            "Volume for %s set to %.2f (fraction %.2f)",
            self.backing_node_name,
            actual,
            self.volume,
        )

        node_id = self.backings[0].node_id
        _run_wpctl("set-volume", node_id, actual)


# ---------------------------------------------------------------------
# Effects: real-time audio processing nodes
# ---------------------------------------------------------------------
#
# A user-facing effect node is NOT connected to directly. Splitter and
# Volume nodes are each a single bare support.null-audio-sink adapter,
# but an effect genuinely needs PipeWire to run a real DSP graph over
# the audio, which a bare null sink can't do. Every effect is instead a
# *sandwich*, owned as one BackedNode:
#
#                     |<--------  PatchSpace node  -------->|
#
#     real source -> [ in dummy sink ]      (input_identity:
#                    (null-audio-sink)        user edges land here)
#                          ||  internal link (monitor -> capture)
#                          \/
#                  [ fx capture ] <-> DSP <-> [ fx playback ]
#                    (the real module: filter-chain for
#                     NoiseCancel/Reverb/SensitivityGate,
#                     echo-cancel for EchoCancelNode)
#                          ||  internal link (playback -> sink)
#                          \/
#     real sink    <- [ out dummy sink ]     (output_identity:
#                    (null-audio-sink)        its monitor ports)
#
# The two dummies are the exact same null-audio-sink adapter
# SplitterNode/VolumeProcessNode use: created once, owned for the
# node's whole life, NEVER rebuilt. Every PatchSpace edge a user draws
# plugs into a dummy, so adding/removing an edge only ever links or
# unlinks a stable, always-running adapter - it never touches the DSP
# module's own capture/playback streams. That lets the module in the
# middle be torn down and reloaded freely (an option change, a crash,
# a plugin-path fix) without any user edge dropping or renegotiating:
# the only links onto the module's streams are PatchSpace's own
# internal_links(), which sync() recomputes by name on every pass and
# re-connects the moment the fresh module appears.
#
# Two permanent silent pw-cat keepalives protect the sandwich ends: a
# feed into the input dummy (so it is always driven, even with the
# user's real source unplugged) and a drain of the output dummy's
# signal (so the output side always has a consumer). Without them a
# fully-unplugged effect could idle/suspend, and PipeWire doesn't
# reliably resume a suspended stream just because a fresh link arrives
# (see VirtualMicNode's docstring for the full "why"). See
# _FilterChainNode's docstring for how these fit with the interior.
#
# Which real module does the DSP:
#
#   * libpipewire-module-filter-chain - runs a graph of LADSPA/builtin
#     plugins over the signal (NoiseCancelNode, ReverbNode,
#     SensitivityGateNode). _FilterChainNode below is the shared
#     sandwich plumbing for this kind.
#   * libpipewire-module-echo-cancel - PipeWire's own WebRTC-based
#     acoustic echo canceller (EchoCancelNode). A different module
#     with a different (multi-stream) args shape - see that class's
#     docstring. It is still directly exposed (its sockets map onto
#     the module's own streams) rather than sandwiched.
#
# A LADSPA plugin (NoiseCancelNode/ReverbNode/SensitivityGateNode) has
# to actually be installed on the machine running the daemon - unlike
# everything above, which only depends on PipeWire itself. Which
# package provides it, and its exact install path, varies by distro.
# If the configured plugin path/label doesn't exist, pw-cli simply
# fails to load the module and OwnedPwNode.create() returns False -
# the same graceful, logged failure every other backed node already
# has (see pw_owned.OwnedPwNode.create()), not a crash - and only the
# sandwich's interior is missing until the `ladspa_plugin`/
# `ladspa_label` config is corrected to match what's actually
# installed (the dummies and keepalives are still created, so the
# node's sockets exist and stay healthy the whole time).


def _spawn_silent_feed(
    name: str,
    target: str,
    pw_cli_command: Tuple[str, ...],
    pw_cli_settle: float,
) -> Optional["OwnedPwProcess"]:
    """Start a silent `pw-cat --playback` stream permanently feeding
    `target` (an Audio/Sink), so that sink never drops to zero active
    links - and therefore never gets suspended by the session manager
    - just because its one real upstream edge happens to be
    disconnected right now. Shared by every keepalive target in this
    file that a user edge can freely connect/disconnect: the dummy
    input sink of every _FilterChainNode sandwich (see that class),
    and EchoCancelNode's mic/probe sides. Same technique
    VirtualMicNode's own keepalive uses - see that class's docstring
    for the full "why" (a suspended node doesn't reliably resume just
    because a fresh link arrives)."""
    proc = OwnedPwProcess(name, pw_cli_command, pw_cli_settle)
    command = (
        "pw-cat",
        "--playback",
        "--volume",
        "0",
        "--target",
        target,
        "--raw",
        "--format",
        "s16",
        "--rate",
        "48000",
        "--channels",
        "2",
        "/dev/zero",
    )
    return proc if proc.create(command) else None


def _spawn_silent_drain(
    name: str,
    target: str,
    pw_cli_command: Tuple[str, ...],
    pw_cli_settle: float,
) -> Optional["OwnedPwProcess"]:
    """The output-side mirror of _spawn_silent_feed: a `pw-cat
    --record` stream permanently draining `target` into /dev/null, so
    that side never drops to zero active links either - the same
    suspend risk _spawn_silent_feed guards against, just on whichever
    side happens to be the *producer*. Echo-cancel's cleaned "out"
    stream, and the dummy output sink of every _FilterChainNode
    sandwich (PipeWire satisfies a record targetting a sink by tapping
    whatever feeds it, so the drain ends up consuming the sandwich's
    playback output), are exactly this: nothing guarantees a real
    consumer is always wired to them, so without a permanent drain an
    unplugged downstream edge could suspend them exactly like an
    un-fed capture side would."""
    proc = OwnedPwProcess(name, pw_cli_command, pw_cli_settle)
    command = (
        "pw-cat",
        "--record",
        "--target",
        target,
        "--raw",
        "--format",
        "s16",
        "--rate",
        "48000",
        "--channels",
        "2",
        "/dev/null",
    )
    return proc if proc.create(command) else None


class _FilterChainNode(BackedNode):
    """
    Shared plumbing for an effect backed by
    `load-module libpipewire-module-filter-chain`, exposed to the
    PatchSpace graph as a *sandwich* (see the "Effects" module comment
    above for the shape): two plain null-audio-sink dummy adapters -
    ``{backing_node_name}_in`` and ``{backing_node_name}_out`` - are
    created once and owned for the node's whole life, and the real
    filter-chain module's capture/playback streams
    (``{backing_node_name}`` / ``{backing_node_name}_fx_out``) sit in
    between them. Every user edge plugs into a dummy, never into the
    module.

    Concretely, each subclass only implements `_filter_graph_args()`
    (the SPA-JSON "filter.graph = { ... }" fragment naming which
    plugin to run). The generic plumbing here provides, with no
    per-class code:

      * the identities: input_identity() is the input dummy sink;
        output_identity() is the output dummy's monitor ports.
      * the private plumbing between the four objects, via
        internal_links() - PatchSpace.sync() keeps those two links
        connected/disconnected/re-connected by name on every pass, so
        they self-heal across every module reload for free (see
        BackedNode.internal_links for how the synthetic-edge diffing
        works).
      * two permanent silent keepalives (see _ensure_keepalives): a
        `pw-cat --playback` feed into the input dummy and a `pw-cat
        --record` drain of the output dummy's signal. PipeWire
        suspends a stream node the moment it has zero active links,
        and once suspended a fresh link arriving later doesn't
        reliably resume the node's own scheduling. The keepalives make
        sure that can never happen: the input dummy is always driven
        (so its monitor - and therefore the whole chain downstream of
        it - never goes idle) even when no user source is wired up,
        and the output side always has a consumer even when nothing is
        plugged in downstream.
      * an interior-only reload (reload_backing()) that swaps just the
        filter-chain module while leaving both dummies and the input
        feed untouched - used by main.py for load-time-only option
        changes (plugin path, wet/dry) and crash recovery. Because the
        user's edges attach to the dummies, such a reload never drops
        or renegotiates a user edge: only the two internal links are
        briefly broken, and sync() re-makes them against the fresh
        module's streams on the next pass. The output drain is
        restarted too, since PipeWire satisfies a record targetting a
        sink by tapping whatever feeds it (i.e. the old module's
        playback stream), which dies with the module.

    Why this shape instead of just exposing the module's own
    capture/playback streams as the input/output (the pre-sandwich
    design): a module reload used to take the user's edges with it -
    every option change or crash dropped the links on both sides, and
    re-wiring a *running* chain into a fresh module was exactly the
    flaky live-to-live renegotiation that made effects unreliable.
    Dummy adapters are the same objects every splitter/volume already
    is: they never rebuild, so plugging and unplugging an effect just
    links/unlinks a stable, always-running sink - and reloading the
    interior is invisible to the rest of the graph.

    If the module itself fails to load (missing LADSPA plugin - see
    the module comment above), the dummies and keepalives are still
    created: the node keeps its sockets and its chain simply carries
    nothing until the `ladspa_plugin`/`ladspa_label` config is fixed
    and something triggers reload_backing(). ensure_backing() is
    deliberately name-based and idempotent (create whatever is
    missing, leave whatever is up) so it doubles as the repair path
    for a backing whose process died on its own (see main.py's
    _repair_dead_backings), and canonicalizes self.backings so the
    module stays the primary (index 0) backing.
    """

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name)
        self._pw_cli_command = pw_cli_command
        self._pw_cli_settle = pw_cli_settle

    # ---- naming ----

    @property
    def _fx_capture_name(self) -> str:
        # The filter-chain module's capture (sink) stream - also the
        # module's primary/owning pw-cli backing. Kept equal to
        # backing_node_name (as in the pre-sandwich design) so live
        # control params target the same name as always.
        return self.backing_node_name

    @property
    def _fx_playback_name(self) -> str:
        # The module's playback (source) stream.
        return f"{self.backing_node_name}_fx_out"

    @property
    def _input_dummy_name(self) -> str:
        # The stable input endpoint users wire real sources into.
        return f"{self.backing_node_name}_in"

    @property
    def _output_dummy_name(self) -> str:
        # The stable output endpoint whose monitor users wire sinks to.
        return f"{self.backing_node_name}_out"

    @property
    def _feed_name(self) -> str:
        return f"{self._input_dummy_name}_keepalive"

    @property
    def _drain_name(self) -> str:
        return f"{self._output_dummy_name}_keepalive"

    # ---- identities / private plumbing ----

    def input_identity(self, port: str = "in") -> dict:
        # Real audio routes into the input dummy (a plain Audio/Sink) -
        # see BackedNode.input_identity for why "name" (exact match) is
        # the right key here. Filter-chain effects have exactly one
        # input, so `port` is ignored (see EchoCancelNode for the one
        # node type that actually branches on it).
        return {"name": self._input_dummy_name}

    def output_identity(self) -> dict:
        # Processed audio comes out of the output dummy's monitor ports,
        # not the module's playback stream directly - exact "nodeName"
        # identity for the same reason VirtualMicNode's loopback uses it
        # (a monitor-enabled sink is matched as a source via its monitor
        # ports; see that class).
        return {"nodeName": self._output_dummy_name}

    def internal_links(self) -> List[Tuple[dict, dict]]:
        # The sandwich's private plumbing, maintained by
        # PatchSpace.sync() exactly like user edges (synthetic edge ids
        # __internal__:{node_id}:{i}): dummy-in's monitor feeds the
        # module's capture side, and the module's playback side feeds
        # the output dummy.
        return [
            (
                {"nodeName": self._input_dummy_name},
                {"name": self._fx_capture_name},
            ),
            (
                {"nodeName": self._fx_playback_name},
                {"name": self._output_dummy_name},
            ),
        ]

    def _filter_graph_args(self) -> str:
        raise NotImplementedError

    # ---- backing lifecycle ----

    def _processor_backing(self) -> Optional[OwnedPwNode]:
        """The OwnedPwNode that owns the filter-chain module's pw-cli
        session (i.e. the capture-side backing) - the one whose
        connection live-control set_params ride on. Canonicalization
        keeps it at self.backings[0], but look it up by name rather
        than by index so a partially-repaired list can't derail it."""
        for owned in self.backings:
            if owned.name == self._fx_capture_name:
                return owned
        return None

    def _spawn_processor(self) -> Optional[OwnedPwNode]:
        """Load the filter-chain module: one pw-cli connection produces
        both the capture and playback streams at once. Only the primary
        (capture-side) OwnedPwNode actually spawns a process; the
        playback side is just a second name for resolve_backing() to
        watch for. Destroying the primary tears both down together,
        since they're owned by the same pw-cli connection (see
        pw_owned.OwnedPwNode's module docstring)."""
        capture_name = self._fx_capture_name
        playback_name = self._fx_playback_name

        args = (
            f'node.description = "{self.id}" '
            f"{self._filter_graph_args()} "
            "capture.props = { "
            f'node.name = "{capture_name}" '
            f'node.description = "{capture_name}" '
            "media.class = Audio/Sink "
            "audio.position = [ FL FR ] "
            "} "
            "playback.props = { "
            f'node.name = "{playback_name}" '
            f'node.description = "{playback_name}" '
            "media.class = Audio/Source "
            "audio.position = [ FL FR ] "
            "}"
        )
        command = "load-module libpipewire-module-filter-chain { " + args + " }"
        logger.info("Creating %s with: %s", type(self).__name__, command)

        owned = OwnedPwNode(capture_name, self._pw_cli_command, self._pw_cli_settle)
        if not owned.create(command):
            logger.error("%s creation failed for %r", type(self).__name__, self.id)
            return None
        self.backings.append(owned)
        self.backings.append(OwnedPwNode(playback_name))
        return owned

    def _ensure_dummy(self, name: str) -> None:
        """Create one null-audio-sink sandwich dummy if it isn't already
        among self.backings (checked by name, so a dead dummy that was
        pruned gets re-created here on a later repair pass).

        When a dummy has to be created it means the old one is gone -
        and any keepalive attached to that side is therefore pointing at
        a vanished sink (a pw-cat client does not re-link on its own), so
        the matching keepalive is dropped here too and re-spawned by
        _ensure_keepalives below, instead of being left as a live-but-
        unlinked stream."""
        if any(b.name == name for b in self.backings):
            return
        config = (
            "factory.name=support.null-audio-sink "
            f'node.name="{name}" '
            f'node.description="{name}" '
            "media.class=Audio/Sink "
            "audio.position=[FL,FR]"
        )
        command = f"create-node adapter {config}"
        logger.info("Creating %s sandwich dummy with: %s", type(self).__name__, command)
        owned = OwnedPwNode(name, self._pw_cli_command, self._pw_cli_settle)
        if not owned.create(command):
            logger.error(
                "%s sandwich dummy %r creation failed for %r",
                type(self).__name__,
                name,
                self.id,
            )
            return
        self.backings.append(owned)

        # Drop the keepalive for the side this dummy replaced (if any) -
        # its old target is gone and it will not re-link on its own.
        keepalive = self._feed_name if name == self._input_dummy_name else self._drain_name
        for existing in list(self.backings):
            if existing.name == keepalive:
                existing.destroy()
                if existing in self.backings:
                    self.backings.remove(existing)

    def _ensure_keepalives(self) -> None:
        """Add the input-dummy feed and the output-dummy drain if either
        is still missing (see class docstring for why each exists).
        Checked by name rather than "any keepalive present" so a partial
        failure - one started, the other didn't - retries only the
        missing one on a later call instead of silently leaving it
        unprotected forever. Feed and drain are independent of the
        module, so a reload_backing() that only swaps the interior does
        not touch the feed (the drain is special-cased there because its
        live tap dies with the old module - see reload_backing)."""
        have = {b.name for b in self.backings}

        if self._feed_name not in have:
            feed = _spawn_silent_feed(
                self._feed_name,
                self._input_dummy_name,
                self._pw_cli_command,
                self._pw_cli_settle,
            )
            if feed:
                self.backings.append(feed)
            else:
                logger.error(
                    "%s input keepalive failed to start for %r",
                    type(self).__name__,
                    self.id,
                )

        if self._drain_name not in have:
            drain = _spawn_silent_drain(
                self._drain_name,
                self._output_dummy_name,
                self._pw_cli_command,
                self._pw_cli_settle,
            )
            if drain:
                self.backings.append(drain)
            else:
                logger.error(
                    "%s output keepalive failed to start for %r",
                    type(self).__name__,
                    self.id,
                )

    def _reorder_backings(self) -> None:
        """Canonicalize self.backings so the module stays the primary
        (index 0) backing regardless of which pieces a partial repair
        re-created in what order - main.py's backing-health logic and
        this file's live-control paths both rely on it."""
        order = (
            self._fx_capture_name,
            self._fx_playback_name,
            self._input_dummy_name,
            self._output_dummy_name,
            self._feed_name,
            self._drain_name,
        )
        rank = {name: i for i, name in enumerate(order)}
        self.backings.sort(key=lambda b: rank.get(b.name, len(order)))

    def ensure_backing(self) -> None:
        """Create whatever of the sandwich is missing, by name:
        processor module, then both dummies, then the keepalives.
        Idempotent - a call that finds the module already up but, say, a
        dummy or keepalive still missing (because it failed to start or
        its process died and was pruned) re-creates only the missing
        piece instead of duplicating what's healthy."""
        if self._processor_backing() is None:
            self._spawn_processor()
        if self._processor_backing() is not None:
            # Only ever register the playback placeholder next to a real
            # module; with no module there is no playback node to watch.
            if not any(b.name == self._fx_playback_name for b in self.backings):
                self.backings.append(OwnedPwNode(self._fx_playback_name))
        self._ensure_dummy(self._input_dummy_name)
        self._ensure_dummy(self._output_dummy_name)
        self._ensure_keepalives()
        self._reorder_backings()

    def reload_backing(self) -> None:
        """Swap ONLY the interior (the filter-chain module), leaving both
        sandwich dummies and the input feed in place - see the class
        docstring for why that is the whole point of the sandwich. Also
        drops and respawns the output drain (its live tap is the old
        module's playback stream, which dies here) and clears any
        process-owning backing that already died on its own (a crashed
        dummy is re-created by ensure_backing just as surely as a
        crashed module is), so this doubles as the crash-recovery path
        for the whole node."""
        for owned in list(self.backings):
            if (
                owned.name in (self._fx_capture_name, self._fx_playback_name)
                or owned.name == self._drain_name
                or (owned.owns_process and not owned.is_alive)
            ):
                owned.destroy()
                if owned in self.backings:
                    self.backings.remove(owned)
        self.ensure_backing()

    def teardown_backing(self) -> None:
        # Reverse creation order: the keepalives feed/drain the dummies,
        # which in turn feed the module - stop the clients before the
        # nodes they target, then the dummies, then the module.
        for owned in reversed(self.backings):
            owned.destroy()
        self.backings.clear()


def _first_existing_plugin(
    candidates: Tuple[Tuple[str, str], ...],
) -> Optional[Tuple[str, str]]:
    """First (path, label) pair from `candidates` whose plugin file
    actually exists on this machine, or None if none do. LADSPA
    install paths are distro-specific - see the candidate lists on
    NoiseCancelNode / SensitivityGateNode -
    so probing a short list of the common ones beats hardcoding a
    single guess that's right on some distros and silently wrong
    (with the failure never surfacing - see ensure_backing()'s
    docstring elsewhere in this file) on everyone else's."""
    for path, label in candidates:
        if path and os.path.isfile(path):
            return path, label
    return None


def _ladspa_profile_paths(rel_name: str) -> List[str]:
    """Common non-/usr places a LADSPA plugin can be installed on this
    machine - a user `nix profile` or the per-user profile dir on
    NixOS - so `_resolve_plugin()` can find an RNNoise/gate install
    without hardcoding a store path."""
    user = os.environ.get("USER", "") or ""
    bases = [
        os.path.expanduser("~/.nix-profile/lib/ladspa"),
        os.path.expanduser("~/.nix-profile/lib64/ladspa"),
        "/etc/profiles/per-user/%s/lib/ladspa" % user if user else None,
    ]
    return [os.path.join(base, rel_name) for base in bases if base]


_STORE_CANDIDATE_CACHE: Dict[Tuple[str, str], Optional[str]] = {}


def _store_ladspa_candidate(prefix: str, rel_path: str) -> Optional[str]:
    """Look up (once, cached) whether /nix/store contains a
    ``<prefix>-*`` output with ``rel_path`` (e.g. an rnnoise-plugin or
    ladspaPlugins install). The store is where `nix develop` /
    `nix profile` puts every package, so this is how the plugin a dev
    shell adds becomes discoverable without hardcoding its hash."""
    key = (prefix, rel_path)
    if key in _STORE_CANDIDATE_CACHE:
        return _STORE_CANDIDATE_CACHE[key]
    found = None
    try:
        for entry in os.scandir("/nix/store"):
            # Store paths are <hash>-<pkgname>-...; match on the
            # package-name part after the first dash, not the whole
            # path (startswith would never match a hashed name).
            if ("-" + prefix) not in entry.name:
                continue
            candidate = os.path.join(entry.path, rel_path)
            if os.path.isfile(candidate):
                found = candidate
                break
    except OSError:
        pass
    _STORE_CANDIDATE_CACHE[key] = found
    return found


class NoiseCancelNode(_FilterChainNode):
    """Real-time ML voice denoising via the RNNoise LADSPA plugin
    (librnnoise_ladspa.so, label "noise_suppressor_mono").

    One real dial (vad_threshold, 0-100: how confident the model has
    to be that a frame is speech before passing it through) keeps the
    suppressor from eating laughter/hums. This node used to be
    switchable between RNNoise, Noise Repellent (LV2) and the SWH
    "Simple Gate" LADSPA engine - RNNoise won, so the others were
    dropped: Noise Repellent can't load without widening the PipeWire
    service's LV2_PATH, and the gate engine became its own standalone
    node type, SensitivityGateNode.

    Rewritten the same way SensitivityGateNode was: vad_threshold used
    to be a load-time-only filter-graph option, so every change (even
    from the settings dialog's spin button, not just a drag) tore down
    and reloaded the whole filter-chain module via
    _schedule_effect_rebuild - and a rebuild here didn't stay local:
    it could audibly hiccup an entire chain. "VAD Threshold (%)" is
    exactly as live-updatable as the sensitivity gate's "Threshold
    (dB)" was - it's just another LADSPA control port on a plugin
    already loaded inside a filter-chain instance - so it gets the
    same fix: set_vad_threshold() pushes it straight down the
    filter-chain's own pw-cli stdin via OwnedPwNode.set_param(), no
    reload, safe to call as often as the caller likes. Only an actual
    plugin-path override (ladspa_plugin/ladspa_label, a settings-only
    field nobody drags) still goes through the reload path, since that
    genuinely does need a different module loaded - and thanks to the
    _FilterChainNode sandwich that reload swaps only the interior
    module, never touching this node's user-facing sockets.

    ensure_backing() probes a short list of common install paths for
    librnnoise_ladspa.so (standard /usr dirs, a nix profile, and
    /nix/store - where `nix develop` puts it) and uses the first that
    actually exists, unless ladspa_plugin/ladspa_label are explicitly
    set (via config), which always win and skip the probe entirely.

    This node is a standard _FilterChainNode sandwich (see that
    class's docstring): user edges plug into the two stable dummy
    sinks, never into the RNNoise module's own streams, and the
    sandwich's feed/drain keepalives mean toggling the edge feeding
    this node, or the edge it feeds, can never leave it idle or
    suspended.
    """

    LABEL = "noise_suppressor_mono"
    _CANDIDATES: Tuple[Tuple[str, str], ...] = (
        ("/usr/lib/ladspa/librnnoise_ladspa.so", "noise_suppressor_mono"),
        (
            "/usr/lib/x86_64-linux-gnu/ladspa/librnnoise_ladspa.so",
            "noise_suppressor_mono",
        ),
        ("/usr/lib64/ladspa/librnnoise_ladspa.so", "noise_suppressor_mono"),
        ("/usr/lib/ladspa/rnnoise_ladspa.so", "noise_suppressor_mono"),
    )

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        vad_threshold: float = 50.0,
        ladspa_plugin: str = "",
        ladspa_label: str = "",
        # `method` is accepted (and forced to rnnoise) only so a patch
        # saved by an older multi-method build still loads cleanly.
        method: str = "rnnoise",
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
        **_ignored,
    ):
        super().__init__(node_id, backing_node_name, pw_cli_command, pw_cli_settle)
        self.method = "rnnoise"
        self.ladspa_plugin = ladspa_plugin
        self.ladspa_label = ladspa_label
        self.vad_threshold = max(0.0, min(100.0, vad_threshold))

    def _resolve_plugin(self) -> Tuple[str, str]:
        """(plugin, label) for the filter graph: the explicit config
        override if both parts are set, else the first existing
        candidate (see _first_existing_plugin for why probing beats a
        single hardcoded guess), else the first candidate anyway so a
        failure at least names what was tried."""
        if self.ladspa_plugin and self.ladspa_label:
            return self.ladspa_plugin, self.ladspa_label
        extra: List[Tuple[str, str]] = [
            (p, self.LABEL) for p in _ladspa_profile_paths("librnnoise_ladspa.so")
        ]
        store = _store_ladspa_candidate(
            "rnnoise-plugin", "lib/ladspa/librnnoise_ladspa.so"
        )
        if store:
            extra.insert(0, (store, self.LABEL))
        found = _first_existing_plugin(tuple(extra) + self._CANDIDATES)
        return found if found else self._CANDIDATES[0]

    def _filter_graph_args(self) -> str:
        # vad_threshold's current value is only the plugin's *starting*
        # value here - set_vad_threshold() updates it live from then on
        # without ever touching this again (see class docstring).
        plugin, label = self._resolve_plugin()
        return (
            "filter.graph = { nodes = [ { "
            "type = ladspa "
            f"name = {self.backing_node_name}_plugin "
            f"plugin = {plugin} "
            f"label = {label} "
            'control = { "VAD Threshold (%)" = '
            f"{self.vad_threshold:.2f} }} "
            "} ] }"
        )

    def _apply_vad_threshold(self) -> None:
        """Push the current dial to the live plugin. Safe to call
        before the backing has resolved a node_id (OwnedPwNode.set_param
        just no-ops until then) - see set_vad_threshold() and
        resolve_backing()."""
        processor = self._processor_backing()
        if processor is None:
            return
        processor.set_param(
            "Props",
            f'{{ params = [ "VAD Threshold (%)" {self.vad_threshold:.2f} ] }}',
        )

    def set_vad_threshold(self, vad_threshold: float) -> None:
        """Live VAD-confidence change - one set-param line down the
        filter-chain's own pw-cli stdin, no module reload, safe to call
        as often as needed (drag or spin-button tick alike)."""
        self.vad_threshold = max(0.0, min(100.0, vad_threshold))
        self._apply_vad_threshold()

    def resolve_backing(self, name: str, node_id: int) -> bool:
        matched = super().resolve_backing(name, node_id)
        if matched and name == self.backing_node_name:
            # Push our actual current value the moment the capture-
            # side node resolves, same as SensitivityGateNode's
            # resolve_backing() - mostly a no-op in practice since
            # _filter_graph_args() already baked it in at load time,
            # but it matters if set_vad_threshold() was called while
            # still unresolved.
            self._apply_vad_threshold()
        return matched


class SensitivityGateNode(_FilterChainNode):
    """A Discord-style voice-activity gate. Audio only passes through
    while the level coming in is above a threshold you set with the
    inline slider on the node (0-100; higher = a louder signal is
    needed to open it - dragging the sensitivity bar up in Discord).

    Rebuilt from scratch around a single guiding rule: sensitivity is
    a value that changes on every tick of a drag, so whatever path it
    takes to reach the live gate has to be cheap and boring, not
    "coalesce a module reload" or "juggle multiple owned processes".
    Two previous designs got that wrong in different ways:

      * Baking the slider straight into the LADSPA gate's own
        "Threshold (dB)" control and reloading the filter-chain module
        on every change (the same load-time-only path NoiseCancelNode/
        ReverbNode use for *their* options, which only ever change
        rarely via a settings dialog, not by dragging).

      * Working around that by never touching the gate itself again
        after creation and instead wrapping it in TWO extra null-sink
        gain stages, adjusted opposite-and-reciprocal via `wpctl` to
        fake a threshold change. That traded one problem for three:
        three independently-owned real objects to create, tear down
        and keep resolved instead of one; a hand-rolled reciprocal
        gain-doubling formula (with its own "-l" overdrive limit hack)
        standing in for what should just be "set a number"; and two
        brand-new `wpctl` *subprocesses* spawned per slider tick on
        top of the pw-cli session the gate already needed - on a fast
        drag that's a couple of processes forked per frame, which is
        exactly the kind of load that made the whole chain (and
        anything routed through it) flaky.

    This version is a single filter-chain instance loaded exactly once
    as the interior of the standard _FilterChainNode sandwich (see
    that class's docstring) - so it plugs into the daemon's existing
    generic BackedNode plumbing (ensure_backing/reload_backing/
    teardown_backing/resolve_backing) with no special cases, and its
    two sandwich dummies are created once and never rebuilt, so
    nothing gate-specific is needed for plug/unplug stability any
    more. The gate's own "Threshold (dB)" control IS the sensitivity
    value - no gain staging, no reciprocal math, nothing bracketing it
    - and it is updated live via `OwnedPwNode.set_param()`, which
    writes one `set-param` line down the pw-cli stdin pipe the
    filter-chain's own process already has open (see pw_owned.py). No
    subprocess spawn, no module reload, nothing to resolve or resync -
    just a number going out over a pipe that's already sitting there.
    set_level() is safe to call on every tick of a slider drag.
    """

    _CANDIDATES: Tuple[Tuple[str, str], ...] = (
        ("/usr/lib/ladspa/gate_1410.so", "gate"),
        ("/usr/lib/x86_64-linux-gnu/ladspa/gate_1410.so", "gate"),
        ("/usr/lib64/ladspa/gate_1410.so", "gate"),
    )

    # sensitivity 0..100 -> LADSPA gate "Threshold (dB)" value,
    # most-sensitive to least-sensitive. This range brackets a normal
    # speaking voice comfortably: THRESHOLD_MIN_DB opens on nearly any
    # sound (including quiet room tone - "opens on a whisper"),
    # THRESHOLD_MAX_DB needs something close to full volume ("needs a
    # shout"). Unlike the old design's fixed threshold + gain-staging
    # workaround, this is the actual value the gate compares the
    # incoming signal against - turning the slider really does move
    # the threshold, live.
    THRESHOLD_MIN_DB = -60.0  # level=0
    THRESHOLD_MAX_DB = 0.0  # level=100

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        level: float = 25.0,
        ladspa_plugin: str = "",
        ladspa_label: str = "",
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name, pw_cli_command, pw_cli_settle)
        self.level = max(0.0, min(100.0, level))
        self.ladspa_plugin = ladspa_plugin
        self.ladspa_label = ladspa_label

    @classmethod
    def level_to_threshold_db(cls, level: float) -> float:
        """0-100 sensitivity -> gate threshold in dB. Linear, so the
        slider's midpoint sits at the midpoint of the dB range too -
        no perceptual curve to reason about when tuning
        THRESHOLD_MIN_DB/THRESHOLD_MAX_DB."""
        level = max(0.0, min(100.0, level))
        span = cls.THRESHOLD_MAX_DB - cls.THRESHOLD_MIN_DB
        return cls.THRESHOLD_MIN_DB + span * (level / 100.0)

    def _resolve_plugin(self) -> Tuple[str, str]:
        if self.ladspa_plugin and self.ladspa_label:
            return self.ladspa_plugin, self.ladspa_label
        extra: List[Tuple[str, str]] = [
            (p, "gate") for p in _ladspa_profile_paths("gate_1410.so")
        ]
        for prefix in (
            "ladspaPlugins",
            "pipewire-ladspa-plugins",
            "ladspa_plugins",
            "swh-plugins",
        ):
            store = _store_ladspa_candidate(prefix, "lib/ladspa/gate_1410.so")
            if store:
                extra.insert(0, (store, "gate"))
                break
        found = _first_existing_plugin(tuple(extra) + self._CANDIDATES)
        return found if found else self._CANDIDATES[0]

    def _filter_graph_args(self) -> str:
        # Only load-time here is the plugin choice and the fixed
        # attack/hold/decay/range shape of the gate - the threshold
        # itself is just its starting value; set_level() below updates
        # it live from then on without ever touching this again.
        plugin, label = self._resolve_plugin()
        threshold = self.level_to_threshold_db(self.level)
        return (
            "filter.graph = { nodes = [ { "
            "type = ladspa "
            f"name = {self.backing_node_name}_plugin "
            f"plugin = {plugin} "
            f"label = {label} "
            "control = { "
            f'"Threshold (dB)" = {threshold:.2f} '
            '"Attack (ms)" = 5 '
            '"Hold (ms)" = 150 '
            '"Decay (ms)" = 200 '
            '"Range (dB)" = -90 '
            '"Output select (-1 = key listen, 0 = gate, 1 = bypass)" = 0 '
            "} } ] }"
        )

    def _apply_threshold(self) -> None:
        """Push the current sensitivity to the live gate. Safe to call
        before the backing has resolved a node_id (OwnedPwNode.set_param
        just no-ops until then) - see set_level() and resolve_backing()."""
        processor = self._processor_backing()
        if processor is None:
            return
        threshold = self.level_to_threshold_db(self.level)
        processor.set_param(
            "Props", f'{{ params = [ "Threshold (dB)" {threshold:.2f} ] }}'
        )

    def set_level(self, level: float) -> None:
        """Live sensitivity change - one set-param line down the
        filter-chain's own pw-cli stdin, no module reload, no extra
        subprocess, safe to call on every slider-drag tick."""
        self.level = max(0.0, min(100.0, level))
        self._apply_threshold()

    def resolve_backing(self, name: str, node_id: int) -> bool:
        matched = super().resolve_backing(name, node_id)
        if matched and name == self.backing_node_name:
            # Push our actual current sensitivity the moment the
            # capture-side node resolves, same as the first-creation
            # path (the module loads with THRESHOLD_MIN_DB..MAX_DB's
            # level_to_threshold_db(self.level) baked in already, so
            # this is mostly a no-op in practice - it only matters if
            # set_level() was called while still unresolved).
            self._apply_threshold()
        return matched


class ReverbNode(_FilterChainNode):
    """
    Reverb via a LADSPA plugin (default: the CAPS plugin suite's
    "Plate" reverb - packaged as "caps"/"ladspa-caps-plugins"
    depending on distro). Same "these are best-effort defaults, fix
    them in this node's config if your system installs it elsewhere"
    caveat as NoiseCancelNode above.

    A standard _FilterChainNode sandwich (see that class's docstring):
    user edges plug into its two stable dummy sinks, never into the
    module's own streams. Its one dial, wet_dry, is a load-time-only
    filter-graph control - changing it reloads just the sandwich's
    interior via reload_backing() (main.py's set_node_property), which
    the dummies make invisible to everything wired to this node.
    """

    DEFAULT_LADSPA_PLUGIN = "/usr/lib/ladspa/caps.so"
    DEFAULT_LADSPA_LABEL = "Plate"

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        ladspa_plugin: str = "",
        ladspa_label: str = "",
        wet_dry: float = 0.3,
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name, pw_cli_command, pw_cli_settle)
        self.ladspa_plugin = ladspa_plugin or self.DEFAULT_LADSPA_PLUGIN
        self.ladspa_label = ladspa_label or self.DEFAULT_LADSPA_LABEL
        # 0 (fully dry) - 1 (fully wet/reverberated).
        self.wet_dry = wet_dry

    def _filter_graph_args(self) -> str:
        return (
            "filter.graph = { nodes = [ { "
            "type = ladspa "
            f"name = {self.backing_node_name}_plugin "
            f"plugin = {self.ladspa_plugin} "
            f"label = {self.ladspa_label} "
            'control = { "dry/wet" = '
            f"{self.wet_dry} }} "
            "} ] }"
        )


class EchoCancelNode(BackedNode):
    """
    Acoustic echo cancellation via PipeWire's own
    libpipewire-module-echo-cancel (WebRTC's AEC implementation,
    shipped with PipeWire itself on most distros - unlike
    NoiseCancelNode/ReverbNode this needs no separate plugin package).
    This is the same echo-cancellation engine EasyEffects' in-process
    "Echo Canceller" wraps, exposed here as a normal PatchSpace node so
    you don't need EasyEffects running to get echo-free audio.

    NOTE: unlike the three _FilterChainNode effects (NoiseCancelNode/
    ReverbNode/SensitivityGateNode), this node is still *directly*
    exposed - its "mic"/"probe"/"out" sockets map straight onto the
    echo-cancel module's own capture/sink/source streams rather than
    onto stable sandwich dummies, because its module genuinely has
    three separately-wired sides that share one AEC state. It therefore
    gets its own keepalive coverage on every socket (see below) instead
    of the sandwich's, and main.py still does a full reload on a new
    edge into it (see _cmd_add_edge).

    PipeWire's module is really four coordinated streams (see
    libpipewire-module-echo-cancel(7)):

        mic --> capture --> | AEC | --> source --> app
        app --> sink   --> |     | --> playback --> speaker

    where the AEC subtracts whatever is fed to `sink` (the reference)
    out of what `capture` records from `mic`. This node surfaces the
    three points PatchSpace's edge model can wire and hides the
    module's purely-internal plumbing:

      * "mic" - an input socket for the raw, uncancelled microphone
        signal. Backed by the module's `capture.props` stream (the
        module records from whatever source you route into it).

      * "probe" - an input socket for the *reference*: the exact audio
        you want removed from the mic. Backed by the module's
        `sink.props`. This is deliberately fully manual - nothing is
        auto-cancelled. Route whatever you want cancelled into it (the
        monitor of a sink, another app's playback, a specific game,
        anything that has output ports); only that signal is removed
        from "mic". Wiring nothing into "probe" simply leaves the mic
        untouched. This is the socket that replaces what EasyEffects'
        old probe-based echo cancel gave you.

      * "out" - an input socket consumers (a conference app, the
        daemon's own virtual mic, ...) pick the cleaned mic up from.
        Backed by the module's `source.props`.

    The module's fourth object - its `playback.props` stream, which in
    the conference-app topology replays the sink so you can hear the
    far end - is created too, under the unique name
    ``{backing_node_name}_playback``, but is NOT exposed as a socket.
    Leave it alone when "probe" is fed from the monitor of a sink you
    already hear through (wiring the module's own playback back into
    that same sink is what causes feedback/howling). It is only for
    the module-native wiring where the *app* feeds "probe" instead of
    your speakers, and you route "probe" onward to the speakers through
    this node's playback leg in the raw patchbay view.

    ``node.autoconnect`` is explicitly turned off on this node's
    capture/playback streams so WirePlumber never silently links them
    to the session's current default source/sink (the daemon keeps the
    default pointed at PatchBay / PatchBay Mic) - routing through this
    node is always exactly what the graph says, nothing more.

    A silent keepalive feeds the module's "mic" (capture) and "probe"
    (sink) sides, and a silent keepalive drains its "out" (source)
    side, for as long as this node exists - so none of the three
    exposed sockets ever goes idle just because the user's edge into
    or out of it happens to be disconnected right now. Without this,
    the module's AEC buffers reset the moment any of these sides drops
    to zero active links, and reconnecting afterward makes the mic
    output stutter or drop out until the module re-aligns (see
    ensure_backing for the full reasoning). The module's fourth,
    unexposed stream (`playback`) is left unprotected - it's only ever
    wired manually in the raw patchbay view (see above), not through
    an edge a user routinely toggles.

    The AEC options below are the module's documented knobs:

      * ``library_name`` - which AEC backend SPA plugin to load
        (`library.name`). Defaults to PipeWire's WebRTC backend
        ``aec/libspa-aec-webrtc``, the only one shipped by default.

      * ``aec_args`` - an SPA-properties string passed to the AEC
        backend (`aec.args`), e.g. tuning the WebRTC AEC's behaviour.
        Leave empty for the backend's own defaults.

      * ``monitor_mode`` - ``monitor.mode``. When enabled the module
        makes NO playback stream and its sink side auto-captures the
        default sink's monitor instead of waiting for a manual "probe"
        feed. That is the "just cancel everything my system plays"
        behaviour; it is off by default because this node exists for
        the full-control "cancel exactly what I route into probe" case.

    Changing any of these on an existing node takes effect when the
    backing is recreated (the daemon tears the module down and reloads
    it - see main.py's set_node_property handling).
    """

    DEFAULT_AEC_LIBRARY = "aec/libspa-aec-webrtc"

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        library_name: str = "",
        aec_args: str = "",
        monitor_mode: bool = False,
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name)
        self.library_name = library_name or self.DEFAULT_AEC_LIBRARY
        self.aec_args = aec_args
        self.monitor_mode = bool(monitor_mode)
        self._pw_cli_command = pw_cli_command
        self._pw_cli_settle = pw_cli_settle

    @property
    def _mic_name(self) -> str:
        # The module's capture stream - what "mic" edges route into.
        return self.backing_node_name

    @property
    def _probe_name(self) -> str:
        # The module's sink stream - what "probe" (reference) edges
        # route into.
        return f"{self.backing_node_name}_probe"

    @property
    def _source_name(self) -> str:
        # The module's source stream - where the cleaned mic comes out.
        return f"{self.backing_node_name}_out"

    @property
    def _playback_name(self) -> str:
        # The module's playback stream (sink -> speaker replays the
        # reference). Not exposed as a socket; exists only for the raw
        # patchbay / module-native topology, and needs a unique name so
        # two echo-cancel nodes can coexist (the module's built-in
        # default name would otherwise collide).
        return f"{self.backing_node_name}_playback"

    def input_identity(self, port: str = "in") -> dict:
        # Anything other than exactly "probe" (including the "in"
        # default every other node type actually uses) is treated as
        # the mic input - the primary/first socket, same convention
        # find_socket_at()/NODE_TYPE_SPECS use on the GUI side (index
        # 0 = "mic").
        if port == "probe":
            return {"name": self._probe_name}
        return {"name": self._mic_name}

    def output_identity(self) -> dict:
        return {"nodeName": self._source_name}

    @staticmethod
    def _stream_props(name: str, autoconnect: bool = False) -> str:
        """SPA-properties object passed as one of the module's
        capture/sink/source/playback.props. `node.autoconnect` is only
        pinned to false on the capture/playback *streams* - the two the
        module creates with PW_STREAM_FLAG_AUTOCONNECT - so the session
        manager never silently links them to the current default
        source/sink; the sink (probe) stream is already manual in the
        default (non monitor.mode) topology."""
        extra = "" if autoconnect else " node.autoconnect = false"
        return (
            "{ "
            f'node.name = "{name}" '
            f'node.description = "{name}" '
            f"{extra} "
            "}"
        )

    def ensure_backing(self) -> None:
        mic_name = self._mic_name
        probe_name = self._probe_name
        source_name = self._source_name
        playback_name = self._playback_name

        if self.backings:
            self._ensure_keepalives(mic_name, probe_name, source_name)
            return

        parts = [f"library.name = {self.library_name}"]
        if self.monitor_mode:
            parts.append("monitor.mode = true")
        if (self.aec_args or "").strip():
            # Same nested-object form capture.props uses - the module
            # reads it back and hands it to its AEC plugin.
            parts.append(f"aec.args = {{ {self.aec_args} }}")
        parts.extend(
            [
                f"capture.props = {self._stream_props(mic_name)}",
                f"sink.props = {self._stream_props(probe_name, autoconnect=self.monitor_mode)}",
                f"source.props = {self._stream_props(source_name)}",
                # playback.props is ignored by the module in monitor.mode.
                f"playback.props = {self._stream_props(playback_name)}",
            ]
        )
        command = (
            "load-module libpipewire-module-echo-cancel { " + " ".join(parts) + " }"
        )
        logger.info("Creating echo-cancel node with: %s", command)

        # One real pw-cli connection/module load produces the whole
        # group of real nodes at once - only the primary (mic-side)
        # OwnedPwNode actually spawns a process; the others are just
        # names for resolve_backing() to watch for, same trick
        # _FilterChainNode.ensure_backing() uses for its own two-node
        # split (see that method's comment for why destroying just the
        # primary is enough to tear all of them down together).
        # monitor.mode makes the module omit the playback stream, so no
        # backing is registered for a node that will never appear.
        owned = OwnedPwNode(mic_name, self._pw_cli_command, self._pw_cli_settle)
        if not owned.create(command):
            logger.error("Echo-cancel node creation failed for %r", self.id)
            return
        self.backings.append(owned)
        self.backings.append(OwnedPwNode(probe_name))
        self.backings.append(OwnedPwNode(source_name))
        if not self.monitor_mode:
            self.backings.append(OwnedPwNode(playback_name))

        self._ensure_keepalives(mic_name, probe_name, source_name)

    def _ensure_keepalives(
        self, mic_name: str, probe_name: str, source_name: str
    ) -> None:
        """Keep every exposed socket permanently active - see class
        docstring for why. Silence in, silence drained out, so none of
        this ever audibly interferes with whatever the user routes for
        real. monitor_mode auto-captures the probe side itself, so a
        manual probe keepalive would just be a second, redundant feed
        fighting the session's own auto-connect - skip it in that mode
        (mic and out still get theirs either way). Checked/added by
        name, same as _FilterChainNode._ensure_keepalives, so a
        keepalive that failed to start on a previous ensure_backing()
        call gets retried on the next one instead of staying missing
        forever."""
        have = {b.name for b in self.backings}

        mic_keepalive_name = f"{mic_name}_keepalive"
        if mic_keepalive_name not in have:
            mic_keepalive = _spawn_silent_feed(
                mic_keepalive_name, mic_name, self._pw_cli_command, self._pw_cli_settle
            )
            if mic_keepalive:
                self.backings.append(mic_keepalive)
            else:
                logger.error(
                    "Echo-cancel mic keepalive failed to start for %r", self.id
                )

        if not self.monitor_mode:
            probe_keepalive_name = f"{probe_name}_keepalive"
            if probe_keepalive_name not in have:
                probe_keepalive = _spawn_silent_feed(
                    probe_keepalive_name,
                    probe_name,
                    self._pw_cli_command,
                    self._pw_cli_settle,
                )
                if probe_keepalive:
                    self.backings.append(probe_keepalive)
                else:
                    logger.error(
                        "Echo-cancel probe keepalive failed to start for %r", self.id
                    )

        source_keepalive_name = f"{source_name}_keepalive"
        if source_keepalive_name not in have:
            source_keepalive = _spawn_silent_drain(
                source_keepalive_name,
                source_name,
                self._pw_cli_command,
                self._pw_cli_settle,
            )
            if source_keepalive:
                self.backings.append(source_keepalive)
            else:
                logger.error(
                    "Echo-cancel out keepalive failed to start for %r", self.id
                )

    def teardown_backing(self) -> None:
        # Stop the keepalives before tearing the module down (their
        # targets go away when the primary pw-cli dies), mirroring
        # VirtualMicNode's reverse-order teardown for its own keepalive.
        for owned in reversed(self.backings):
            owned.destroy()
        self.backings.clear()


# ---------------------------------------------------------------------
# Virtual devices: user-created speakers and microphones
# ---------------------------------------------------------------------


class VirtualSpeakerNode(BackedNode):
    """
    A user-named virtual output device: a monitor-enabled null-audio-
    sink, exactly like VolumeProcessNode's backing except it exists to
    be a permanent, nameable device rather than a gain control. Apps
    (or other PatchSpace nodes) can be routed into it as a sink
    (input_identity()) and whatever plays through it can be picked up
    downstream via its monitor ports as a source (output_identity()) -
    both directions come for free from BackedNode, same as every other
    backed node type.
    """

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        device_label: str = "",
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name)
        self.device_label = device_label
        self._pw_cli_command = pw_cli_command
        self._pw_cli_settle = pw_cli_settle

    def ensure_backing(self) -> None:
        if self.backings:
            return

        description = self.device_label or self.backing_node_name
        config = (
            "factory.name=support.null-audio-sink "
            f'node.name="{self.backing_node_name}" '
            f'node.description="{description}" '
            "media.class=Audio/Sink "
            "audio.position=[FL,FR] "
            "monitor.channel-volumes=1"
        )
        command = f"create-node adapter {config}"
        logger.info("Creating virtual speaker with: %s", command)

        owned = OwnedPwNode(
            self.backing_node_name, self._pw_cli_command, self._pw_cli_settle
        )
        if owned.create(command):
            self.backings.append(owned)
        else:
            logger.error("Virtual speaker creation failed for %r", self.id)


class VirtualMicNode(BackedNode):
    """
    A user-named virtual input device (microphone), backed by three
    real objects instead of one (creation order = backings order):

      backings[0] - the underlying null-audio-sink everything gets fed
      into. input_identity()/output_identity() are overridden below to
      NOT point here directly - real edges route into this sink, but
      nothing routes out of it directly; see backings[1].

      backings[1] - a Stream/Output/Audio node loopback-republishing
      that sink's monitor as an actual Audio/Source, via a standalone
      `pw-loopback` process (see OwnedPwProcess) whose -P playback
      properties give the visible node its name/media.class and whose
      -C capture properties point node.target/capture.sink at
      backings[0]. This is what makes the mic show up in another app's
      recording dropdown at all - a bare monitor-enabled null-sink (as
      used by VirtualSpeakerNode/VolumeProcessNode) only ever exposes a
      "Monitor of ..." source, which most apps won't offer as a mic.

      backings[2] - a permanent keepalive: a silent pw-cat stream
      played into backings[0]'s sink input, via OwnedPwProcess rather
      than OwnedPwNode since it's a plain long-running command, not a
      pw-cli create-node request. This exists purely so the mic never
      goes idle: without something always feeding the sink, the
      moment the user's real input edge is disconnected or gated
      (e.g. floating the node temporarily) some apps drop or pause a
      recording source whose stream has gone silent/inactive rather
      than treating it as "still there, just quiet". The keepalive
      guarantees at least one active input at all times, independent
      of anything the user does upstream.
    """

    def __init__(
        self,
        node_id: NodeId,
        backing_node_name: str,
        device_label: str = "",
        pw_cli_command: Tuple[str, ...] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
    ):
        super().__init__(node_id, backing_node_name)
        self.device_label = device_label
        self._pw_cli_command = pw_cli_command
        self._pw_cli_settle = pw_cli_settle

    def ensure_backing(self) -> None:
        if self.backings:
            return

        description = self.device_label or self.backing_node_name
        sink_name = f"{self.backing_node_name}_sink"
        loopback_name = self.backing_node_name  # what edges actually route via

        # 1. The underlying sink everything (real input + keepalive) feeds into.
        sink_config = (
            "factory.name=support.null-audio-sink "
            f'node.name="{sink_name}" '
            f'node.description="{description} (input)" '
            "media.class=Audio/Sink "
            "audio.position=[FL,FR] "
            "monitor.channel-volumes=1"
        )
        sink_owned = OwnedPwNode(sink_name, self._pw_cli_command, self._pw_cli_settle)
        if not sink_owned.create(f"create-node adapter {sink_config}"):
            logger.error("Virtual mic sink creation failed for %r", self.id)
            return
        self.backings.append(sink_owned)

        # 2. Republish that sink's monitor as a real Audio/Source loopback -
        #    this is the object other apps actually see as "the mic".
        #
        #    Deliberately NOT a pw-cli `load-module` call: that requires
        #    hand-escaping a nested SPA-JSON properties string inside a
        #    single stdin line, which has twice now silently failed to
        #    parse (pw-cli accepted the line and stayed alive, so
        #    OwnedPwNode.create() reported success, but the properties -
        #    including capture.props' node.target - never actually took
        #    effect, and PipeWire/WirePlumber auto-connected the loopback's
        #    capture side to the default source instead of our sink).
        #    pw-loopback is a standalone foreground process built for
        #    exactly this - properties passed as separate argv elements
        #    (a Python list, not a shell string), so there's no
        #    quoting/escaping step left to get wrong.
        #
        #    NOTE: -P/-C are NOT property flags - they set --playback/
        #    --capture *TARGET*, i.e. a device name to search for. Passing
        #    a JSON properties blob there makes pw-loopback fail to find a
        #    matching device and fall back to the session's default
        #    sink/source for both ends, which is exactly the "loopback to
        #    default" feedback-through-your-speakers bug this caused. The
        #    actual properties flags are --capture-props/--playback-props.
        #    Also "capture.sink"/"node.target" aren't real property keys -
        #    the loopback module uses "stream.capture.sink" and
        #    "target.object" respectively.
        capture_name = f"{self.backing_node_name}_capture"
        playback_props = (
            f'{{ node.name = "{loopback_name}" '
            f'node.description = "{description}" '
            "media.class = Audio/Source }"
        )
        capture_props = (
            f'{{ node.name = "{capture_name}" '
            f'target.object = "{sink_name}" '
            "stream.capture.sink = true "
            "audio.position = [ FL FR ] }"
        )
        loopback_command = (
            "pw-loopback",
            "--playback-props",
            playback_props,
            "--capture-props",
            capture_props,
        )
        loopback_owned = OwnedPwProcess(
            loopback_name, self._pw_cli_command, self._pw_cli_settle
        )
        if loopback_owned.create(loopback_command):
            self.backings.append(loopback_owned)
        else:
            logger.error("Virtual mic loopback creation failed for %r", self.id)

        # 3. Silent keepalive, always feeding the sink so the mic never
        #    reads as idle (see class docstring).
        keepalive_name = f"{self.backing_node_name}_keepalive"
        keepalive_command = (
            "pw-cat",
            "--playback",
            "--volume",
            "0",
            "--target",
            sink_name,
            "--raw",
            "--format",
            "s16",
            "--rate",
            "48000",
            "--channels",
            "2",
            "/dev/zero",
        )
        keepalive = OwnedPwProcess(
            keepalive_name, self._pw_cli_command, self._pw_cli_settle
        )
        if keepalive.create(keepalive_command):
            self.backings.append(keepalive)
        else:
            logger.error("Virtual mic keepalive stream failed to start for %r", self.id)

    def input_identity(self, port: str = "in") -> dict:
        # Real audio (the user's actual mic, or anything else feeding
        # this virtual mic) routes into the underlying sink, not the
        # loopback node.
        return {"name": f"{self.backing_node_name}_sink"}

    def output_identity(self) -> dict:
        # Consumers pick this mic up via the loopback's Audio/Source
        # output, not the sink's monitor directly. Exact node.name
        # identity ("nodeName") rather than the substring "name" key -
        # without it this filter would also match this mic's own
        # underlying "{name}_sink" Audio/Sink (whose monitor carries the
        # same signal), silently wiring a duplicate feed to every
        # consumer of this virtual mic.
        return {"nodeName": self.backing_node_name}

    def teardown_backing(self) -> None:
        # Tear down in the reverse of creation order: the keepalive
        # and loopback both depend on the sink still existing, so kill
        # them first rather than relying on BackedNode's default
        # creation-order teardown.
        for owned in reversed(self.backings):
            owned.destroy()
        self.backings.clear()


# ---------------------------------------------------------------------
# The graph itself
# ---------------------------------------------------------------------


@dataclass(frozen=True)
class Edge:
    id: EdgeId
    from_node: NodeId
    to_node: NodeId
    # Which of to_node's declared input sockets this edge feeds -
    # "in" for every node type with exactly one input (everything
    # except EchoCancelNode today), so this defaults identically to
    # the pre-multi-input behavior for every existing edge/node type.
    # See BackedNode.input_identity(port=...) and PatchSpace.sync(),
    # which is what actually gives this meaning.
    to_port: str = "in"


class PatchSpace:
    """
    Owns the node graph and drives the real PipeWire graph to match
    it.

    Call sync() after any structural edit (add_node/remove_node/
    add_edge/remove_edge) or after flipping a GateNode's .enabled or a
    VolumeProcessNode's volume. sync() is a full reconciliation pass:
    for every edge, it recomputes exactly which (output_port,
    input_port) pairs that edge should currently be connected as, and
    diffs that against the pairs it connected for that same edge last
    time (self._edge_links) - disconnecting whatever's no longer
    wanted and connecting whatever's newly wanted. That per-edge diff
    is the only bookkeeping involved, which is what makes a gate or a
    removed node's effect on the live graph exact and immediate,
    rather than depending on a separate rule cache staying in sync
    with reality.
    """

    def __init__(self, graph: PipewireGraph):
        self.graph = graph
        self._lock = threading.Lock()

        self.nodes: Dict[NodeId, Node] = {}
        self.edges: Dict[EdgeId, Edge] = {}
        self._edges_into: Dict[NodeId, List[Edge]] = {}
        self._edges_out_of: Dict[NodeId, List[Edge]] = {}

        # edge_id -> exact set of (output_port, input_port) pairs
        # PatchSpace connected for that edge as of the last sync().
        self._edge_links: Dict[EdgeId, Set[Tuple[int, int]]] = {}

    # ---------- graph editing ----------

    def add_node(self, node: Node) -> NodeId:
        with self._lock:
            self.nodes[node.id] = node
            if isinstance(node, BackedNode):
                node.ensure_backing()
        return node.id

    def remove_node(self, node_id: NodeId) -> None:
        with self._lock:
            node = self.nodes.pop(node_id, None)
            if node is None:
                return
            for edge in list(self._edges_into.get(node_id, [])) + list(
                self._edges_out_of.get(node_id, [])
            ):
                self._remove_edge_locked(edge.id)
            if isinstance(node, BackedNode):
                node.teardown_backing()

    @staticmethod
    def _edge_id(from_node: NodeId, to_node: NodeId, to_port: str) -> EdgeId:
        """Canonical edge id for a (from_node, to_node, to_port)
        triple - shared by add_edge() and rename_node() so the two
        can't drift into computing this differently. Only multi-input
        node types (EchoCancelNode today) ever pass a to_port other
        than "in" - see Edge.to_port's comment - so this only departs
        from the plain "from->to" id existing edges/tooling already
        expect when it actually needs to: a mic edge and a probe edge
        between the same two nodes would otherwise collide on one id.
        """
        if to_port == "in":
            return f"{from_node}->{to_node}"
        return f"{from_node}->{to_node}:{to_port}"

    def add_edge(
        self, from_node: NodeId, to_node: NodeId, to_port: str = "in"
    ) -> EdgeId:
        with self._lock:
            if from_node not in self.nodes or to_node not in self.nodes:
                raise KeyError("both endpoints must already be added")
            target = self.nodes[to_node]
            if target.is_transparent() and self._edges_into.get(to_node):
                raise ValueError(
                    f"{to_node} is a transparent node and already has an "
                    "upstream edge - splitters/gates take exactly one input"
                )
            edge_id = self._edge_id(from_node, to_node, to_port)
            edge = Edge(edge_id, from_node, to_node, to_port)
            self.edges[edge_id] = edge
            self._edges_into.setdefault(to_node, []).append(edge)
            self._edges_out_of.setdefault(from_node, []).append(edge)
            return edge_id

    def remove_edge(self, edge_id: EdgeId) -> None:
        with self._lock:
            self._remove_edge_locked(edge_id)

    def rename_node(self, old_id: NodeId, new_id: NodeId) -> None:
        """Change a node's id in place, re-keying everything that
        indexes on it: self.nodes, both edge-adjacency dicts, and
        every Edge that touches it (Edge is a frozen dataclass so a
        renamed endpoint means a new Edge object, not a mutation).

        Does NOT touch anything about the node's live backing (a
        BackedNode's pw-cli process/description was named from the
        *old* id at creation time and keeps that name) - only the
        PatchSpace-side identity changes, which is all a rename needs:
        the live PipeWire objects underneath are unaffected, so this
        can never cause an audio glitch.
        """
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

            # Every edge with old_id as either endpoint is referenced
            # from up to two places (its own to/from adjacency lists);
            # collect them once via both dicts, then rebuild each as a
            # fresh Edge and re-add it everywhere the old one lived.
            affected = list(self._edges_into.pop(old_id, [])) + list(
                self._edges_out_of.pop(old_id, [])
            )
            for old_edge in affected:
                self.edges.pop(old_edge.id, None)
                self._edge_links.pop(old_edge.id, None)

                if old_edge.to_node == old_id:
                    other_id, other_list = old_edge.from_node, self._edges_out_of
                else:
                    other_id, other_list = old_edge.to_node, self._edges_into
                other_list.get(other_id, []).remove(old_edge)

                new_from = (
                    new_id if old_edge.from_node == old_id else old_edge.from_node
                )
                new_to = new_id if old_edge.to_node == old_id else old_edge.to_node
                new_edge = Edge(
                    self._edge_id(new_from, new_to, old_edge.to_port),
                    new_from,
                    new_to,
                    old_edge.to_port,
                )

                self.edges[new_edge.id] = new_edge
                self._edges_into.setdefault(new_to, []).append(new_edge)
                self._edges_out_of.setdefault(new_from, []).append(new_edge)

    def _remove_edge_locked(self, edge_id: EdgeId) -> None:
        edge = self.edges.pop(edge_id, None)
        if edge is None:
            return
        self._edges_into.get(edge.to_node, []).remove(edge)
        self._edges_out_of.get(edge.from_node, []).remove(edge)

    # ---------- resolving what feeds an edge ----------

    def _resolve_sources(self, node_id: NodeId) -> List[dict]:
        """
        Walk backward from node_id through any chain of transparent
        nodes (splitters, gates) until real identities are found:
        InputNode.source_filters(), or a BackedNode's
        output_identity(). Returns [] if the chain dead-ends (no
        upstream edge) or passes through a closed gate.
        """
        node = self.nodes.get(node_id)
        if node is None:
            return []

        if isinstance(node, InputNode):
            return node.source_filters()

        if isinstance(node, BackedNode):
            return [node.output_identity()]

        if isinstance(node, TransparentNode):
            if not node.gate_open():
                return []
            upstream_edges = self._edges_into.get(node_id, [])
            if not upstream_edges:
                return []
            # Transparent nodes take at most one upstream edge (see
            # add_edge), so there's only ever one to follow.
            upstream = self._resolve_sources(upstream_edges[0].from_node)

            if isinstance(node, ExcludeFilterNode):
                exclude = node.exclude_filter()
                if exclude is not None:
                    # AND this node's own exclusion into every filter
                    # dict already gathered upstream, rather than
                    # replacing them - a chain of several
                    # ExcludeFilterNodes builds up one "exclude" entry
                    # per node this way (see the class docstring).
                    upstream = [
                        {**f, "exclude": [*f.get("exclude", []), exclude]}
                        for f in upstream
                    ]
            return upstream

        return []

    # ---------- driving the live graph ----------

    def sync(self) -> None:
        """Recompute and apply the exact set of real links every edge
        wants right now."""
        with self._lock:
            desired: Dict[EdgeId, Set[Tuple[int, int]]] = {}
            # Edge/internal-link ids whose desired state could not be
            # computed this pass (see the per-edge try/except below).
            # Their current links are deliberately left untouched -
            # neither disconnected as "stale" nor recorded as newly
            # desired - since touching them on a guess would be worse
            # than waiting for a pass that can compute them.
            unresolved: Set[EdgeId] = set()

            for node_id, node in self.nodes.items():
                is_output = isinstance(node, OutputNode)
                is_backed = isinstance(node, BackedNode)
                if not (is_output or is_backed):
                    continue  # only real "sinks" need anything routed into them

                # sink_filters now lives *inside* the edge loop rather
                # than being computed once per node: a BackedNode's
                # input_identity(edge.to_port) can return a different
                # real target per edge for a multi-input node type
                # (EchoCancelNode's "mic" vs "probe" - see that
                # class and Edge.to_port's comment); every other node
                # type here still has exactly one input ("in"), so
                # this is identical to the old once-per-node value for
                # every one of them.
                for edge in self._edges_into.get(node_id, []):
                    # The desired-pair computation for ONE edge must
                    # never be able to abort the whole reconciliation
                    # pass (which would wedge every *other* edge too):
                    # an unexpected exception here - a node type whose
                    # identity/recursion throws against the current
                    # graph snapshot, say - is logged, that edge is
                    # marked unresolved, and its existing links are
                    # left exactly as they are until a later pass can
                    # recompute them. Guessing (dropping the links, or
                    # treating it as "no longer desired") would be
                    # worse than waiting a pass.
                    try:
                        sink_filters = (
                            node.sink_filters()
                            if is_output
                            else [node.input_identity(edge.to_port)]
                        )
                        sources = self._resolve_sources(edge.from_node)
                        if not sources:
                            desired[edge.id] = set()
                            continue  # gated off, or nothing upstream yet

                        pairs: Set[Tuple[int, int]] = set()
                        for source_node_id in pwmatch.find_source_nodes(
                            self.graph, sources
                        ):
                            for sink_filter in sink_filters:
                                for target_node_id in pwmatch.find_target_nodes(
                                    self.graph, sink_filter
                                ):
                                    pairs |= pwmatch.resolve_channel_pairs(
                                        self.graph,
                                        source_node_id,
                                        target_node_id,
                                        sink_filter.get("type"),
                                    )
                        desired[edge.id] = pairs
                    except Exception as exc:
                        logger.warning(
                            "Failed to compute desired links for edge %s "
                            "(leaving its current links in place): %s",
                            edge.id,
                            exc,
                        )
                        unresolved.add(edge.id)

                if is_backed:
                    # Private plumbing between this node's own backing
                    # objects (see BackedNode.internal_links) - not a
                    # PatchSpace edge, so it gets a synthetic id and is
                    # folded into the same desired/_edge_links diffing
                    # below rather than a separate code path.
                    try:
                        for i, (source_identity, sink_identity) in enumerate(
                            node.internal_links()
                        ):
                            link_id = f"__internal__:{node_id}:{i}"
                            pairs = set()
                            for source_node_id in pwmatch.find_source_nodes(
                                self.graph, [source_identity]
                            ):
                                for target_node_id in pwmatch.find_target_nodes(
                                    self.graph, sink_identity
                                ):
                                    pairs |= pwmatch.resolve_channel_pairs(
                                        self.graph,
                                        source_node_id,
                                        target_node_id,
                                        sink_identity.get("type"),
                                    )
                            desired[link_id] = pairs
                    except Exception as exc:
                        # Same isolation as a user edge above: a node
                        # whose internal plumbing can't be computed this
                        # pass keeps whatever links it already has
                        # instead of being torn down on a guess.
                        logger.warning(
                            "Failed to compute internal links for node %s "
                            "(leaving its current links in place): %s",
                            node_id,
                            exc,
                        )
                        prefix = f"__internal__:{node_id}:"
                        unresolved.update(
                            k for k in self._edge_links if k.startswith(prefix)
                        )

            live_linked = self.graph.linked_pairs()

            # Disconnect anything we previously connected for an edge
            # that no longer wants that exact pair (edge removed,
            # gate closed, endpoint rewired, ...). This is what makes
            # a closed gate or a torn-down node take effect
            # immediately instead of leaving a stale link behind.
            # Unresolved edges are skipped entirely - their links are
            # deliberately preserved (see the computation loop above).
            for edge_id, old_pairs in list(self._edge_links.items()):
                if edge_id in unresolved:
                    continue
                new_pairs = desired.get(edge_id, set())
                stale = old_pairs - new_pairs
                for pair in stale:
                    try:
                        self.graph.disconnect(*pair)
                    except Exception as exc:
                        logger.debug(
                            "disconnect %s for edge %s failed (already gone?): %s",
                            pair,
                            edge_id,
                            exc,
                        )
                if edge_id not in desired:
                    del self._edge_links[edge_id]

            # Connect whatever's newly desired and isn't linked yet.
            for edge_id, pairs in desired.items():
                for pair in pairs:
                    if pair not in live_linked:
                        try:
                            self.graph.connect(*pair)
                        except Exception as exc:
                            logger.warning(
                                "connect %s for edge %s failed: %s", pair, edge_id, exc
                            )
                self._edge_links[edge_id] = pairs
