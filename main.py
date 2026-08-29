#!/usr/bin/env python3
"""
main.py - Patchbay bootstrap. Wires a PipewireGraph up to a RuleRouter
and keeps the process alive. Rule editing (add/remove) can happen at
any time via router.add_rule()/router.remove_rule() - this is where a
future GUI/CLI would hook in.

Startup sequence:
  1. PipewireGraph(virtual_sink_name="PatchBay") creates a "PatchBay"
     null-audio-sink via pw-cli right before pw-dump starts, so it's
     already present in the very first full-graph snapshot. It's torn
     down again automatically when the graph stops (see the `with
     graph:` block below / graph.stop()).
  2. Register on_initial_sync BEFORE start() - it fires exactly once,
     right after pw-dump's first full-graph snapshot has been applied.
  3. That callback logs known audio devices (so you can copy exact
     node.name values into rules) and runs router.sync() once against
     the complete graph.
  4. From then on, RuleRouter's own on_change hook (auto_sync_on_change,
     enabled by default) keeps things in sync incrementally as nodes/
     ports/links come and go - no polling required.

TEST HOTKEY: while this is running, press spacebar in this terminal to
clear all rules (and disconnect everything they created). Remove
_start_key_listener() once you don't need it anymore.
"""
import logging
import select
import sys
import termios
import threading
import time
import tty

from pwgraph import PipewireGraph
from pwroute import RuleRouter

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

STARTUP_RULES = [
    {
        "id": "s8ie65df",
        "rule": "connect",
        "sourceFilters": [
            {
                "id": None,
                "name": "LibreWolf",
                "mediaName": None,
            },
        ],
        "sinkFilters": [
            {
                "id": None,
                "name": "Chromium input",
                "type": None,
            },
        ],
    },
    # {
    #    "id": "s8ie64df",
    #    "rule": "connect",
    #    "sourceFilters": [
    #        {
    #            "id": None,
    #            "name": "LibreWolf",
    #            "mediaNameRegex": "youtube",
    #        },
    #    ],
    #    "sinkFilters": [
    #        {
    #            "id": None,
    #            "nameRegex": "94.4B.F8.88.EC.81",
    #            "type": None,
    #        },
    #    ],
    # },
    {
        "id": "a8ie64df",
        "rule": "connect",
        "sourceFilters": [
            {
                "id": None,
                "name": "PatchBay",
                "mediaNameRegex": None,
            },
        ],
        "sinkFilters": [
            {
                "id": None,
                "description": "Ryzen HD Audio Controller Analog Stereo",
                "type": None,
            },
            {
                "id": None,
                "descriptionRegex": "TOZO",
                "type": None,
            },
        ],
    },
    # Example: route everything played into the "PatchBay" virtual
    # sink onward to your real hardware output. Uncomment and fill in
    # the real node.name below - check the "Known audio devices" log
    # line printed right after startup for the exact value, rather
    # than guessing at it.
    # {
    #     "id": "patchbay-out",
    #     "rule": "connect",
    #     "sourceFilters": [
    #         {"id": None, "name": "PatchBay", "mediaName": None},
    #     ],
    #     "sinkFilters": [
    #         {"id": None, "name": "PUT_YOUR_REAL_OUTPUT_NODE_NAME_HERE", "type": None},
    #     ],
    # },
]


def on_new_audio(node_id, data):
    """Log audio streams only, not every node."""
    props = data.get("info", {}).get("props", {})
    stream_class = props.get("media.class", "")
    name = props.get("application.name") or props.get("node.name")
    media = props.get("media.name")
    if "Stream/" in stream_class:
        print(f"NODE READY: {name} {media} | {stream_class}")


def _log_audio_devices(graph: PipewireGraph) -> None:
    """
    Print every known Audio/Sink and Audio/Source node's exact
    node.name, so you can copy the real value straight into a rule's
    sinkFilters/sourceFilters instead of grepping pw-dump by hand.
    """
    print("Known audio devices (use node.name in sinkFilters/sourceFilters):")
    for node_data in graph.nodes().values():
        props = node_data.get("info", {}).get("props", {})
        media_class = props.get("media.class", "")
        if media_class not in ("Audio/Sink", "Audio/Source"):
            continue
        name = props.get("node.name")
        description = props.get("node.description") or props.get("node.nick") or ""
        print(f"  [{media_class}] node.name={name!r}  ({description})")


def _start_key_listener(
    router: RuleRouter, stop_event: threading.Event
) -> threading.Thread:
    """
    Watches this terminal for a spacebar press and clears all rules when
    it sees one. Only works when stdin is an interactive TTY (POSIX).
    """

    def listen():
        if not sys.stdin.isatty():
            logger.warning("stdin is not a TTY - spacebar test hotkey disabled")
            return
        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not stop_event.is_set():
                ready, _, _ = select.select([sys.stdin], [], [], 0.2)
                if not ready:
                    continue
                ch = sys.stdin.read(1)
                if ch == " ":
                    removed = router.clear_rules()
                    print(
                        f"[TEST] Spacebar pressed - cleared {removed} rule(s) and disconnected their connections"
                    )
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    return thread


def main() -> None:
    # virtual_sink_name="PatchBay" creates a null-audio-sink node named
    # exactly "PatchBay" before pw-dump starts, and destroys it again
    # when the graph stops. Apps can select it as an output device;
    # its monitor ports carry whatever gets played into it, which a
    # rule can then route onward to a real hardware output (see the
    # commented example rule above).
    graph = PipewireGraph(virtual_sink_name="PatchBay")
    graph.on_node_created(on_new_audio)

    router = RuleRouter(graph)
    router.add_rules(STARTUP_RULES)

    initial_sync_done = threading.Event()

    def on_initial_sync(g: PipewireGraph) -> None:
        print(f"Initial graph loaded: {len(g.nodes())} nodes, {len(g.ports())} ports")
        _log_audio_devices(g)
        print("Performing initial sync...")
        router.sync()
        print("Initial sync complete")
        initial_sync_done.set()

    # Must register before start() - this fires exactly once, right
    # after pw-dump's first full-graph snapshot (the initial JSON
    # array) has been applied to the graph.
    graph.on_initial_sync(on_initial_sync)

    stop_event = threading.Event()

    with graph:
        print("Waiting for initial graph state...")

        # Belt-and-suspenders timeout in case pw-dump never emits an
        # initial array for some reason (e.g. empty graph / odd
        # pw-dump version) - don't hang forever.
        if not initial_sync_done.wait(timeout=10):
            print(
                "Warning: no initial snapshot after 10s - continuing without "
                "initial sync; auto-sync will still catch changes as they happen"
            )

        key_thread = _start_key_listener(router, stop_event)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            stop_event.set()
            key_thread.join(timeout=1)
            # Clean up all connections before exit
            print("Cleaning up connections...")
            router.clear_rules()


if __name__ == "__main__":
    main()
