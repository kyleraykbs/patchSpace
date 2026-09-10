"""
view_mixin.py

Pan/zoom/undo/context-menu behavior shared by both graph widgets
(PipeWireGraphWidget and PatchSpaceGraphWidget). Neither widget's own
code needs to touch pan/zoom math or popover placement directly -
that's all here.
"""

from __future__ import annotations

from gi.repository import Gdk, Gtk, Graphene

from constants import ZOOM_MIN, ZOOM_MAX, ZOOM_STEP, UNDO_LIMIT


class GraphViewMixin:
    def _init_view_controls(self):
        self.zoom = 1.0
        self._last_pointer = (400.0, 300.0)
        self.undo_stack = []

        self.middle_pan_gesture = Gtk.GestureDrag()
        self.middle_pan_gesture.set_button(Gdk.BUTTON_MIDDLE)
        self.middle_pan_gesture.connect("drag-begin", self._on_middle_pan_begin)
        self.middle_pan_gesture.connect("drag-update", self._on_middle_pan_update)
        self.middle_pan_gesture.connect("drag-end", self._on_middle_pan_end)
        self.add_controller(self.middle_pan_gesture)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll_zoom)
        self.add_controller(scroll)

        key = Gtk.EventControllerKey()
        key.connect("key-pressed", self._on_view_key_pressed)
        self.add_controller(key)

    def to_world(self, x, y):
        return ((x - self.pan_x) / self.zoom, (y - self.pan_y) / self.zoom)

    def apply_view_transform(self, cr):
        cr.translate(self.pan_x, self.pan_y)
        cr.scale(self.zoom, self.zoom)

    def _on_middle_pan_begin(self, gesture, start_x, start_y):
        self.grab_focus()
        self.panning = True
        self.pan_drag_start = (self.pan_x, self.pan_y)
        self.set_cursor(Gdk.Cursor.new_from_name("grabbing", None))

    def _on_middle_pan_update(self, gesture, offset_x, offset_y):
        if not self.panning:
            return
        self.pan_x = self.pan_drag_start[0] + offset_x
        self.pan_y = self.pan_drag_start[1] + offset_y
        self.queue_draw()

    def _on_middle_pan_end(self, gesture, offset_x, offset_y):
        self.panning = False
        self.set_cursor(None)

    def _on_scroll_zoom(self, controller, dx, dy):
        if dy == 0:
            return False
        factor = ZOOM_STEP if dy < 0 else (1.0 / ZOOM_STEP)
        old_zoom = self.zoom
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, old_zoom * factor))
        if new_zoom == old_zoom:
            return True
        px, py = self._last_pointer
        wx, wy = self.to_world(px, py)
        self.zoom = new_zoom
        self.pan_x = px - wx * new_zoom
        self.pan_y = py - wy * new_zoom
        self.queue_draw()
        return True

    def track_pointer(self, x, y):
        self._last_pointer = (x, y)

    def push_undo(self, fn):
        self.undo_stack.append(fn)
        if len(self.undo_stack) > UNDO_LIMIT:
            self.undo_stack.pop(0)

    def undo(self):
        if not self.undo_stack:
            return
        fn = self.undo_stack.pop()
        try:
            fn()
        except Exception:
            import traceback

            traceback.print_exc()

    def _on_view_key_pressed(self, controller, keyval, keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_z, Gdk.KEY_Z):
            self.undo()
            return True
        return False

    def popup_context_menu(self, popover, x, y):
        """
        Show a popover context menu at the specified (x, y) coordinates.
        This method works reliably with GTK4's Python bindings.
        """
        # 1. Set the popover's parent to the current widget
        popover.set_parent(self)

        # 2. Create a Gdk.Rectangle at the cursor's position
        #    The coordinates are in the parent widget's space
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1

        # 3. Tell the popover to point to this rectangle
        popover.set_pointing_to(rect)

        # 4. Ensure the popover is cleaned up when closed
        popover.connect("closed", lambda p: p.unparent())

        # 5. Display the popover
        popover.popup()
