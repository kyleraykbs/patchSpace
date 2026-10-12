#!/usr/bin/env bash
# End-to-end smoke: the daemon, its modules and the graph, with no trace of
# the user's session.
#
# It runs a *private* PipeWire + WirePlumber in a temp runtime dir (the recipe
# in agents/NOTES.md — never the live session, and the daemon's orphan sweep is
# pointed at an instance the user has nothing on), starts the daemon on a temp
# socket with a temp panel dir, and drives the whole thing over that socket:
#
#   * the module set (`patchspace-modules` list, disable/enable)
#   * a declarative panel loading, its nodes online
#   * the primitives module: the daemon creates a node, the module runs it,
#     a control reaches it, the values it computes come back
#   * two placements of one panel diverging (phase 4's per-placement diffs)
#
# Everything is torn down at the end, including the private PipeWire.
#
#   ./scripts/smoke.sh            # in `nix develop`
#
# Exit code 0 = every step passed.
set -uo pipefail

HERE=$(cd "$(dirname "$0")/.." && pwd)
SRC="$HERE/src"
RT=$(mktemp -d /tmp/patchspace-smoke.XXXXXX)
FAILED=0
PIDS=()

cleanup() {
  for pid in "${PIDS[@]:-}"; do kill "$pid" 2>/dev/null; done
  sleep 0.3
  for pid in "${PIDS[@]:-}"; do kill -9 "$pid" 2>/dev/null; done
  rm -rf "$RT"
}
trap cleanup EXIT

step() { printf '\n== %s\n' "$1"; }
ok()   { printf '   ok   %s\n' "$1"; }
bad()  { printf '   FAIL %s\n' "$1"; FAILED=1; }
check() { # check <description> <expected> <actual>
  if [ "$2" = "$3" ]; then ok "$1"; else bad "$1 (expected '$2', got '$3')"; fi
}

mkdir -p "$RT/run" "$RT/config/wireplumber/wireplumber.conf.d" "$RT/panels" "$RT/cache"
chmod 700 "$RT/run"
cat > "$RT/config/wireplumber/wireplumber.conf.d/90-no-hardware.conf" << 'EOF'
wireplumber.profiles = {
  main = {
    monitor.alsa = disabled
    monitor.bluez = disabled
    monitor.v4l2 = disabled
  }
}
EOF

export XDG_RUNTIME_DIR="$RT/run"
export PIPEWIRE_RUNTIME_DIR="$RT/run"
export XDG_CONFIG_HOME="$RT/config"
export XDG_CACHE_HOME="$RT/cache"
export XDG_DATA_HOME="$RT/data"
export HOME="$RT/home"
mkdir -p "$XDG_DATA_HOME" "$HOME"

export PYTHONPATH="$SRC${PYTHONPATH:+:$PYTHONPATH}"
SOCKET="$RT/patchspace.sock"
PANEL="$RT/panels/kit.json"

step "private PipeWire + WirePlumber"
pipewire > "$RT/pipewire.log" 2>&1 & PIDS+=($!)
sleep 0.7
wireplumber > "$RT/wireplumber.log" 2>&1 & PIDS+=($!)
sleep 1.2
if [ -S "$RT/run/pipewire-0" ]; then ok "private pipewire is up"; else bad "no private pipewire socket"; fi

step "a declarative panel"
python3 - "$PANEL" << 'PYEOF'
import json, sys
json.dump({
    "type": "panel", "mode": "read-only", "label": "Kit", "auto_load": True,
    "placement": {"x": 0, "y": 0, "w": 420, "h": 260, "anchored": True},
    "config": {
        "nodes": {
            "vol": {"type": "volume", "params": {"initial_volume": 0.4, "x": 20, "y": 30}},
            "out": {"type": "patchspace_device", "params": {"label": "Smoke", "x": 20, "y": 150}},
        },
        "edges": [{"from": "vol", "to": "out"}],
    },
}, open(sys.argv[1], "w"))
PYEOF
[ -f "$PANEL" ] && ok "panel file written" || bad "panel file missing"

step "the daemon (temp socket, temp panel dir)"
PATCHSPACE_SOCKET="$SOCKET" python -m central.main \
  --socket "$SOCKET" --panel-dir "$RT/panels:rw" --root-panel "$RT/root.json" \
  > "$RT/daemon.log" 2>&1 & PIDS+=($!)
for _ in $(seq 1 60); do [ -S "$SOCKET" ] && break; sleep 0.5; done
if [ -S "$SOCKET" ]; then ok "daemon is serving"; else bad "daemon never bound its socket"; fi
if grep -q "refusing to start a second instance" "$RT/daemon.log"; then bad "the daemon refused (another instance?)"; fi

step "the socket protocol: nodes, modules, and a primitives node"
python3 - "$SOCKET" << 'PYEOF'
import json, socket, sys, time

sock_path = sys.argv[1]
failures = []

def call(cmd, timeout=10.0):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps(cmd) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode())

def call_as_display(cmd, timeout=10.0):
    """One module-API command on a fresh *display* connection: the daemon
    requires `hello` before it will route a control or an edge."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(sock_path)
        s.sendall((json.dumps({"type": "hello", "proto": 1, "role": "display",
                               "name": "smoke", "version": ""}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            buf += s.recv(65536)
        s.sendall((json.dumps(cmd) + "\n").encode())
        buf = buf.split(b"\n", 1)[1] if b"\n" in buf else b""
        while b"\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf.split(b"\n", 1)[0].decode())


def check(desc, ok, extra=""):
    print(("   ok   " if ok else "   FAIL ") + desc + ("" if ok else f" {extra}"))
    if not ok:
        failures.append(desc)


def wait_for(desc, predicate, timeout=45.0):
    """Poll until `predicate()` holds: the daemon loads its session on a
    background thread and spawns its module processes, so nothing about the
    graph is ready the moment the socket appears."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                check(desc, True)
                return
        except Exception:
            pass
        time.sleep(0.25)
    check(desc, False)

# The declarative panel's nodes come up asynchronously.
wait_for("the panel's volume node is online",
         lambda: call({"command": "get_nodes"})["nodes"].get("kit::vol", {}).get("status") == "online")
wait_for("its edge is wired",
         lambda: any(e.get("from_node") == "kit::vol" and e.get("to_node") == "kit::out"
                     for e in call({"command": "get_nodes"})["edges"].values()))

# The module set (the primitives process takes a moment to attach).
report = call({"command": "modules"})
names = {m["name"]: m for m in report["modules"]}
check("pipewire is enabled and running in-process",
      names.get("pipewire", {}).get("running") is True, names.get("pipewire"))
check("primitives is enabled", names.get("primitives", {}).get("enabled") is True,
      names.get("primitives"))
wait_for("its process attached",
         lambda: {m["name"]: m for m in call({"command": "modules"})["modules"]}
         .get("primitives", {}).get("running") is True)

# A node the daemon does not model: the module runs it.
reply = call({"command": "add_node", "node_type": "number", "node_id": "n1",
              "config": {"value": 6}})
check("the daemon created a primitives node", reply.get("status") == "ok", reply)

for _ in range(40):
    nodes = call({"command": "get_nodes"})["nodes"]
    if nodes.get("n1", {}).get("status") == "online":
        break
    time.sleep(0.25)
node = call({"command": "get_nodes"})["nodes"].get("n1", {})
check("the module announced it back", node.get("status") == "online", node)
check("it is marked as module-owned", node.get("module_owned") is True, node)
check("the module reported its value", node.get("values", {}).get("out") == 6.0, node)

# A control from a display reaches the module.
reply = call_as_display({"type": "node_control", "instance_id": "n1",
                         "key": "value", "value": 42})
check("a control is accepted", reply.get("type") == "ok", reply)
for _ in range(40):
    node = call({"command": "get_nodes"})["nodes"].get("n1", {})
    if node.get("values", {}).get("out") == 42.0:
        break
    time.sleep(0.25)
check("the module applied it", node.get("values", {}).get("out") == 42.0, node)

# Two placements of one panel diverge.
call({"command": "place_panel", "stem": "kit", "name": "kit_2", "x": 500, "y": 0})
time.sleep(1.0)
nodes = call({"command": "get_nodes"})["nodes"]
check("the second placement came up", nodes.get("kit_2::vol", {}).get("status") == "online",
      sorted(nodes))
call({"command": "set_volume", "node_id": "kit_2::vol", "volume": 0.9})
time.sleep(2.5)                      # the autosave writes the diffs
nodes = call({"command": "get_nodes"})["nodes"]
check("the local change stayed in its placement",
      abs(nodes.get("kit_2::vol", {}).get("volume", 0) - 0.9) < 1e-6,
      nodes.get("kit_2::vol"))
check("the blueprint's placement is untouched",
      abs(nodes.get("kit::vol", {}).get("volume", 0) - 0.4) < 1e-6,
      nodes.get("kit::vol"))

# Disabling a module takes its nodes offline; enabling brings them back.
reply = call({"command": "set_module", "name": "pipewire", "enable": False})
check("disabling pipewire is accepted", reply.get("status") == "ok", reply)
for _ in range(40):
    node = call({"command": "get_nodes"})["nodes"].get("kit::vol", {})
    if node.get("status") != "online":
        break
    time.sleep(0.25)
check("its nodes went offline", node.get("status") == "offline_module", node)
check("the graph is still there", "kit::vol" in call({"command": "get_nodes"})["nodes"])

call({"command": "set_module", "name": "pipewire", "enable": True})
for _ in range(40):
    node = call({"command": "get_nodes"})["nodes"].get("kit::vol", {})
    if node.get("status") == "online":
        break
    time.sleep(0.25)
check("re-enabling brought them back", node.get("status") == "online", node)

sys.exit(1 if failures else 0)
PYEOF
[ $? -eq 0 ] || FAILED=1

step "result"
if [ "$FAILED" -eq 0 ]; then
  echo "   every step passed"
else
  echo "   something failed — the logs are below"
  tail -20 "$RT/daemon.log"
fi
# The diff sidecar is the phase-4 persistence: written beside the session.
if [ -f "$RT/placement_diffs.json" ] && grep -q "kit_2" "$RT/placement_diffs.json"; then
  ok "the placement diff is on disk"
else
  bad "no placement diff on disk"
fi
exit "$FAILED"
