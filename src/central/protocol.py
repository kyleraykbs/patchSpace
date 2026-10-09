"""
protocol.py — the daemon side of the module API: per-connection role state
and the request handling.

A socket connection is *untyped* until it sends ``hello``.  Anything else
(``{"command": ...}``) is the legacy protocol and stays exactly as it was,
so phase 1/2 can land while the GUI keeps working.  Once a connection has
greeted, its role decides what it may do:

* ``module`` — declare types, announce instances, report health, answer
  migrations;
* ``display`` — (phase 2) subscribe to the graph and send user actions;
* ``app`` — ask the registry what is available.

Handling is synchronous and returns the reply messages for the caller to
write back (usually one, but ``type_declare`` fans out one ``migrate`` per
persisted instance its types own, then one terminal ``declared``/``error``
— so a client reads until it sees something that is not a ``migrate``).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

from central import module_api as api
from central.registry import Registry

Reply = Union[Dict[str, Any], List[Dict[str, Any]]]


@dataclass
class Connection:
    """What the daemon remembers about one client socket."""

    role: str = ""
    module_id: str = ""
    name: str = ""
    version: str = ""
    socket: Any = None
    send_lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def greeted(self) -> bool:
        return bool(self.role)


class ProtocolHandler:
    """Turns module-API messages into registry changes and replies."""

    def __init__(self, registry: Registry, topology: Optional[Any] = None) -> None:
        self.registry = registry
        #: The generic structure (spaces/placements/edges).  Optional so a
        #: registry-only test harness needs no graph; a display's snapshot
        #: is empty without it.
        self.topology = topology

    # -- entry point -----------------------------------------------------

    def handle(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        kind = msg.get("type")
        handler = {
            "hello": self._hello,
            "type_declare": self._type_declare,
            "node_announce": self._node_announce,
            "node_remove": self._node_remove,
            "node_health": self._node_health,
            "migrate_result": self._migrate_result,
            "registry_query": self._registry_query,
            "catalog_request": self._catalog_request,
            "snapshot_request": self._snapshot_request,
        }.get(kind)
        if handler is None:
            return api.error(f"unknown message type {kind!r}")
        return handler(msg, conn)

    # -- helpers ---------------------------------------------------------

    def _needs_role(self, conn: Connection, *roles: str) -> Optional[Dict[str, Any]]:
        if not conn.greeted:
            return api.error("send hello first")
        if conn.role not in roles:
            return api.error(f"{conn.role} connections may not do that")
        return None

    def _needs_module(self, conn: Connection) -> Optional[Dict[str, Any]]:
        return self._needs_role(conn, api.ROLE_MODULE)

    # -- handlers --------------------------------------------------------

    def _hello(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        ok, message = api.validate_hello(msg)
        if not ok:
            return api.error(message)
        if conn.greeted:
            return api.error("this connection has already greeted")
        conn.role = msg["role"]
        conn.name = msg["name"]
        conn.version = str(msg.get("version") or "")
        if conn.role == api.ROLE_MODULE:
            conn.module_id = self.registry.allocate_module_id(conn.name)
            self.registry.attach(conn.module_id, conn.name, conn.version, conn.role)
        return api.hello_ok(daemon="patchspaced")

    def _type_declare(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        errors, migrations = self.registry.declare_types(
            conn.module_id, msg.get("types") or [],
            menu=msg.get("menu"), vocabulary=msg.get("vocabulary"),
            aliases=msg.get("aliases"),
        )
        replies: List[Dict[str, Any]] = []
        # The migration fan-out comes first, then one terminal reply: a
        # client reads until it sees something that is not a `migrate`.
        for item in migrations:
            replies.append(api.migrate(
                item["instance_id"], item["type_id"], item["config"],
                item["from_schema"], item["to_schema"],
            ))
        for error in errors:
            replies.append(api.error(error, ref="type_declare"))
        if not errors:
            replies.append({
                "type": "declared",
                "module_id": conn.module_id,
                "types": sorted(self.registry.module(conn.module_id).types),
            })
        return replies

    def _node_announce(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        bound, created, errors = self.registry.announce(
            conn.module_id, msg.get("nodes") or []
        )
        replies: List[Dict[str, Any]] = []
        for error in errors:
            replies.append(api.error(error, ref="node_announce"))
        replies.append({
            "type": "announced",
            "module_id": conn.module_id,
            "bound": bound,
            "created": created,
        })
        return replies

    def _node_remove(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        gone = self.registry.unannounce(
            conn.module_id, msg.get("instance_ids") or []
        )
        return {"type": "removed", "instance_ids": gone}

    def _node_health(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        health = msg.get("health") or {}
        if not isinstance(health, dict):
            return api.error("health must map instance ids to states")
        self.registry.set_health(conn.module_id, health)
        return {"type": "ok", "ref": "node_health"}

    def _migrate_result(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        instance_id = str(msg.get("instance_id") or "")
        if not instance_id:
            return api.error("migrate_result needs an instance_id")
        stored = self.registry.set_config(conn.module_id, instance_id,
                                          msg.get("config"))
        if not stored:
            return api.error(f"{instance_id} is not one of this module's nodes")
        return {"type": "ok", "ref": "migrate_result", "instance_id": instance_id}

    def _registry_query(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """An app (or a display) asking what is available.  No module role
        needed: this is the query path apps use instead of dialing modules."""
        type_id = str(msg.get("type_id") or "")
        capability = str(msg.get("capability") or "")
        if not type_id and not capability:
            return api.error("registry_query needs a type_id or a capability")
        instances: List[Dict[str, Any]] = []
        if capability:
            answer = self.registry.capability_query(capability)
            types = answer["types"]
        else:
            answer = self.registry.type_query(type_id)
            types = [answer["type"]] if answer.get("type") else []
        wanted = {t["type_id"] for t in types}
        for instance in self.registry.instances():
            if instance.type_id in wanted:
                instances.append(instance.to_dict())
        return api.registry_reply(
            [m.to_dict() for m in self.registry.connected_modules()],
            types,
            instances,
        )

    def _catalog_request(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """The whole type catalog (plus the modules' menu and vocabulary):
        what a display needs once to be able to draw any node, including one
        whose module is not attached."""
        if not conn.greeted:
            return api.error("send hello first")
        catalog = self.registry.catalog()
        return api.catalog_reply(catalog["types"], catalog["menu"],
                                 catalog["vocabulary"], catalog["aliases"])

    def _snapshot_request(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """The graph as a display sees it: instances with their status,
        where each sits, and the edges between them."""
        if not conn.greeted:
            return api.error("send hello first")
        instances = [i.to_dict() for i in self.registry.instances()]
        placements: List[Dict[str, Any]] = []
        edges: List[Dict[str, Any]] = []
        spaces: List[Dict[str, Any]] = []
        if self.topology is not None:
            for instance in instances:
                placement = self.topology.placement(instance["instance_id"])
                if placement is not None:
                    entry = placement.to_dict()
                    entry["instance_id"] = instance["instance_id"]
                    placements.append(entry)
            edges = [e.to_dict() for e in self.topology.edges.values()]
            spaces = [s.to_dict() for s in self.topology.spaces.values()]
        return api.snapshot_reply(instances, placements, edges, spaces)
