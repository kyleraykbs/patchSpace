"""
registry.py — attached modules, their declared node types, and the node
instances (including placements no module is currently backing).

This is the daemon-side catalogue the whole plan hangs off:

* a **module** attaches (``hello`` role=module), declares its **types**
  (``type_declare``) and announces the **instances** it is running
  (``node_announce``);
* a **placement** is an instance the graph knows about even when nothing
  is backing it — a node the user placed for a module that is disabled,
  detached, or not written yet.  It keeps its id, space, position and
  config, and reports ``unbacked`` (the gray node) or
  ``offline_module`` (its owning module is known but not attached);
* an **app** asks the registry what is available instead of dialing a
  module (``capability_query`` / ``type_query``);
* a **declared space** (agents/SPACES.md §18) is a module's own: the record
  lives in the space tree (``central/spaces.py``), and ``announce`` hands the
  space each accepted node lands in over to it (``on_space``) rather than
  holding a second copy of what a space is.

Status is computed here, never by a module: ``online``,
``offline_module``, ``offline_dep`` (a node its ``requires`` list names is
offline), ``unbacked``.  A module only reports *health* (``ok`` /
``starting`` / ``dead``) about instances it is actually running.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from central import module_api as api
from central import spaces as spaces_mod


@dataclass
class TypeDecl:
    """One node type a module provides."""

    type_id: str
    module_id: str
    schema: int = 1
    ports: List[Dict[str, Any]] = field(default_factory=list)
    controls: List[Dict[str, Any]] = field(default_factory=list)
    display: Dict[str, Any] = field(default_factory=dict)
    capabilities: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    #: port name -> the **value slot** that port is (``api.value_slots``):
    #: every input port whose kind is a value kind, with the control key the
    #: delivery is spelled for and the options it accepts.  Derived from the
    #: declaration when the type is declared — one listing, built by the
    #: module API, read by the relay and by the consumer side of every module
    #: (agents/SPACES.md §19).  Not serialized: it is the same declaration a
    #: display already installs, seen through the value grammar.
    slots: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type_id": self.type_id,
            "module_id": self.module_id,
            "schema": self.schema,
            "ports": list(self.ports),
            "controls": list(self.controls),
            "display": dict(self.display),
            "capabilities": list(self.capabilities),
            "requires": list(self.requires),
        }


@dataclass
class InstanceRecord:
    """A node instance (live or a bare placement)."""

    instance_id: str
    type_id: str
    space: str = "main"
    config: Dict[str, Any] = field(default_factory=dict)
    requires: List[str] = field(default_factory=list)
    declared_by: str = ""          # module that announced it, "" for a bare placement
    status: str = api.STATUS_UNBACKED
    reason: str = ""
    health: str = api.HEALTH_UNKNOWN
    #: The module's last reported output values (``{port: value}``) — what
    #: a display shows for a node that computes something.  Empty for a
    #: node whose module reports none.
    values: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        out = {
            "instance_id": self.instance_id,
            "type_id": self.type_id,
            "space": self.space,
            "config": dict(self.config),
            "requires": list(self.requires),
            "declared_by": self.declared_by,
            "status": self.status,
            "reason": self.reason,
            "health": self.health,
        }
        if self.values:
            out["values"] = dict(self.values)
        return out


@dataclass
class ModuleRecord:
    module_id: str
    name: str
    version: str = ""
    role: str = api.ROLE_MODULE
    connected: bool = True
    types: Dict[str, TypeDecl] = field(default_factory=dict)
    instances: Set[str] = field(default_factory=set)
    #: Presentation data the module owns and a display consumes verbatim:
    #: the add-node menu and the vocabulary tables (field labels, choice
    #: lists).  The daemon relays them; it does not interpret them.
    menu: List[Dict[str, Any]] = field(default_factory=list)
    vocabulary: Dict[str, Any] = field(default_factory=dict)
    #: Pre-rename type keys this module still answers to.
    aliases: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "module_id": self.module_id,
            "name": self.name,
            "version": self.version,
            "role": self.role,
            "connected": self.connected,
            "types": sorted(self.types),
            "instances": sorted(self.instances),
        }


class Registry:
    """The daemon's catalogue of modules, types and instances."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._modules: Dict[str, ModuleRecord] = {}
        self._instances: Dict[str, InstanceRecord] = {}
        #: (instance_id, port) -> ``(known, value)``: what a **value slot** holds
        #: when nothing drives it - the port name *is* the control key a
        #: `drives_control` port is named for, so one bookkeeping serves a
        #: control port and a declared value port alike (agents/SPACES.md §19).
        #: Captured when a driver is wired onto the slot's port (§14), so the
        #: value the module had *then* is what unwiring restores, and updated
        #: whenever the user sets the control.  The instance's own ``config``
        #: stays the record of what the module was told; this is the drive's
        #: bookkeeping, kept apart so a module re-announcing its config
        #: cannot make a driven value look like the stored one.
        self._local_controls: Dict[Tuple[str, str], Tuple[bool, Any]] = {}
        #: type_id -> the module that declared it last (kept after detach so
        #: a disabled module's placements report offline_module, not unbacked)
        self._type_owner: Dict[str, str] = {}

    # -- modules ---------------------------------------------------------

    def allocate_module_id(self, name: str) -> str:
        """The id a module connects under.  A module that reconnects (its
        old record is detached) keeps its id, so its placements and edges
        stay bound to it across a restart."""
        with self._lock:
            base = name or "module"
            for module_id, record in self._modules.items():
                if record.name == base and not record.connected:
                    return module_id
            candidate = base
            n = 1
            while candidate in self._modules:
                n += 1
                candidate = f"{base}#{n}"
            return candidate

    def attach(self, module_id: str, name: str, version: str = "",
               role: str = api.ROLE_MODULE) -> ModuleRecord:
        with self._lock:
            record = self._modules.get(module_id)
            if record is None:
                record = ModuleRecord(module_id=module_id, name=name or module_id,
                                      version=version, role=role)
                self._modules[module_id] = record
            record.connected = True
            record.name = name or record.name
            record.version = version or record.version
            record.role = role or record.role
            self._recompute()
            return record

    def detach(self, module_id: str) -> List[str]:
        """Mark a module gone.  Its instances (and everything whose
        ``requires`` closure touches them) stop being online; the return
        value is the set of instance ids whose status changed."""
        with self._lock:
            record = self._modules.get(module_id)
            if record is None:
                return []
            before = self._statuses()
            record.connected = False
            record.instances.clear()
            for instance in self._instances.values():
                if instance.declared_by == module_id:
                    instance.declared_by = ""
                    instance.health = api.HEALTH_UNKNOWN
            self._recompute()
            return self._changed(before)

    def connected_modules(self) -> List[ModuleRecord]:
        with self._lock:
            return [m for m in self._modules.values() if m.connected]

    def module(self, module_id: str) -> Optional[ModuleRecord]:
        with self._lock:
            return self._modules.get(module_id)

    def client_name(self, client_id: str) -> str:
        """The friendly name a client id is known by — what a payload shows
        for "whose space is this" (agents/SPACES.md §18), ``""`` when the
        daemon has no such client (a declaration whose declarer has gone)."""
        with self._lock:
            record = self._modules.get(client_id)
            return record.name if record is not None else ""

    # -- types -----------------------------------------------------------

    def declare_types(self, module_id: str,
                      decls: Iterable[Dict[str, Any]],
                      menu: Optional[List[Dict[str, Any]]] = None,
                      vocabulary: Optional[Dict[str, Any]] = None,
                      aliases: Optional[Dict[str, str]] = None
                      ) -> Tuple[List[str], List[Dict[str, Any]]]:
        """Register a module's type catalog.

        Returns ``(errors, migrations)`` where ``migrations`` are the
        persisted instances of the declared types that the module has not
        announced yet — the daemon sends one ``migrate`` per item so the
        module can upgrade configs written under an older schema instead of
        losing them.
        """
        errors: List[str] = []
        migrations: List[Dict[str, Any]] = []
        with self._lock:
            record = self._modules.setdefault(
                module_id, ModuleRecord(module_id=module_id, name=module_id)
            )
            if menu is not None:
                record.menu = list(menu)
            if vocabulary is not None:
                record.vocabulary = dict(vocabulary)
            if aliases is not None:
                record.aliases = dict(aliases)
            for decl in decls:
                ok, message = api.validate_type_decl(decl)
                if not ok:
                    errors.append(message)
                    continue
                type_id = decl["type_id"]
                owner = self._type_owner.get(type_id)
                if owner and owner != module_id and self._is_connected(owner):
                    errors.append(
                        f"{type_id} is already declared by {owner}"
                    )
                    continue
                self._type_owner[type_id] = module_id
                # A value control is a node parameter made addressable: the
                # daemon auto-declares an input port for it, named for the
                # control (agents/SPACES.md §14).  Merged here, once, so
                # every place that asks a type for its ports - the catalog a
                # display installs, the snapshot, and the connection rules -
                # sees the declared ports and the auto-declared ones alike.
                declared_ports = list(decl.get("ports", []))
                controls = list(decl.get("controls", []))
                ports = declared_ports + api.control_input_ports(
                    declared_ports, controls
                )
                record.types[type_id] = TypeDecl(
                    type_id=type_id,
                    module_id=module_id,
                    schema=int(decl.get("schema", 1)),
                    ports=ports,
                    controls=controls,
                    display=dict(decl.get("display", {})),
                    capabilities=list(decl.get("capabilities", [])),
                    requires=list(decl.get("requires", [])),
                    # The value grammar, derived once from the declaration:
                    # which ports carry a value, the control key their
                    # delivery is spelled for, and what a `choice` accepts
                    # (agents/SPACES.md §19).
                    slots=api.value_slots(ports, controls),
                )
            # Persisted instances of the types just declared that nothing is
            # running: hand them to the module for migration.  Only types
            # declared by *this* module (a type it dropped is not migrated).
            declared = set(record.types)
            for instance in self._instances.values():
                if instance.type_id not in declared:
                    continue
                if instance.declared_by:
                    continue
                rec = self._type_decl(instance.type_id)
                migrations.append({
                    "instance_id": instance.instance_id,
                    "type_id": instance.type_id,
                    "config": dict(instance.config),
                    "from_schema": int(instance.config.get("schema_version") or 0),
                    "to_schema": int(rec.schema) if rec else 1,
                })
            self._recompute()
        return errors, migrations

    def retire_types(self, module_id: str) -> List[str]:
        """Drop a module's type declarations (it no longer provides them).
        The ownership is kept, so its placements read offline_module."""
        with self._lock:
            record = self._modules.get(module_id)
            retired: List[str] = []
            if record is not None:
                retired = sorted(record.types)
                record.types.clear()
            self._recompute()
            return retired

    def set_type_owner(self, type_id: str, module_id: str) -> None:
        """Restore ownership from persisted state (a disabled module whose
        placements must read offline_module rather than unbacked)."""
        with self._lock:
            self._type_owner[type_id] = module_id
            self._recompute()

    def type_decl(self, type_id: str) -> Optional[TypeDecl]:
        with self._lock:
            return self._type_decl(type_id)

    def declared_types(self) -> List[TypeDecl]:
        with self._lock:
            return sorted(
                (t for m in self._modules.values() if m.connected
                 for t in m.types.values()),
                key=lambda t: (t.module_id, t.type_id),
            )

    def _type_decl(self, type_id: str) -> Optional[TypeDecl]:
        for record in self._modules.values():
            decl = record.types.get(type_id)
            if decl is not None:
                return decl
        return None

    def _is_connected(self, module_id: str) -> bool:
        record = self._modules.get(module_id)
        return bool(record and record.connected)

    # -- placements and instances ---------------------------------------

    def place(self, instance_id: str, type_id: str, space: str = "main",
              config: Optional[Dict[str, Any]] = None,
              requires: Optional[List[str]] = None) -> InstanceRecord:
        """Record a placement with no module behind it (persistence load)."""
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                instance = InstanceRecord(instance_id=instance_id, type_id=type_id)
                self._instances[instance_id] = instance
            instance.type_id = type_id
            instance.space = space or instance.space
            if config is not None:
                instance.config = dict(config)
            if requires is not None:
                instance.requires = list(requires)
            self._recompute()
            return instance

    def announce(self, module_id: str, nodes: Iterable[Dict[str, Any]],
                 on_space: Optional[Callable[[str], None]] = None
                 ) -> Tuple[List[str], List[str], List[str]]:
        """A module's running instances.

        Returns ``(bound, created, errors)``: ``bound`` are instance ids it
        adopted from existing placements, ``created`` are new ones.

        ``on_space`` is how the space an accepted node lands in is kept real
        (agents/SPACES.md §18): it is called once per accepted node with the
        node's container space id (:func:`spaces.node_space` — the node's own
        id path when it has one, else the space it names), *after* the lock
        is dropped, and the caller decides what to do about a space it has
        never heard of.  The registry stays the one that knows which node
        landed where; the space tree belongs to the daemon, and this is the
        hand-off — a declared space is created or updated there, never here.
        """
        bound: List[str] = []
        created: List[str] = []
        errors: List[str] = []
        containers: List[str] = []
        with self._lock:
            record = self._modules.get(module_id)
            if record is None:
                record = ModuleRecord(module_id=module_id, name=module_id)
                self._modules[module_id] = record
            for node in nodes:
                ok, message = api.validate_node_announce(node)
                if not ok:
                    errors.append(message)
                    continue
                instance_id = node["instance_id"]
                existing = self._instances.get(instance_id)
                if existing is not None:
                    bound.append(instance_id)
                    instance = existing
                else:
                    instance = InstanceRecord(
                        instance_id=instance_id, type_id=node["type_id"]
                    )
                    self._instances[instance_id] = instance
                    created.append(instance_id)
                instance.type_id = node["type_id"]
                if node.get("space"):
                    instance.space = node["space"]
                if isinstance(node.get("config"), dict):
                    instance.config = dict(node["config"])
                if node.get("requires") is not None:
                    instance.requires = list(node["requires"])
                instance.declared_by = module_id
                record.instances.add(instance_id)
                containers.append(
                    spaces_mod.node_space(instance_id, str(node.get("space") or ""))
                )
            self._recompute()
        if on_space is not None:
            for space_id in containers:
                on_space(space_id)
        return bound, created, errors

    def unannounce(self, module_id: str, instance_ids: Iterable[str]) -> List[str]:
        """A module no longer runs these instances (kept as placements)."""
        with self._lock:
            record = self._modules.get(module_id)
            gone: List[str] = []
            for instance_id in instance_ids:
                instance = self._instances.get(instance_id)
                if instance is None or instance.declared_by != module_id:
                    continue
                instance.declared_by = ""
                instance.health = api.HEALTH_UNKNOWN
                if record is not None:
                    record.instances.discard(instance_id)
                gone.append(instance_id)
            self._recompute()
            return gone

    def set_health(self, module_id: str, health: Dict[str, str]) -> None:
        with self._lock:
            for instance_id, value in health.items():
                instance = self._instances.get(instance_id)
                if instance is not None and instance.declared_by == module_id:
                    instance.health = value

    def set_values(self, module_id: str, values: Dict[str, Dict[str, Any]]) -> None:
        """Record a module's reported output values (see `node_values`)."""
        with self._lock:
            for instance_id, ports in values.items():
                instance = self._instances.get(instance_id)
                if instance is not None and instance.declared_by == module_id:
                    instance.values = {str(k): v for k, v in dict(ports).items()}

    def set_config(self, module_id: str, instance_id: str,
                   config: Optional[Dict[str, Any]]) -> bool:
        """Store a config a module returned from migration (or replace the
        stored one).  ``None`` means the module declined: the daemon keeps
        the old config and recreates the instance from defaults."""
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                return False
            # A migration result arrives *before* the announce that binds
            # the instance, so the type's owner is the authority here, not
            # `declared_by`.
            if (instance.declared_by or self._type_owner.get(instance.type_id)) != module_id:
                return False
            if isinstance(config, dict):
                instance.config = dict(config)
            return True

    def forget_instance(self, instance_id: str) -> bool:
        """Drop an instance entirely (its node was deleted, not merely
        offline).  A placement alone is not forgotten by accident: the
        daemon calls this only for instances a module was running."""
        with self._lock:
            instance = self._instances.pop(instance_id, None)
            for marker in [m for m in self._local_controls if m[0] == instance_id]:
                del self._local_controls[marker]
            if instance is not None and instance.declared_by:
                record = self._modules.get(instance.declared_by)
                if record is not None:
                    record.instances.discard(instance_id)
            return instance is not None

    def instance(self, instance_id: str) -> Optional[InstanceRecord]:
        with self._lock:
            return self._instances.get(instance_id)

    def instances(self, module_id: str = "") -> List[InstanceRecord]:
        with self._lock:
            values = self._instances.values()
            if module_id:
                values = [i for i in values if i.declared_by == module_id]
            return sorted(values, key=lambda i: i.instance_id)

    def placements(self) -> List[InstanceRecord]:
        with self._lock:
            return sorted(self._instances.values(), key=lambda i: i.instance_id)

    # -- status ----------------------------------------------------------

    def _statuses(self) -> Dict[str, str]:
        return {i.instance_id: i.status for i in self._instances.values()}

    def _changed(self, before: Dict[str, str]) -> List[str]:
        return [iid for iid, status in self._statuses().items()
                if before.get(iid) != status]

    def _recompute(self) -> None:
        """Recompute every instance's status: who is online, whose module is
        known but gone, what is a bare placement, and what merely *depends*
        on something offline."""
        for instance in self._instances.values():
            decl = self._type_decl(instance.type_id)
            owner = self._type_owner.get(instance.type_id)
            if (
                decl is not None
                and instance.declared_by
                and instance.declared_by == decl.module_id
                and self._is_connected(instance.declared_by)
            ):
                instance.status = api.STATUS_ONLINE
                instance.reason = ""
            elif owner is None:
                # No module has ever declared this type: the gray placement.
                instance.status = api.STATUS_UNBACKED
                instance.reason = "no module provides this node type"
            elif decl is not None and instance.declared_by and decl.module_id != instance.declared_by:
                # Announced by a module other than the one that declared the
                # type: the announcer is not the authority for it.
                instance.status = api.STATUS_OFFLINE_MODULE
                instance.reason = f"{instance.type_id} belongs to {decl.module_id}"
            elif not self._is_connected(owner):
                instance.status = api.STATUS_OFFLINE_MODULE
                instance.reason = f"{owner} is not attached"
            else:
                # The owning module is attached but is not running this
                # instance right now (it declined to adopt it, or has not
                # announced it yet).
                instance.status = api.STATUS_OFFLINE_MODULE
                instance.reason = f"not provided by {owner} right now"
        # Dependency closure: an online node whose requires names an offline
        # node is offline_dep.  Cycles terminate because each node is only
        # downgraded once.
        offline = {
            i.instance_id: i.status
            for i in self._instances.values()
            if i.status != api.STATUS_ONLINE
        }
        dependents: Dict[str, Set[str]] = {}
        for instance in self._instances.values():
            for needed in instance.requires:
                dependents.setdefault(needed, set()).add(instance.instance_id)
        pending = list(offline)
        while pending:
            current = pending.pop()
            for dependent in dependents.get(current, ()):
                record = self._instances[dependent]
                if record.status != api.STATUS_ONLINE:
                    continue
                record.status = api.STATUS_OFFLINE_DEP
                record.reason = f"needs {current}, which is offline"
                pending.append(dependent)

    def statuses(self) -> Dict[str, str]:
        with self._lock:
            return self._statuses()

    # -- app queries -----------------------------------------------------

    def port_kind(self, instance_id: str, port: str, direction: str) -> str:
        """The declared kind of one port, "" when the instance/type/port is
        unknown.  This is what the graph's connection rules resolve against
        (``Graph.can_connect(..., kind_of=registry.port_kind)``)."""
        with self._lock:
            entry = self._port_entry(instance_id, port, direction)
            return str(entry.get("kind") or "") if entry else ""

    def port_multi(self, instance_id: str, port: str) -> bool:
        """Whether a port declared the legacy ``multi: true`` opt-out (no
        shipped type does; ``cardinality: "many"`` is the spelling now)."""
        with self._lock:
            entry = self._port_entry(instance_id, port)
            return bool(entry.get("multi", False)) if entry else False

    def port_cardinality(self, instance_id: str, port: str,
                         direction: str = "") -> str:
        """How many drivers one port takes — ``"one"`` or ``"many"``.

        The type's own declaration decides: ``cardinality`` when it states
        one, ``many`` for a declared ``merge`` (a merge makes sense only
        for several drivers) and for the legacy ``multi: true`` opt-out,
        otherwise the pre-S4 rule for the port's kind
        (:func:`api.default_cardinality`).  This is what the graph's
        connection rules resolve against
        (``Graph.can_connect(..., cardinality_of=registry.port_cardinality)``);
        the graph itself holds no kind knowledge.
        """
        with self._lock:
            entry = self._port_entry(instance_id, port, direction)
            if entry is None:
                # Nothing declared about an unknown port: refuse nothing
                # here (the kind lookup has already refused the edge).
                return api.CARDINALITY_MANY
            declared = entry.get("cardinality")
            if declared in api.CARDINALITIES:
                return str(declared)
            if entry.get("merge") in api.MERGES:
                return api.CARDINALITY_MANY
            if bool(entry.get("multi", False)):
                return api.CARDINALITY_MANY
            return api.default_cardinality(str(entry.get("kind") or ""))

    def port_merge(self, instance_id: str, port: str, direction: str = "") -> str:
        """How a receiving port that takes several drivers combines them —
        ``"mix"``/``"sum"``/``"last"``, or ``""`` when it declares none.

        Surfaced to whatever consumes it: the declaration travels verbatim
        to a display in the catalogs the daemon serves (``catalog_reply``,
        ``type_query``, ``snapshot`` — ``TypeDecl.to_dict`` copies the
        ports), and a module that owns the receiving node reads its own."""
        with self._lock:
            entry = self._port_entry(instance_id, port, direction)
            if entry is None:
                return ""
            declared = entry.get("merge")
            return str(declared) if declared in api.MERGES else ""

    def port_drives_control(self, instance_id: str, port: str,
                            direction: str = api.DIR_IN) -> str:
        """The control key a port drives, or ``""`` for an ordinary port.

        A ``drives_control`` port is *named for* the control it drives
        (agents/SPACES.md §14), so the name **is** the key: the daemon relays
        a producer's value onto such a port as a ``control_set`` for exactly
        this key, and the consumer module applies it through the callback it
        already has.  The port is either the one the daemon auto-declared for
        a value control (:func:`api.control_input_ports`) or one the module
        declared itself — which ``validate_type_decl`` holds to the same
        rule, an input named for a control it declares.
        """
        with self._lock:
            entry = self._port_entry(instance_id, port, direction)
            if entry is None or not entry.get("drives_control"):
                return ""
            return str(entry.get("name") or "")

    # -- the value grammar (agents/SPACES.md §19) ------------------------

    def value_slot(self, instance_id: str, port: str,
                   direction: str = api.DIR_IN) -> Optional[Dict[str, Any]]:
        """The **value slot** an input port is, or ``None``.

        A slot is an input port whose kind is a value kind
        (:func:`api.value_slots`, built from the type's declaration): the
        vocabulary is what decides, so a port the connection rules let a
        value wire into is exactly a port the relay carries one to.  ``None``
        means the port takes no value at all — an audio/bundle/filter/
        impulse/sound/camera port, an output, a port this instance's type
        does not declare, or a port whose direction does not match.

        This is the same listing the consuming module builds for itself from
        the declarations it announced, so the daemon and the consumer cannot
        disagree about *which* ports take a value, only about whether one
        particular value fits — and that is :meth:`value_problem`."""
        with self._lock:
            instance = self._instances.get(str(instance_id))
            if instance is None:
                return None
            decl = self._type_decl(instance.type_id)
            if decl is None:
                return None
            slot = decl.slots.get(str(port))
            if slot is None:
                return None
            # The declaration still has to answer for this instance's port —
            # with a direction given, an output of the same name is not it.
            if self._port_entry(instance_id, port, direction) is None:
                return None
            return dict(slot)

    def value_problem(self, instance_id: str, port: str, value: Any) -> str:
        """Why this value cannot land in this port, or ``""``.

        **The one acceptance rule** (agents/SPACES.md §19), asked of the
        declaration: the daemon refuses to deliver a value the consumer's
        declaration cannot take, with this as the reason, and the consumer
        module refuses to keep one.

        ``""`` when the port is no value slot at all — a `setting`, a button,
        an audio jack — because such a port takes no value and nothing here is
        refusing it (the relay simply has nothing to carry), and ``""`` when
        the slot takes what it is given."""
        slot = self.value_slot(instance_id, port)
        if slot is None:
            return ""
        problem = api.value_problem(slot["kind"], slot["choices"], value)
        if not problem:
            return ""
        return f"{instance_id}.{port}: {problem}"

    # -- a value slot's local value (agents/SPACES.md §14, §19) ----------
    #
    # A driven slot has to have somewhere to go back to when its driver is
    # unwired.  That value lives here rather than being read off the instance
    # config at unwire time: a module re-announcing its config (a reconnect,
    # a re-spawn) announces what it currently holds, which while driven is the
    # *relayed* value - so the daemon freezes what the slot held when the
    # drive began.  The user setting the control is the other writer: that is
    # what the slot is stored as when nothing drives it.
    #
    # The key is the **port name**, which is also the control key a
    # `drives_control` port is named for (§14) - so one bookkeeping serves
    # both ways a value arrives: an auto-declared control port and a declared
    # value port resolve, rank and restore identically (§19).

    def capture_slot(self, instance_id: str, key: str) -> Tuple[bool, Any]:
        """Freeze what a value slot holds locally, before a driver takes it
        over.

        Called when an edge lands on the slot's port.  A slot already
        captured (a second driver on a port declared ``many``, or a control
        the user has set) keeps what it has: the drive is not a new value, it
        is the value the slot goes back to.
        """
        with self._lock:
            marker = (str(instance_id), str(key))
            if marker in self._local_controls:
                return self._local_controls[marker]
            instance = self._instances.get(str(instance_id))
            if instance is None:
                return False, None
            stored = (str(key) in instance.config,
                      instance.config.get(str(key)))
            self._local_controls[marker] = stored
            return stored

    def set_slot_value(self, instance_id: str, key: str, value: Any) -> bool:
        """Record the user setting a control: it is now the slot's stored
        local value (what unwiring a driver restores), and part of the
        instance's config — which is what the daemon hands a module that
        re-attaches, and what a display reads in a snapshot.

        The module keeps running its own copy; this is the daemon's.
        """
        with self._lock:
            instance = self._instances.get(str(instance_id))
            if instance is None:
                return False
            instance.config[str(key)] = value
            self._local_controls[(str(instance_id), str(key))] = (True, value)
            return True

    def local_slot_value(self, instance_id: str, key: str) -> Tuple[bool, Any]:
        """``(known, value)``: the value a slot holds when nothing drives it,
        as far as the daemon knows it.

        Known when the user has set the control, or when the drive that is
        ending captured a value off the instance config.  A slot nothing has
        ever set has *no* stored value — the daemon says so and restores
        nothing rather than inventing one, because what an unset slot means
        (the module's own default) is the module's own business (§19's third
        rank: the declared default belongs to the node, not to the daemon).
        """
        with self._lock:
            return self._local_controls.get(
                (str(instance_id), str(key)), (False, None)
            )

    def _port_entry(self, instance_id: str, port: str,
                    direction: str = "") -> Optional[Dict[str, Any]]:
        """One port's declaration entry (name, and direction when given)."""
        instance = self._instances.get(instance_id)
        if instance is None:
            return None
        decl = self._type_decl(instance.type_id)
        if decl is None:
            return None
        for entry in decl.ports:
            if entry.get("name") != port:
                continue
            if direction and entry.get("direction") != direction:
                continue
            return entry
        return None

    def type_query(self, type_id: str) -> Dict[str, Any]:
        with self._lock:
            decl = self._type_decl(type_id)
            if decl is None:
                return {"type_id": type_id, "available": False, "modules": []}
            module = self._modules.get(decl.module_id)
            return {
                "type_id": type_id,
                "available": bool(module and module.connected),
                "modules": [module.module_id] if module else [],
                "type": decl.to_dict(),
            }

    def capability_query(self, capability: str) -> Dict[str, Any]:
        """Which attached modules can serve ``capability``, and with what."""
        with self._lock:
            hits: List[Dict[str, Any]] = []
            for decl in self.declared_types():
                if capability in decl.capabilities:
                    hits.append(decl.to_dict())
            return {"capability": capability, "available": bool(hits), "types": hits}

    def catalog(self) -> Dict[str, Any]:
        """What a display needs to draw any node: every attached module's
        declared types, plus the presentation data it owns (menu order,
        vocabulary tables)."""
        with self._lock:
            modules = [m for m in self._modules.values() if m.connected]
            menu: List[Dict[str, Any]] = []
            vocabulary: Dict[str, Any] = {}
            aliases: Dict[str, str] = {}
            for module in modules:
                menu.extend(module.menu)
                for key, value in module.vocabulary.items():
                    vocabulary.setdefault(key, value)
                for key, value in module.aliases.items():
                    aliases.setdefault(key, value)
            return {
                "types": [t.to_dict() for t in self.declared_types()],
                "menu": menu,
                "vocabulary": vocabulary,
                "aliases": aliases,
            }

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "modules": [m.to_dict() for m in self._modules.values()],
                "types": [t.to_dict() for t in self.declared_types()],
                "instances": [i.to_dict() for i in self.instances()],
            }
