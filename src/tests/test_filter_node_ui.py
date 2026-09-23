"""The Filter node's face: its title box and its Include/Exclude button.

The node is the one place a bundle gets narrowed, so it carries both controls
itself: the title box it matches members against, and - under it - the big
Include/Exclude button (the gate toggle's geometry with those captions).  The
layout is what can silently break when either control changes: the field must
sit *above* the button rather than underneath it, and both must stay inside
the node with the sockets clear.

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
        "title": "YouTube",
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


def test_the_title_box_sits_above_the_button_inside_the_node():
    w, _ = _widget(_filter_node())
    field = w._field_rect("f")
    button = w._gate_rect("f")
    top = w.nodes["f"]["y"]
    bottom = top + w.node_height("f")
    assert field[1] + field[3] <= button[1], (field, button)
    assert top <= field[1] and button[1] + button[3] <= bottom, (field, button)


def test_the_button_is_clickable_and_the_box_is_not_under_it():
    w, _ = _widget(_filter_node())
    bx, by = _centre(w._gate_rect("f"))
    assert w.find_filter_mode_at(bx, by) == "f"
    fx, fy = _centre(w._field_rect("f"))
    assert w.find_filter_mode_at(fx, fy) is None
    assert w.find_field_at(fx, fy) == "f"


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
