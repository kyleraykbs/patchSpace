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

    def input_identity(self) -> dict:
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

    def resolve_backing(self, name: str, node_id: int) -> bool:
        """Called by the daemon once a real node with `name` shows up
        in the live graph, so any OwnedPwNode still waiting to learn
        its id can pick it up. Returns True if one of ours matched."""
        for owned in self.backings:
            if owned.node_id is None and owned.name == name:
                owned.resolve(node_id)
                return True
        return False


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

    def input_identity(self) -> dict:
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

    def input_identity(self) -> dict:
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

    def add_edge(self, from_node: NodeId, to_node: NodeId) -> EdgeId:
        with self._lock:
            if from_node not in self.nodes or to_node not in self.nodes:
                raise KeyError("both endpoints must already be added")
            target = self.nodes[to_node]
            if target.is_transparent() and self._edges_into.get(to_node):
                raise ValueError(
                    f"{to_node} is a transparent node and already has an "
                    "upstream edge - splitters/gates take exactly one input"
                )
            edge_id = f"{from_node}->{to_node}"
            edge = Edge(edge_id, from_node, to_node)
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
                new_edge = Edge(f"{new_from}->{new_to}", new_from, new_to)

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

            for node_id, node in self.nodes.items():
                is_output = isinstance(node, OutputNode)
                is_backed = isinstance(node, BackedNode)
                if not (is_output or is_backed):
                    continue  # only real "sinks" need anything routed into them

                sink_filters = (
                    node.sink_filters() if is_output else [node.input_identity()]
                )

                for edge in self._edges_into.get(node_id, []):
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

            live_linked = self.graph.linked_pairs()

            # Disconnect anything we previously connected for an edge
            # that no longer wants that exact pair (edge removed,
            # gate closed, endpoint rewired, ...). This is what makes
            # a closed gate or a torn-down node take effect
            # immediately instead of leaving a stale link behind.
            for edge_id, old_pairs in list(self._edge_links.items()):
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
