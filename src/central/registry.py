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
  module (``capability_query`` / ``type_query``).

Status is computed here, never by a module: ``online``,
``offline_module``, ``offline_dep`` (a node its ``requires`` list names is
offline), ``unbacked``.  A module only reports *health* (``ok`` /
``starting`` / ``dead``) about instances it is actually running.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from central import module_api as api


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

    def to_dict(self) -> Dict[str, Any]:
        return {
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


@dataclass
class ModuleRecord:
    module_id: str
    name: str
    version: str = ""
    role: str = api.ROLE_MODULE
    connected: bool = True
    types: Dict[str, TypeDecl] = field(default_factory=dict)
    instances: Set[str] = field(default_factory=set)

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
        #: type_id -> the module that declared it last (kept after detach so
        #: a disabled module's placements report offline_module, not unbacked)
        self._type_owner: Dict[str, str] = {}
        self._counter = 0

    # -- modules ---------------------------------------------------------

    def allocate_module_id(self, name: str) -> str:
        with self._lock:
            self._counter += 1
            base = name or "module"
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

    # -- types -----------------------------------------------------------

    def declare_types(self, module_id: str,
                      decls: Iterable[Dict[str, Any]]) -> Tuple[List[str], List[Dict[str, Any]]]:
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
                record.types[type_id] = TypeDecl(
                    type_id=type_id,
                    module_id=module_id,
                    schema=int(decl.get("schema", 1)),
                    ports=list(decl.get("ports", [])),
                    controls=list(decl.get("controls", [])),
                    display=dict(decl.get("display", {})),
                    capabilities=list(decl.get("capabilities", [])),
                    requires=list(decl.get("requires", [])),
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

    def announce(self, module_id: str, nodes: Iterable[Dict[str, Any]]
                 ) -> Tuple[List[str], List[str], List[str]]:
        """A module's running instances.

        Returns ``(bound, created, errors)``: ``bound`` are instance ids it
        adopted from existing placements, ``created`` are new ones.
        """
        bound: List[str] = []
        created: List[str] = []
        errors: List[str] = []
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
            self._recompute()
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
            instance = self._instances.get(instance_id)
            if instance is None:
                return ""
            decl = self._type_decl(instance.type_id)
            if decl is None:
                return ""
            for entry in decl.ports:
                if entry.get("name") == port and entry.get("direction") == direction:
                    return str(entry.get("kind") or "")
            return ""

    def port_multi(self, instance_id: str, port: str) -> bool:
        """Whether a single-driver input port opted out of the one-driver
        rule (``multi: true`` in its declaration)."""
        with self._lock:
            instance = self._instances.get(instance_id)
            if instance is None:
                return False
            decl = self._type_decl(instance.type_id)
            if decl is None:
                return False
            for entry in decl.ports:
                if entry.get("name") == port:
                    return bool(entry.get("multi", False))
            return False

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

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "modules": [m.to_dict() for m in self._modules.values()],
                "types": [t.to_dict() for t in self.declared_types()],
                "instances": [i.to_dict() for i in self.instances()],
            }
