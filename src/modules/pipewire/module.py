"""
module.py — the PipeWire module's client side of the module API.

Phase 1 runs it *in process*: the daemon constructs one of these, it greets
the protocol handler, declares the type catalog from `declarations.py`,
answers the migration messages the daemon fans out for persisted nodes, and
announces the nodes that exist right now.  Everything it sends is an
ordinary module-API message and every reply comes back through the same
handler, so phase 3 can put a socket between the two without touching this
file's logic.

What it deliberately does **not** do is reach into the daemon: the node
inventory (what exists, where, with which config) arrives as a callable the
daemon supplies, and the operations it is asked to perform (set a control,
realize an edge) are reported back to those same callables.  In phase 3 the
callables are replaced by the module's own graph — the daemon never learns
the difference.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from central import module_api as api
from central import protocol as protocol_mod
from modules.pipewire import declarations

logger = logging.getLogger(__name__)

#: An inventory callback returns the announce-shaped nodes:
#: ``[{"instance_id", "type_id", "space", "config", "requires"}]``.
Inventory = Callable[[], List[Dict[str, Any]]]

#: A migrate callback upgrades one persisted config:
#: ``(config) -> (upgraded_config, fixes)``.
MigrateConfig = Callable[[Dict[str, Any]], tuple]


class PipewireModule:
    """The PipeWire provider, speaking the module API."""

    name = "pipewire"
    version = "1.0"

    def __init__(
        self,
        handler: protocol_mod.ProtocolHandler,
        *,
        type_map: Mapping[str, type],
        inventory: Inventory,
        migrate_config: Optional[MigrateConfig] = None,
        space: str = "main",
    ) -> None:
        self.handler = handler
        self.type_map = type_map
        self.inventory = inventory
        self.migrate_config = migrate_config
        self.space = space
        self.conn = protocol_mod.Connection()
        #: instance_id -> the config this module last agreed to
        self.configs: Dict[str, Dict[str, Any]] = {}
        #: What the module was last told to do (control sets, edge asks) -
        #: the in-process stand-in for "the module did the work".
        self.control_log: List[Dict[str, Any]] = []
        self.edge_log: List[Dict[str, Any]] = []
        self.last_status: Dict[str, str] = {}

    # -- the attach handshake -------------------------------------------

    def attach(self) -> Dict[str, Any]:
        """Greet, declare the catalog, answer migrations, announce nodes.

        A second attach is a module *restart*: it is a fresh client
        connection.  The daemon hands the same module id back (a
        disconnected module keeps its name), so the placements bind again
        instead of being orphaned."""
        self.conn = protocol_mod.Connection()
        # The daemon forwards a display's control/edge requests to the module
        # that owns the node; in-process that is a callable, out-of-process a
        # socket write (`Connection.send`).
        self.conn.deliver = self.apply
        replies = self._call(api.hello(api.ROLE_MODULE, self.name, self.version))
        reply = replies[-1] if isinstance(replies, list) else replies
        if reply.get("type") != "hello_ok":
            raise RuntimeError(f"pipewire module refused: {reply}")
        catalog = declarations.declarations(self.type_map)
        replies = self._call(api.type_declare(
            catalog,
            menu=declarations.menu(),
            vocabulary=declarations.vocabulary(),
            aliases=declarations.legacy_aliases(),
        ))
        migrated = 0
        for message in replies if isinstance(replies, list) else [replies]:
            if message.get("type") == "migrate":
                self._answer_migration(message)
                migrated += 1
        announced = self.announce()
        return {
            "module_id": self.conn.module_id,
            "types": len(catalog),
            "migrated": migrated,
            "bound": announced.get("bound", []),
            "created": announced.get("created", []),
        }

    def detach(self) -> None:
        """Say goodbye: the daemon offlines everything this module owns."""
        self.handler.registry.detach(self.conn.module_id)

    # -- inventory -------------------------------------------------------

    def announce(self) -> Dict[str, Any]:
        nodes = list(self.inventory())
        for node in nodes:
            node.setdefault("space", self.space)
            node.setdefault("requires", [])
            if isinstance(node.get("config"), dict):
                self.configs[node["instance_id"]] = dict(node["config"])
        replies = self._call(api.node_announce(nodes))
        summary: Dict[str, Any] = {}
        for message in replies if isinstance(replies, list) else [replies]:
            if message.get("type") == "announced":
                summary = message
            elif message.get("type") == "error":
                logger.warning("pipewire announce refused a node: %s", message)
        return summary

    def own_instance_ids(self) -> List[str]:
        return sorted(self.configs)

    # -- what the daemon asks of this module -----------------------------

    def apply(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle one daemon -> module message.  Returns a reply when the
        message expects one (nothing does yet except a migration)."""
        kind = message.get("type")
        if kind == "migrate":
            return self._answer_migration(message)
        if kind == "control_set":
            self.control_log.append(dict(message))
            instance = message.get("instance_id", "")
            if instance in self.configs:
                self.configs[instance][str(message.get("key"))] = message.get("value")
            return None
        if kind == "edge_apply":
            self.edge_log.append(dict(message))
            return None
        if kind == "status_change":
            self.last_status = dict(message.get("instances") or {})
            return None
        if kind == "value_set":
            # Control-plane values flow between instances; they are not
            # state, so nothing is remembered here.
            return None
        logger.warning("pipewire module ignored %r", kind)
        return None

    # -- internals -------------------------------------------------------

    def _answer_migration(self, message: Dict[str, Any]) -> Dict[str, Any]:
        instance_id = str(message.get("instance_id"))
        config = dict(message.get("config") or {})
        upgraded = config
        if self.migrate_config is not None:
            upgraded, _fixes = self.migrate_config(config)
        self.configs[instance_id] = dict(upgraded)
        return self._call(api.migrate_result(instance_id, upgraded))

    def _call(self, message: Dict[str, Any]):
        return self.handler.handle(message, self.conn)
