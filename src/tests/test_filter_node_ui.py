"""The Filter node's face: its sockets and its Include/Exclude button.

The node is a filter: a bundle input, its classifier input(s), a bundle out -
and one control, the big Include/Exclude button (the gate toggle's geometry
with those captions) to choose between keeping what the classifiers match and
keeping everything else.  It has no text box of its own; selecting by title is
the Title classifier's job, plugged into the filter input like the rest.

Needs GTK; skipped when unavailable."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"
))


class _Client:
    def __init__(self):
        self.sent = []

    def send(self, cmd):
        self.sent.append(cmd)

    def is_connected(self):
        return True


def _filter_node(nid="f", **attrs):
    node = {
        "id": nid,
        "type": "filter",
        "label": "Filter",
        "exclude": False,
        "x": 0.0,
        "y": 0.0,
        "ready": True,
        "connected": True,
        "selection_label": "Filter",
        "declarative": False,
        "filter_inputs": ["filter1", "filter2"],
    }
    node.update(attrs)
    return node


def _widget(node):
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
    if not Gtk.init_check():
        pytest.skip("no display available for GTK")
    from gui.node_specs import spec_for
    from gui.patchspace_widget import PatchSpaceGraphWidget

    client = _Client()
    w = PatchSpaceGraphWidget(client)
    w.physics_active = False
    w.layout_awake = False
    w.update_from_daemon(
        {"nodes": {"f": node}, "edges": {}, "panels": [], "groups": []}
    )
    return w, client


def _centre(rect):
    x, y, w, h = rect
    return (x + w / 2.0, y + h / 2.0)


def test_the_node_is_its_sockets_and_the_switch():
    """No box on the face: the bundle in, the classifier inputs, the out, and
    the Include/Exclude button."""
    w, _ = _widget(_filter_node())
    from gui.node_specs import spec_for

    assert spec_for("filter").field is None
    assert w.nodes["f"]["inputs"] == ["in", "filter1", "filter2"]
    # Nothing on the node is a text field, and the button is clickable.
    assert w.find_field_at(0.0, 0.0) is None
    bx, by = _centre(w._gate_rect("f"))
    assert w.find_filter_mode_at(bx, by) == "f"


def test_flipping_the_switch_updates_the_node_and_tells_the_daemon():
    w, client = _widget(_filter_node())
    w._toggle_filter_mode("f")  # what a click on the button calls
    assert w.nodes["f"]["exclude"] is True
    assert [c for c in client.sent if c.get("property") == "exclude"] == [
        {
            "command": "set_node_property",
            "node_id": "f",
            "property": "exclude",
            "value": True,
        }
    ]
    w._toggle_filter_mode("f")
    assert w.nodes["f"]["exclude"] is False


def test_a_poll_echo_does_not_undo_a_just_clicked_switch():
    """The daemon's reply lands a poll or two after the click; the value the
    user just set must survive until the daemon confirms it."""
    w, _ = _widget(_filter_node())
    w._toggle_filter_mode("f")
    # The poll still carries the pre-click value.
    w.update_from_daemon(
        {"nodes": {"f": _filter_node()}, "edges": {}, "panels": [], "groups": []}
    )
    assert w.nodes["f"]["exclude"] is True
    # Once the daemon agrees, its value is taken as-is.
    w.update_from_daemon(
        {"nodes": {"f": _filter_node(exclude=True)}, "edges": {}, "panels": [],
         "groups": []}
    )
    assert w.nodes["f"]["exclude"] is True
