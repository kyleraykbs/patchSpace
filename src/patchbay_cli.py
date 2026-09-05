#!/usr/bin/env python3
"""
patchbay_cli.py

Minimal synchronous Unix-socket client for the PatchBay daemon,
meant for one-shot command-line tools (export_config.py,
apply_config.py) that send a command and need its response right
away - unlike socket_client.PatchBayClient, which is built for a GUI
event loop (background thread, responses queued and drained on a
GLib timer). This one blocks on each call until exactly one reply
comes back, which is all a top-to-bottom script needs, and it avoids
pulling in threading/Gtk just to run a script.

Same wire protocol as socket_client.py and main.py's _handle_client:
one JSON object per line in, one JSON object per line back, replies
arrive in the same order requests were sent.
"""

from __future__ import annotations

import json
import socket
from typing import Any, Dict

# Duplicated from gui/constants.py rather than imported from it: this
# module is meant to run as a standalone CLI tool (export_config.py /
# apply_config.py), and importing across into the gui package would
# tie its sys.path layout to wherever those scripts happen to be run
# from. Keep this in sync with SOCKET_PATH in gui/constants.py and
# main.py if it ever changes.
SOCKET_PATH = "/tmp/patchbay.sock"


class PatchBayClient:
    def __init__(self, path: str = SOCKET_PATH):
        self.path = path
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.connect(self.path)
        self._buffer = b""

    def _send(self, cmd: Dict[str, Any]) -> Dict[str, Any]:
        self._sock.sendall((json.dumps(cmd) + "\n").encode("utf-8"))
        while b"\n" not in self._buffer:
            chunk = self._sock.recv(4096)
            if not chunk:
                raise ConnectionError("daemon closed the connection")
            self._buffer += chunk
        line, self._buffer = self._buffer.split(b"\n", 1)
        return json.loads(line.decode("utf-8"))

    # ---------- commands used by export_config.py / apply_config.py ----------

    def add_node(self, node_type: str, node_id: str, **config: Any) -> Dict[str, Any]:
        return self._send(
            {
                "command": "add_node",
                "node_type": node_type,
                "node_id": node_id,
                "config": config,
            }
        )

    def add_edge(self, from_node: str, to_node: str) -> Dict[str, Any]:
        return self._send(
            {"command": "add_edge", "from_node": from_node, "to_node": to_node}
        )

    def export_config(self) -> Dict[str, Any]:
        return self._send({"command": "export_config"})

    # ---------- lifecycle ----------

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def __enter__(self) -> "PatchBayClient":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()
