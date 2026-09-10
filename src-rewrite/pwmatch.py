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
from functools import lru_cache
from typing import Dict, List, Optional, Pattern, Set, Tuple

logger = logging.getLogger(__name__)

# media classes the engine will treat as a routable *source*: ordinary
# app playback streams, sinks (their monitor ports mirror what plays
# into them), and hardware capture devices.
SOURCE_MEDIA_CLASSES = ("Stream/Output/Audio", "Audio/Sink", "Audio/Source")

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
