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

#: The kinds that are *not* values: a stream, a set of streams, a classifier
#: predicate, an event, a file reference, a video feed.  A port of one of
#: these kinds never carries a value (agents/SPACES.md §19): it is wired, and
#: what flows is the module's own business.
NODE_KINDS = ("audio", "bundle", "filter", "impulse", "sound", "camera")

#: **The** value vocabulary (§19): what a value port carries, and what a
#: *control* can carry (§14) - a control whose value kind is one of these is
#: a node parameter made addressable, so the daemon auto-declares an input
#: port for it (:func:`control_input_ports`).  Any other control a module
#: declares - a button, a readout, a file dialog, a generic inline `setting`
#: - is a widget, not a value, and gains nothing.  One tuple, read by the
#: connection rules, the relay, the type validation and the consumer side;
#: `boolean` is deliberately in here and not in :data:`NODE_KINDS` (a
#: control signal *is* a value; the PipeWire model's own boolean wires are
#: one module's way of reading one).
VALUE_KINDS = ("number", "boolean", "text", "choice")

#: The kind whose value is one of a declared option list - the one kind whose
#: *value* a declaration constrains (:func:`value_problem`).
CHOICE_KIND = "choice"

#: Port kinds the daemon itself knows: the node kinds and the value kinds
#: together (a test holds this to `NODE_KINDS | VALUE_KINDS`, so the two
#: vocabularies cannot drift).  A module may only declare ports whose kind is
#: in here.  Each pairs only with itself, except audio and bundle
#: (:func:`central.graph.kinds_compatible`).
PORT_KINDS = ("audio", "bundle", "boolean", "filter", "impulse", "sound",
              "camera", "number", "text", "choice")

DIR_IN = "in"
DIR_OUT = "out"

#: Which value kind a control's *widget* carries.  A module declares the
#: widget (PLAN §7's vocabulary: `slider`/`switch`/`field`/`choice` and the
#: module's own richer spellings) and never a value kind, so the kind is
#: derived here - one table, in the vocabulary both sides read.  A control
#: may also state its own ``kind`` (in :data:`VALUE_KINDS`) when its widget
#: says nothing; the stated kind wins.  A widget absent from this table
#: carries no value and gets no port.
WIDGET_VALUE_KINDS = {
    # a number (a level, a gain, a ratio)
    "slider": "number",
    "number": "number",
    "spin": "number",
    "percent": "number",
    "gain": "number",
    "volume": "number",
    "level": "number",
    "sensitivity": "number",
    "wetdry": "number",
    # an on/off state
    "switch": "boolean",
    "toggle": "boolean",
    "checkbox": "boolean",
    "boolean": "boolean",
    "fallback_onoff": "boolean",
    # a string (a pattern, a name, a path)
    "field": "text",
    "text": "text",
    "string": "text",
    # one of a declared option list
    "choice": "choice",
    "select": "choice",
    "enum": "choice",
}


def control_value_kind(control: Dict[str, Any]) -> str:
    """What a declared control carries: one of :data:`VALUE_KINDS`, or ``""``
    for a control that is not a value (a button, an indicator, a `setting`).

    The control's own ``kind`` says so when it states one; otherwise its
    ``widget`` is resolved through :data:`WIDGET_VALUE_KINDS`.  Either way a
    module declares no extra metadata (agents/SPACES.md §14)."""
    declared = str(control.get("kind") or "")
    if declared in VALUE_KINDS:
        return declared
    return WIDGET_VALUE_KINDS.get(str(control.get("widget") or ""), "")


def is_value_kind(kind: str) -> bool:
    """Whether a port of this kind carries a **value** (agents/SPACES.md
    §19): ``number``, ``boolean``, ``text`` or ``choice``.

    The kind decides what a value port is - not a flag - which is why a port
    a connection rule lets a value wire into is also a port the relay carries
    one to.  A kind outside :data:`VALUE_KINDS` is a node kind (a stream, an
    event, a feed) and takes no value."""
    return kind in VALUE_KINDS


def choices_of(declared: Any) -> Tuple[str, ...]:
    """An option list a declaration carries (a control's or a value port's),
    as strings - ``()`` when it declares none, and also for anything that is
    not a list (validation refuses that; reading one is not a crash).  One
    reader, so a control and a port state their options the same way."""
    if not isinstance(declared, (list, tuple)):
        return ()
    return tuple(str(option) for option in declared)


def value_problem(kind: str, choices: Any, value: Any) -> str:
    """Why a slot of ``kind`` (declaring ``choices``) cannot take ``value``,
    or ``""`` when it can - **the one acceptance rule** (§19).

    Both sides of the relay ask it: the daemon before it hands a producer's
    value to a consumer, and the consuming module before it keeps one.  So an
    option a declaration does not name is refused with a reason rather than
    silently delivered by the bus or silently coerced by the node.

    Only ``choice`` constrains the *value*: it travels as its option, and an
    option the slot does not declare (or a choice slot that declares nothing)
    is the whole of the refusal.  Every other value kind takes what it is
    handed - how a node turns it into what it computes with (a number from a
    string, a boolean from a bit) is that module's own coercion, as §14 has
    it - so this returns ``""`` for them."""
    if kind not in VALUE_KINDS:
        return (f"{kind or 'an unknown kind'} is not a value kind "
                f"({', '.join(VALUE_KINDS)})")
    if kind != CHOICE_KIND:
        return ""
    options = choices_of(choices)
    if not options:
        return "a choice slot declares no options"
    if str(value) not in options:
        return f"{value!r} is not one of {', '.join(options)}"
    return ""


#: How many drivers a *receiving* port takes, declared per port as
#: ``cardinality`` in the type declaration: ``"one"`` refuses a second
#: inbound edge, ``"many"`` accepts several (the daemon never merges - the
#: module that owns the receiving node does).  A port that declares nothing
#: keeps the pre-S4 rule (:func:`default_cardinality`), so an old
#: declaration behaves exactly as it did.
CARDINALITY_ONE = "one"
CARDINALITY_MANY = "many"
CARDINALITIES = (CARDINALITY_ONE, CARDINALITY_MANY)

#: How a receiving node that takes several drivers combines them, declared
#: as ``merge`` on the port.  The daemon does not merge: it relays the
#: declaration (it travels to a display in the catalog, unchanged) and, a
#: merge making sense only for several drivers, treats a declared merge as
#: a declaration of ``"many"`` too.
MERGE_MIX = "mix"
MERGE_SUM = "sum"
MERGE_LAST = "last"
MERGES = (MERGE_MIX, MERGE_SUM, MERGE_LAST)

#: The kinds whose *input* takes exactly one driver (a level signal, an
#: event, a file reference, a video feed - not a mixable bus).  This is the
#: fallback for a port that declares nothing: verbatim the rule the deleted
#: ``central/graph.py`` ``SINGLE_DRIVER_KINDS`` list named, so no
#: undeclared type changes behaviour.  A port that *is* a bus says so in
#: its own declaration (the space ports do).
DEFAULT_ONE_KINDS = ("boolean", "impulse", "sound", "camera")


def default_cardinality(kind: str) -> str:
    """The cardinality of a port that declares none — the pre-S4 rule."""
    return CARDINALITY_ONE if kind in DEFAULT_ONE_KINDS else CARDINALITY_MANY


def control_input_ports(ports: List[Dict[str, Any]],
                        controls: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The input ports a type's ``controls`` auto-declare (agents/SPACES.md
    §14).

    Every value control becomes an input port *named for the control* —
    ``{name: <control key>, kind, direction: "in", drives_control: true}`` —
    so a node's parameter is an addressable input, and the daemon can relay
    a producer's value onto it as a ``control_set`` (the consumer's own
    ``on_control`` path, so no module needs a new callback).  The port
    declares ``cardinality: "one"``: a parameter is driven by one producer,
    and a module that wants several says ``many`` in its own declaration.

    The daemon calls this when a type is declared, and merges the result into
    the type's ``ports`` — so the auto-declared port appears wherever a
    type's ports are asked for (the catalog a display installs, the
    snapshot, and the connection rules).  Two rules keep it out of the way of
    what the module declared:

    * a name the type already declares as an **input port** is left exactly
      as it is (the module's declaration wins; this adds nothing);
    * a control key declared twice (the shipped catalog does this: a
      `sensitivity` control spelled once as its own widget and once as a
      generic `setting`) contributes one port, never two.
    """
    taken = {str(port.get("name")) for port in ports
             if port.get("direction") == DIR_IN}
    auto: List[Dict[str, Any]] = []
    for control in controls:
        key = str(control.get("key") or "")
        kind = control_value_kind(control)
        if not key or not kind or key in taken:
            continue
        taken.add(key)
        auto.append({
            "name": key,
            "kind": kind,
            "direction": DIR_IN,
            "drives_control": True,
            "cardinality": CARDINALITY_ONE,
        })
    return auto


def value_slots(ports: List[Dict[str, Any]],
                controls: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Every **value slot** one type declares, by port name - the one listing
    (§19), built from the module's own declaration.

    A slot is an **input port whose kind is a value kind**
    (:func:`is_value_kind`): the vocabulary decides, not a flag, so a port the
    connection rules let a producer's value wire into is exactly a port the
    relay carries that value to.  The auto-declared ports of the type's value
    controls (:func:`control_input_ports`) are slots like any other, which is
    what makes S3's path and S5's the same one.

    Each entry is ``{"name", "kind", "control", "choices"}``:

    * ``control`` is the control key when the delivery is spelled
      ``control_set`` (a ``drives_control`` port is named for the control it
      drives, so the name *is* the key), ``""`` for a raw port the consumer
      takes through its ``on_value`` path;
    * ``choices`` is what the slot accepts when it is a choice - the control's
      own option list, or the port's.
    """
    slots: Dict[str, Dict[str, Any]] = {}
    by_key: Dict[str, Dict[str, Any]] = {}
    for control in controls or []:
        if isinstance(control, dict):
            by_key.setdefault(str(control.get("key") or ""), control)
    declared = list(ports or [])
    for port in declared + control_input_ports(declared, list(controls or [])):
        if not isinstance(port, dict) or port.get("direction") != DIR_IN:
            continue
        kind = str(port.get("kind") or "")
        name = str(port.get("name") or "")
        if not name or name in slots or not is_value_kind(kind):
            continue
        control = by_key.get(name) if port.get("drives_control") else None
        options = (control or {}).get("choices")
        if options is None:
            options = port.get("choices")
        slots[name] = {
            "name": name,
            "kind": kind,
            "control": name if port.get("drives_control") else "",
            "choices": choices_of(options),
        }
    return slots


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
                    "cardinality": "one",     # "one" (default) | "many"
                    "merge": "last",          # "mix" | "sum" | "last"
                    "multi": false}],         # legacy spelling of "many"
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

    A port's ``cardinality`` is the one declaration the daemon *does*
    interpret: it is what the connection rules resolve against, in place of
    the old by-kind list (``CARDINALITIES``).  Absent means the pre-S4 rule
    (:func:`default_cardinality`), so a declaration written before
    cardinality existed keeps its behaviour exactly.
    """
    message: Dict[str, Any] = {"type": "type_declare", "types": list(types)}
    if menu is not None:
        message["menu"] = list(menu)
    if vocabulary is not None:
        message["vocabulary"] = dict(vocabulary)
    if aliases is not None:
        message["aliases"] = dict(aliases)
    return message


#: What a ``space_declare`` may say about the space it declares
#: (agents/SPACES.md §18).  ``space_id`` is the identity — the same id *is*
#: the same space, which is what makes a declaration idempotent — and the
#: rest is what the declaring app knows about it.  An app declares on every
#: attach, and the declaration is the only thing that makes its space exist:
#: a declared space is never written to disk, so re-declaring is how it
#: survives updates and reboots with no imperative state to migrate.
SPACE_DECLARE_FIELDS = ("space_id", "label", "color", "node_view", "parent")


def space_declare(space: Dict[str, Any]) -> Dict[str, Any]:
    """A module declaring (or updating) the space it owns.

    ``space`` carries ``space_id`` and any of ``label``/``color``/
    ``node_view``/``parent`` (see :data:`SPACE_DECLARE_FIELDS`).  The daemon
    creates the space if it does not have it, updates the fields it is given
    if it does, and answers with the record it ended up with
    (:func:`space_declared`).  A declaration that would claim a space that is
    permanent, declared by a file, or owned by another client is refused with
    a reason and changes nothing.
    """
    return {"type": "space_declare", "space": dict(space)}


def space_declared(space: Dict[str, Any]) -> Dict[str, Any]:
    """The record a declaration ended up with — the answer to
    :func:`space_declare`, in the shape a payload carries a space in."""
    return {"type": "space_declared", "space": dict(space)}


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
    """A relayed value, addressed at the instance *and port* it drives
    (agents/SPACES.md §14).

    The daemon is the bus: a producer reports its outputs (``node_values``),
    the daemon looks up the edges leaving each reported port, and hands the
    value to the consumer module that owns the port at the other end.  Where
    that port drives a control — every value control gets a ``drives_control``
    input port named for it — the relay is spelled ``control_set``, because
    that is the path the consumer already implements (``on_control``, exactly
    as if the user had set the control), so no module needs a new callback.

    This shape is that same relay addressed at a raw port: the value of an
    input port that is *not* a control, which a module takes through
    ``on_value``.  Nothing emits it yet — the control path above is the one
    every shipped consumer implements — but it is the seam for a module that
    wants the value rather than a parameter.
    """
    return {"type": "value_set", "instance_id": instance_id, "port": port, "value": value}


def node_spawn(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Daemon -> module: run these instances.

    The daemon owns identity and placement; a module owns running the node.
    When the user creates a node of a type a *module* provides (rather than
    one the daemon models itself), this is how it is asked to run it.  Each
    node is announce-shaped (``instance_id``, ``type_id``, ``space``,
    ``config``) so a module can treat it exactly like one it announced
    itself."""
    return {"type": "node_spawn", "nodes": list(nodes)}


def node_stop(instance_ids: List[str]) -> Dict[str, Any]:
    """Daemon -> module: stop running these (the placement stays, so the
    node is still there — offline — if the module comes back)."""
    return {"type": "node_stop", "instance_ids": list(instance_ids)}


def node_values(values: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Module -> daemon: the current output values of its instances
    (``{instance_id: {port: value}}``).

    Not state: a display reads them to show what a node is computing right
    now (the primitives module's display nodes).  The daemon keeps the last
    report so a display polling the graph sees them.
    """
    return {"type": "node_values",
            "values": {k: dict(v) for k, v in values.items()}}


def node_control(instance_id: str, key: str, value: Any) -> Dict[str, Any]:
    """A *display* setting one control of one node.

    Distinct from ``control_set`` (daemon -> module): this is the request
    form.  The daemon routes it to whichever module owns the instance, so a
    display never has to know which module that is — or dial it.
    """
    return {"type": "node_control", "instance_id": instance_id, "key": key,
            "value": value}


def node_edge(from_node: str, to_node: str, to_port: str = "in",
              from_port: str = "out", op: str = "add") -> Dict[str, Any]:
    """A display connecting (``op="add"``) or disconnecting (``"remove"``)
    two nodes.  The daemon validates it against the declared ports — the one
    authority — records it, and tells the owning modules to realize it."""
    return {"type": "node_edge", "op": op, "from_node": from_node,
            "to_node": to_node, "to_port": to_port, "from_port": from_port}


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
    # The controls come first: a port may declare that it drives one, and
    # the declaration is only meaningful against the control it names.
    controls = decl.get("controls", [])
    if not isinstance(controls, list) or any(
        not isinstance(c, dict) or not _is_str(c.get("key")) for c in controls
    ):
        return False, f"{decl.get('type_id')}: every control needs a key"
    for control in controls:
        stated = control.get("kind")
        if stated is not None and stated not in VALUE_KINDS:
            return False, (
                f"{decl.get('type_id')}.{control.get('key')}: unknown control kind "
                f"{stated!r} (expected one of {', '.join(VALUE_KINDS)}, or absent "
                f"to derive it from the widget)"
            )
        # A choice's options are the *declaration's* (agents/SPACES.md §19):
        # the daemon and the consumer both read them from here to decide
        # whether a value may land, so a choice that names none can take
        # nothing and is refused at declare time rather than silently
        # swallowing every value later.
        declared_choices = control.get("choices")
        if declared_choices is not None and (
            not isinstance(declared_choices, list)
            or any(not isinstance(option, str) for option in declared_choices)
        ):
            return False, (
                f"{decl.get('type_id')}.{control.get('key')}: choices must be a "
                f"list of strings"
            )
        if control_value_kind(control) == CHOICE_KIND and not choices_of(
            declared_choices
        ):
            return False, (
                f"{decl.get('type_id')}.{control.get('key')}: a choice control "
                f"must declare its options (a choice that names none can take "
                f"no value)"
            )
    control_keys = {str(control.get("key")) for control in controls}
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
        # `choices` is a *value port's* field (§19): the options a choice port
        # accepts, or the suggestions a text one offers.  A node kind has no
        # values to name options for, a `choices` list has to be strings, and
        # a choice **input** that names none can take none — all three are
        # refused here rather than ignored or silently swallowed later.
        port_choices = port.get("choices")
        if port_choices is not None:
            if not is_value_kind(port.get("kind")):
                return False, (
                    f"{decl.get('type_id')}.{port.get('name')}: choices is a value "
                    f"port's field, and {port.get('kind')!r} is not a value kind"
                )
            if not isinstance(port_choices, list) or any(
                not isinstance(option, str) for option in port_choices
            ):
                return False, (
                    f"{decl.get('type_id')}.{port.get('name')}: choices must be a "
                    f"list of strings"
                )
        if (port.get("kind") == CHOICE_KIND
                and port.get("direction") == DIR_IN
                and not choices_of(port_choices)):
            return False, (
                f"{decl.get('type_id')}.{port.get('name')}: a choice input port "
                f"must declare its options (a choice that names none can take no "
                f"value)"
            )
        cardinality = port.get("cardinality")
        if cardinality is not None and cardinality not in CARDINALITIES:
            return False, (
                f"{decl.get('type_id')}.{port.get('name')}: unknown cardinality "
                f"{cardinality!r} (expected one of "
                f"{', '.join(CARDINALITIES)}, or absent for the default)"
            )
        merge = port.get("merge")
        if merge is not None and merge not in MERGES:
            return False, (
                f"{decl.get('type_id')}.{port.get('name')}: unknown merge "
                f"{merge!r} (expected one of {', '.join(MERGES)}, or absent)"
            )
        # A port that drives a control is named for it, so the name *is* the
        # key the relay addresses (agents/SPACES.md §14).  The daemon
        # declares these itself for every value control; a module declaring
        # one must therefore point at a control it declared, on an input.
        drives = port.get("drives_control")
        if drives is not None and not isinstance(drives, bool):
            return False, (
                f"{decl.get('type_id')}.{port.get('name')}: drives_control must "
                f"be true or false"
            )
        if drives:
            if port.get("direction") != DIR_IN:
                return False, (
                    f"{decl.get('type_id')}.{port.get('name')}: a port that "
                    f"drives a control is an input, not an output"
                )
            if str(port.get("name")) not in control_keys:
                return False, (
                    f"{decl.get('type_id')}.{port.get('name')}: drives a control, "
                    f"so it must be named for one ({decl.get('type_id')} declares "
                    f"{', '.join(sorted(control_keys)) or 'none'})"
                )
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


def validate_space_declare(space: Any) -> Tuple[bool, str]:
    """The *shape* of a ``space_declare`` payload (agents/SPACES.md §18).

    What the id may be, and whether the claim is allowed at all, is the
    daemon's space model's business (``central/spaces.py``): this checks the
    message, so a malformed declaration is refused as a message rather than
    half-applied as a space."""
    if not isinstance(space, dict):
        return False, "a space declaration must be an object"
    unknown = sorted(set(space) - set(SPACE_DECLARE_FIELDS))
    if unknown:
        return False, (
            f"unknown space declaration field(s): {', '.join(unknown)} "
            f"(a declaration carries {', '.join(SPACE_DECLARE_FIELDS)})"
        )
    if space.get("space_id") is None or not isinstance(space.get("space_id"), str):
        return False, "a space declaration needs a space_id"
    for key in ("label", "color", "parent"):
        if space.get(key) is not None and not isinstance(space.get(key), str):
            return False, f"space declaration {key} must be a string"
    if space.get("node_view") is not None and not isinstance(
        space.get("node_view"), bool
    ):
        return False, "space declaration node_view must be true or false"
    return True, ""
