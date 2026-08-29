"""
pwgraph.py

Single-file library for building a live-updating model of the PipeWire
graph, backed by `pw-dump -m`.
"""

from __future__ import annotations

import codecs
import json
import logging
import os
import re
import select
import subprocess
import threading
import time as _time
from typing import Any, Callable, Dict, Optional, Sequence, Set, Tuple

logger = logging.getLogger(__name__)

NODE_TYPE = "PipeWire:Interface:Node"
PORT_TYPE = "PipeWire:Interface:Port"
LINK_TYPE = "PipeWire:Interface:Link"

DEFAULT_MEDIA_CLASS_MAP: Dict[str, str] = {
    "Audio/Sink": "hardware_output",
    "Video/Sink": "hardware_output",
    "Audio/Source": "hardware_input",
    "Video/Source": "hardware_input",
}

OnChange = Callable[["PipewireGraph"], None]
OnError = Callable[[Exception], None]
NodeClassifier = Callable[[dict], str]
NodeCreatedCallback = Callable[[int, dict], None]
NodeRemovedCallback = Callable[[int], None]
NodeChangedCallback = Callable[[int, dict, dict], None]


class GraphMonitorError(Exception):
    pass


class ProcessStartError(GraphMonitorError):
    pass


class ProcessExitedError(GraphMonitorError):
    pass


class LinkError(GraphMonitorError):
    """
    Raised only for genuine pw-link failures. "Already linked" (connect)
    and "already unlinked" (disconnect) are treated as success and never
    raise - PipeWire's own graph dump can lag a link/unlink we just made
    by a cycle or two, so callers shouldn't have to special-case that.
    """


def _default_classifier(media_class_map: Dict[str, str]) -> NodeClassifier:
    def classify(node: dict) -> str:
        mclass = node.get("info", {}).get("props", {}).get("media.class", "")
        if mclass in media_class_map:
            return media_class_map[mclass]
        if mclass.startswith("Stream/Output"):
            return "app_output"
        if mclass.startswith("Stream/Input"):
            return "app_input"
        return "other"

    return classify


class PipewireGraph:
    def __init__(
        self,
        on_change: Optional[OnChange] = None,
        on_error: Optional[OnError] = None,
        classifier: Optional[NodeClassifier] = None,
        media_class_map: Optional[Dict[str, str]] = None,
        dump_command: Sequence[str] = ("pw-dump", "-m"),
        link_command: Sequence[str] = ("pw-link",),
        unlink_command: Sequence[str] = ("pw-link", "-d"),
        read_chunk_size: int = 65536,  # Increased buffer size
        node_ready_debounce: float = 0.15,
        node_ready_max_wait: float = 2.0,
        virtual_sink_name: Optional[str] = None,
        virtual_sink_channels: Sequence[str] = ("FL", "FR"),
        virtual_sink_set_default: bool = True,
        pw_cli_command: Sequence[str] = ("pw-cli",),
        pw_cli_settle: float = 0.3,
        wpctl_command: Sequence[str] = ("wpctl",),
    ):
        self.on_error = on_error

        merged_map = dict(DEFAULT_MEDIA_CLASS_MAP)
        if media_class_map:
            merged_map.update(media_class_map)
        self._classify = classifier or _default_classifier(merged_map)

        self._dump_command = list(dump_command)
        self._link_command = list(link_command)
        self._unlink_command = list(unlink_command)
        self._read_chunk_size = read_chunk_size

        self._node_ready_debounce = node_ready_debounce
        self._node_ready_max_wait = node_ready_max_wait

        # Optional virtual sink ("PatchBay"-style dummy output). If
        # virtual_sink_name is set, a null-audio-sink node with that
        # exact node.name is requested via `pw-cli create-node` right
        # before start() spins up pw-dump (so it's already present in
        # the very first full-graph snapshot instead of showing up as
        # a spurious "node created" event a moment later), and torn
        # down again in stop() via _destroy_virtual_sink(). Apps can
        # select it as their output device; its monitor ports carry
        # whatever gets played into it, which a RuleRouter rule can
        # then route onward to a real hardware sink.
        #
        # NOTE on pw-cli: a node created via `pw-cli create-node` is
        # NOT a server-resident object - pw-cli creates it inside its
        # own client process and exports it to the server over that
        # process's connection. The node's lifetime is tied to that
        # specific connection: as soon as the pw-cli process that
        # created it disconnects, the server tears the node down along
        # with it. A one-shot ("open, send command, wait briefly, then
        # quit") invocation therefore destroys the node moments after
        # creating it - sometimes just before, sometimes just after,
        # the next pw-dump snapshot, which is why that approach looked
        # like it worked intermittently instead of reliably failing or
        # succeeding.
        #
        # The fix used here: the pw-cli process that creates the node
        # is kept RUNNING for as long as the virtual sink should exist
        # (tracked in self._virtual_sink_proc), and is only terminated
        # in _destroy_virtual_sink(), which is what actually tears the
        # node down.
        #
        # Because pw-cli's own reply text isn't a reliable way to learn
        # the new node's id (format varies by version), the id is
        # authoritatively resolved by looking up `virtual_sink_name` in
        # the graph right after the first pw-dump snapshot arrives -
        # see _resolve_virtual_sink_on_initial_sync().
        #
        # If virtual_sink_set_default is True (the default), the sink
        # is also made the system default output via `wpctl
        # set-default` once its id is resolved. Whatever was default
        # beforehand is remembered (via `wpctl inspect
        # @DEFAULT_AUDIO_SINK@`) and restored when the virtual sink is
        # torn down.
        self._virtual_sink_name = virtual_sink_name
        self._virtual_sink_channels = list(virtual_sink_channels)
        self._virtual_sink_set_default = virtual_sink_set_default
        self._pw_cli_command = list(pw_cli_command)
        self._pw_cli_settle = pw_cli_settle
        self._wpctl_command = list(wpctl_command)
        self._virtual_sink_id: Optional[int] = None
        self._virtual_sink_proc: Optional[subprocess.Popen] = None
        self._previous_default_sink_id: Optional[int] = None

        self.objects: Dict[Any, dict] = {}
        self._lock = threading.RLock()
        self._proc: Optional[subprocess.Popen] = None
        self._thread: Optional[threading.Thread] = None
        self._stopping = threading.Event()

        self._node_created_callbacks: list[NodeCreatedCallback] = []
        self._node_removed_callbacks: list[NodeRemovedCallback] = []
        self._node_changed_callbacks: list[NodeChangedCallback] = []
        self._on_change_callbacks: list[OnChange] = []
        if on_change is not None:
            self._on_change_callbacks.append(on_change)

        # Fires exactly once, after the first full pw-dump snapshot
        # (the initial JSON array) has been applied to self.objects.
        # This is the reliable "graph is built, safe to do an initial
        # reconciliation pass" signal - no polling/guessing required.
        self._initial_dump_received = False
        self._initial_sync_callbacks: list[OnChange] = []

        self._previous_nodes: Dict[int, dict] = {}

        self._pending_nodes: Dict[int, dict] = {}
        self._pending_created_at: Dict[int, float] = {}
        self._pending_timers: Dict[int, threading.Timer] = {}

    # ---------- public accessors ----------

    def all_objects(self) -> Dict[Any, dict]:
        with self._lock:
            return dict(self.objects)

    def nodes(self) -> Dict[int, dict]:
        return self._objects_of_type(NODE_TYPE)

    def ports(self) -> Dict[int, dict]:
        return self._objects_of_type(PORT_TYPE)

    def links(self) -> Dict[int, dict]:
        return self._objects_of_type(LINK_TYPE)

    def linked_pairs(self) -> Set[tuple]:
        """Set of (output_port_id, input_port_id) currently linked,
        per the graph's own state. May lag a just-made link by a
        dump cycle - use connect()'s return value, not this, to decide
        whether a call actually did work."""
        pairs = set()
        for link_data in self.links().values():
            info = link_data.get("info", {})
            out_port = info.get("output-port-id")
            in_port = info.get("input-port-id")
            if out_port is not None and in_port is not None:
                pairs.add((out_port, in_port))
        return pairs

    def node_kind(self, node: dict) -> str:
        return self._classify(node)

    def ports_for_node(self, node_id: int) -> Dict[int, dict]:
        return {
            pid: p
            for pid, p in self.ports().items()
            if p.get("info", {}).get("props", {}).get("node.id") == node_id
        }

    def _objects_of_type(self, type_name: str) -> Dict[Any, dict]:
        with self._lock:
            return {
                oid: o for oid, o in self.objects.items() if o.get("type") == type_name
            }

    # ---------- event registration ----------

    def on_node_created(self, callback: NodeCreatedCallback) -> None:
        self._node_created_callbacks.append(callback)

    def on_node_removed(self, callback: NodeRemovedCallback) -> None:
        self._node_removed_callbacks.append(callback)

    def on_node_changed(self, callback: NodeChangedCallback) -> None:
        self._node_changed_callbacks.append(callback)

    def on_change(self, callback: OnChange) -> None:
        self._on_change_callbacks.append(callback)

    def on_initial_sync(self, callback: OnChange) -> None:
        """
        Register a callback that fires exactly once, right after the
        first full pw-dump snapshot has been applied to the graph. Use
        this for "build the graph, then do one reconciliation pass"
        startup logic instead of polling nodes()/ports() in a sleep
        loop. If the initial dump has already arrived by the time you
        call this (e.g. you registered late), the callback will NOT be
        re-fired retroactively - register before start() to be safe.
        """
        self._initial_sync_callbacks.append(callback)

    # ---------- pw-cli helpers ----------

    def _read_available(self, proc: subprocess.Popen, timeout: float) -> str:
        """
        Non-blocking read of whatever text a still-running subprocess
        has written to stdout within `timeout` seconds. We can't use
        proc.communicate() here - it blocks until the process exits,
        but the pw-cli session is deliberately kept running as the
        long-lived owner of the node it created (see the class-level
        NOTE on pw-cli, above _create_virtual_sink).
        """
        if proc.stdout is None:
            return ""
        fd = proc.stdout.fileno()
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

    # ---------- virtual sink lifecycle ----------

    def _create_virtual_sink(self) -> None:
        """
        Create a null-audio-sink node named self._virtual_sink_name by
        sending `pw-cli create-node` to an interactive pw-cli session
        that is then kept RUNNING for as long as the virtual sink
        should exist.

        IMPORTANT: a node created via `pw-cli create-node` is owned by
        the pw-cli client connection that created it - pw-cli exports
        it to the server over its own core connection, rather than the
        server hosting it independently. If that pw-cli process exits,
        the server tears the node down along with it.

        This was the actual cause of the virtual sink "disappearing"
        in earlier versions of this code: pw-cli was run one-shot (or
        kept open only briefly to "let it settle") and then quit,
        which destroyed the node moments after creating it - sometimes
        just before, sometimes just after, the next pw-dump snapshot,
        depending on timing. That's why it looked like it worked
        intermittently rather than reliably failing or succeeding.

        The fix: keep this pw-cli process alive for the whole lifetime
        of the virtual sink, and only terminate it in
        _destroy_virtual_sink() when the node should actually go away.
        """
        if not self._virtual_sink_name:
            return

        position = ",".join(self._virtual_sink_channels)
        props = (
            "{ factory.name=support.null-audio-sink "
            f"node.name={self._virtual_sink_name} "
            "node.description=PatchBay Virtual Sink "
            "media.class=Audio/Sink "
            f"audio.position=[{position}] }}"
        )

        try:
            proc = subprocess.Popen(
                self._pw_cli_command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except OSError as exc:
            logger.warning(
                "Failed to start pw-cli for virtual sink %r: %s",
                self._virtual_sink_name,
                exc,
            )
            return

        try:
            assert proc.stdin is not None
            proc.stdin.write(f"create-node adapter {props}\n")
            proc.stdin.flush()
        except OSError as exc:
            logger.warning(
                "Failed to send create-node to pw-cli for virtual sink %r: %s",
                self._virtual_sink_name,
                exc,
            )
            proc.kill()
            return

        output = self._read_available(proc, self._pw_cli_settle)

        if proc.poll() is not None:
            # pw-cli exited on its own (e.g. bad command syntax) - there's
            # no long-lived owner, so any node it briefly created is
            # already gone.
            stderr = proc.stderr.read() if proc.stderr is not None else ""
            logger.warning(
                "pw-cli exited unexpectedly while creating virtual sink %r "
                "(exit code %s): %s",
                self._virtual_sink_name,
                proc.returncode,
                (stderr or output).strip(),
            )
            return

        # Keep this process running - it's the owner of the exported
        # node for as long as the virtual sink should exist.
        self._virtual_sink_proc = proc

        match = re.search(r"\bid[:\s]+(\d+)", output) or re.search(
            r"^\s*(\d+)\s*$", output, re.MULTILINE
        )
        if match:
            self._virtual_sink_id = int(match.group(1))
            logger.info(
                "Requested virtual sink %r (pw-cli reported id %s, will confirm "
                "from graph)",
                self._virtual_sink_name,
                self._virtual_sink_id,
            )
        else:
            logger.info(
                "Requested virtual sink %r (id will be confirmed from the graph)",
                self._virtual_sink_name,
            )

    def _resolve_virtual_sink_on_initial_sync(self) -> None:
        """
        Called once, right after the first pw-dump snapshot is applied.
        Authoritatively resolves self._virtual_sink_id by looking up
        virtual_sink_name in the now-populated graph (rather than
        trusting pw-cli's own reply text, which isn't reliably
        parseable across versions/invocation modes). Falls back to the
        id pw-cli itself reported (if any) in case node.name didn't end
        up set the way we expected, and as a last resort logs every
        node's id/name so a mismatch is visible instead of silent.
        """
        if not self._virtual_sink_name:
            return

        found_id = None
        for node_id, node_data in self.nodes().items():
            props = node_data.get("info", {}).get("props", {})
            if props.get("node.name") == self._virtual_sink_name:
                found_id = node_id
                break

        if found_id is None and self._virtual_sink_id is not None:
            # Fall back to the id pw-cli itself reported at creation
            # time, in case node.name isn't what we expected.
            candidate = self.nodes().get(self._virtual_sink_id)
            if candidate is not None:
                props = candidate.get("info", {}).get("props", {})
                actual_name = props.get("node.name")
                logger.warning(
                    "Virtual sink lookup by name failed, but pw-cli-reported id "
                    "%s exists in the graph with node.name=%r (expected %r) - "
                    "using it anyway.",
                    self._virtual_sink_id,
                    actual_name,
                    self._virtual_sink_name,
                )
                found_id = self._virtual_sink_id

        if found_id is None:
            logger.warning(
                "Virtual sink %r did not appear in the initial graph snapshot "
                "under that name or under pw-cli-reported id %s. Current "
                "nodes for comparison:",
                self._virtual_sink_name,
                self._virtual_sink_id,
            )
            for node_id, node_data in sorted(self.nodes().items()):
                props = node_data.get("info", {}).get("props", {})
                logger.warning(
                    "  id=%s node.name=%r media.class=%r factory.name=%r",
                    node_id,
                    props.get("node.name"),
                    props.get("media.class"),
                    props.get("factory.name"),
                )
            return

        if found_id != self._virtual_sink_id:
            self._virtual_sink_id = found_id
            logger.info(
                "Confirmed virtual sink %r as node id %s",
                self._virtual_sink_name,
                found_id,
            )

        if self._virtual_sink_set_default:
            self._set_default_sink()

    def _get_default_sink_id(self) -> Optional[int]:
        try:
            result = subprocess.run(
                [*self._wpctl_command, "inspect", "@DEFAULT_AUDIO_SINK@"],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("Failed to read current default sink via wpctl: %s", exc)
            return None

        if result.returncode != 0:
            logger.warning(
                "wpctl inspect @DEFAULT_AUDIO_SINK@ failed: %s",
                (result.stderr or result.stdout).strip(),
            )
            return None

        match = re.search(r"\bid\s+(\d+)", result.stdout)
        if not match:
            logger.warning(
                "Could not parse default sink id from wpctl output: %r",
                result.stdout.strip(),
            )
            return None
        return int(match.group(1))

    def _set_default_sink(self) -> None:
        """
        Point Wireplumber's default sink at the virtual sink, so apps
        that follow the system default pick it up without the user
        manually reselecting an output device. Remembers whatever was
        default before switching (by id), so _restore_default_sink()
        can put it back when the virtual sink goes away. Requires
        self._virtual_sink_id to already be resolved (see
        _resolve_virtual_sink_on_initial_sync).
        """
        if not self._virtual_sink_name or self._virtual_sink_id is None:
            return

        self._previous_default_sink_id = self._get_default_sink_id()

        try:
            result = subprocess.run(
                [*self._wpctl_command, "set-default", str(self._virtual_sink_id)],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "Failed to set default sink to %r (id %s): %s",
                self._virtual_sink_name,
                self._virtual_sink_id,
                exc,
            )
            return

        if result.returncode == 0:
            logger.info(
                "Set %r (id %s) as the default audio sink",
                self._virtual_sink_name,
                self._virtual_sink_id,
            )
        else:
            logger.warning(
                "wpctl set-default failed for %r (id %s): %s",
                self._virtual_sink_name,
                self._virtual_sink_id,
                (result.stderr or result.stdout).strip(),
            )

    def _restore_default_sink(self) -> None:
        """
        Restore whatever sink was default before we switched to the
        virtual sink (see _set_default_sink). No-op if we never
        recorded a previous default (e.g. virtual_sink_set_default was
        False, or reading the old default failed).
        """
        if self._previous_default_sink_id is None:
            return
        previous_id = self._previous_default_sink_id
        self._previous_default_sink_id = None
        try:
            result = subprocess.run(
                [*self._wpctl_command, "set-default", str(previous_id)],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode != 0:
                logger.warning(
                    "Failed to restore previous default sink (id %s): %s",
                    previous_id,
                    (result.stderr or result.stdout).strip(),
                )
            else:
                logger.info("Restored default sink (id %s)", previous_id)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(
                "Failed to restore previous default sink (id %s): %s", previous_id, exc
            )

    def _destroy_virtual_sink(self) -> None:
        proc = self._virtual_sink_proc
        self._virtual_sink_proc = None
        node_id = self._virtual_sink_id
        self._virtual_sink_id = None

        if proc is None:
            # No owning pw-cli process tracked - still try to restore
            # whatever the default sink was before.
            self._restore_default_sink()
            return

        try:
            if proc.stdin is not None:
                try:
                    if node_id is not None:
                        proc.stdin.write(f"destroy {node_id}\n")
                    proc.stdin.write("quit\n")
                    proc.stdin.flush()
                except OSError:
                    pass
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=1)

        # Disconnecting pw-cli's core connection tears down whatever
        # it exported, whether or not our explicit "destroy" above got
        # through cleanly - so this is logged unconditionally.
        logger.info("Destroyed virtual sink (node id %s)", node_id)

        self._restore_default_sink()

    # ---------- link control ----------

    def connect(self, output_port_id: int, input_port_id: int) -> bool:
        """
        Link two ports with pw-link. Idempotent: if the link already
        exists, this is a silent no-op. Only a genuine, unexplained
        failure raises LinkError.

        Returns True if this call actually created a new link, False if
        the link already existed (still a success, just nothing to do).
        """
        result = subprocess.run(
            [*self._link_command, str(output_port_id), str(input_port_id)],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            return True  # newly created
        stderr = (result.stderr or "").strip()
        if "File exists" in stderr:
            return False  # already linked - success, but nothing new
        raise LinkError(
            f"pw-link {output_port_id} -> {input_port_id} failed: "
            f"{stderr or result.stdout.strip() or f'exit status {result.returncode}'}"
        )

    def disconnect(self, output_port_id: int, input_port_id: int) -> bool:
        """
        Unlink two ports with pw-link -d. Returns True if this call
        actually removed a link, False if it was already gone.
        """
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

    # ---------- lifecycle ----------

    def start(self) -> None:
        if self._proc is not None:
            raise RuntimeError("PipewireGraph already started")

        # Request the virtual sink (if configured) before starting the
        # pw-dump monitor, so it's already present in the very first
        # full-graph snapshot. Its id/default-sink status get confirmed
        # from that snapshot in _resolve_virtual_sink_on_initial_sync().
        self._create_virtual_sink()

        try:
            self._proc = subprocess.Popen(
                self._dump_command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            self._destroy_virtual_sink()
            raise ProcessStartError(
                f"failed to start {self._dump_command!r}: {exc}"
            ) from exc

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

        self._destroy_virtual_sink()

        # Allow a fresh start()/stop() cycle to see a new initial dump.
        self._initial_dump_received = False

    def __enter__(self) -> "PipewireGraph":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # ---------- internals ----------

    def _read_loop(self) -> None:
        assert self._proc is not None
        assert self._proc.stdout is not None

        buf = ""
        decoder = json.JSONDecoder()
        text_decoder = codecs.getincrementaldecoder("utf-8")()
        fd = self._proc.stdout.fileno()

        try:
            # Read all output from pw-dump -m.
            #
            # IMPORTANT: we read the raw fd with os.read(), not
            # file_obj.read(n). A subprocess pipe is a non-interactive
            # stream, and buffered readers (TextIOWrapper/BufferedReader)
            # are documented to keep issuing raw reads on a
            # non-interactive stream until they collect the full n bytes
            # requested (or hit EOF) - they will NOT hand back a
            # complete, already-available JSON array just because one is
            # sitting in the pipe. That caused the initial pw-dump
            # snapshot (often well under read_chunk_size bytes) to sit
            # unprocessed until enough *later* traffic accumulated to
            # cross the threshold - which looked like "initial sync does
            # nothing until some other stream event happens".
            #
            # os.read(fd, n) is a single syscall: it returns as soon as
            # any data (1 to n bytes) is available, which is what we
            # want here since we already do our own message framing via
            # decoder.raw_decode() in _drain_buffer(). We decode the raw
            # bytes ourselves with an incremental UTF-8 decoder so a
            # chunk boundary can never split a multi-byte character
            # (e.g. in a unicode application.name) across two reads.
            while not self._stopping.is_set():
                chunk = os.read(fd, self._read_chunk_size)
                if not chunk:
                    break

                buf += text_decoder.decode(chunk)
                buf = self._drain_buffer(buf, decoder)

            if not self._stopping.is_set():
                self._handle_unexpected_exit()
        except Exception as exc:
            if not self._stopping.is_set():
                self._report_error(exc)

    def _drain_buffer(self, buf: str, decoder: json.JSONDecoder) -> str:
        """
        Drain complete JSON objects from the buffer.
        Handles both individual objects and arrays of objects.
        """
        while True:
            stripped = buf.lstrip()
            if not stripped:
                return ""

            # Check if we're starting an array
            if stripped.startswith("["):
                # Try to parse the entire array
                try:
                    obj, idx = decoder.raw_decode(stripped)
                    consumed = len(buf) - len(stripped) + idx
                    buf = buf[consumed:]

                    # pw-dump -m sends the full existing graph as a
                    # single array exactly once, up front. Everything
                    # after that arrives as individual objects. Treat
                    # the first array we see as "initial snapshot".
                    is_initial = not self._initial_dump_received
                    self._initial_dump_received = True

                    # If obj is a list, process each item
                    if isinstance(obj, list):
                        self._apply_batch(obj)
                    else:
                        self._apply_batch([obj])

                    if is_initial:
                        self._fire_initial_sync()

                    # After an array, there might be more data
                    continue
                except json.JSONDecodeError:
                    # Need more data to complete the array
                    return buf

            # Try to parse individual objects
            try:
                obj, idx = decoder.raw_decode(stripped)
                consumed = len(buf) - len(stripped) + idx
                buf = buf[consumed:]
                self._apply_batch([obj])
            except json.JSONDecodeError:
                # Need more data
                return buf

    def _handle_unexpected_exit(self) -> None:
        stderr = ""
        if self._proc and self._proc.stderr:
            stderr_bytes = self._proc.stderr.read()
            stderr = stderr_bytes.decode("utf-8", errors="replace")
        self._report_error(
            ProcessExitedError(
                f"{self._dump_command!r} exited unexpectedly: {stderr.strip()}"
            )
        )

    def _report_error(self, exc: Exception) -> None:
        if self.on_error:
            self.on_error(exc)
        else:
            logger.error("pipewire_graph error: %s", exc)

    def _fire_initial_sync(self) -> None:
        self._resolve_virtual_sink_on_initial_sync()
        for callback in self._initial_sync_callbacks:
            try:
                callback(self)
            except Exception as e:
                logger.error(f"Error in initial_sync callback: {e}")

    def _apply_batch(self, batch) -> None:
        """Apply a batch of objects to the graph state."""
        if not isinstance(batch, list):
            batch = [batch]

        with self._lock:
            old_nodes = dict(self._previous_nodes)
            touched_port_node_ids: Set[int] = set()

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
                            touched_port_node_ids.add(pnode)
                    self.objects.pop(oid, None)
                else:
                    existing = self.objects.get(oid, {})
                    existing.update(item)
                    self.objects[oid] = existing
                    if existing.get("type") == PORT_TYPE:
                        pnode = existing.get("info", {}).get("props", {}).get("node.id")
                        if pnode is not None:
                            touched_port_node_ids.add(pnode)

            new_nodes = self._objects_of_type(NODE_TYPE)
            self._previous_nodes = dict(new_nodes)

        for node_id in touched_port_node_ids:
            self._touch_pending_node(node_id)

        self._detect_and_fire_node_events(old_nodes, new_nodes)

        for callback in self._on_change_callbacks:
            try:
                callback(self)
            except Exception as e:
                logger.error(f"Error in on_change callback: {e}")

    def _detect_and_fire_node_events(
        self, old_nodes: Dict[int, dict], new_nodes: Dict[int, dict]
    ) -> None:
        old_ids = set(old_nodes.keys())
        new_ids = set(new_nodes.keys())

        created_ids = new_ids - old_ids
        for node_id in created_ids:
            self._stage_pending_node(node_id, new_nodes[node_id])

        removed_ids = old_ids - new_ids
        for node_id in removed_ids:
            self._cancel_pending_node(node_id)
            for callback in self._node_removed_callbacks:
                try:
                    callback(node_id)
                except Exception as e:
                    logger.error(f"Error in node_removed callback: {e}")

        common_ids = new_ids & old_ids
        for node_id in common_ids:
            old_data = old_nodes[node_id]
            new_data = new_nodes[node_id]
            if old_data == new_data:
                continue

            with self._lock:
                is_pending = node_id in self._pending_nodes
                if is_pending:
                    self._pending_nodes[node_id] = new_data

            if is_pending:
                self._touch_pending_node(node_id)
                continue

            for callback in self._node_changed_callbacks:
                try:
                    callback(node_id, old_data, new_data)
                except Exception as e:
                    logger.error(f"Error in node_changed callback: {e}")

    # ---------- node-ready staging ----------

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
            elapsed = _time.monotonic() - created_at
            remaining_to_cap = self._node_ready_max_wait - elapsed
            delay = (
                0.0
                if remaining_to_cap <= 0
                else min(self._node_ready_debounce, remaining_to_cap)
            )

            old_timer = self._pending_timers.get(node_id)
            if old_timer:
                old_timer.cancel()

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

        for callback in self._node_created_callbacks:
            try:
                callback(node_id, latest)
            except Exception as e:
                logger.error(f"Error in node_created callback: {e}")

    def _cancel_pending_node(self, node_id: int) -> None:
        with self._lock:
            timer = self._pending_timers.pop(node_id, None)
            self._pending_nodes.pop(node_id, None)
            self._pending_created_at.pop(node_id, None)
        if timer:
            timer.cancel()


if __name__ == "__main__":
    import time

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    def on_node_created(node_id, node_data):
        name = node_data.get("info", {}).get("props", {}).get("node.name", "unknown")
        print(f"🟢 NODE CREATED (settled): {node_id} - {name}")

    def on_initial_sync(g):
        print(
            f"📦 Initial dump received: {len(g.nodes())} nodes, {len(g.ports())} ports"
        )

    graph = PipewireGraph()
    graph.on_node_created(on_node_created)
    graph.on_initial_sync(on_initial_sync)

    with graph:
        print("🎵 PipeWire Graph Monitor Started")
        print("Waiting for initial graph state...")
        time.sleep(2)

        nodes = graph.nodes()
        ports = graph.ports()
        print(f"Initial state: {len(nodes)} nodes, {len(ports)} ports")

        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("\n👋 Exiting...")
