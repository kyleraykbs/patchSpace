#!/usr/bin/env python3
"""
apply_config.py - Idempotently apply a PatchSpace configuration.

Reads the same shape export_config.py writes:

    {"nodes": {node_id: {"type": ..., "params": {...}}},
     "edges": [{"from": ..., "to": ...}, ...]}

and replays it as add_node/add_edge calls. Both commands are
idempotent on the daemon side (main.py's _cmd_add_node/_cmd_add_edge
return already_existed=True instead of erroring or duplicating), so
re-running this against a daemon that already has some or all of the
config is safe - existing nodes get their config fields updated in
place, edges that already exist are left alone.
"""
import json
import sys

import migrations
from patchbay_cli import PatchBayClient


def apply_config(config_file):
    """Apply a configuration from a JSON file."""
    with open(config_file) as f:
        config = json.load(f)
    # Bring an old-schema config forward before replaying it (see
    # migrations.py) so applying a legacy export creates the current
    # node shapes instead of re-introducing deprecated ones.
    config, migration_fixes = migrations.migrate(config)
    for fix in migration_fixes:
        print(f"  ~ {fix}")

    client = PatchBayClient()
    try:
        # Add all nodes
        nodes = config.get("nodes", {})
        for node_id, node_config in nodes.items():
            node_type = node_config.get("type")
            node_params = node_config.get("params", {})
            result = client.add_node(node_type, node_id, **node_params)
            status = result.get("status")
            if status == "ok":
                if result.get("already_existed"):
                    print(f"  ✓ Node {node_id} already exists")
                else:
                    print(f"  + Node {node_id} created")
            elif status == "error":
                print(f"  ✗ Node {node_id}: {result.get('message')}")

        # Add all edges
        edges = config.get("edges", [])
        for edge in edges:
            from_node = edge.get("from")
            to_node = edge.get("to")
            to_port = edge.get("to_port", "in")
            from_port = edge.get("from_port", "out")
            result = client.add_edge(from_node, to_node, to_port, from_port)
            status = result.get("status")
            suffix = ""
            if to_port != "in":
                suffix += f" (to port {to_port})"
            if from_port != "out":
                suffix += f" (from port {from_port})"
            port_suffix = suffix
            if status == "ok":
                if result.get("already_existed"):
                    print(
                        f"  ✓ Edge {from_node}->{to_node}{port_suffix} already exists"
                    )
                else:
                    print(f"  + Edge {from_node}->{to_node}{port_suffix} created")
            elif status == "error":
                print(
                    f"  ✗ Edge {from_node}->{to_node}{port_suffix}: {result.get('message')}"
                )
    finally:
        client.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: apply_config.py <config.json>")
        sys.exit(1)
    apply_config(sys.argv[1])
