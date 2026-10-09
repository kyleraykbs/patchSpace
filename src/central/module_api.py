"""
module_api.py — the wire vocabulary for talking to the central daemon.

Every client of the daemon (a *module* that owns nodes, a *display* that
draws them, an *app* that just asks what is available) speaks the same
JSON-lines protocol on the socket.  This module is the one place that
names the messages, so the daemon and the modules cannot drift.

The legacy GUI protocol (`{"command": ...}`) still exists alongside it:
a message with a ``"type"`` key is module API, one with ``"command"`` is
the old protocol.  Phase 2 of agents/PLAN.md moves the display onto the
module API and the ``command`` shape retires.

Message shapes are plain JSON-able dicts, deliberately not classes: the
same objects have to survive ``json.dumps``/``json.loads`` unchanged
between the daemon and a module process (phase 3).

Roles
-----
``module``  owns node types and node instances.  It declares what it can
            provide, announces the instances it is running, and is told
            what the graph wants from it (control sets, edge realizes,
            migrations, status).
``display`` subscribes to the graph and sends user actions.
``app``     only queries the registry ("is something loaded that can do
            X?").  Never dials a module directly.

Node lifecycle status (``STATUS_*``, daemon-computed, persisted with the
placement):
``online``          the module that owns the type is attached and running it
``offline_module``  the owning module is detached or disabled
``offline_dep``     a node this one declares in ``requires`` is offline
``unbacked``        no attached module has ever declared the type — the
                    gray placement the user kept across reboots

Health (``HEALTH_*``, module-reported, transient): what the module says
about a live instance right now.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

PROTO = 1

ROLE_MODULE = "module"
ROLE_DISPLAY = "display"
ROLE_APP = "app"
ROLES = (ROLE_MODULE, ROLE_DISPLAY, ROLE_APP)

STATUS_ONLINE = "online"
STATUS_OFFLINE_MODULE = "offline_module"
STATUS_OFFLINE_DEP = "offline_dep"
STATUS_UNBACKED = "unbacked"
STATUSES = (
    STATUS_ONLINE,
    STATUS_OFFLINE_MODULE,
    STATUS_OFFLINE_DEP,
    STATUS_UNBACKED,
)

HEALTH_OK = "ok"
HEALTH_STARTING = "starting"
HEALTH_DEAD = "dead"
HEALTH_UNKNOWN = ""

#: Port kinds the daemon itself knows.  A module may only declare ports
#: whose kind is in here.  `number` is the primitives module's kind (a
#: numeric value: a constant, a slider, a maths result); like every other
#: non-audio kind it pairs only with itself.
PORT_KINDS = ("audio", "bundle", "boolean", "filter", "impulse", "sound",
              "camera", "number")

DIR_IN = "in"
DIR_OUT = "out"


# ----------------------------------------------------------------------
# builders (daemon -> client and client -> daemon)
# ----------------------------------------------------------------------

def hello(role: str, name: str, version: str = "") -> Dict[str, Any]:
    return {"type": "hello", "proto": PROTO, "role": role, "name": name, "version": version}


def hello_ok(proto: int = PROTO, daemon: str = "") -> Dict[str, Any]:
    return {"type": "hello_ok", "proto": proto, "daemon": daemon}


def error(message: str, ref: str = "") -> Dict[str, Any]:
    msg = {"type": "error", "message": message}
    if ref:
        msg["ref"] = ref
    return msg


def type_declare(types: List[Dict[str, Any]],
                 menu: Optional[List[Dict[str, Any]]] = None,
                 vocabulary: Optional[Dict[str, Any]] = None,
                 aliases: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """A module's catalog.

    Each type::

        {"type_id": "virtual_camera",         # stable, never renamed
         "schema": 3,                          # bumped when the shape changes
         "ports": [{"name": "in", "kind": "camera", "direction": "in",
                    "multi": false}],
         "controls": [{"key": "output_device", "widget": "field",
                       "label": "Device"}],
         "display": {"name": "Virtual Cam", "category": "video",
                     "color": "#5b9bd5", "icon": "camera-video"},
         "capabilities": ["virtual-camera"],
         "requires": []}                       # type ids this one needs

    ``menu`` is the module's add-node menu (categories in order, each with
    the types a user may create) and ``vocabulary`` its presentation tables
    (field labels, choice lists).  Both are relayed to a display unchanged —
    the daemon does not interpret them.
    """
    message: Dict[str, Any] = {"type": "type_declare", "types": list(types)}
    if menu is not None:
        message["menu"] = list(menu)
    if vocabulary is not None:
        message["vocabulary"] = dict(vocabulary)
    if aliases is not None:
        message["aliases"] = dict(aliases)
    return message


def node_announce(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """The instances a module is running (bulk on attach, then deltas).

    Each node::

        {"instance_id": "v4l2cam_1",   # persistent; same id across reboots
         "type_id": "v4l2_virtual_camera",
         "config": {...},              # opaque to the daemon
         "space": "main",              # routing space the placement sits in
         "requires": ["cam_in_1"]}     # instance ids this one cannot run without
    """
    return {"type": "node_announce", "nodes": list(nodes)}


def node_remove(instance_ids: List[str]) -> Dict[str, Any]:
    return {"type": "node_remove", "instance_ids": list(instance_ids)}


def node_health(health: Dict[str, str]) -> Dict[str, Any]:
    """``{instance_id: HEALTH_*}`` — what the module reports right now."""
    return {"type": "node_health", "health": dict(health)}


def migrate_result(instance_id: str, config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Answer to ``migrate``: the upgraded config, or ``None`` to decline
    (the daemon then recreates the instance from placement + defaults)."""
    return {"type": "migrate_result", "instance_id": instance_id, "config": config}


def edge_apply(edge: Dict[str, Any], op: str) -> Dict[str, Any]:
    """Tell a module to realize (or drop) one edge its nodes take part in."""
    return {"type": "edge_apply", "op": op, "edge": dict(edge)}


def control_set(instance_id: str, key: str, value: Any) -> Dict[str, Any]:
    return {"type": "control_set", "instance_id": instance_id, "key": key, "value": value}


def status_change(instances: Dict[str, str], reason: str = "") -> Dict[str, Any]:
    """The daemon telling a module what it computed for its instances."""
    msg = {"type": "status_change", "instances": dict(instances)}
    if reason:
        msg["reason"] = reason
    return msg


def migrate(instance_id: str, type_id: str, config: Dict[str, Any],
            from_schema: int, to_schema: int) -> Dict[str, Any]:
    """Hand a persisted instance to its module on attach."""
    return {
        "type": "migrate",
        "instance_id": instance_id,
        "type_id": type_id,
        "config": dict(config),
        "from_schema": int(from_schema),
        "to_schema": int(to_schema),
    }


def value_set(instance_id: str, port: str, value: Any) -> Dict[str, Any]:
    """Relayed control-plane value (number/boolean) from producer to consumer."""
    return {"type": "value_set", "instance_id": instance_id, "port": port, "value": value}


def registry_query(type_id: str = "", capability: str = "") -> Dict[str, Any]:
    msg: Dict[str, Any] = {"type": "registry_query"}
    if type_id:
        msg["type_id"] = type_id
    if capability:
        msg["capability"] = capability
    return msg


def registry_reply(modules: List[Dict[str, Any]], types: List[Dict[str, Any]],
                   instances: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "type": "registry_reply",
        "modules": list(modules),
        "types": list(types),
        "instances": list(instances),
    }


def catalog_request() -> Dict[str, Any]:
    """A display asking for the type catalog once, so it can draw nodes it
    has never heard of."""
    return {"type": "catalog_request"}


def catalog_reply(types: List[Dict[str, Any]],
                  menu: Optional[List[Dict[str, Any]]] = None,
                  vocabulary: Optional[Dict[str, Any]] = None,
                  aliases: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    return {
        "type": "catalog_reply",
        "types": list(types),
        "menu": list(menu or []),
        "vocabulary": dict(vocabulary or {}),
        "aliases": dict(aliases or {}),
    }


def snapshot_request() -> Dict[str, Any]:
    """A display asking for the graph: what exists, where, and in what
    state.  The daemon answers with instances (identity + status),
    placements (space/position) and edges."""
    return {"type": "snapshot_request"}


def snapshot_reply(instances: List[Dict[str, Any]], placements: List[Dict[str, Any]],
                   edges: List[Dict[str, Any]], spaces: List[Dict[str, Any]],
                   revision: int = 0) -> Dict[str, Any]:
    return {
        "type": "snapshot_reply",
        "revision": revision,
        "instances": list(instances),
        "placements": list(placements),
        "edges": list(edges),
        "spaces": list(spaces),
    }


# ----------------------------------------------------------------------
# validation
# ----------------------------------------------------------------------

def _is_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def validate_hello(msg: Dict[str, Any]) -> Tuple[bool, str]:
    role = msg.get("role")
    if role not in ROLES:
        return False, f"unknown role {role!r} (expected one of {', '.join(ROLES)})"
    if not _is_str(msg.get("name")):
        return False, "hello needs a name"
    proto = msg.get("proto", PROTO)
    if not isinstance(proto, int) or proto < 1:
        return False, f"bad protocol version {proto!r}"
    return True, ""


def validate_type_decl(decl: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(decl, dict):
        return False, "a type declaration must be an object"
    if not _is_str(decl.get("type_id")):
        return False, "a type declaration needs a type_id"
    schema = decl.get("schema", 1)
    if not isinstance(schema, int) or schema < 1:
        return False, f"{decl.get('type_id')}: schema must be a positive int"
    ports = decl.get("ports", [])
    if not isinstance(ports, list):
        return False, f"{decl.get('type_id')}: ports must be a list"
    for port in ports:
        if not isinstance(port, dict) or not _is_str(port.get("name")):
            return False, f"{decl.get('type_id')}: every port needs a name"
        if port.get("direction") not in (DIR_IN, DIR_OUT):
            return False, (
                f"{decl.get('type_id')}.{port.get('name')}: direction must be "
                f"{DIR_IN!r} or {DIR_OUT!r}"
            )
        if port.get("kind") not in PORT_KINDS:
            return False, (
                f"{decl.get('type_id')}.{port.get('name')}: unknown port kind "
                f"{port.get('kind')!r}"
            )
    controls = decl.get("controls", [])
    if not isinstance(controls, list) or any(
        not isinstance(c, dict) or not _is_str(c.get("key")) for c in controls
    ):
        return False, f"{decl.get('type_id')}: every control needs a key"
    return True, ""


def validate_node_announce(node: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(node, dict):
        return False, "an announced node must be an object"
    if not _is_str(node.get("instance_id")):
        return False, "an announced node needs an instance_id"
    if not _is_str(node.get("type_id")):
        return False, f"{node.get('instance_id')}: an announced node needs a type_id"
    requires = node.get("requires", [])
    if not isinstance(requires, list) or any(not _is_str(r) for r in requires):
        return False, f"{node.get('instance_id')}: requires must be a list of ids"
    return True, ""
