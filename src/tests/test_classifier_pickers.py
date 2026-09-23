"""The Title and Application classifiers' pickers.

Both fields are *chosen* from the live graph rather than typed: the field asks
the daemon for the list (the streams' media.name / the applications'
application.name), then opens a dropdown - search entry on top, scrolling list
under it - and picking a row sets the node's property.  The search also offers
the typed text itself, because these classifiers match substrings ("YouTube"
has to stay settable when every live title is longer than that).

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


def _node(nid, node_type, **attrs):
    node = {
        "id": nid,
        "type": node_type,
        "label": "Classifier",
        "x": 0.0,
        "y": 0.0,
        "ready": True,
        "connected": True,
        "selection_label": "",
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

    client = _Client()
    w = PatchSpaceGraphWidget(client)
    w.physics_active = False
    w.layout_awake = False
    w.update_from_daemon(
        {"nodes": {node["id"]: node}, "edges": {}, "panels": [], "groups": []}
    )
    return w, client


def _descendants(widget):
    yield widget
    child = widget.get_first_child() if hasattr(widget, "get_first_child") else None
    while child is not None:
        yield from _descendants(child)
        child = child.get_next_sibling()


def _visible_button_labels(popover):
    from gi.repository import Gtk

    return [w.get_label() for w in _descendants(popover)
            if isinstance(w, Gtk.Button) and w.get_visible()]


def test_the_filter_rule_matches_substrings_and_offers_typed_text():
    gi = pytest.importorskip("gi")
    gi.require_version("Gtk", "4.0")
    from gui.patchspace_widget import choice_row_visibility

    labels = ["YouTube - a video", "Spotify - a song"]
    # Everything shows while the search box is empty.
    assert choice_row_visibility(labels, "") == (labels, False)
    # Substring, case-insensitive.
    assert choice_row_visibility(labels, "SPOT") == (["Spotify - a song"], True)
    # Exactly a label => no extra row.
    assert choice_row_visibility(labels, "spotify - a song") == (
        ["Spotify - a song"], False
    )
    # Nothing matches, but the text is still settable (substring matching).
    assert choice_row_visibility(labels, "you") == (["YouTube - a video"], True)


def test_the_searchable_popover_filters_scrolls_and_picks():
    from gi.repository import Gtk

    w, _ = _widget(_node("c", "title_classifier", title=""))
    popover = w._show_choice_popover(
        10, 20, "Title:",
        [("YouTube - a video", "YouTube - a video"),
         ("Spotify - a song", "Spotify - a song")],
        lambda value: None,
        search_hint="Search titles",
    )
    # The list lives in a scrolling container (a live title list is long).
    assert any(isinstance(widget, Gtk.ScrolledWindow)
               for widget in _descendants(popover))
    entry = next(widget for widget in _descendants(popover)
                 if isinstance(widget, Gtk.SearchEntry))

    entry.set_text("spot")
    assert _visible_button_labels(popover) == ['Use "spot"', "Spotify - a song"]
    entry.set_text("spotify - a song")
    assert _visible_button_labels(popover) == ["Spotify - a song"]
    entry.set_text("")
    assert _visible_button_labels(popover) == [
        "YouTube - a video", "Spotify - a song"
    ]


def test_picking_the_typed_text_sets_the_substring():
    w, _ = _widget(_node("c", "title_classifier", title=""))
    picked = []
    popover = w._show_choice_popover(
        10, 20, "Title:", [("YouTube - a video", "YouTube - a video")],
        picked.append, search_hint="Search titles",
    )
    from gi.repository import Gtk

    entry = next(widget for widget in _descendants(popover)
                 if isinstance(widget, Gtk.SearchEntry))
    entry.set_text("YouTube")
    typed = next(
        widget for widget in _descendants(popover)
        if isinstance(widget, Gtk.Button) and widget.get_label() == 'Use "YouTube"'
    )
    typed.emit("clicked")
    assert picked == ["YouTube"]


def test_the_title_field_asks_for_titles_then_opens_the_dropdown():
    w, client = _widget(_node("c", "title_classifier", title=""))
    w.show_field_edit("c", 10, 20)
    assert [c["command"] for c in client.sent] == ["get_titles"]

    calls = []
    w._show_choice_popover = lambda *a, **kw: calls.append((a, kw))
    w.on_titles(["YouTube - a video", "Spotify - a song"])
    (sx, sy, title, choices, on_pick), kw = calls[0]
    assert (sx, sy, title) == (10, 20, "Title:")
    assert choices == [("YouTube - a video", "YouTube - a video"),
                       ("Spotify - a song", "Spotify - a song")]
    assert kw.get("search_hint")

    on_pick("YouTube - a video")
    assert client.sent[-1] == {
        "command": "set_node_property", "node_id": "c",
        "property": "title", "value": "YouTube - a video",
    }


def test_the_application_key_field_asks_for_apps_then_opens_the_dropdown():
    w, client = _widget(_node("c", "app_classifier", app_key=""))
    w.show_field_edit("c", 7, 8)
    assert [c["command"] for c in client.sent] == ["get_apps"]

    calls = []
    w._show_choice_popover = lambda *a, **kw: calls.append((a, kw))
    w.on_apps(["discord", "vesktop"])
    (sx, sy, title, choices, on_pick), kw = calls[0]
    assert (sx, sy, title) == (7, 8, "Application:")
    assert choices == [("discord", "discord"), ("vesktop", "vesktop")]
    assert kw.get("search_hint")

    on_pick("vesktop")
    assert client.sent[-1] == {
        "command": "set_node_property", "node_id": "c",
        "property": "app_key", "value": "vesktop",
    }


def test_the_application_field_asks_for_apps_then_opens_the_dropdown():
    w, client = _widget(_node("c", "app_name_classifier", app_name=""))
    w.show_field_edit("c", 5, 6)
    assert [c["command"] for c in client.sent] == ["get_applications"]

    calls = []
    w._show_choice_popover = lambda *a, **kw: calls.append((a, kw))
    w.on_applications({
        "outputs": [{"name": "Firefox"}, {"name": "Spotify"}],
        "inputs": [{"name": "Firefox"}],
    })
    (sx, sy, title, choices, on_pick), kw = calls[0]
    assert (sx, sy, title) == (5, 6, "Application:")
    # Both directions, deduped, sorted.
    assert choices == [("Firefox", "Firefox"), ("Spotify", "Spotify")]
    assert kw.get("search_hint")

    on_pick("Spotify")
    assert client.sent[-1] == {
        "command": "set_node_property", "node_id": "c",
        "property": "app_name", "value": "Spotify",
    }
