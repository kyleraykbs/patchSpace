"""
view_mixin.py

Pan/zoom/undo/context-menu behavior shared by both graph widgets
(PipeWireGraphWidget and PatchSpaceGraphWidget). Neither widget's own
code needs to touch pan/zoom math or popover placement directly -
that's all here.
"""

from __future__ import annotations

from gi.repository import Gdk, GLib, Gtk, Graphene

from constants import ZOOM_MIN, ZOOM_MAX, ZOOM_STEP, UNDO_LIMIT


class GraphViewMixin:
    def _init_view_controls(self):
        self.zoom = 1.0
        self._last_pointer = (400.0, 300.0)
        self.undo_stack = []
        # Only ever one context popover at a time - a second right-click
        # pops the previous one down first, so their grabs can't stack
        # and leave the widget unresponsive.
        self._context_popover = None

        self.middle_pan_gesture = Gtk.GestureDrag()
        self.middle_pan_gesture.set_button(Gdk.BUTTON_MIDDLE)
        self.middle_pan_gesture.connect("drag-begin", self._on_middle_pan_begin)
        self.middle_pan_gesture.connect("drag-update", self._on_middle_pan_update)
        self.middle_pan_gesture.connect("drag-end", self._on_middle_pan_end)
        self.add_controller(self.middle_pan_gesture)

        scroll = Gtk.EventControllerScroll.new(Gtk.EventControllerScrollFlags.BOTH_AXES)
        scroll.connect("scroll", self._on_scroll_zoom)
        self.add_controller(scroll)

        # Two-finger pinch: a touchscreen, and a trackpad whose pinch the
        # compositor forwards (Wayland sends it as a zoom gesture, not as
        # Ctrl+scroll).  `scale` is cumulative since the gesture began, so the
        # zoom in effect at ::begin is what it is applied to - using the live
        # zoom instead would compound and run away.
        self._pinch_start_zoom = 1.0
        pinch = Gtk.GestureZoom()
        pinch.connect("begin", self._on_pinch_begin)
        pinch.connect("scale-changed", self._on_pinch_zoom)
        self.add_controller(pinch)

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

    def zoom_about(self, x, y, factor):
        """Zoom by `factor` keeping whatever is under `(x, y)` there - the one
        piece of pan/zoom math the wheel and a pinch share."""
        old_zoom = self.zoom
        new_zoom = max(ZOOM_MIN, min(ZOOM_MAX, old_zoom * factor))
        if new_zoom == old_zoom:
            return False
        wx, wy = self.to_world(x, y)
        self.zoom = new_zoom
        self.pan_x = x - wx * new_zoom
        self.pan_y = y - wy * new_zoom
        self.queue_draw()
        return True

    def _on_scroll_zoom(self, controller, dx, dy):
        if dy == 0:
            return False
        factor = ZOOM_STEP if dy < 0 else (1.0 / ZOOM_STEP)
        self.zoom_about(self._last_pointer[0], self._last_pointer[1], factor)
        return True

    def _on_pinch_begin(self, gesture, _sequence=None):
        self._pinch_start_zoom = self.zoom

    def _on_pinch_zoom(self, gesture, scale):
        """A pinch (or a trackpad's zoom gesture): zoom about the point
        between the touches, the way the wheel zooms about the pointer."""
        if not scale or scale <= 0.0:
            return
        ok, cx, cy = gesture.get_bounding_box_center()
        if not ok:
            # A trackpad zoom gesture has no touches to take a centre from:
            # fall back to wherever the pointer is.
            cx, cy = self._last_pointer
        self.zoom_about(cx, cy, (self._pinch_start_zoom * scale) / self.zoom)

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
        if keyval == Gdk.KEY_Escape:
            # Safety valve: close a context popover whose input grab may be
            # holding the window (see popup_context_menu).
            self.dismiss_context_popover()
            return True
        return False

    def popup_context_menu(self, popover, x, y, focus_widget=None):
        """
        Show a popover context menu at the specified (x, y) coordinates.
        This method works reliably with GTK4's Python bindings.

        ``focus_widget``, if given, is focused once the popover has actually
        popped up (see the deferred ``_show`` below).  Grabbing focus on a
        widget inside a not-yet-mapped popover is unreliable in GTK4 - the
        widget ends up unfocused and a click into it can lose to leftover
        pointer-grab state - so the caller must not call ``grab_focus``
        itself."""


        # 0. Dismiss any menu that is already open before opening this
        #    one, so two popovers never hold the pointer at once.
        if self._context_popover is not None and self._context_popover is not popover:
            try:
                self._context_popover.popdown()
            except Exception:
                pass
            self._context_popover = None
        self._context_popover = popover

        # 1. Set the popover's parent to the current widget
        popover.set_parent(self)
        popover.set_autohide(True)

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
        def _on_closed(p):
            p.unparent()
            if self._context_popover is p:
                self._context_popover = None

        popover.connect("closed", _on_closed)

        # 5. Display the popover - deferred to an idle so it isn't
        # shown (and doesn't take its input grab) in the middle of the
        # button-press/drag event that opened it.  Popping up
        # synchronously from a gesture handler can leave that gesture
        # and the popover fighting over the pointer, which showed up as
        # the canvas going unresponsive after "drag, then right-click".
        #
        # Guarded so a popdown landing between scheduling and running
        # can't resurrect a dismissed popover: one shown without a later
        # ::closed holds an input grab and leaves the *whole window*
        # refusing mouse input.
        def _show():
            if self._context_popover is not popover:
                return GLib.SOURCE_REMOVE
            # A deferred popup can outlive the window it was requested from -
            # the window closes, or the canvas is torn down, between scheduling
            # and running.  Popping up a popover that is no longer rooted is not
            # a warning in GTK, it is a segfault (the widget tree behind it has
            # been freed), which is what made the whole app die at random after
            # closing a menu.
            if popover.get_root() is None or self.get_root() is None:
                return GLib.SOURCE_REMOVE
            if not popover.get_visible() and popover.get_parent() is not None:
                popover.popup()
                if focus_widget is not None:
                    focus_widget.grab_focus()
            return GLib.SOURCE_REMOVE

        GLib.idle_add(_show)

    def dismiss_context_popover(self, force=True):
        """Force any open context popover down (a safety valve for a grab
        that got stuck - see popup_context_menu).

        ``force=False`` leaves a popover that has been *requested* but not
        yet shown alone.  ``on_drag_begin`` runs on the same left-button
        press that ``on_click`` uses to open a popover (the field editor,
        the three-dot menu); the popover's ``popup()`` is deferred to an
        idle, so at drag-begin time it is still pending.  Cancelling it
        there meant the field editor never appeared - while the
        device/app chooser worked only because a daemon round-trip opened
        it after the press had fully finished."""
        pop = getattr(self, "_context_popover", None)
        if pop is None:
            return
        if not force and not pop.get_visible():
            # Requested-but-not-shown: don't cancel it.
            return
        self._context_popover = None
        try:
            if pop.get_visible():
                pop.popdown()
            if pop.get_parent() is not None:
                pop.unparent()
        except Exception:
            pass

