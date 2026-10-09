"""Keep the suite off the machine it runs on.

Several modules resolve their defaults from `$HOME` at *import* time: the
daemon's panel directory, the session cache, the pre-rename PatchBay paths
(`~/.local/share/patchbay/panels`), and the default socket.  A test that
exercises "the default paths" - or one whose daemon adopts whatever is in the
legacy panel dir and then *installs* it - would otherwise create real
PipeWire objects inside the developer's live session.  That happened: running
`tests/test_panels_daemon.py` on a machine with a pre-rename panel dir left
~130 orphaned pw-cli/pw-cat helpers and a churning graph.

pytest imports this before any test module, so those defaults land in a
throwaway directory.  `XDG_RUNTIME_DIR` is deliberately left alone: GTK needs
it for the display, and `PATCHSPACE_SOCKET` below already keeps tests away
from the real socket.
"""

import os
import sys
import tempfile

import pytest

# The GUI is a namespace package run with its own directory on the path, and
# its modules import each other *flat* (`import node_catalog`).  Put that
# directory on the path here, at collection time, so a test module may do the
# same — importing `gui.node_catalog` instead would load a second copy of the
# same file and the widget would read an empty catalog.
_SRC_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_GUI_DIR = os.path.join(_SRC_ROOT, "gui")
if _GUI_DIR not in sys.path:
    sys.path.insert(0, _GUI_DIR)

_home = tempfile.mkdtemp(prefix="patchspace-tests-")
os.environ["HOME"] = _home
os.environ["XDG_DATA_HOME"] = os.path.join(_home, "share")
os.environ["XDG_CACHE_HOME"] = os.path.join(_home, "cache")
os.environ["XDG_CONFIG_HOME"] = os.path.join(_home, "config")
# The env-driven knobs, so even a test that ignores HOME is contained.
os.environ["PATCHSPACE_SOCKET"] = os.path.join(_home, "patchspace.sock")
os.environ["PATCHSPACE_PANEL_DIR"] = os.path.join(_home, "panels")
os.environ["PATCHSPACE_ROOT_PANEL"] = os.path.join(_home, "last_session.json")
for _d in (os.environ["XDG_DATA_HOME"], os.environ["XDG_CACHE_HOME"],
           os.environ["XDG_CONFIG_HOME"], os.environ["PATCHSPACE_PANEL_DIR"]):
    os.makedirs(_d, exist_ok=True)


# The GUI's node catalog is normally served by the daemon (see
# gui/node_catalog.py).  Tests that check what the GUI knows install the
# built-in module's catalog directly, so they exercise the same data the
# daemon would send without needing a daemon.  Autouse, because it is
# process-global state: one test clearing the catalog must not leave the
# next one drawing unknown nodes.
@pytest.fixture(autouse=True)
def _builtin_catalog():
    import node_catalog
    from modules.pipewire import declarations

    node_catalog.install(
        declarations.catalog(),
        declarations.menu(),
        declarations.vocabulary(),
        declarations.legacy_aliases(),
    )
    yield node_catalog
