"""
protocol.py — the daemon side of the module API: per-connection role state
and the request handling.

A socket connection is *untyped* until it sends ``hello``.  Anything else
(``{"command": ...}``) is the legacy protocol and stays exactly as it was,
so phase 1/2 can land while the GUI keeps working.  Once a connection has
greeted, its role decides what it may do:

* ``module`` — declare types, declare the space it owns (``space_declare``,
  agents/SPACES.md §18), announce instances, report health, answer
  migrations;
* ``display`` — (phase 2) subscribe to the graph and send user actions;
* ``app`` — ask the registry what is available.

Handling is synchronous and returns the reply messages for the caller to
write back (usually one, but ``type_declare`` fans out one ``migrate`` per
persisted instance its types own, then one terminal ``declared``/``error``
— so a client reads until it sees something that is not a ``migrate``).
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Union

from central import module_api as api
from central import spaces as spaces_mod
from central.graph import edge_id as graph_edge_id
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
    #: How to hand a message to this client.  A socket connection writes it;
    #: an *in-process* module (the built-in PipeWire provider, until it moves
    #: to its own process) has a callable instead.  One forwarding path for
    #: both, which is what lets the daemon route a control to a module
    #: without knowing where it runs.
    deliver: Optional[Callable[[Dict[str, Any]], None]] = None

    def send(self, message: Dict[str, Any]) -> bool:
        if self.deliver is not None:
            try:
                self.deliver(message)
                return True
            except Exception:
                logging.getLogger(__name__).exception(
                    "delivering %s to %s failed", message.get("type"), self.module_id
                )
                return False
        if self.socket is None:
            return False
        try:
            with self.send_lock:
                self.socket.sendall((json.dumps(message) + "\n").encode("utf-8"))
            return True
        except OSError:
            return False

    @property
    def greeted(self) -> bool:
        return bool(self.role)


class ProtocolHandler:
    """Turns module-API messages into registry changes and replies."""

    def __init__(self, registry: Registry, topology: Optional[Any] = None,
                 spaces: Optional[Callable[[], Dict[str, Any]]] = None) -> None:
        self.registry = registry
        #: The generic structure (spaces/placements/edges).  Optional so a
        #: registry-only test harness needs no graph; a display's snapshot
        #: is empty without it.
        self.topology = topology
        #: How to reach the container spaces — `central/spaces.py`'s tree,
        #: which is what a `space_declare` creates and where an announced
        #: node's space is made real (agents/SPACES.md §18).  A *callable*
        #: because the daemon swaps its tree wholesale when it loads it
        #: (`PatchSpaceDaemon._install_spaces`), exactly as
        #: `registry.port_kind` is handed to the graph as a function rather
        #: than a table.  None: this handler has no spaces (a harness that
        #: only drives the registry), and a declaration is refused.
        self.spaces_of = spaces
        #: module_id -> the connection to reach it on.  A socket module
        #: writes to its socket; the in-process provider has a callable
        #: (`Connection.deliver`).  This is what lets the daemon route a
        #: display's request to whichever module owns the node.
        self._module_conns: Dict[str, Connection] = {}

    def register_module(self, conn: Connection) -> None:
        if conn.module_id:
            self._module_conns[conn.module_id] = conn

    def unregister_module(self, conn: Connection) -> None:
        if conn.module_id and self._module_conns.get(conn.module_id) is conn:
            del self._module_conns[conn.module_id]

    # -- entry point -----------------------------------------------------

    def handle(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        kind = msg.get("type")
        handler = {
            "hello": self._hello,
            "type_declare": self._type_declare,
            "space_declare": self._space_declare,
            "node_announce": self._node_announce,
            "node_remove": self._node_remove,
            "node_health": self._node_health,
            "migrate_result": self._migrate_result,
            "registry_query": self._registry_query,
            "catalog_request": self._catalog_request,
            "snapshot_request": self._snapshot_request,
            "node_control": self._node_control,
            "node_edge": self._node_edge,
            "node_values": self._node_values,
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
            self.register_module(conn)
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
            conn.module_id, msg.get("nodes") or [],
            on_space=lambda space_id: self.ensure_space(space_id, conn.module_id),
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

    # -- the app's own space (agents/SPACES.md §18) -----------------------

    def space_tree(self) -> Optional[Dict[str, Any]]:
        """The container spaces right now (None when this handler has none)."""
        return self.spaces_of() if self.spaces_of is not None else None

    def ensure_space(self, space_id: str, client_id: str) -> Optional[Any]:
        """Make the space an announcing node lands in exist, as the
        announcer's own if the daemon has never heard of it (§18)."""
        tree = self.space_tree()
        if tree is None:
            return None
        return spaces_mod.announce_space(tree, space_id, client_id)

    def release_spaces(self, client_id: str) -> List[str]:
        """A client's connection is gone: it keeps no claim on the spaces it
        declared (the records stay — see `spaces.release_declared`)."""
        tree = self.space_tree()
        if tree is None:
            return []
        return spaces_mod.release_declared(tree, client_id)

    def _space_declare(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """A module declaring the space it owns — on every attach (§18).

        The claim is the declaring connection's: `declared_by` is the client
        id this connection greeted with, and the check that refuses a
        takeover is against that id, not against anything the payload says
        (a client cannot name itself into somebody else's space).
        """
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        tree = self.space_tree()
        if tree is None:
            return api.error("this daemon has no spaces", ref="space_declare")
        declaration = msg.get("space")
        ok, message = api.validate_space_declare(declaration)
        if not ok:
            return api.error(message, ref="space_declare")
        space, message = spaces_mod.declare_space(
            tree, declaration, conn.module_id
        )
        if space is None:
            return api.error(message, ref="space_declare")
        return api.space_declared(self.space_payload(space))

    def space_payload(self, space: Any, nodes: Optional[List[str]] = None) -> Dict[str, Any]:
        """One space as a payload carries it: the record plus what a reader
        needs to find it in the graph — the space id under its pre-S1 name
        too, and the instances that landed in it."""
        record = spaces_mod.space_record(
            space, declared_by_name=self.registry.client_name(space.declared_by)
        )
        record["space_id"] = space.id
        record["anchored"] = bool(space.anchored)
        record["nodes"] = self.instances_in(space.id) if nodes is None else nodes
        return record

    def spaces_payload(self, tree: Any) -> List[Dict[str, Any]]:
        """Every space's record, with the instances in each — one pass over
        the registry, so a snapshot does not re-scan it per space."""
        by_space: Dict[str, List[str]] = {}
        for instance in self.registry.instances():
            by_space.setdefault(
                spaces_mod.node_space(instance.instance_id, instance.space), []
            ).append(instance.instance_id)
        return [
            self.space_payload(space, sorted(by_space.get(space.id, [])))
            for space in tree
        ]

    def instances_in(self, space_id: str) -> List[str]:
        """The instances sitting in one container space, by id path."""
        return sorted(
            instance.instance_id
            for instance in self.registry.instances()
            if spaces_mod.node_space(instance.instance_id, instance.space) == space_id
        )

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

    def _node_values(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """A module reporting what its nodes currently compute."""
        problem = self._needs_module(conn)
        if problem is not None:
            return problem
        values = msg.get("values") or {}
        if not isinstance(values, dict):
            return api.error("node_values needs a mapping")
        self.registry.set_values(conn.module_id, values)
        self.relay_values(values)
        return {"type": "ok", "ref": "node_values"}

    # -- the value relay (agents/SPACES.md §14) ---------------------------
    #
    # A node's parameter is an addressable input: every value control gets an
    # input port named for it (`drives_control`, auto-declared from the
    # module's own `controls` list), and a producer's reported value drives
    # the consumer's control exactly as the user's `node_control` does.  The
    # daemon stays the bus: it never looks inside the value, and the consumer
    # applies it through the callback it already has.
    #
    # Three moments join here — and they are the three messages this handler
    # already owns, which is why the relay lives with them rather than in
    # another module:
    #
    #   node_values   a producer reported what it computes -> relay it;
    #   node_edge     a driver was wired or unwired -> relay the producer's
    #                 value, or put the control back to its stored value;
    #   node_control  the user set a control -> remember that as the value it
    #                 holds locally.

    def relay_values(self, values: Dict[str, Dict[str, Any]]) -> int:
        """Forward each reported output to every control its edges drive.

        Returns how many relays were sent (a value a producer reports on a
        port nothing consumes goes nowhere, which is not an error)."""
        if self.topology is None:
            return 0
        sent = 0
        for instance_id, ports in values.items():
            if not isinstance(ports, dict):
                continue
            for port, value in ports.items():
                for edge in self.topology.edges_from(str(instance_id), str(port)):
                    if self.deliver_control(edge.to_node, edge.to_port, value):
                        sent += 1
        return sent

    def bind_control_edge(self, edge: Dict[str, Any], op: str) -> bool:
        """An edge onto a ``drives_control`` port was made or dropped.

        On ``add`` the producer's last reported value is relayed, if it has
        reported one: the wiring itself carries no value, so the consumer only
        hears about it when there is something to say (and it hears every
        later report through :meth:`relay_values`).

        On ``remove`` the control goes back to what it holds locally — unless
        another driver remains (a port the module declared ``many``), in which
        case that driver's reported value is relayed instead and the control
        is still driven.
        """
        if self.topology is None:
            return False
        to_node = str(edge.get("to_node") or "")
        to_port = str(edge.get("to_port") or "")
        if not self.registry.port_drives_control(to_node, to_port):
            return False
        if op == "add":
            return self.relay_current_value(
                str(edge.get("from_node") or ""),
                str(edge.get("from_port") or ""),
                to_node, to_port,
            )
        remaining = self.topology.edges_into(to_node, to_port)
        if remaining:
            driver = remaining[-1]
            return self.relay_current_value(driver.from_node, driver.from_port,
                                            to_node, to_port)
        return self.restore_control(to_node, to_port)

    def restore_control(self, instance_id: str, port: str) -> bool:
        """Put one control back to the value it holds locally (if the daemon
        knows one — a control nothing has ever set has nothing to go back
        to), and stop driving it."""
        key = self.registry.port_drives_control(instance_id, port)
        if not key:
            return False
        known, value = self.registry.local_control_value(instance_id, key)
        if not known:
            return False
        return self.deliver_control(instance_id, key, value)

    def relay_current_value(self, from_node: str, from_port: str,
                            to_node: str, to_port: str) -> bool:
        """Relay the value a producer last reported on one of its outputs."""
        producer = self.registry.instance(from_node)
        if producer is None or from_port not in producer.values:
            return False
        return self.deliver_control(to_node, to_port, producer.values[from_port])

    def deliver_control(self, instance_id: str, port: str, value: Any) -> bool:
        """Hand one value to the module that owns ``instance_id``, addressed
        at the control ``port`` names — as a ``control_set``, the path that
        module already applies (so no module needs a new callback)."""
        key = self.registry.port_drives_control(instance_id, port)
        if not key:
            return False
        instance = self.registry.instance(instance_id)
        if instance is None or not instance.declared_by:
            return False
        target = self._module_conns.get(instance.declared_by)
        if target is None:
            return False
        return target.send(api.control_set(instance_id, key, value))

    # -- the daemon asking a module to run something ----------------------

    def spawn_instance(self, instance_id: str, type_id: str, space: str = "main",
                       config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Create an instance of a *module-provided* type and ask the module
        to run it.

        Used when the user adds a node the daemon does not model itself
        (a primitives node, say): the daemon owns the identity and the
        placement, the module owns running it.  Returns a status dict for the
        caller (`_cmd_add_node`).
        """
        instance = self.registry.instance(instance_id)
        if instance is not None and instance.declared_by:
            return {"status": "error", "message": f"Node {instance_id} already exists"}
        decl = self.registry.type_decl(type_id)
        if decl is None:
            return {"status": "error", "message": f"Unknown node type {type_id}"}
        owner = decl.module_id
        target = self._module_conns.get(owner)
        if target is None:
            return {"status": "error",
                    "message": f"{owner} is not attached, so {type_id} cannot run"}
        self.registry.place(instance_id, type_id, space=space, config=config or {})
        node = {
            "instance_id": instance_id,
            "type_id": type_id,
            "space": space,
            "config": dict(config or {}),
        }
        if not target.send(api.node_spawn([node])):
            return {"status": "error", "message": f"could not reach {owner}"}
        return {"status": "ok", "node_id": instance_id, "module_id": owner}

    def stop_instance(self, instance_id: str) -> bool:
        """Ask whichever module runs this instance to stop (the placement
        stays: the node is still there, offline)."""
        instance = self.registry.instance(instance_id)
        if instance is None or not instance.declared_by:
            return False
        target = self._module_conns.get(instance.declared_by)
        if target is None:
            return False
        return target.send(api.node_stop([instance_id]))

    def _node_control(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """A display setting one control of one node: routed to whichever
        module owns it, so the display never has to know (or dial it)."""
        if not conn.greeted:
            return api.error("send hello first")
        instance_id = str(msg.get("instance_id") or "")
        key = str(msg.get("key") or "")
        if not instance_id or not key:
            return api.error("node_control needs an instance_id and a key")
        instance = self.registry.instance(instance_id)
        if instance is None:
            return api.error(f"unknown node {instance_id}")
        if not instance.declared_by:
            return api.error(
                f"{instance_id} is not running ({instance.status}: {instance.reason})"
            )
        target = self._module_conns.get(instance.declared_by)
        if target is None:
            return api.error(f"{instance.declared_by} is not attached")
        if not target.send(api.control_set(instance_id, key, msg.get("value"))):
            return api.error(f"could not reach {instance.declared_by}")
        # The control's stored local value is now this one: it is what
        # unwiring a driver puts back (agents/SPACES.md §14), and it is part
        # of the instance's config, which is what the daemon hands the module
        # when it re-attaches.
        self.registry.set_control_value(instance_id, key, msg.get("value"))
        return {"type": "ok", "ref": "node_control", "instance_id": instance_id}

    def _node_edge(self, msg: Dict[str, Any], conn: Connection) -> Reply:
        """A display connecting or disconnecting two nodes.  The daemon
        validates it (the one authority), records it, and tells the owning
        modules to realize it."""
        if not conn.greeted:
            return api.error("send hello first")
        if self.topology is None:
            return api.error("this daemon has no graph")
        op = str(msg.get("op") or "add")
        if op not in ("add", "remove"):
            return api.error(f"unknown edge op {op!r}")
        from_node = str(msg.get("from_node") or "")
        to_node = str(msg.get("to_node") or "")
        to_port = str(msg.get("to_port") or "in")
        from_port = str(msg.get("from_port") or "out")
        if not from_node or not to_node:
            return api.error("node_edge needs both endpoints")
        eid = graph_edge_id(from_node, to_node, to_port, from_port)
        edge = {
            "id": eid,
            "from_node": from_node,
            "to_node": to_node,
            "to_port": to_port,
            "from_port": from_port,
        }
        if op == "add":
            ok, message = self.topology.can_connect(
                from_node, to_node, to_port, from_port,
                self.registry.port_kind, self.registry.port_cardinality,
            )
            if not ok:
                return api.error(message)
            self.topology.connect(
                from_node, to_node, to_port, from_port,
                self.registry.port_kind, self.registry.port_cardinality,
            )
        else:
            if not self.topology.disconnect(from_node, to_node, to_port, from_port):
                return api.error(f"no such edge {eid}")
        # Every module whose nodes take part has to realize (or drop) it.
        for instance_id in (from_node, to_node):
            instance = self.registry.instance(instance_id)
            if instance is None or not instance.declared_by:
                continue
            target = self._module_conns.get(instance.declared_by)
            if target is not None:
                target.send(api.edge_apply(edge, op))
        # An edge landing on a control's port means the control is now driven
        # (agents/SPACES.md §14): the wiring bookkeeping above has gone out,
        # so remember what the control held and relay what the producer has.
        # Unwiring the last driver puts the stored value back.
        if op == "add":
            key = self.registry.port_drives_control(to_node, to_port)
            if key:
                self.registry.capture_control(to_node, key)
        self.bind_control_edge(edge, op)
        return {"type": "ok", "ref": "node_edge", "op": op, "edge": edge}

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
        where each sits, and the edges between them.

        ``spaces`` are the **container** spaces (`central/spaces.py`'s tree:
        the boxes nodes actually live in, an app's declared space among
        them, with `declared_by` so a display can say whose it is — §18),
        each carrying its `space_id` and `readonly` under those pre-S1 names
        too.  A daemon that has not loaded a tree shows the routing regions
        instead, which is what a display saw before containers.
        """
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
        tree = self.space_tree()
        if tree:
            spaces = self.spaces_payload(tree.values())
        elif self.topology is not None:
            spaces = [s.to_dict() for s in self.topology.spaces.values()]
        return api.snapshot_reply(instances, placements, edges, spaces)
