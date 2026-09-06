#!/usr/bin/env python3
"""
main.py - Patchbay daemon with Unix socket API.
"""
import json
import logging
import os
import socket
import threading
import time
from typing import Any, Dict, Optional
from pathlib import Path

from patchSpace import (
    PatchSpace,
    Node,
    RegexInputNode,
    RegexOutputNode,
    MediaClassInputNode,
    MediaClassOutputNode,
    DescriptionInputNode,
    DescriptionOutputNode,
    SplitterNode,
    GateNode,
    ExcludeFilterNode,
    VolumeProcessNode,
    BackedNode,
    LiveResolvableNode,
    DeviceInputNode,
    DeviceOutputNode,
    AppInputNode,
    AppOutputNode,
    PatchBayDeviceNode,
    VirtualSpeakerNode,
    VirtualMicNode,
)
from pwgraph import PipewireGraph
from pwroute import RuleRouter

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SOCKET_PATH = "/tmp/patchbay.sock"

# Registry: type string -> Node subclass
NODE_TYPE_REGISTRY: Dict[str, type] = {
    "regex_input": RegexInputNode,
    "media_class_input": MediaClassInputNode,
    "description_input": DescriptionInputNode,
    "regex_output": RegexOutputNode,
    "media_class_output": MediaClassOutputNode,
    "description_output": DescriptionOutputNode,
    "splitter": SplitterNode,
    "gate": GateNode,
    "exclude_filter": ExcludeFilterNode,
    "volume": VolumeProcessNode,
    "device_input": DeviceInputNode,
    "device_output": DeviceOutputNode,
    "app_input": AppInputNode,
    "app_output": AppOutputNode,
    "patchbay_device": PatchBayDeviceNode,
    "virtual_speaker": VirtualSpeakerNode,
    "virtual_mic": VirtualMicNode,
}

# Reverse mapping: class -> type string (for serialization)
CLASS_TO_TYPE = {cls: key for key, cls in NODE_TYPE_REGISTRY.items()}

DEVICE_TYPE = "PipeWire:Interface:Device"

# Where the daemon auto-saves the current PatchSpace after every
# structural/config change (see _auto_export_session /
# _MUTATING_COMMANDS below). Deliberately NOT loaded on startup - see
# _auto_export_session's docstring - only pulled back in on an
# explicit "Import Last Session" from the GUI's hamburger menu, which
# reads this same path directly (see constants.SESSION_CACHE_PATH;
# duplicated there for the same reason SOCKET_PATH is duplicated in
# patchbay_cli.py - keep the two in sync if this ever changes).
SESSION_CACHE_PATH = os.path.expanduser("~/.cache/patchbay/last_session.json")

# Commands that can change the PatchSpace's structure or per-node
# config (as opposed to e.g. get_nodes or ping) - handle_command
# triggers _auto_export_session() after any of these succeeds.
_MUTATING_COMMANDS = {
    "add_node",
    "remove_node",
    "add_edge",
    "remove_edge",
    "rename_node",
    "set_node_property",
    "set_gate",
    "set_volume",
    "set_volume_range",
    "set_device_volume",
    "set_device_profile",
    "reset",
}


class PatchBayDaemon:
    def __init__(self):
        self.graph = PipewireGraph(virtual_sink_name="PatchBay")
        self.router = RuleRouter(self.graph)
        self.patch_space = PatchSpace(self.graph)
        self.graph.on_change(self._on_graph_change)

        self._lock = threading.Lock()
        self._clients: set[socket.socket] = set()
        self._running = False

        self._sync_debounce_s = 0.08
        # Upper bound on how long continuous churn can postpone a
        # sync() - without this, a graph that never goes quiet for
        # 80ms (e.g. an app rapidly opening/closing streams) would
        # starve reconciliation indefinitely.
        self._sync_max_wait_s = 0.5
        self._sync_timer: Optional[threading.Timer] = None
        self._sync_pending_since: Optional[float] = None

        # Periodic backstop, same role as pwroute.RuleRouter's
        # start_polling(): guarantees any transient bad reconciliation
        # (a sync() that ran against a momentarily-incomplete graph
        # snapshot and mis-disconnected something) gets corrected
        # within a bounded time even if no further graph event happens
        # to trigger a retry on its own.
        self._safety_sync_interval_s = 2.0

        self.graph.on_node_created(self._on_pw_node_created)
        self.graph.on_node_removed(self._on_pw_node_removed)

    def _on_graph_change(self, graph: PipewireGraph) -> None:
        run_now = False
        with self._lock:
            now = time.monotonic()
            if self._sync_pending_since is None:
                self._sync_pending_since = now
            elapsed = now - self._sync_pending_since
            if elapsed >= self._sync_max_wait_s:
                if self._sync_timer is not None:
                    self._sync_timer.cancel()
                    self._sync_timer = None
                self._sync_pending_since = None
                run_now = True
            else:
                if self._sync_timer is not None:
                    self._sync_timer.cancel()
                self._sync_timer = threading.Timer(
                    self._sync_debounce_s, self._debounced_sync
                )
                self._sync_timer.daemon = True
                self._sync_timer.start()
        if run_now:
            self.patch_space.sync()

    def _debounced_sync(self) -> None:
        with self._lock:
            self._sync_timer = None
            self._sync_pending_since = None
        self.patch_space.sync()

    def _safety_sync(self) -> None:
        try:
            self.patch_space.sync()
            # Continuously re-lock in configured device volume/profile
            # (Bluetooth codec) settings, the same way sync() above
            # continuously re-locks in the desired routing - so a
            # Bluetooth reconnect resetting the codec, or anything
            # external nudging a device's volume, gets corrected
            # within one tick instead of silently sticking. Cheap
            # no-op for anything not currently resolved (see
            # DeviceControlMixin.apply_device_settings).
            with self._lock:
                controllable = [
                    node
                    for node in self.patch_space.nodes.values()
                    if hasattr(node, "apply_device_settings")
                ]
            for node in controllable:
                node.apply_device_settings()
        finally:
            if self._running:
                t = threading.Timer(self._safety_sync_interval_s, self._safety_sync)
                t.daemon = True
                t.start()

    def _on_pw_node_created(self, node_id: int, node_data: dict) -> None:
        props = node_data.get("info", {}).get("props", {})
        name = props.get("node.description") or props.get("node.name")
        resolved_backing = False
        resolved_live = False
        with self._lock:
            if name:
                for node in self.patch_space.nodes.values():
                    if isinstance(node, BackedNode) and node.resolve_backing(
                        name, node_id
                    ):
                        if isinstance(node, VolumeProcessNode):
                            node.set_volume(node.volume)
                        logger.info(f"Resolved backing node {node_id} for {node.id!r}")
                        resolved_backing = True
                        break

            for node in self.patch_space.nodes.values():
                if isinstance(node, LiveResolvableNode) and node.matches_live_node(
                    props
                ):
                    node.resolve_live(node_id, props)
                    logger.info(f"Resolved live node {node_id} for {node.id!r}")
                    resolved_live = True

        if resolved_backing or resolved_live:
            self.patch_space.sync()

    def _on_pw_node_removed(self, node_id: int) -> None:
        with self._lock:
            affected = False
            for node in self.patch_space.nodes.values():
                if (
                    isinstance(node, LiveResolvableNode)
                    and node.live_node_id == node_id
                ):
                    node.resolve_live(None, None)
                    affected = True
        if affected:
            self.patch_space.sync()

    def _try_immediate_resolve(self, node) -> None:
        """Called right after adding a device/app node, so selecting
        an already-plugged-in device resolves instantly instead of
        waiting for the next node_created event (which won't come,
        since the node already exists)."""
        for live_id, live_data in self.graph.nodes().items():
            props = live_data.get("info", {}).get("props", {})
            if node.matches_live_node(props):
                node.resolve_live(live_id, props)
                return

    # ---------- Node factory ----------

    def _cmd_connect_ports(self, cmd: dict) -> dict:
        out = cmd.get("output_port")
        inp = cmd.get("input_port")
        if not isinstance(out, int) or not isinstance(inp, int):
            return {
                "status": "error",
                "message": "output_port and input_port must be integers",
            }
        try:
            created = self.graph.connect(out, inp)
            return {"status": "ok", "created": created}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _cmd_disconnect_ports(self, cmd: dict) -> dict:
        out = cmd.get("output_port")
        inp = cmd.get("input_port")
        if not isinstance(out, int) or not isinstance(inp, int):
            return {
                "status": "error",
                "message": "output_port and input_port must be integers",
            }
        try:
            removed = self.graph.disconnect(out, inp)
            return {"status": "ok", "removed": removed}
        except Exception as e:
            return {"status": "error", "message": str(e)}

    def _create_node(
        self, node_type: str, node_id: str, config: dict
    ) -> Optional[Node]:
        cls = NODE_TYPE_REGISTRY.get(node_type)
        if cls is None:
            logger.warning(f"Unknown node type: {node_type}")
            return None

        if cls is RegexInputNode:
            return RegexInputNode(node_id, config.get("pattern", ""))
        elif cls is MediaClassInputNode:
            return MediaClassInputNode(node_id, config.get("media_class", ""))
        elif cls is DescriptionInputNode:
            return DescriptionInputNode(node_id, config.get("description", ""))
        elif cls is RegexOutputNode:
            return RegexOutputNode(
                node_id, config.get("pattern", ""), config.get("port_type")
            )
        elif cls is MediaClassOutputNode:
            return MediaClassOutputNode(
                node_id, config.get("media_class", ""), config.get("port_type")
            )
        elif cls is DescriptionOutputNode:
            return DescriptionOutputNode(
                node_id, config.get("description", ""), config.get("port_type")
            )
        elif cls is SplitterNode:
            return SplitterNode(node_id)
        elif cls is GateNode:
            return GateNode(node_id, config.get("enabled", True))
        elif cls is ExcludeFilterNode:
            return ExcludeFilterNode(node_id, config.get("pattern", ""))
        elif cls is VolumeProcessNode:
            return VolumeProcessNode(
                node_id,
                config.get("backing_node_name", f"patchbay_{node_id}"),
                config.get("initial_volume", 1.0),
            )
        elif cls is DeviceInputNode:
            return DeviceInputNode(
                node_id,
                config.get("device_name", ""),
                config.get("description", ""),
                device_volume=config.get("device_volume", 1.0),
                profile_index=config.get("profile_index"),
                profile_description=config.get("profile_description", ""),
            )
        elif cls is DeviceOutputNode:
            return DeviceOutputNode(
                node_id,
                config.get("device_name", ""),
                config.get("description", ""),
                device_volume=config.get("device_volume", 1.0),
                profile_index=config.get("profile_index"),
                profile_description=config.get("profile_description", ""),
            )
        elif cls is AppInputNode:
            return AppInputNode(node_id, config.get("app_name", ""))
        elif cls is AppOutputNode:
            return AppOutputNode(node_id, config.get("app_name", ""))
        elif cls is PatchBayDeviceNode:
            return PatchBayDeviceNode(node_id)
        elif cls is VirtualSpeakerNode:
            return VirtualSpeakerNode(
                node_id,
                config.get("backing_node_name", f"patchbay_{node_id}"),
                config.get("device_label", ""),
            )
        elif cls is VirtualMicNode:
            return VirtualMicNode(
                node_id,
                config.get("backing_node_name", f"patchbay_{node_id}"),
                config.get("device_label", ""),
            )
        else:
            raise AssertionError(f"Unhandled node type in registry: {node_type}")

    # ---------- Command handlers ----------

    def handle_command(self, cmd: dict) -> dict:
        command = cmd.get("command")
        try:
            if command == "add_node":
                response = self._cmd_add_node(cmd)
            elif command == "connect_ports":
                response = self._cmd_connect_ports(cmd)
            elif command == "disconnect_ports":
                response = self._cmd_disconnect_ports(cmd)
            elif command == "remove_node":
                response = self._cmd_remove_node(cmd)
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
            elif command == "rename_node":
                response = self._cmd_rename_node(cmd)
            elif command == "add_rule":
                response = self._cmd_add_rule(cmd)
            elif command == "remove_rule":
                response = self._cmd_remove_rule(cmd)
            elif command == "get_state":
                response = self._cmd_get_state(cmd)
            elif command == "get_graph":
                response = self._cmd_get_graph(cmd)
            elif command == "get_nodes":
                response = self._cmd_get_nodes(cmd)
            elif command == "get_rules":
                response = self._cmd_get_rules(cmd)
            elif command == "reset":
                response = self._cmd_reset(cmd)
            elif command == "export_config":
                response = self._cmd_export_config(cmd)
            elif command == "get_hardware_devices":
                response = self._cmd_get_hardware_devices(cmd)
            elif command == "get_applications":
                response = self._cmd_get_applications(cmd)
            elif command == "set_device_volume":
                response = self._cmd_set_device_volume(cmd)
            elif command == "get_device_profiles":
                response = self._cmd_get_device_profiles(cmd)
            elif command == "set_device_profile":
                response = self._cmd_set_device_profile(cmd)
            elif command == "ping":
                response = {"status": "ok", "message": "pong"}
            else:
                response = {"status": "error", "message": f"Unknown command: {command}"}
        except Exception as e:
            logger.error(f"Command {command} failed: {e}")
            return {"status": "error", "message": str(e)}

        # Keep the on-disk recovery cache current after anything that
        # could have changed the PatchSpace's structure or config -
        # see _auto_export_session's docstring for why this is a
        # cache (never auto-loaded) rather than a real save.
        if command in _MUTATING_COMMANDS and response.get("status") == "ok":
            self._auto_export_session()

        return response

    def _cmd_add_node(self, cmd: dict) -> dict:
        node_type = cmd.get("node_type")
        node_id = cmd.get("node_id")
        config = cmd.get("config", {})

        if not node_type or not node_id:
            return {"status": "error", "message": "node_type and node_id required"}

        expected_cls = NODE_TYPE_REGISTRY.get(node_type)
        if expected_cls is None:
            return {"status": "error", "message": f"Unknown node type: {node_type}"}

        with self._lock:
            if node_id in self.patch_space.nodes:
                existing = self.patch_space.nodes[node_id]
                if type(existing) is not expected_cls:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} exists but with a different type",
                    }
                # Update config fields
                for key, value in config.items():
                    if hasattr(existing, key):
                        setattr(existing, key, value)
                if "label" in config:
                    setattr(existing, "label", config["label"])
                # device_volume/profile_index were just set as plain
                # attributes by the loop above like any other config
                # field - push them out to the live device too (if
                # resolved), same as set_device_volume/
                # set_device_profile do, so re-running apply_config
                # actually re-locks in a changed volume/codec instead
                # of only updating the stored value.
                if hasattr(existing, "apply_device_settings") and (
                    "device_volume" in config
                    or "profile_index" in config
                    or "profile_description" in config
                ):
                    existing.apply_device_settings()
                self.patch_space.sync()
                return {"status": "ok", "node_id": node_id, "already_existed": True}

            node = self._create_node(node_type, node_id, config)
            if node is None:
                return {"status": "error", "message": f"Unknown node type: {node_type}"}

            if "label" in config:
                setattr(node, "label", config["label"])

            self.patch_space.add_node(node)
            if isinstance(node, LiveResolvableNode):
                self._try_immediate_resolve(node)
            self.patch_space.sync()
            return {"status": "ok", "node_id": node_id, "already_existed": False}

    def _cmd_remove_node(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        if not node_id:
            return {"status": "error", "message": "node_id required"}

        with self._lock:
            if node_id not in self.patch_space.nodes:
                return {"status": "error", "message": f"Node {node_id} not found"}

            self.patch_space.remove_node(node_id)
            self.patch_space.sync()
            return {"status": "ok"}

    def _cmd_rename_node(self, cmd: dict) -> dict:
        old_id = cmd.get("old_node_id")
        new_id = cmd.get("new_node_id")
        if not old_id or not new_id:
            return {
                "status": "error",
                "message": "old_node_id and new_node_id required",
            }
        with self._lock:
            try:
                self.patch_space.rename_node(old_id, new_id)
            except (KeyError, ValueError) as e:
                return {"status": "error", "message": str(e)}
            self.patch_space.sync()
            return {"status": "ok", "node_id": new_id}

    def _cmd_set_node_property(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        prop = cmd.get("property")
        value = cmd.get("value")
        if not node_id or not prop:
            return {"status": "error", "message": "node_id and property required"}
        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not node:
                return {"status": "error", "message": f"Node {node_id} not found"}
            if prop == "label":
                setattr(node, "label", value)
            elif prop == "pattern":
                if hasattr(node, "pattern"):
                    node.pattern = value
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no 'pattern' property",
                    }
            elif prop == "media_class":
                if hasattr(node, "media_class"):
                    node.media_class = value
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no 'media_class' property",
                    }
            elif prop == "description":
                if hasattr(node, "description"):
                    node.description = value
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no 'description' property",
                    }
            elif prop == "device_name":
                if hasattr(node, "device_name"):
                    node.device_name = value
                    if isinstance(node, LiveResolvableNode):
                        node.resolve_live(None, None)
                        self._try_immediate_resolve(node)
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no 'device_name' property",
                    }
            elif prop == "app_name":
                if hasattr(node, "app_name"):
                    node.app_name = value
                    if isinstance(node, LiveResolvableNode):
                        node.resolve_live(None, None)
                        self._try_immediate_resolve(node)
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no 'app_name' property",
                    }
            elif prop == "device_label":
                if hasattr(node, "device_label"):
                    node.device_label = value
                    # device_label only feeds the real node's
                    # node.description, which - unlike volume/profile -
                    # isn't a live-settable Props parameter, only a
                    # creation-time property. sync() below re-links
                    # ports but never touches that, so without
                    # recreating the backing here the live device keeps
                    # whatever description it was created with (often
                    # blank, since a node is usually added before it's
                    # labeled) while the GUI happily shows the new
                    # text - the mismatch this branch exists to close.
                    # backing_node_name is unchanged, so
                    # input_identity()/output_identity() still match
                    # and the sync() call below reconnects everything.
                    if isinstance(node, BackedNode):
                        node.teardown_backing()
                        node.ensure_backing()
                else:
                    return {
                        "status": "error",
                        "message": f"Node {node_id} has no 'device_label' property",
                    }
            else:
                return {"status": "error", "message": f"Unknown property {prop}"}
            self.patch_space.sync()
            return {"status": "ok"}

    def _cmd_add_edge(self, cmd: dict) -> dict:
        from_node = cmd.get("from_node")
        to_node = cmd.get("to_node")

        if not from_node or not to_node:
            return {"status": "error", "message": "from_node and to_node required"}

        with self._lock:
            edge_id = f"{from_node}->{to_node}"
            if edge_id in self.patch_space.edges:
                return {"status": "ok", "edge_id": edge_id, "already_existed": True}

            try:
                edge_id = self.patch_space.add_edge(from_node, to_node)
                self.patch_space.sync()
                return {"status": "ok", "edge_id": edge_id, "already_existed": False}
            except (KeyError, ValueError) as e:
                return {"status": "error", "message": str(e)}

    def _cmd_remove_edge(self, cmd: dict) -> dict:
        edge_id = cmd.get("edge_id")
        if not edge_id:
            return {"status": "error", "message": "edge_id required"}

        with self._lock:
            if edge_id not in self.patch_space.edges:
                return {"status": "error", "message": f"Edge {edge_id} not found"}

            self.patch_space.remove_edge(edge_id)
            self.patch_space.sync()
            return {"status": "ok"}

    def _cmd_set_gate(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        enabled = cmd.get("enabled", True)

        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not isinstance(node, GateNode):
                return {"status": "error", "message": f"Node {node_id} is not a gate"}

            node.enabled = enabled
            self.patch_space.sync()
            return {"status": "ok"}

    def _cmd_set_volume_range(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        min_val = cmd.get("min", 0.0)
        max_val = cmd.get("max", 1.0)
        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not isinstance(node, VolumeProcessNode):
                return {
                    "status": "error",
                    "message": f"Node {node_id} is not a volume node",
                }
            node.volume_min = min_val
            node.volume_max = max_val
            # Re-apply current volume with new range
            node.set_volume(node.volume)
            self.patch_space.sync()
            return {"status": "ok"}

    def _cmd_set_volume(self, cmd: dict) -> dict:
        node_id = cmd.get("node_id")
        volume = cmd.get("volume", 1.0)

        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not isinstance(node, VolumeProcessNode):
                return {
                    "status": "error",
                    "message": f"Node {node_id} is not a volume node",
                }

            node.set_volume(volume)
            self.patch_space.sync()
            return {"status": "ok", "node_id": node_id, "volume": node.volume}

    def _cmd_add_rule(self, cmd: dict) -> dict:
        rule = cmd.get("rule")
        if not rule:
            return {"status": "error", "message": "rule required"}

        if "id" in rule and rule["id"] in self.router.rules():
            return {"status": "ok", "rule_id": rule["id"], "already_existed": True}

        rule_id = self.router.add_rule(rule)
        return {"status": "ok", "rule_id": rule_id}

    def _cmd_remove_rule(self, cmd: dict) -> dict:
        rule_id = cmd.get("rule_id")
        if not rule_id:
            return {"status": "error", "message": "rule_id required"}

        removed = self.router.remove_rule(rule_id)
        if not removed:
            return {"status": "error", "message": f"Rule {rule_id} not found"}
        return {"status": "ok"}

    def _cmd_get_hardware_devices(self, cmd: dict) -> dict:
        devices = {"inputs": [], "outputs": []}
        for node_id, node_data in self.graph.nodes().items():
            props = node_data.get("info", {}).get("props", {})
            media_class = props.get("media.class")
            name = props.get("node.name")
            if not name or media_class not in ("Audio/Source", "Audio/Sink"):
                continue
            # Our own nodes - the PatchBay virtual sink, splitters,
            # volume backings (see patchSpace.py's pw-cli create-node
            # calls) - are created with factory.name=
            # support.null-audio-sink and media.class=Audio/Sink,
            # exactly the same media.class real hardware outputs use.
            # media.class alone can't tell them apart, which is why
            # they were showing up in "Hardware Output" alongside real
            # devices. Real hardware nodes are attached to a parent
            # ALSA/Bluez Device object (device.id set); our
            # software-only nodes have no such parent, so that's the
            # actual distinguishing signal - checking factory.name too
            # as a belt-and-suspenders in case a future backed-node
            # type ever gets a device.id-like property some other way.
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

    def _cmd_set_device_volume(self, cmd: dict) -> dict:
        """Set a hardware device's volume. This is node CONFIG now,
        not a one-off wpctl call: the value is stored on the node (see
        patchSpace.DeviceControlMixin), so it shows up in
        get_nodes/get_state, round-trips through export_config/
        apply_config, and gets re-applied automatically every time
        this device (re)resolves and on every safety-sync tick -
        which is also why this no longer errors when the device isn't
        currently connected: the value is stored regardless and simply
        takes effect the next time it is."""
        node_id = cmd.get("node_id")
        volume = cmd.get("volume", 1.0)
        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not hasattr(node, "device_volume"):
                return {
                    "status": "error",
                    "message": f"Node {node_id} has no controllable device",
                }
            node.device_volume = volume
        # Applied outside the lock - apply_device_settings() shells
        # out to wpctl (up to a couple seconds on a slow/unresponsive
        # device) and only touches this one node's own state, so
        # there's no reason to hold the daemon lock across it.
        node.apply_device_settings()
        return {
            "status": "ok",
            "node_id": node_id,
            "volume": volume,
            "applied": node.live_node_id is not None,
        }

    def _cmd_get_device_profiles(self, cmd: dict) -> dict:
        """
        Best-effort enumeration of a hardware device's available
        profiles - this is how Bluetooth codec choice (SBC/AAC/LDAC/
        etc.) is exposed in PipeWire/WirePlumber: as separate
        profiles on the Device object, not a property on the Node.

        UNVERIFIED against a live system: reads
        info.params.EnumProfile off the pw-dump Device object. If
        this comes back empty, run
            pw-dump | jq '.[] | select(.type == "PipeWire:Interface:Device")'
        once and check the actual key names/casing, then adjust the
        two .get() calls below to match.
        """
        node_id = cmd.get("node_id")
        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not isinstance(node, LiveResolvableNode) or node.live_node_id is None:
                return {"status": "ok", "node_id": node_id, "profiles": []}
            live_props = dict(node.live_props)

        device_id = live_props.get("device.id")
        if device_id is None:
            return {"status": "ok", "node_id": node_id, "profiles": []}

        profiles = []
        for obj_id, obj_data in self.graph.all_objects().items():
            if obj_data.get("type") != DEVICE_TYPE or obj_id != device_id:
                continue
            params = obj_data.get("info", {}).get("params", {})
            enum_profile = params.get("EnumProfile", [])
            for p in enum_profile:
                index = p.get("index")
                if index is None:
                    # See this method's docstring: the EnumProfile key
                    # names here are unverified against a live system.
                    # If this fires, `pw-dump`'s actual key for the
                    # profile index doesn't match "index" - check with
                    #   pw-dump | jq '.[] | select(.type == "PipeWire:Interface:Device")'
                    # and fix the .get("index") call above accordingly.
                    # Handing the GUI a profile with index=None would
                    # let the user "pick" it, but set_device_profile
                    # rejects a null profile_index, so the choice
                    # silently never gets stored - excluding it here
                    # instead makes the mismatch visible in the log
                    # rather than a codec pick that quietly does
                    # nothing.
                    logger.warning(
                        "Device %s EnumProfile entry has no usable "
                        "'index' key (raw entry: %r) - skipping it. "
                        "The EnumProfile key mapping in "
                        "_cmd_get_device_profiles likely needs fixing.",
                        device_id,
                        p,
                    )
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

    def _cmd_set_device_profile(self, cmd: dict) -> dict:
        """Set a hardware device's profile (this is how Bluetooth
        codec choice - SBC/AAC/LDAC/etc - is exposed; see
        _cmd_get_device_profiles). Same CONFIG semantics as
        _cmd_set_device_volume above: persisted on the node, reapplied
        on every resolve + safety-sync, stored even while the device
        is disconnected so it takes effect the moment it reappears.
        `description` is optional - the GUI already knows the label it
        just showed the user for this profile_index, and passing it
        along here means get_nodes/export_config can display it
        without an extra get_device_profiles round trip."""
        node_id = cmd.get("node_id")
        profile_index = cmd.get("profile_index")
        description = cmd.get("description", "")
        if profile_index is None:
            return {"status": "error", "message": "profile_index required"}
        with self._lock:
            node = self.patch_space.nodes.get(node_id)
            if not hasattr(node, "profile_index"):
                return {
                    "status": "error",
                    "message": f"Node {node_id} has no controllable device",
                }
            node.profile_index = profile_index
            node.profile_description = description
        node.apply_device_settings()
        return {
            "status": "ok",
            "node_id": node_id,
            "applied": node.live_node_id is not None,
        }

    def _build_export_config(self) -> dict:
        """The actual config-building logic behind export_config -
        pulled out on its own so _auto_export_session (writes this to
        a cache file after every mutation) and _cmd_export_config
        (returns it over the socket on request) share one
        implementation instead of two copies that could drift apart.
        See _cmd_export_config's docstring below for the shape."""
        with self._lock:
            nodes = {}
            for node_id, node in self.patch_space.nodes.items():
                node_type = CLASS_TO_TYPE.get(type(node), "unknown")
                params: Dict[str, Any] = {}

                if isinstance(node, VolumeProcessNode):
                    params["backing_node_name"] = node.backing_node_name
                    params["initial_volume"] = node.volume
                elif isinstance(node, GateNode):
                    params["enabled"] = node.enabled
                elif isinstance(node, BackedNode):
                    params["backing_node_name"] = node.backing_node_name

                if hasattr(node, "pattern"):
                    params["pattern"] = node.pattern
                if hasattr(node, "media_class"):
                    params["media_class"] = node.media_class
                if hasattr(node, "description"):
                    params["description"] = node.description
                if hasattr(node, "device_name"):
                    params["device_name"] = node.device_name
                if hasattr(node, "app_name"):
                    params["app_name"] = node.app_name
                if hasattr(node, "device_volume"):
                    params["device_volume"] = node.device_volume
                if hasattr(node, "profile_index") and node.profile_index is not None:
                    params["profile_index"] = node.profile_index
                    params["profile_description"] = node.profile_description

                label = getattr(node, "label", "")
                if label:
                    params["label"] = label

                nodes[node_id] = {"type": node_type, "params": params}

            edges = [
                {"from": edge.from_node, "to": edge.to_node}
                for edge in self.patch_space.edges.values()
            ]

        return {"nodes": nodes, "edges": edges}

    def _auto_export_session(self) -> None:
        """Best-effort persist of the current PatchSpace to
        SESSION_CACHE_PATH, called from handle_command after any
        _MUTATING_COMMANDS succeeds.

        Deliberately never read back on startup - that's the whole
        point of it being a *cache* rather than a real save file: a
        daemon restart (or a crash) always comes back to an empty
        graph, exactly like today, and the previous session is only
        ever pulled back in if the user explicitly asks for it (the
        GUI's "Import Last Session" button - see
        patchspace_widget.show_import_last_session). That avoids the
        surprise of a session someone deliberately reset or replaced
        silently reappearing on the next launch.

        Failures here (disk full, no permission on ~/.cache, ...) are
        logged and swallowed rather than surfaced as a command error -
        losing the recovery cache for one tick is a much smaller
        problem than an add_node/set_volume/etc. call starting to fail
        because of an unrelated disk issue."""
        try:
            config = self._build_export_config()
            cache_dir = os.path.dirname(SESSION_CACHE_PATH)
            if cache_dir:
                os.makedirs(cache_dir, exist_ok=True)
            # Write-then-rename so a reader (or a crash mid-write)
            # never sees a half-written file - os.replace is atomic
            # within the same filesystem, which ~/.cache always is
            # relative to its own temp file here.
            tmp_path = SESSION_CACHE_PATH + ".tmp"
            with open(tmp_path, "w") as f:
                json.dump(config, f, indent=2, sort_keys=True)
            os.replace(tmp_path, SESSION_CACHE_PATH)
        except OSError as exc:
            logger.warning(
                "Failed to auto-save session cache to %s: %s",
                SESSION_CACHE_PATH,
                exc,
            )

    def _cmd_export_config(self, cmd: dict) -> dict:
        """
        Dump the current PatchSpace as a config dict shaped exactly
        like what add_node/add_edge consume - each node's "params" is
        the same dict that would be passed as `config` to add_node,
        so importing this straight back in (see apply_config.py) is
        idempotent: every add_node/add_edge call comes back with
        already_existed=True instead of creating duplicates.

        Live-only bookkeeping (live_node_id/live_props, backing_node_id,
        connected/selection_label/is_bluetooth, etc.) is intentionally
        left out - those are runtime state resolved fresh on import
        (see LiveResolvableNode/BackedNode.resolve_backing), not config
        a user set.
        """
        return {"status": "ok", "config": self._build_export_config()}

    def _cmd_reset(self, cmd: dict) -> dict:
        with self._lock:
            for edge_id in list(self.patch_space.edges.keys()):
                self.patch_space.remove_edge(edge_id)

            for node_id in list(self.patch_space.nodes.keys()):
                self.patch_space.remove_node(node_id)

            self.router.clear_rules()
            self.patch_space.sync()
            return {"status": "ok", "message": "Graph reset"}

    def _cmd_get_state(self, cmd: dict) -> dict:
        return {
            "status": "ok",
            "nodes": self._serialize_nodes(),
            "edges": self._serialize_edges(),
            "rules": list(self.router.rules().values()),
            "graph": self._serialize_graph(),
        }

    def _cmd_get_graph(self, cmd: dict) -> dict:
        return {"status": "ok", "graph": self._serialize_graph()}

    def _cmd_get_nodes(self, cmd: dict) -> dict:
        return {
            "status": "ok",
            "nodes": self._serialize_nodes(),
            "edges": self._serialize_edges(),
        }

    def _cmd_get_rules(self, cmd: dict) -> dict:
        return {"status": "ok", "rules": list(self.router.rules().values())}

    # ---------- Serialization helpers ----------

    def _serialize_nodes(self) -> dict:
        result = {}
        for node_id, node in self.patch_space.nodes.items():
            # Look up the registry key from the node class
            node_type = CLASS_TO_TYPE.get(type(node), "unknown")
            node_data = {
                "id": node.id,
                "type": node_type,
                "label": getattr(node, "label", ""),
            }
            if isinstance(node, GateNode):
                node_data["enabled"] = node.enabled
            elif isinstance(node, VolumeProcessNode):
                node_data["volume"] = node.volume  # fraction
                node_data["volume_min"] = node.volume_min
                node_data["volume_max"] = node.volume_max
                node_data["backing_node_id"] = node.backing_node_id
            elif isinstance(node, BackedNode):
                node_data["backing_node_name"] = node.backing_node_name

            if hasattr(node, "pattern"):
                node_data["pattern"] = node.pattern
            if hasattr(node, "media_class"):
                node_data["media_class"] = node.media_class
            if hasattr(node, "description"):
                node_data["description"] = node.description
            if hasattr(node, "device_name"):
                node_data["device_name"] = node.device_name
            if hasattr(node, "app_name"):
                node_data["app_name"] = node.app_name
            if hasattr(node, "device_label"):
                node_data["device_label"] = node.device_label
            if hasattr(node, "device_volume"):
                node_data["device_volume"] = node.device_volume
            if hasattr(node, "profile_index"):
                node_data["profile_index"] = node.profile_index
                node_data["profile_description"] = node.profile_description
            if isinstance(node, LiveResolvableNode):
                node_data["connected"] = node.live_node_id is not None
                node_data["is_bluetooth"] = (
                    node.live_props.get("device.api") == "bluez5"
                )
                node_data["selection_label"] = (
                    node.live_props.get("node.description")
                    or node.live_props.get("node.nick")
                    or getattr(node, "device_name", "")
                    or getattr(node, "app_name", "")
                )

            result[node_id] = node_data
        return result

    def _serialize_edges(self) -> dict:
        return {
            edge_id: {
                "id": edge.id,
                "from_node": edge.from_node,
                "to_node": edge.to_node,
            }
            for edge_id, edge in self.patch_space.edges.items()
        }

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

        return {
            "nodes": nodes,
            "ports": ports,
            "links": links,
        }

    # ---------- Unix socket server ----------
    # (unchanged - keep the rest of the file as is)

    def _handle_client(self, client_socket: socket.socket):
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
                    except json.JSONDecodeError as e:
                        response = {"status": "error", "message": f"Invalid JSON: {e}"}
                    client_socket.sendall((json.dumps(response) + "\n").encode("utf-8"))
        except Exception as e:
            logger.debug(f"Client handler error: {e}")
        finally:
            self._clients.discard(client_socket)
            client_socket.close()

    def _start_socket_server(self):
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(SOCKET_PATH)
        server.listen(5)
        os.chmod(SOCKET_PATH, 0o666)
        logger.info(f"Listening on {SOCKET_PATH}")
        while self._running:
            try:
                client_socket, _ = server.accept()
                thread = threading.Thread(
                    target=self._handle_client, args=(client_socket,), daemon=True
                )
                thread.start()
            except Exception as e:
                if self._running:
                    logger.error(f"Socket accept error: {e}")
        server.close()
        if os.path.exists(SOCKET_PATH):
            os.unlink(SOCKET_PATH)

    def start(self):
        self._running = True
        initial_sync_done = threading.Event()

        def on_initial_sync(g: PipewireGraph) -> None:
            logger.info(
                f"Initial graph loaded: {len(g.nodes())} nodes, {len(g.ports())} ports"
            )
            initial_sync_done.set()

        self.graph.on_initial_sync(on_initial_sync)
        with self.graph:
            if not initial_sync_done.wait(timeout=10):
                logger.warning("No initial snapshot after 10s - continuing anyway")
            socket_thread = threading.Thread(
                target=self._start_socket_server, daemon=True
            )
            socket_thread.start()

            safety_timer = threading.Timer(
                self._safety_sync_interval_s, self._safety_sync
            )
            safety_timer.daemon = True
            safety_timer.start()

            logger.info("PatchBay daemon running. Press Ctrl+C to exit.")
            logger.info(f"Connect via: socat - UNIX-CONNECT:{SOCKET_PATH}")
            try:
                while self._running:
                    time.sleep(1)
            except KeyboardInterrupt:
                logger.info("\nShutting down...")
            finally:
                self._running = False
                self.router.clear_rules()

    def stop(self):
        self._running = False


def main():
    daemon = PatchBayDaemon()
    daemon.start()


if __name__ == "__main__":
    main()
