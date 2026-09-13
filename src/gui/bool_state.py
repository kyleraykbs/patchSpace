"""Reconciliation of daemon-reported boolean control state.

Kept in its own tiny module (no GTK import) so the poll-merge rule can be
unit-tested without pulling in the whole graph widget.  See
patchspace_widget.update_from_daemon, which is the only caller.
"""


def resolve_bool_state_from_poll(reported, ctrl_connected, previous):
    """The value to store for a node's ``bool_state`` from a get_nodes poll.

    A driven gate/switcher can momentarily come back with ``None`` while
    its ctrl signal is still wired - e.g. a bool-warp publisher briefly
    re-created by a panel sync.  Dropping to ``None`` there makes the
    on/off indicator fall back to the node's stored default, so it
    visibly flashes (or sticks on the wrong state if the None persists
    for several polls).  Keep the last value the daemon actually
    resolved instead; anything else the daemon reports - including
    ``None`` once the ctrl signal is genuinely disconnected - wins."""
    if reported is None and ctrl_connected and previous is not None:
        return previous
    return reported
