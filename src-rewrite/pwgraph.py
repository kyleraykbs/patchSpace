"""
pwgraph.py

A live, self-updating model of the PipeWire graph, backed by a single
long-running ``pw-dump -m`` process.  Also owns the graph-mutation verbs
(link/unlink via pw-link) and the best-effort cleanup of leftover
objects/helpers from uncleanly-killed previous daemon runs.

Notable simplification over earlier versions: the daemon's built-in
"PatchBay" sink and "PatchBay Mic" are no longer constructed here.
They are ordinary supervised virtual-device nodes like any user-created
one (see pwnodes.py / main.py), so this module contains no virtual
sink/mic lifecycle code at all - one less source of state.

Reading the stream
------------------
``pw-dump -m`` prints the full current graph once as a JSON array, then
one JSON object per change.  The initial array is the authoritative
"graph is built" signal: after it is applied, the ``on_initial_sync``
callbacks fire exactly once.  Everything after is diffed against the
previous snapshot so node-created/node-removed/node-changed callbacks
only fire for real transitions.

A note on buffering: the pipe is read with ``os.read`` (one syscall,
returns as soon as *any* data is available) and framed with an
incremental JSON decoder - never ``file.read(n)``, which on a
non-interactive stream blocks until the full n bytes arrive and would
starve the initial snapshot.  An incremental UTF-8 decoder keeps a
multi-byte character from being split across chunk boundaries.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import select
import signal
import subprocess
import threading
import time as _time
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

NODE_TYPE = "PipeWire:Interface:Node"
PORT_TYPE = "PipeWire:Interface:Port"
LINK_TYPE = "PipeWire:Interface:Link"
CLIENT_TYPE = "PipeWire:Interface:Client"

OnChange = Callable[["PipewireGraph"], None]
OnError = Callable[[Exception], None]
NodeCreatedCallback = Callable[[int, dict], None]
NodeRemovedCallback = Callable[[int, dict], None]


class GraphMonitorError(Exception):
    pass


class ProcessStartError(GraphMonitorError):
    pass


class ProcessExitedError(GraphMonitorError):
    pass


class LinkError(GraphMonitorError):
    """Raised only for genuine pw-link failures.  "Already linked" /
    "already unlinked" are treated as success - the graph dump can lag a
    link/unlink by a cycle, so callers shouldn't have to special-case it."""


def _name_is_backing_of(name: str, marker: str) -> bool:
    """True if ``name`` is exactly ``marker`` or one of its ``_``-suffixed
    siblings (``marker_in``, ``marker_fx_out``, ``marker_in_keepalive``,
    ...).

    Deliberately NOT a bare prefix match.  Every real object this project
    creates is ``{backing}`` or ``{backing}_<suffix>``, so requiring the
    underscore keeps a marker from matching a *different* node whose
    backing merely starts with it
    (``noise_cancel_..._1`` vs ``noise_cancel_..._10``).  With a bare
    ``startswith`` the reap for a newly added node could kill a live
    sibling node's nodes/owning processes - the "adding another effect
    tears down the existing one, killing all audio" failure class."""
    return name == marker or name.startswith(marker + "_")


def _name_matches_marker(name: str, marker: str) -> bool:
    """Whether a live node name belongs to the object(s) a sweep
    ``marker`` identifies.  Markers come in two deliberately different
    shapes:

      * an **exact backing name** (``noise_cancel_node_..._1``) - covers
        itself and its ``_``-suffixed siblings only (see
        ``_name_is_backing_of``); the reap on add uses this shape so it
        can never spill onto a differently named node, and
      * an **owned prefix** - the daemon's ``_OWNED_PREFIXES`` and the
        startup sweep markers built from them, all ending in ``_``
        (``noise_cancel_node_``, ``patchbay_``, ...).  These are not
        backing names, so they must stay a plain prefix match; scoping
        them like a backing name made every owned object invisible to
        the crash-recovery sweep, so a crashed run's stale echo/noise
        streams lingered and a fresh AI Noise Cancel wired against them
        and took the chain silent."""
    if marker.endswith("_"):
        return name.startswith(marker)
    return _name_is_backing_of(name, marker)


def _read_available(pipe, timeout: float) -> str:
    """Non-blocking read of everything a still-running subprocess has
    written to a pipe within ``timeout`` seconds."""
    if pipe is None:
        return ""
    fd = pipe.fileno()
    deadline = _time.monotonic() + timeout
    chunks = []
    while True:
        remaining = deadline - _time.monotonic()
        if remaining <= 0:
            break
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            break
        try:
            chunk = os.read(fd, 4096)
        except OSError:
            break
        if not chunk:
            break
        chunks.append(chunk.decode("utf-8", errors="replace"))
    return "".join(chunks)


class PipewireGraph:
    def __init__(
        self,
        on_error: Optional[OnError] = None,
        dump_command: Sequence[str] = ("pw-dump", "-m"),
        link_command: Sequence[str] = ("pw-link",),
        unlink_command: Sequence[str] = ("pw-link", "-d"),
        read_chunk_size: int = 65536,
        node_ready_debounce: float = 0.15,
        node_ready_max_wait: float = 2.0,
        pw_cli_command: Sequence[str] = ("pw-cli",),
    ):
        self.on_error = on_error
        self._dump_command = list(dump_command)
        self._link_command = list(link_command)
        self._unlink_command = list(unlink_command)
        self._pw_cli_command = list(pw_cli_command)
        self._read_chunk_size = read_chunk_size
        self._node_ready_debounce = node_ready_debounce
        self._node_ready_max_wait = node_ready_max_wait

        self.objects: Dict[int, dict] = {}
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

        self._node_created_callbacks: List[NodeCreatedCallback] = []
        self._node_removed_callbacks: List[NodeRemovedCallback] = []
        self._on_change_callbacks: List[OnChange] = []
        self._initial_sync_callbacks: List[OnChange] = []

        self._initial_dump_received = False
        self._previous_nodes: Dict[int, dict] = {}

        # node-ready staging: a freshly-seen node is held briefly so a
        # single node with churning props doesn't fire node_created many
        # times; it fires once the props go quiet or the cap is hit.
        self._pending_nodes: Dict[int, dict] = {}
        self._pending_created_at: Dict[int, float] = {}
        self._pending_timers: Dict[int, threading.Timer] = {}

    # ------------------------------------------------------------------
    # accessors
    # ------------------------------------------------------------------

    def all_objects(self) -> Dict[int, dict]:
        with self._lock:
            return dict(self.objects)

    def nodes(self) -> Dict[int, dict]:
        return self._objects_of_type(NODE_TYPE)

    def ports(self) -> Dict[int, dict]:
        return self._objects_of_type(PORT_TYPE)

    def links(self) -> Dict[int, dict]:
        return self._objects_of_type(LINK_TYPE)

    def linked_pairs(self) -> Set[tuple]:
        """(output_port, input_port) pairs currently linked per the
        graph's own state.  May lag a just-made link by a dump cycle."""
        pairs = set()
        for link_data in self.links().values():
            info = link_data.get("info", {})
            out_port = info.get("output-port-id")
            in_port = info.get("input-port-id")
            if out_port is not None and in_port is not None:
                pairs.add((out_port, in_port))
        return pairs

    def node_props(self, node_id: int) -> dict:
        node = self.objects.get(node_id, {})
        return node.get("info", {}).get("props", {}) if node else {}

    def ports_for_node(self, node_id: int) -> Dict[int, dict]:
        return {
            pid: p
            for pid, p in self.ports().items()
            if p.get("info", {}).get("props", {}).get("node.id") == node_id
        }

    def node_id_by_name(self, name: str) -> Optional[int]:
        with self._lock:
            for oid, obj in self.objects.items():
                if obj.get("type") != NODE_TYPE:
                    continue
                if obj.get("info", {}).get("props", {}).get("node.name") == name:
                    return int(oid)
        return None

    def has_node_with_name(self, name: str) -> bool:
        return self.node_id_by_name(name) is not None

    def names_with_prefix(self, prefix: str) -> List[str]:
        with self._lock:
            found = []
            for obj in self.objects.values():
                if obj.get("type") != NODE_TYPE:
                    continue
                name = obj.get("info", {}).get("props", {}).get("node.name")
                if name and name.startswith(prefix):
                    found.append(name)
            return found

    def _objects_of_type(self, type_name: str) -> Dict[int, dict]:
        with self._lock:
            return {
                int(oid): obj
                for oid, obj in self.objects.items()
                if obj.get("type") == type_name
            }

    # ------------------------------------------------------------------
    # event registration
    # ------------------------------------------------------------------

    def on_node_created(self, cb: NodeCreatedCallback) -> None:
        self._node_created_callbacks.append(cb)

    def on_node_removed(self, cb: NodeRemovedCallback) -> None:
        self._node_removed_callbacks.append(cb)

    def on_change(self, cb: OnChange) -> None:
        self._on_change_callbacks.append(cb)

    def on_initial_sync(self, cb: OnChange) -> None:
        """Fires exactly once, after the first full pw-dump snapshot has
        been applied.  Register before start()."""
        self._initial_sync_callbacks.append(cb)

    # ------------------------------------------------------------------
    # link control
    # ------------------------------------------------------------------

    def connect(self, output_port_id: int, input_port_id: int) -> bool:
        """Link two ports.  Idempotent: an existing link is a silent
        no-op; a vanished port (normal churn race) logs quietly and is a
        no-op; only a genuine failure raises LinkError."""
        result = subprocess.run(
            [*self._link_command, str(output_port_id), str(input_port_id)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        if "file exists" in lowered:
            return False
        if "no such" in lowered or "not found" in lowered:
            logger.debug(
                "pw-link %s -> %s: port(s) no longer exist (%s)",
                output_port_id,
                input_port_id,
                stderr,
            )
            return False
        raise LinkError(
            f"pw-link {output_port_id} -> {input_port_id} failed: "
            f"{stderr or result.stdout.strip() or f'exit status {result.returncode}'}"
        )

    def disconnect(self, output_port_id: int, input_port_id: int) -> bool:
        """Unlink two ports.  Idempotent."""
        result = subprocess.run(
            [*self._unlink_command, str(output_port_id), str(input_port_id)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True
        stderr = (result.stderr or "").strip()
        lowered = stderr.lower()
        if "no such" in lowered or "not linked" in lowered or "file exists" in lowered:
            return False
        raise LinkError(
            f"pw-link -d {output_port_id} -> {input_port_id} failed: "
            f"{stderr or result.stdout.strip() or f'exit status {result.returncode}'}"
        )

    # ------------------------------------------------------------------
    # leftover-object cleanup (crash recovery)
    # ------------------------------------------------------------------

    def reap_stale_for_names(self, markers: Sequence[str]) -> int:
        """Best-effort removal of anything a previous (uncleanly-killed)
        daemon left behind under ``markers``, before this daemon creates
        its own objects with those names.

        Every object this project creates is named after (or launched
        with a --target pointing at) a ``backing_node_name``, so any live
        node already carrying one of these markers - or any pw-loopback /
        pw-cat helper process still running against them - can only be a
        leftover from an earlier run.  Leftovers would otherwise collide
        with the fresh objects we are about to create, and PatchSpace's
        name-based sync would route through whichever copy it matched
        first.

        Name matching is prefix-based so one marker per
        ``backing_node_name`` covers every ``{name}_in``/``{name}_out``
        sibling.  Purely best-effort; every step is guarded.  Returns how
        many stale objects/processes were cleaned up."""
        markers = [m for m in markers if m]
        if not markers:
            return 0
        swept = self._terminate_orphan_helpers(set(markers))
        stale, owner_pids = self._snapshot_stale(markers)
        # Kill the actual owning process first, not just the node id.
        # Per pwproc.py's ownership model this is the one teardown path
        # the server always honors atomically and completely - a client
        # disconnect drops every object that client holds (a module's
        # capture *and* playback streams together, all its dummies'
        # keepalives, etc.) in one go, which a foreign "destroy <id>"
        # below can't guarantee for anything but the single id named.
        # pw-cli sessions in particular can't be found by
        # _terminate_orphan_helpers (their argv carries no marker - the
        # node name is only ever sent over stdin after spawn, see
        # pwproc.OwnedPwNode.create), so this pid correlation is the
        # only way to reap them at all.
        my_pid = os.getpid()
        killed_owners = False
        for pid in owner_pids:
            if pid == my_pid:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                swept += 1
                killed_owners = True
                logger.warning("Reaped orphaned owning process pid %s", pid)
            except ProcessLookupError:
                pass
            except PermissionError as exc:
                logger.warning("Failed to reap orphaned owner pid %s: %s", pid, exc)
        if killed_owners:
            # Give the server a moment to process the disconnect before
            # the fallback destroy pass below re-dumps/acts on names
            # that should already be gone.
            _time.sleep(0.2)
        # Fallback for whatever the pid correlation didn't catch (e.g.
        # a client whose application.process.id wasn't reported, or a
        # node whose owning client already exited on its own).
        swept += self._destroy_nodes(stale)
        # Wait for the graph snapshot to actually drop the reaped
        # objects before returning.  If the caller immediately creates a
        # replacement with the same name, PipeWire can hand the new node
        # the just-freed id, and a lagging node-removed event for the old
        # object then matches (by id) the new backing and tears it down -
        # the "live object disappeared while alive" reaping thrash.
        self._wait_markers_gone(markers, timeout=3.0)
        return swept

    def _wait_markers_gone(self, markers: Sequence[str],
                           timeout: float = 3.0) -> bool:
        """Poll until no live node name starts with any marker (or the
        timeout elapses).  Best-effort; used by reap_stale_for_names to
        let the removal events drain before a same-named replacement is
        created."""
        deadline = _time.monotonic() + timeout
        while _time.monotonic() < deadline:
            if not self._live_names_matching(markers):
                return True
            _time.sleep(0.05)
        return False

    def _live_names_matching(self, markers: Sequence[str]) -> list:
        try:
            nodes = self.nodes()
        except Exception:
            return []
        hits = []
        for data in nodes.values():
            name = data.get("info", {}).get("props", {}).get("node.name", "")
            if name and any(_name_matches_marker(name, m) for m in markers):
                hits.append(name)
        return hits

    def _terminate_orphan_helpers(self, markers: Set[str]) -> int:
        """SIGTERM every pw-loopback / pw-cat helper whose command line
        references one of ``markers`` and which we did not spawn.  A
        crashed daemon's helpers get reparented to init and keep running
        forever; they own nothing once their nodes are destroyed, but
        they linger and can race a later recreation."""
        my_pid = os.getpid()
        reaped = 0
        for pid in self._iter_pids():
            if pid == my_pid:
                continue
            try:
                if self._read_ppid(pid) == my_pid:
                    continue  # a live helper we spawned
                cmdline = self._read_cmdline(pid)
            except (OSError, ValueError, IndexError):
                continue
            if not cmdline:
                continue
            prog = os.path.basename(cmdline[0])
            if prog not in ("pw-loopback", "pw-cat"):
                continue
            args_text = " ".join(cmdline)
            if not any(marker in args_text for marker in markers):
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                reaped += 1
                logger.warning("Reaped orphaned %s helper pid %s", prog, pid)
            except (ProcessLookupError, PermissionError) as exc:
                logger.warning("Failed to reap orphaned helper pid %s: %s", pid, exc)
        return reaped

    def _snapshot_stale(
        self, markers: Sequence[str]
    ) -> Tuple[Dict[int, str], Set[int]]:
        """One-shot full pw-dump -> ({node_id: name} for nodes whose
        name starts with any marker, {pid, ...} of the OS processes
        that own those nodes' client connections).  Returns ({}, set())
        if the dump fails (cleanup is best-effort).

        The pid set is what makes real cleanup possible for anything
        spawned as a bare ``pw-cli`` session (every filter-chain/
        create-node backing this project owns): those sessions can't be
        found by matching argv the way _terminate_orphan_helpers finds
        pw-loopback/pw-cat, since the node name is only ever sent to
        pw-cli over stdin after it's already running, never on its
        command line.  PipeWire clients self-report the PID that
        connected them as ``application.process.id``, so cross-
        referencing each stale node's ``client.id`` against the
        matching Client object's props recovers it."""
        command = list(self._dump_command)
        if "-m" in command:
            command.remove("-m")
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("pw-dump failed during stale-object cleanup: %s", exc)
            return {}, set()
        if result.returncode != 0:
            logger.warning(
                "pw-dump failed during stale-object cleanup: %s",
                (result.stderr or result.stdout).strip(),
            )
            return {}, set()
        try:
            objects = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            logger.warning("Could not parse pw-dump output: %s", exc)
            return {}, set()
        if not isinstance(objects, list):
            objects = [objects]

        client_pids: Dict[int, int] = {}
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != CLIENT_TYPE:
                continue
            client_id = obj.get("id")
            pid = obj.get("info", {}).get("props", {}).get("application.process.id")
            if client_id is None or pid is None:
                continue
            try:
                client_pids[client_id] = int(pid)
            except (TypeError, ValueError):
                continue

        found: Dict[int, str] = {}
        pids: Set[int] = set()
        for obj in objects:
            if not isinstance(obj, dict) or obj.get("type") != NODE_TYPE:
                continue
            node_id = obj.get("id")
            props = obj.get("info", {}).get("props", {})
            name = props.get("node.name")
            if node_id is None or not name:
                continue
            if not any(_name_matches_marker(name, m) for m in markers):
                continue
            found[node_id] = name
            pid = client_pids.get(props.get("client.id"))
            if pid is not None:
                pids.add(pid)
        return found, pids

    def _destroy_nodes(self, nodes: Dict[int, str]) -> int:
        destroyed = 0
        for node_id, name in nodes.items():
            try:
                result = subprocess.run(
                    [*self._pw_cli_command, "destroy", str(node_id)],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                logger.warning("Failed to destroy stale node %r: %s", name, exc)
                continue
            if result.returncode == 0:
                destroyed += 1
                logger.info("Destroyed stale leftover node %r (id %s)", name, node_id)
            else:
                logger.warning(
                    "Failed to destroy stale node %r (id %s): %s",
                    name,
                    node_id,
                    (result.stderr or result.stdout).strip(),
                )
        return destroyed

    @staticmethod
    def _iter_pids() -> List[int]:
        try:
            entries = os.listdir("/proc")
        except OSError:
            return []
        return [int(e) for e in entries if e.isdigit()]

    @staticmethod
    def _read_cmdline(pid: int) -> List[str]:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read()
        return [p.decode("utf-8", errors="replace") for p in raw.split(b"\0") if p]

    @staticmethod
    def _read_ppid(pid: int) -> int:
        with open(f"/proc/{pid}/stat", "rb") as f:
            raw = f.read()
        after_comm = raw.rfind(b")")
        fields = raw[after_comm + 1 :].split()
        return int(fields[1])

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("PipewireGraph already started")
        try:
            self._proc = subprocess.Popen(
                self._dump_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise ProcessStartError(f"failed to start {self._dump_command!r}: {exc}") from exc
        self._stopping.clear()
        self._thread = threading.Thread(target=self._read_loop, daemon=True)
        self._thread.start()

    def stop(self, timeout: Optional[float] = 5.0) -> None:
        self._stopping.set()
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait(timeout=1)
        if self._thread:
            self._thread.join(timeout=timeout)
        self._proc = None
        self._thread = None

        with self._lock:
            timers = list(self._pending_timers.values())
            self._pending_timers.clear()
            self._pending_nodes.clear()
            self._pending_created_at.clear()
        for t in timers:
            t.cancel()

        self._initial_dump_received = False

    def __enter__(self) -> "PipewireGraph":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # internals: the read loop
    # ------------------------------------------------------------------

    def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        buf = ""
        decoder = json.JSONDecoder()
        text_decoder = codecs.getincrementaldecoder("utf-8")()
        fd = self._proc.stdout.fileno()
        try:
            while not self._stopping.is_set():
                chunk = os.read(fd, self._read_chunk_size)
                if not chunk:
                    break
                buf += text_decoder.decode(chunk)
                buf = self._drain_buffer(buf, decoder)
            if not self._stopping.is_set():
                self._report_error(
                    ProcessExitedError(f"{self._dump_command!r} exited unexpectedly")
                )
        except Exception as exc:
            if not self._stopping.is_set():
                self._report_error(exc)

    def _drain_buffer(self, buf: str, decoder: json.JSONDecoder) -> str:
        while True:
            stripped = buf.lstrip()
            if not stripped:
                return ""
            if stripped.startswith("["):
                try:
                    obj, idx = decoder.raw_decode(stripped)
                    consumed = len(buf) - len(stripped) + idx
                    buf = buf[consumed:]
                    is_initial = not self._initial_dump_received
                    self._initial_dump_received = True
                    self._apply_batch(obj if isinstance(obj, list) else [obj])
                    if is_initial:
                        self._fire_initial_sync()
                    continue
                except json.JSONDecodeError:
                    return buf
            try:
                obj, idx = decoder.raw_decode(stripped)
                consumed = len(buf) - len(stripped) + idx
                buf = buf[consumed:]
                self._apply_batch([obj])
            except json.JSONDecodeError:
                return buf

    def _report_error(self, exc: Exception) -> None:
        if self.on_error:
            self.on_error(exc)
        else:
            logger.error("pipewire_graph error: %s", exc)

    def _fire_initial_sync(self) -> None:
        for cb in self._initial_sync_callbacks:
            try:
                cb(self)
            except Exception as exc:
                logger.error("Error in initial_sync callback: %s", exc)

    def _apply_batch(self, batch) -> None:
        if not isinstance(batch, list):
            batch = [batch]
        with self._lock:
            old_nodes = dict(self._previous_nodes)
            touched_port_nodes: Set[int] = set()
            for item in batch:
                if not isinstance(item, dict) or "id" not in item:
                    continue
                oid = item["id"]
                removed = ("info" in item and item["info"] is None) or (
                    "props" in item and item.get("props") is None and "info" not in item
                )
                if removed:
                    existing = self.objects.get(oid)
                    if existing and existing.get("type") == PORT_TYPE:
                        pnode = existing.get("info", {}).get("props", {}).get("node.id")
                        if pnode is not None:
                            touched_port_nodes.add(pnode)
                    self.objects.pop(oid, None)
                else:
                    existing = self.objects.setdefault(oid, {})
                    existing.update(item)
                    if existing.get("type") == PORT_TYPE:
                        pnode = existing.get("info", {}).get("props", {}).get("node.id")
                        if pnode is not None:
                            touched_port_nodes.add(pnode)
            new_nodes = self._objects_of_type(NODE_TYPE)
            self._previous_nodes = dict(new_nodes)

        for node_id in touched_port_nodes:
            self._touch_pending_node(node_id)
        self._detect_and_fire_node_events(old_nodes, new_nodes)
        for cb in self._on_change_callbacks:
            try:
                cb(self)
            except Exception as exc:
                logger.error("Error in on_change callback: %s", exc)

    def _detect_and_fire_node_events(self, old_nodes, new_nodes) -> None:
        old_ids = set(old_nodes.keys())
        new_ids = set(new_nodes.keys())
        for node_id in new_ids - old_ids:
            self._stage_pending_node(node_id, new_nodes[node_id])
        for node_id in old_ids - new_ids:
            self._cancel_pending_node(node_id)
            # Pass the node's last-known data too: PipeWire reuses node
            # ids after a node is destroyed, so a consumer matching a
            # removal by id alone can tear down an unrelated object that
            # has since taken the id. The name lets it verify identity.
            for cb in self._node_removed_callbacks:
                try:
                    cb(node_id, old_nodes.get(node_id, {}))
                except Exception as exc:
                    logger.error("Error in node_removed callback: %s", exc)
        for node_id in old_ids & new_ids:
            if old_nodes[node_id] == new_nodes[node_id]:
                continue
            with self._lock:
                is_pending = node_id in self._pending_nodes
                if is_pending:
                    self._pending_nodes[node_id] = new_nodes[node_id]
            if is_pending:
                self._touch_pending_node(node_id)

    # ------------------------------------------------------------------
    # node-ready staging
    # ------------------------------------------------------------------

    def _stage_pending_node(self, node_id: int, node_data: dict) -> None:
        with self._lock:
            self._pending_nodes[node_id] = node_data
            self._pending_created_at[node_id] = _time.monotonic()
        self._touch_pending_node(node_id)

    def _touch_pending_node(self, node_id: int) -> None:
        with self._lock:
            if node_id not in self._pending_nodes:
                return
            created_at = self._pending_created_at.get(node_id, _time.monotonic())
            remaining = self._node_ready_max_wait - (_time.monotonic() - created_at)
            delay = 0.0 if remaining <= 0 else min(self._node_ready_debounce, remaining)
            old = self._pending_timers.pop(node_id, None)
            if old:
                old.cancel()
            timer = threading.Timer(delay, self._fire_pending_node, args=(node_id,))
            timer.daemon = True
            self._pending_timers[node_id] = timer
            timer.start()

    def _fire_pending_node(self, node_id: int) -> None:
        with self._lock:
            node_data = self._pending_nodes.pop(node_id, None)
            self._pending_created_at.pop(node_id, None)
            self._pending_timers.pop(node_id, None)
        if node_data is None:
            return
        with self._lock:
            latest = self.objects.get(node_id, node_data)
        for cb in self._node_created_callbacks:
            try:
                cb(node_id, latest)
            except Exception as exc:
                logger.error("Error in node_created callback: %s", exc)

    def _cancel_pending_node(self, node_id: int) -> None:
        with self._lock:
            timer = self._pending_timers.pop(node_id, None)
            self._pending_nodes.pop(node_id, None)
            self._pending_created_at.pop(node_id, None)
        if timer:
            timer.cancel()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def on_initial(g):
        logger.info("Initial dump: %d nodes, %d ports", len(g.nodes()), len(g.ports()))

    graph = PipewireGraph()
    graph.on_initial_sync(on_initial)
    with graph:
        try:
            while True:
                _time.sleep(1)
        except KeyboardInterrupt:
            pass
