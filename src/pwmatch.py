"""
pwmatch.py

Stateless matching helpers that turn a PatchSpace filter dict into real,
live node ids and (output_port, input_port) pairs on a
pwgraph.PipewireGraph.

This is a bag of pure functions with no stored state.  The graph engine
calls them fresh on every reconciliation pass and keeps its own
bookkeeping of what it connected last time - so "did we already connect
this" always has exactly one right answer instead of two (a cache and
reality) that can drift apart.

Filter semantics
----------------
Source filters (matched against a candidate source node's props):

  * id                 exact graph node id
  * nodeName           EXACT, case-sensitive match against node.name.
                       Every node that identifies itself by a real
                       object's unique node.name uses this ("name"
                       would also match anything whose name merely
                       contains it).
  * name               case-insensitive SUBSTRING match against
                       application.name-or-node.name.
  * nameRegex          regex (re.search) against the same haystack.
  * mediaName(.Regex)  substring/regex against media.name.
  * mediaClass(.Regex) substring/regex against media.class.
  * description(.Regex) substring/regex against node.description (or
                       node.nick / node.name).
  * exclude            list of source-filter dicts, recursively, that a
                       candidate must match NONE of (this is what backs
                       the ExcludeFilter node).
  * appKey             exact match against the *application* a stream belongs
                       to, as the desktop names it (see app_key): the systemd
                       app scope behind the stream's process, so every stream
                       an app made - including its audio subprocesses -
                       matches the one key.

Sink filters / targets (matched against a candidate sink node):

  * id / nameRegex / name  (name is EXACT on node.name, not substring)
  * mediaClass(.Regex) / description(.Regex)
  * type               selects which port group ("probe", "playback", ...)
                       to connect into on the target.

Any criterion set on one filter dict must ALL match (AND); OR-ing
several filters is the caller's job (the graph engine passes lists).

A target/source with no identifying field matches nothing.
"""

from __future__ import annotations

import logging
import re
import time
from functools import lru_cache
from typing import Dict, List, Optional, Pattern, Set, Tuple

logger = logging.getLogger(__name__)

# Media class for nodes the engine owns purely as internal routing
# plumbing - splitters and the dummy sinks bracketing a chain effect.
# It must be an `Audio/Sink/*` subclass, not a custom top-level class:
# the PipeWire adapter only creates the sink/monitor ports for an
# Audio/Sink* class (a non-Audio class yields a node with zero ports).
# The `pipewire-pulse` module, however, only exposes a node as a sink on
# an EXACT "Audio/Sink" match (see its pw_manager_object_is_sink), so
# this subclass keeps every port while staying invisible to Pulse device
# enumeration - no Discord/Chromium "new audio device" toast.  It still
# has to be listed in SOURCE_MEDIA_CLASSES below so the engine routes it.
INTERNAL_MEDIA_CLASS = "Audio/Sink/Internal"
INTERNAL_SOURCE_MEDIA_CLASS = "Audio/Source/Internal"

# media classes the engine will treat as a routable *source*: ordinary
# app playback streams, sinks (their monitor ports mirror what plays
# into them), hardware capture devices, and our own non-Pulse internal
# plumbing (see INTERNAL_MEDIA_CLASS).
SOURCE_MEDIA_CLASSES = (
    "Stream/Output/Audio",
    "Audio/Sink",
    "Audio/Source",
    INTERNAL_MEDIA_CLASS,
    INTERNAL_SOURCE_MEDIA_CLASS,
)

# Every node.name prefix this project creates objects under.  Also the
# basis of is_patchspace_owned() below, which the External Only classifier
# uses to filter Patch Space's own plumbing out of a bundle.
PATCHSPACE_OWNED_PREFIXES = (
    "patchspace_",
    # The pre-rename prefix (PatchBay -> Patch Space).  Still listed on
    # purpose: a session that was running before the rename left objects
    # named ``patchbay_*`` behind, and they have to be recognised as ours -
    # otherwise the startup sweep can't reap them and an External Only
    # bundle starts leaking our own plumbing into "the rest of the world".
    "patchbay_",
    "echo_cancel_node_",
    "light_noise_cancel_node_",
    "noise_cancel_node_",
    "reverb_node_",
    "normalize_node_",
    "volume_node_",
    "volume_mute_",
    "virtual_speaker_node_",
    "virtual_mic_node_",
    "splitter_",
    "bundle_output_",
)

# The built-in virtual devices' exact node.names.  Mixed-case and
# space-y on purpose (they are user-visible devices), and every piece
# of their plumbing merely *starts with* one of them ("Patch Space_sink",
# "Patch Space Mic_sink", ...) - hence the prefix match below.
PATCHSPACE_BUILTIN_NAMES = (
    "Patch Space",
    "Patch Space Mic",
    # Pre-rename device names, same reasoning as the prefix list above: the
    # old built-in sink/mic may still exist in a session this daemon adopts.
    "PatchBay",
    "PatchBay Mic",
)


# systemd names the scope of a launched application ``app-<name>-<pid>.scope``
# (or ``app-<name>@<uid>.service``), where <name> is the app id the launcher
# used - the same name the desktop shows for that app's windows.  '-' inside
# the name is escaped as \x2d, so a plain split on '-' doesn't do it.
_APP_SCOPE_RE = re.compile(r"app-([^/]+?)(?:-\d+\.scope|@\d+\.service)\s*$")
_SYSTEMD_ESCAPE_RE = re.compile(r"\\x([0-9a-fA-F]{2})")


def app_scope_key(cgroup: str) -> str:
    """The application name in a process's cgroup text, or "" when the
    process isn't inside an application scope.

    ``/proc/<pid>/cgroup`` for an app the session launched ends in e.g.
    ``app-vesktop-3807669.scope``; this returns "vesktop".  Pure, so the
    parsing is testable without a live process."""
    match = _APP_SCOPE_RE.search(cgroup or "")
    if not match:
        return ""
    raw = match.group(1)
    # systemd escapes the characters it can't put in a unit name; '-' is the
    # one that actually turns up in app ids (com.discordapp.Discord is fine,
    # a hypothetical "my-app" comes back as "my\x2dapp").
    return _SYSTEMD_ESCAPE_RE.sub(
        lambda m: chr(int(m.group(1), 16)), raw
    )


def _pid_app_scope(pid: int) -> str:
    """The app scope of a live process, cached briefly: pids are reused, so
    this can't be cached for the lifetime of the daemon."""
    now = time.monotonic()
    hit = _PID_SCOPE_CACHE.get(pid)
    if hit is not None and now - hit[0] < PID_SCOPE_TTL:
        return hit[1]
    try:
        with open(f"/proc/{int(pid)}/cgroup", encoding="utf-8") as fh:
            key = app_scope_key(fh.read())
    except (OSError, ValueError, TypeError):
        key = ""
    _PID_SCOPE_CACHE[pid] = (now, key)
    if len(_PID_SCOPE_CACHE) > 512:
        _PID_SCOPE_CACHE.clear()
    return key


def app_key(props: dict) -> str:
    """The *application* a live stream belongs to, as the desktop names it.

    PipeWire's own props describe the creator *subprocess*: an Electron app's
    audio service reports ``application.name`` "Chromium input" and
    ``application.process.binary`` "electron", while what the user knows is
    "vesktop" - which is the systemd app scope its process sits in.  So: the
    scope, else the process binary, else the application name, else "" (a
    stream that identifies nothing matches no application)."""
    scope = _pid_app_scope(props.get("application.process.id") or 0)
    if scope:
        return scope
    binary = str(props.get("application.process.binary") or "").strip()
    if binary:
        return binary
    return str(props.get("application.name") or "").strip()


def is_patchspace_owned(props: dict) -> bool:
    """Whether a live node is one of Patch Space's own objects - a built-in
    virtual device, an effect dummy/keepalive, a module stream - rather
    than some external app or hardware device.

    Deliberately generous (any backing prefix, the ``Audio/*/Internal``
    classes, any name starting with a built-in device name): it backs the
    External Only classifier, where missing an owned node would leak our
    plumbing into a "the rest of the world" bundle."""
    name = props.get("node.name") or ""
    if props.get("media.class") in (
        INTERNAL_MEDIA_CLASS,
        INTERNAL_SOURCE_MEDIA_CLASS,
    ):
        return True
    if name.endswith("_keepalive"):
        return True
    if any(name.startswith(prefix) for prefix in PATCHSPACE_OWNED_PREFIXES):
        return True
    return any(name.startswith(builtin) for builtin in PATCHSPACE_BUILTIN_NAMES)


#: pid -> (monotonic timestamp, app scope name); see _pid_app_scope.
_PID_SCOPE_CACHE: Dict[int, Tuple[float, str]] = {}
#: How long one /proc read stays good.  Short: pids are reused, and an app
#: that just started should show up in the picker promptly.
PID_SCOPE_TTL = 5.0


# groups[group_name][channel] -> port id
PortGroups = Dict[str, Dict[str, int]]

_TARGET_IDENTITY_KEYS = (
    "id",
    "name",
    "nameRegex",
    "mediaClass",
    "mediaClassRegex",
    "description",
    "descriptionRegex",
)


@lru_cache(maxsize=256)
def _compile(pattern: str) -> Optional[Pattern[str]]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        logger.warning("Invalid regex %r in filter: %s", pattern, exc)
        return None


def _match_regex_or_substring(regex: Optional[str], sub: Optional[str], haystack: str) -> bool:
    if regex is not None:
        pattern = _compile(regex)
        return pattern is not None and pattern.search(haystack) is not None
    return sub.lower() in haystack.lower()


def _source_haystack(props: dict) -> str:
    return props.get("application.name") or props.get("node.name") or ""


def _desc(props: dict) -> str:
    return (
        props.get("node.description")
        or props.get("node.nick")
        or props.get("node.name")
        or ""
    )


# ---------------------------------------------------------------------------
# Source matching
# ---------------------------------------------------------------------------


def matches_source_filter(props: dict, filt: dict) -> bool:
    """Does a candidate source node (by its live props) satisfy one
    source-filter entry?"""
    if filt.get("externalOnly") and is_patchspace_owned(props):
        # The "everything that isn't ours" presets.  Without this, Patch
        # Space's own keepalives - which *are* Stream/Output/Audio - are
        # members of All Apps, so a filter there is fed the pipeline's own
        # plumbing, and a bundle carrying that back into an output is a loop.
        return False

    filt_id = filt.get("id")
    if filt_id is not None and filt_id != props.get("_node_id"):
        return False

    node_name = filt.get("nodeName")
    if node_name is not None and (props.get("node.name") or "") != node_name:
        return False

    if filt.get("nameRegex") is not None or filt.get("name") is not None:
        if not _match_regex_or_substring(
            filt.get("nameRegex"), filt.get("name"), _source_haystack(props)
        ):
            return False

    if filt.get("appKey") is not None:
        wanted = str(filt.get("appKey") or "").strip().lower()
        if not wanted or app_key(props).lower() != wanted:
            return False

    if filt.get("mediaNameRegex") is not None or filt.get("mediaName") is not None:
        if not _match_regex_or_substring(
            filt.get("mediaNameRegex"),
            filt.get("mediaName"),
            props.get("media.name") or "",
        ):
            return False

    if filt.get("mediaClassRegex") is not None or filt.get("mediaClass") is not None:
        if not _match_regex_or_substring(
            filt.get("mediaClassRegex"),
            filt.get("mediaClass"),
            props.get("media.class") or "",
        ):
            return False

    if filt.get("descriptionRegex") is not None or filt.get("description") is not None:
        if not _match_regex_or_substring(
            filt.get("descriptionRegex"), filt.get("description"), _desc(props)
        ):
            return False

    for exclude in filt.get("exclude") or []:
        if matches_source_filter(props, exclude):
            return False
    return True


def find_source_nodes(graph, filters: List[dict]) -> List[int]:
    """Every live node classified as a source that matches ANY filter."""
    matches = []
    for node_id, node_data in graph.nodes().items():
        props = node_data.get("info", {}).get("props", {})
        if props.get("media.class") not in SOURCE_MEDIA_CLASSES:
            continue
        props = dict(props)
        props["_node_id"] = node_id
        if any(matches_source_filter(props, f) for f in filters):
            matches.append(node_id)
    return matches


# ---------------------------------------------------------------------------
# Sink / target matching
# ---------------------------------------------------------------------------


def matches_sink_target(node_id: int, props: dict, target: dict) -> bool:
    """Like matches_source_filter, but for sink targets: ``name`` is an
    EXACT match on node.name."""
    if target.get("externalOnly") and is_patchspace_owned(props):
        # As with find_source_nodes: All Outputs must not offer our own sinks
        # and dummy monitors as destinations.
        return False
    if not any(target.get(k) is not None for k in _TARGET_IDENTITY_KEYS):
        return False

    target_id = target.get("id")
    if target_id is not None and target_id != node_id:
        return False

    if target.get("nameRegex") is not None or target.get("name") is not None:
        name = props.get("node.name") or ""
        if target.get("nameRegex") is not None:
            pattern = _compile(target["nameRegex"])
            if pattern is None or not pattern.search(name):
                return False
        elif name != target.get("name"):
            return False

    if target.get("mediaClassRegex") is not None or target.get("mediaClass") is not None:
        if not _match_regex_or_substring(
            target.get("mediaClassRegex"),
            target.get("mediaClass"),
            props.get("media.class") or "",
        ):
            return False

    if target.get("descriptionRegex") is not None or target.get("description") is not None:
        if not _match_regex_or_substring(
            target.get("descriptionRegex"), target.get("description"), _desc(props)
        ):
            return False

    return True


def find_target_nodes(graph, target: dict) -> List[int]:
    """Resolve a sink target to every currently-matching node id."""
    target_id = target.get("id")
    if target_id is not None:
        return [target_id] if target_id in graph.nodes() else []
    matches = []
    for node_id, node_data in graph.nodes().items():
        props = node_data.get("info", {}).get("props", {})
        if matches_sink_target(node_id, props, target):
            matches.append(node_id)
    return matches


# ---------------------------------------------------------------------------
# Port groups / channel pairs
# ---------------------------------------------------------------------------


def port_groups_for_node(graph, node_id: int, direction: str) -> PortGroups:
    """Group a node's ports (of the given direction) by their port-name
    prefix: "probe_FL"/"probe_FR" -> group "probe"."""
    groups: PortGroups = {}
    for port_id, port_data in graph.ports_for_node(node_id).items():
        props = port_data.get("info", {}).get("props", {})
        if props.get("port.direction") != direction:
            continue
        channel = props.get("audio.channel")
        if not channel:
            continue
        port_name = props.get("port.name") or props.get("port.alias") or ""
        groups.setdefault(_group_name(port_name, channel), {})[channel] = port_id
    return groups


def _group_name(port_name: str, channel: str) -> str:
    suffix = f"_{channel}"
    if port_name.endswith(suffix):
        prefix = port_name[: -len(suffix)]
        if prefix:
            return prefix
    return "default"


def select_group(
    groups: PortGroups, type_filter: Optional[str], node_id: int
) -> Optional[Dict[str, int]]:
    if type_filter is not None:
        for name, group in groups.items():
            if name.lower() == type_filter.lower():
                return group
        return None
    if not groups:
        return None
    if len(groups) == 1:
        return next(iter(groups.values()))
    logger.warning(
        "Node %s has multiple port groups (%s) - an edge into it needs an "
        "explicit port 'type' to disambiguate",
        node_id,
        ", ".join(sorted(groups)),
    )
    return None


def resolve_channel_pairs(
    graph, source_node_id: int, target_node_id: int, type_filter: Optional[str] = None
) -> Set[Tuple[int, int]]:
    """The concrete (output_port, input_port) pairs connecting
    source_node_id to target_node_id, matched up by shared audio.channel
    within the selected port groups."""
    source_groups = port_groups_for_node(graph, source_node_id, "out")
    target_groups = port_groups_for_node(graph, target_node_id, "in")
    source_group = select_group(source_groups, None, source_node_id)
    target_group = select_group(target_groups, type_filter, target_node_id)
    if not source_group or not target_group:
        return set()

    channels = set(source_group) & set(target_group)
    if not channels:
        return set()

    current = set(graph.ports().keys())
    return {
        (source_group[ch], target_group[ch])
        for ch in channels
        if source_group[ch] in current and target_group[ch] in current
    }
