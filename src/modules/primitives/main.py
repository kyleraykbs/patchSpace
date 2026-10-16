"""
main.py — the primitives module, as a process the daemon runs.

    python -m modules.primitives.main

It attaches over the socket, declares its types (see `types.py`), and then
runs whatever the daemon asks of it: the daemon creates a node (the user
added one in the canvas), this module runs it and reports the values it
computes, so the display can show them.

Nothing here knows about PipeWire, the GUI, or the daemon's own model — the
module owns its nodes and speaks the module API (see `modules/host.py`).
"""

from __future__ import annotations

import logging
import sys
from typing import Any, Dict

from central import module_api as api
from modules.host import ModuleHost
from modules.primitives import types
from modules.primitives.engine import Engine

logger = logging.getLogger(__name__)


class Primitives:
    """The module: an engine, and the host that carries its messages."""

    def __init__(self, socket_path: str = "") -> None:
        # The engine is handed this module's own declarations: which ports
        # are value slots, and what a `choice` accepts, come from one listing
        # (`api.value_slots`) — the same one the daemon derives, so the two
        # sides of the relay cannot disagree (agents/SPACES.md §19).
        self.engine = Engine(types.TYPES)
        self.host = ModuleHost(
            "primitives",
            "1.0",
            socket_path=socket_path or None,
            declarations=types.TYPES,
            menu=types.MENU,
            vocabulary={},
            on_control=self._control,
            on_edge=self._edge,
            on_value=self.apply_value,
            on_spawn=self._spawn,
            on_stop=self._stop,
        )

    def run(self) -> int:
        if not self.host.connect():
            return 3
        # Nothing runs until the daemon asks: the graph the user edits is
        # the daemon's, and it spawns instances as they are created.
        self.host.announce([])
        self.host.run_forever()
        return 0

    # -- what the daemon asks -------------------------------------------

    def _control(self, message: Dict[str, Any]) -> None:
        instance_id = str(message.get("instance_id") or "")
        if self.engine.set_control(instance_id, str(message.get("key") or ""),
                                   message.get("value")):
            self._publish()

    def _edge(self, message: Dict[str, Any]) -> None:
        edge = message.get("edge") or {}
        # Only this module's own edges matter to it; the daemon forwards
        # every edge a node of ours takes part in, including ones to nodes
        # other modules run (those values arrive as `value_set`).
        if edge.get("from_node") in self.engine.nodes or edge.get("to_node") in self.engine.nodes:
            self.engine.apply_edge(edge, str(message.get("op") or "add"))
            self._publish()

    def _spawn(self, message: Dict[str, Any]) -> None:
        nodes = [n for n in (message.get("nodes") or [])
                 if n.get("type_id") in {t["type_id"] for t in types.TYPES}]
        if nodes:
            self.engine.add_nodes(nodes)
            self.host.announce(nodes)
            self._publish()

    def _stop(self, message: Dict[str, Any]) -> None:
        instance_ids = [str(i) for i in (message.get("instance_ids") or [])]
        if not instance_ids:
            return
        self.engine.remove_nodes(instance_ids)
        self.host.unannounce(instance_ids)

    def _publish(self) -> None:
        """Hand the daemon what the nodes currently compute, so a display
        polling the graph can show it."""
        self.host.report_values(self.engine.values)

    # -- used by the tests ----------------------------------------------

    def apply_value(self, message: Dict[str, Any]) -> None:
        """A value arriving from another module (`value_set`), addressed at
        the instance and the *port* it drives.

        This is the daemon's spelling of the relay for a value port that is
        not a control (`display.text`); the other spelling is `control_set`,
        which `_control` handles.  Both land in `set_control`, which is the
        node's one slot per input and the one place a value is refused if
        this module's declaration cannot take it (agents/SPACES.md §19) — so
        a relayed value is applied rather than dropped, and an undeclared
        `choice` option is refused rather than kept.
        """
        instance_id = str(message.get("instance_id") or "")
        port = str(message.get("port") or "")
        if self.engine.set_control(instance_id, port, message.get("value")):
            self._publish()


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    return Primitives().run()


if __name__ == "__main__":
    sys.exit(main())
