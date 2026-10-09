"""
modules_cli.py — `patchspace-modules`: list, enable and disable the daemon's
built-in modules.

    patchspace-modules                 # what exists, enabled, running
    patchspace-modules enable primitives
    patchspace-modules disable pipewire

It talks to the running daemon over the same socket as everything else (the
daemon owns the config file and applies a change live); with no daemon it
prints what the config file says and exits non-zero, so a script can tell.
"""

from __future__ import annotations

import argparse
import json
import socket
import sys
from typing import Any, Dict, Optional

from central import module_config
from central.patchspace_cli import _default_socket_path


def _request(command: Dict[str, Any], socket_path: str) -> Optional[Dict[str, Any]]:
    """One command, one reply — the daemon's line protocol."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(5.0)
            sock.connect(socket_path)
            sock.sendall((json.dumps(command) + "\n").encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buffer += chunk
    except OSError as exc:
        print(f"patchspace-modules: no daemon on {socket_path} ({exc})",
              file=sys.stderr)
        return None
    if b"\n" not in buffer:
        print("patchspace-modules: the daemon closed the connection",
              file=sys.stderr)
        return None
    try:
        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
    except json.JSONDecodeError as exc:
        print(f"patchspace-modules: bad reply ({exc})", file=sys.stderr)
        return None


def _print_report(reply: Dict[str, Any]) -> None:
    modules = reply.get("modules") or []
    if not modules:
        print("no modules")
        return
    width = max(len(m["name"]) for m in modules)
    for module in modules:
        state = "enabled " if module["enabled"] else "disabled"
        running = "running" if module["running"] else "stopped"
        pid = f" pid {module['pid']}" if module.get("pid") else ""
        kind = {"inproc": "in the daemon", "process": "own process"}.get(
            module["kind"], module["kind"]
        )
        print(f"{module['name']:<{width}}  {state}  {running:<7} {kind}{pid}")
    where = reply.get("config_path") or ""
    if where:
        note = " (managed by the deployment)" if reply.get("managed") else ""
        print(f"\nconfig: {where}{note}")


def _offline_report() -> int:
    """No daemon: report the file's contents, and say so."""
    config = module_config.ModuleConfig()
    rows = [
        {"name": name, "kind": module_config.BUILTINS.get(name, {}).get("kind", "unknown"),
         "enabled": config.enabled(name), "running": False, "pid": None}
        for name in config.names()
    ]
    print("(no daemon running — showing the config file)")
    _print_report({"modules": rows, "config_path": config.path,
                   "managed": config.managed})
    return 1


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="patchspace-modules",
        description="List, enable and disable the daemon's built-in modules.",
    )
    parser.add_argument("action", nargs="?", default="list",
                        choices=["list", "enable", "disable"],
                        help="what to do (default: list)")
    parser.add_argument("name", nargs="?", default="", help="the module's name")
    parser.add_argument("--socket", default=None,
                        help="the daemon's socket (default: the usual one)")
    args = parser.parse_args(argv)

    socket_path = args.socket or _default_socket_path()

    if args.action == "list":
        reply = _request({"command": "modules"}, socket_path)
        if reply is None:
            return _offline_report()
        if reply.get("status") != "ok":
            print(f"patchspace-modules: {reply.get('message')}", file=sys.stderr)
            return 1
        _print_report(reply)
        return 0

    if not args.name:
        print(f"patchspace-modules: {args.action} needs a module name",
              file=sys.stderr)
        return 2
    reply = _request({"command": "set_module", "name": args.name,
                      "enable": args.action == "enable"}, socket_path)
    if reply is None:
        return _offline_report()
    if reply.get("status") != "ok":
        print(f"patchspace-modules: {reply.get('message')}", file=sys.stderr)
        return 1
    _print_report(reply)
    return 0


if __name__ == "__main__":
    sys.exit(main())
