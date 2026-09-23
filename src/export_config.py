#!/usr/bin/env python3
"""
export_config.py - Dump the daemon's current PatchSpace to a JSON
file, in exactly the shape apply_config.py expects to read back in:

    {"nodes": {node_id: {"type": ..., "params": {...}}},
     "edges": [{"from": ..., "to": ...}, ...]}

The heavy lifting (deciding what counts as "config" vs. runtime-only
state like live connection ids) happens server-side in main.py's
_cmd_export_config - this script is just the thin CLI wrapper around
the "export_config" command.
"""
import json
import sys

from patchspace_cli import PatchSpaceClient


def export_config(output_file):
    """Fetch the current config from the daemon and write it to disk."""
    client = PatchSpaceClient()
    try:
        result = client.export_config()
    finally:
        client.close()

    if result.get("status") != "ok":
        print(f"  ✗ export failed: {result.get('message')}")
        sys.exit(1)

    config = result["config"]
    with open(output_file, "w") as f:
        json.dump(config, f, indent=2, sort_keys=True)

    node_count = len(config.get("nodes", {}))
    edge_count = len(config.get("edges", []))
    print(f"  + wrote {node_count} node(s) and {edge_count} edge(s) to {output_file}")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: export_config.py <output.json>")
        sys.exit(1)
    export_config(sys.argv[1])
