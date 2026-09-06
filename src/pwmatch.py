"""
pwmatch.py

Stateless matching helpers for turning a PatchSpace filter dict (see
patchSpace.py - same filter schema as pwroute.py's sourceFilters/
sinkFilters) into real, live node ids and port pairs on a
pwgraph.PipewireGraph.

This is deliberately just a bag of pure functions with no stored
state of its own (no cache, no rule ids) - PatchSpace.sync() calls
these fresh on every pass and does its own bookkeeping of what it
connected last time (see PatchSpace._edge_links). That's what makes
the "did we already connect this" question always have exactly one
right answer instead of two (a cache and reality) that can drift
apart - which is what made gates and volume nodes unreliable to
disconnect under the old rule-based router.

Filter field semantics (id/name/nameRegex/mediaClass/mediaClassRegex/
description/descriptionRegex, port group "type", channel matching) are
identical to pwroute.py's rule schema - see that module's docstring
for the full reference if you need it; it is not repeated here.

Two additions beyond pwroute.py's schema, both source-filter-only:

  * "nodeName" - an EXACT, case-sensitive match against the candidate's
    live `node.name` property (unlike "name", which is a case-insensitive
    SUBSTRING match against application.name-or-node.name). This is what
    every node that identifies itself by a real object's node.name should
    use (BackedNode identities, DeviceInputNode, the built-in PatchBay
    convenience nodes): node.name is a unique identifier, so loose
    substring matching makes one such node accidentally select any other
    live node whose name merely *contains* it - e.g. a virtual sink
    "PatchBay" whose filter would also match the daemon's own virtual
    microphone internals "PatchBay Mic"/"PatchBay Mic_sink". See
    patchSpace.py for the nodes that generate nodeName filters.

  * "exclude" - a list of filter dicts (same schema, recursively) that a
    candidate must match NONE of in addition to matching the filter
    itself. This is what backs patchSpace.ExcludeFilterNode - see
    matches_source_filter() below.
"""

from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Dict, List, Optional, Pattern, Set, Tuple

from pwgraph import PipewireGraph

logger = logging.getLogger(__name__)

# groups[group_name][channel] = port_id
PortGroups = Dict[str, Dict[str, int]]

# Node kinds sync() is willing to treat as a rule *source* - ordinary
# app playback, a sink's monitor ports (what lets a virtual sink like
# "PatchBay" act as a source), and hardware capture devices.
SOURCE_MEDIA_CLASSES = ("Stream/Output/Audio", "Audio/Sink", "Audio/Source")


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> Optional[Pattern[str]]:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.warning("Invalid regex %r in filter: %s", pattern, e)
        return None


def matches_source_filter(props: dict, filt: dict) -> bool:
    """Does a candidate source node (by its live props) satisfy one
    sourceFilters-style entry? All criteria set on `filt` must match
    (AND) - OR-ing several filters together is the caller's job."""
    filt_id = filt.get("id")
    if filt_id is not None and filt_id != props.get("_node_id"):
        return False

    node_name = filt.get("nodeName")
    if node_name is not None and (props.get("node.name") or "") != node_name:
        return False

    name_regex = filt.get("nameRegex")
    filt_name = filt.get("name")
    if name_regex is not None or filt_name is not None:
        haystack = props.get("application.name") or props.get("node.name") or ""
        if name_regex is not None:
            pattern = _compile_regex(name_regex)
            if pattern is None or not pattern.search(haystack):
                return False
        elif filt_name.lower() not in haystack.lower():
            return False

    media_regex = filt.get("mediaNameRegex")
    filt_media = filt.get("mediaName")
    if media_regex is not None or filt_media is not None:
        media_name = props.get("media.name") or ""
        if media_regex is not None:
            pattern = _compile_regex(media_regex)
            if pattern is None or not pattern.search(media_name):
                return False
        elif filt_media.lower() not in media_name.lower():
            return False

    class_regex = filt.get("mediaClassRegex")
    filt_class = filt.get("mediaClass")
    if class_regex is not None or filt_class is not None:
        media_class = props.get("media.class") or ""
        if class_regex is not None:
            pattern = _compile_regex(class_regex)
            if pattern is None or not pattern.search(media_class):
                return False
        elif filt_class.lower() not in media_class.lower():
            return False

    desc_regex = filt.get("descriptionRegex")
    filt_desc = filt.get("description")
    if desc_regex is not None or filt_desc is not None:
        description = (
            props.get("node.description")
            or props.get("node.nick")
            or props.get("node.name")
            or ""
        )
        if desc_regex is not None:
            pattern = _compile_regex(desc_regex)
            if pattern is None or not pattern.search(description):
                return False
        elif filt_desc.lower() not in description.lower():
            return False

    # "exclude" is a list of filter dicts (same schema, recursively -
    # an exclude entry can itself carry its own "exclude" list) that
    # this candidate must NOT match ANY of. This is how
    # patchSpace.ExcludeFilterNode narrows an upstream source filter
    # without needing a separate "NOT" concept in pwmatch itself -
    # chaining several exclude-filter nodes just appends more entries
    # to this same list (see PatchSpace._resolve_sources), so N
    # chained exclude nodes become N entries here, each checked with
    # this exact function.
    excludes = filt.get("exclude")
    if excludes and any(matches_source_filter(props, ex) for ex in excludes):
        return False

    return True


def find_source_nodes(graph: PipewireGraph, filters: List[dict]) -> List[int]:
    """Every live node classified as a valid source (see
    SOURCE_MEDIA_CLASSES) that matches ANY of `filters` (OR)."""
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


def matches_sink_target(node_id: int, props: dict, target: dict) -> bool:
    """Like matches_source_filter, but for sinkFilters-style target
    identification: "name" is an EXACT match on node.name, and a
    target with no identifying field at all matches nothing."""
    target_id = target.get("id")
    target_name_regex = target.get("nameRegex")
    target_name = target.get("name")
    target_class_regex = target.get("mediaClassRegex")
    target_class = target.get("mediaClass")
    target_desc_regex = target.get("descriptionRegex")
    target_desc = target.get("description")

    if all(
        v is None
        for v in (
            target_id,
            target_name_regex,
            target_name,
            target_class_regex,
            target_class,
            target_desc_regex,
            target_desc,
        )
    ):
        return False

    if target_id is not None and target_id != node_id:
        return False

    if target_name_regex is not None:
        pattern = _compile_regex(target_name_regex)
        if pattern is None or not pattern.search(props.get("node.name") or ""):
            return False
    elif target_name is not None:
        if props.get("node.name") != target_name:
            return False

    if target_class_regex is not None:
        pattern = _compile_regex(target_class_regex)
        if pattern is None or not pattern.search(props.get("media.class") or ""):
            return False
    elif target_class is not None:
        if target_class.lower() not in (props.get("media.class") or "").lower():
            return False

    if target_desc_regex is not None or target_desc is not None:
        description = (
            props.get("node.description")
            or props.get("node.nick")
            or props.get("node.name")
            or ""
        )
        if target_desc_regex is not None:
            pattern = _compile_regex(target_desc_regex)
            if pattern is None or not pattern.search(description):
                return False
        elif target_desc.lower() not in description.lower():
            return False

    return True


def find_target_nodes(graph: PipewireGraph, target: dict) -> List[int]:
    """Resolve a sinkFilters-style entry to every currently-matching
    node id - "id" and exact "name" match at most one, the rest can
    fan out to several."""
    target_id = target.get("id")
    if target_id is not None:
        return [target_id] if target_id in graph.nodes() else []

    matches = []
    for node_id, node_data in graph.nodes().items():
        props = node_data.get("info", {}).get("props", {})
        if matches_sink_target(node_id, props, target):
            matches.append(node_id)
    return matches


def port_groups_for_node(
    graph: PipewireGraph, node_id: int, direction: str
) -> PortGroups:
    """Group a node's ports (of the given port.direction) by their
    port-name prefix, e.g. "probe_FL"/"probe_FR" -> group "probe"."""
    groups: PortGroups = {}
    for port_id, port_data in graph.ports_for_node(node_id).items():
        props = port_data.get("info", {}).get("props", {})
        if props.get("port.direction") != direction:
            continue
        channel = props.get("audio.channel")
        if not channel:
            continue
        port_name = props.get("port.name") or props.get("port.alias") or ""
        group_name = _group_name(port_name, channel)
        groups.setdefault(group_name, {})[channel] = port_id
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
    graph: PipewireGraph,
    source_node_id: int,
    target_node_id: int,
    type_filter: Optional[str] = None,
) -> Set[Tuple[int, int]]:
    """The concrete (output_port, input_port) pairs that connect
    source_node_id's output ports to target_node_id's input ports,
    matched up by shared audio.channel within the selected port
    groups. Returns an empty set if the nodes have no compatible
    ports right now (ambiguous groups, no shared channels, etc.)."""
    source_groups = port_groups_for_node(graph, source_node_id, "out")
    target_groups = port_groups_for_node(graph, target_node_id, "in")

    source_group = select_group(source_groups, None, source_node_id)
    target_group = select_group(target_groups, type_filter, target_node_id)
    if not source_group or not target_group:
        return set()

    channels = set(source_group) & set(target_group)
    if not channels:
        return set()

    current_ports = set(graph.ports().keys())
    pairs = {
        (source_group[ch], target_group[ch])
        for ch in channels
        if source_group[ch] in current_ports and target_group[ch] in current_ports
    }
    return pairs
