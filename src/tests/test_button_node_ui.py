"""The Button node's face: its caption and its hover cue.

A Button fires an impulse, so its face says what pressing it does ("Trigger")
rather than the node type's name ("Button"), and it lifts a step while the
pointer is over it so it reads as pressable.

Needs GTK; skipped when unavailable."""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "gui"
))


class _Client:
    def send(self, cmd):
        pass

    def is_connected(self):
        return True


def _button_node(nid="b", **attrs):
    node = {
        "id": nid,
        "type": "button",
        "label": "Button",
        "x": 0.0,
        "y": 0.0,
        "ready": True,
        "connected": True,
        "selection_label": "Button",
        "declarative": False,
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

    w = PatchSpaceGraphWidget(_Client())
    w.physics_active = False
    w.layout_awake = False
    w.pan_x, w.pan_y, w.zoom = 0.0, 0.0, 1.0
    w.update_from_daemon(
        {"nodes": {node["id"]: node}, "edges": {}, "panels": [], "groups": []}
    )
    # Nodes fade in on their first drawn frame; a one-shot render would paint
    # the node at alpha 0 and probe the canvas instead of the face.
    w._anim_seen = set(w.nodes)
    w._node_alpha = {nid: 1.0 for nid in w.nodes}
    return w


def _render_centre_pixel(w, x, y):
    """Draw one frame offscreen and read a pixel, the way the bench renders
    do - the only way to see what a cairo face actually paints."""
    import cairo

    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 400, 300)
    cr = cairo.Context(surface)
    w.on_draw(w, cr, 400, 300)
    surface.flush()
    buf = memoryview(surface.get_data()).cast("B")
    i = (int(y) * surface.get_stride() + int(x) * 4)
    return tuple(buf[i:i + 4])


def test_the_face_says_trigger_unless_the_node_was_renamed():
    w = _widget(_button_node())
    assert w._impulse_label({"type": "button", "label": "Button"}) == "Trigger"
    assert w._impulse_label({"type": "button", "label": ""}) == "Trigger"
    # A label the user set is theirs to keep.
    assert w._impulse_label({"type": "button", "label": "Kick"}) == "Kick"


def test_the_face_changes_color_while_hovered():
    w = _widget(_button_node())
    bx, by, bw, bh = w._gate_rect("b")
    # Inside the face, clear of the caption in the middle.
    probe = (bx + 8, by + bh / 2.0)

    idle = _render_centre_pixel(w, *probe)
    w.on_motion(None, *probe)          # the pointer arrives on the button
    assert w.hover_impulse == "b"
    hovered = _render_centre_pixel(w, *probe)
    assert hovered != idle, (idle, hovered)

    w.on_leave(None)                   # and leaves again
    assert w.hover_impulse is None
    assert _render_centre_pixel(w, *probe) == idle


def test_an_unwired_impulse_input_gets_its_own_face():
    """A node whose impulse input has no wire shows a face that fires it, so a
    player can be tested without a Button - and it goes away once wired."""
    node = _button_node("pl")
    node.update({"type": "sound_player", "label": "Sound Player", "playing": 0})
    w = _widget(node)
    fx, fy, fw, fh = w._impulse_face_rect("pl")
    assert w.find_impulse_fallback_at(fx + fw / 2, fy + fh / 2) == "pl"

    # A wire into the impulse input takes the face away.
    w.edges["btn->pl:impulse"] = {
        "from_node": "btn", "from_port": "out", "to_node": "pl",
        "to_port": "impulse",
    }
    assert w.find_impulse_fallback_at(fx + fw / 2, fy + fh / 2) is None
