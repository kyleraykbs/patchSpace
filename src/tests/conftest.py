"""Keep the suite off the machine it runs on.

Several modules resolve their defaults from `$HOME` at *import* time: the
daemon's space directory, the session cache, the pre-rename PatchBay paths
(`~/.local/share/patchbay/panels`) and the pre-S0 `panels` spelling under the
current name (`~/.local/share/patchspace/panels`), and the default socket.  A
test that exercises "the default paths" - or one whose daemon adopts whatever
is in the legacy space dir and then *installs* it - would otherwise create real
PipeWire objects inside the developer's live session.  That happened: running
`tests/test_spaces_daemon.py` on a machine with a pre-rename space dir left
~130 orphaned pw-cli/pw-cat helpers and a churning graph.

pytest imports this before any test module, so those defaults land in a
throwaway directory.

The client's tests (the canvas, the catalog parser, the socket client) live in
the display repo; this suite is the daemon's alone and needs no display.
"""

import os
import tempfile

_home = tempfile.mkdtemp(prefix="patchspace-tests-")
os.environ["HOME"] = _home
os.environ["XDG_DATA_HOME"] = os.path.join(_home, "share")
os.environ["XDG_CACHE_HOME"] = os.path.join(_home, "cache")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, "config")
# The env-driven knobs, so even a test that ignores HOME is contained.
os.environ["PATCHSPACE_SOCKET"] = os.path.join(_home, "patchspace.sock")
os.environ["PATCHSPACE_SPACE_DIR"] = os.path.join(_home, "spaces")
os.environ["PATCHSPACE_ROOT_SPACE"] = os.path.join(_home, "last_session.json")
os.environ["PATCHSPACE_MODULES_CONFIG"] = os.path.join(_home, "modules.json")
for _d in (os.environ["XDG_DATA_HOME"], os.environ["XDG_CACHE_HOME"],
           os.environ["XDG_CONFIG_HOME"], os.environ["PATCHSPACE_SPACE_DIR"]):
    os.makedirs(_d, exist_ok=True)
