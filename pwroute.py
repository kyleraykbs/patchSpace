"""
pwroute.py

Rule-based auto-routing on top of a `pwgraph.PipewireGraph`. This layer
owns rules (what should be connected to what) and reconciliation (making
the live graph match those rules). It knows nothing about how rules are
persisted or edited by a UI - that's the patchbay's job.

Rule schema
-----------
    {
        "id": "s8ie65df",              # unique rule id, lives on the rule
                                        # itself (see "Rule ids" below).
        "rule": "connect",             # label, currently informational only
        "sourceFilters": [
            {
                "id": None,             # exact source node id, or None = any
                "name": "LibreWolf",    # substring match (case-insensitive)
                                        # against application.name / node.name,
                                        # or None = any
                "nameRegex": None,      # OPTIONAL. Regex (case-insensitive,
                                        # re.search - unanchored, so it
                                        # behaves like the substring match
                                        # but with pattern power) against the
                                        # same haystack as "name". If set,
                                        # this takes priority over "name"
                                        # for this filter entry - you only
                                        # need one or the other.
                                        # e.g. "^(LibreWolf|Firefox)$"
                "mediaName": None,      # substring match against media.name,
                                        # or None = any
                "mediaNameRegex": None, # OPTIONAL. Same regex treatment as
                                        # nameRegex, but against media.name.
                                        # Takes priority over "mediaName".
                "description": None,    # substring match (case-insensitive)
                                        # against node.description / node.nick,
                                        # or None = any
                "descriptionRegex": None, # OPTIONAL. Regex (case-insensitive,
                                        # re.search) against node.description /
                                        # node.nick. Takes priority over
                                        # "description" if set.
            },
        ],
        "sinkFilters": [
            {
                "id": None,             # exact target node id, or None
                "name": "Chromium input",  # exact match on node.name, or None
                "nameRegex": None,      # OPTIONAL. Regex (case-insensitive,
                                        # re.search) against node.name.
                                        # Takes priority over "name" if set.
                                        # Note this is unanchored/substring-
                                        # style matching via regex, which is
                                        # looser than the exact match "name"
                                        # does on its own - that's deliberate,
                                        # to make regex useful here at all.
                "description": None,    # substring match (case-insensitive)
                                        # against node.description / node.nick,
                                        # or None = any
                "descriptionRegex": None, # OPTIONAL. Regex (case-insensitive,
                                        # re.search) against node.description /
                                        # node.nick. Takes priority over
                                        # "description" if set.
                "type": None,           # which port group on the target node
                                        # to connect into - see below.
            },
        ],
    }

There is no directionality between the two lists - it's simply "every
output stream that matches any entry in sourceFilters gets connected to
every node matched by every entry in sinkFilters". sourceFilters entries
are OR'd together (a node only needs to match one of them); each
sinkFilters entry is resolved and routed to independently, so a rule can
fan a single matching source out to several different sinks (or several
sinks defined with different port "type"s on the same node).

Sources: sync() considers two kinds of nodes as candidate rule sources -
ordinary app playback streams (media.class == "Stream/Output/Audio")
and audio sinks (media.class == "Audio/Sink"), whose monitor ports
mirror whatever's currently playing into them. The latter is what lets
a virtual "dummy" sink (e.g. a null-audio-sink named "PatchBay") act as
a source in a rule: route apps INTO PatchBay via PipeWire's normal
device selection, then a rule with sourceFilters matching "PatchBay"
routes its monitor ports onward to a real hardware output.

sinkFilters[].id / .name / .nameRegex: at least one must be set to
identify the target node. Priority when several are set: id > nameRegex
> name.

sinkFilters[].type ("port group"): some nodes expose more than one
group of ports for the same direction - e.g. a webrtc echo-canceller's
sink typically has both "playback_FL/FR" (normal input) and
"probe_FL/FR" (echo-reference input) port groups. A port's group name
is derived from its port.name with the trailing "_<channel>" stripped
(so "probe_FL" -> group "probe", "playback_FL" -> group "playback").
"type" selects which group to connect into by name (case-insensitive
exact match). type=None means "any": if the node only has one input
group that's used automatically; if it has more than one, the rule is
ambiguous and is skipped (with a log message) until "type" is set.

Channels: whatever audio.channel keys the chosen source and target
groups have in common are connected (e.g. FL/FR for stereo, or a
single MONO/AUX channel for a mono probe input) - stereo is not
assumed.

Regex filters: an invalid regex pattern logs a warning and causes that
filter to match nothing (it will never fail loudly / crash sync()).
Compiled patterns are cached, so using the same pattern string across
many rules or many sync() passes doesn't repeatedly pay compilation
cost.

Rule ids
--------
Rules carry their own "id" - there's no separate id table to keep in
sync. add_rule() will generate one if the rule you hand it doesn't
already have one, and echoes it back onto the stored rule (and as the
return value) so you can hang on to it for remove_rule(). If you pass
a rule that already has an "id" (e.g. reloading a persisted rule),
that id is kept, which also makes add_rule() an upsert: adding a rule
whose id matches an existing rule replaces it. rules() returns the
live rules exactly as stored, so querying current state is just
reading the "id" field back off each one - no external bookkeeping
required.
"""

from __future__ import annotations

import logging
import re
import secrets
import string
import threading
from functools import lru_cache
from typing import Dict, List, Optional, Pattern, Set, Tuple

from pwgraph import PipewireGraph

logger = logging.getLogger(__name__)

RuleId = str

# groups[group_name][channel] = port_id
PortGroups = Dict[str, Dict[str, int]]

_ID_ALPHABET = string.ascii_lowercase + string.digits
_ID_LENGTH = 8


@lru_cache(maxsize=256)
def _compile_regex(pattern: str) -> Optional[Pattern[str]]:
    """
    Compile and cache a regex pattern for filter matching. Case-
    insensitive, to match the case-insensitivity of the plain
    substring "name"/"mediaName" filters. Returns None (rather than
    raising) on an invalid pattern, so a typo in one rule can't take
    down sync() for every rule - the caller treats None as "never
    matches" and logs once via the warning below.
    """
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error as e:
        logger.warning("Invalid regex %r in rule filter: %s", pattern, e)
        return None


class RuleRouter:
    """
    Wraps a PipewireGraph and keeps ports connected according to a set
    of rules. Safe to run multiple independent instances against the
    same or different PipewireGraph objects - all routing state is
    instance-local (no module globals).

    Rules are managed exclusively through add_rule()/remove_rule() -
    there's no bulk "rules=" constructor argument, so a caller loading
    persisted rules just loops over them and calls add_rule() for each.
    """

    def __init__(
        self,
        graph: PipewireGraph,
        auto_sync_on_change: bool = True,
    ):
        self._graph = graph
        self._lock = threading.Lock()

        self._rules: Dict[RuleId, dict] = {}
        self._rule_connections: Dict[RuleId, Set[Tuple[int, int]]] = {}

        # Cache of (source_port, target_port) pairs we've already
        # confirmed are linked, so we don't re-check/re-log every cycle.
        self._routed_cache: Set[Tuple[int, int]] = set()

        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()

        if auto_sync_on_change:
            graph.on_change(self._on_graph_change)

    # ---------- rule management ----------

    def add_rule(self, rule: dict) -> RuleId:
        """
        Add a rule and return its id (use this id to remove it later).

        If `rule` already has an "id" (e.g. it was previously removed,
        or is being restored from persisted storage), that id is kept
        and this call behaves as an upsert - it replaces any existing
        rule with the same id. Otherwise a fresh id is generated and
        written onto the stored copy of the rule.
        """
        with self._lock:
            rule_id = self._store_rule_locked(rule)
            # If this is an upsert, clean up old connections first
            if rule_id in self._rule_connections:
                self._disconnect_rule_connections(rule_id)
        self.sync()
        return rule_id

    def add_rules(self, rules: List[dict]) -> List[RuleId]:
        """
        Add several rules at once, syncing only after all of them are
        stored. Equivalent to calling add_rule() in a loop, but avoids
        a full reconciliation pass per rule - useful when loading a
        persisted rule set at startup. Returns the ids in the same
        order as `rules`, generating one for any rule that doesn't
        already carry an "id" (same upsert behavior as add_rule() for
        rules that do).
        """
        with self._lock:
            rule_ids = []
            for rule in rules:
                rule_id = self._store_rule_locked(rule)
                # If this is an upsert, clean up old connections first
                if rule_id in self._rule_connections:
                    self._disconnect_rule_connections(rule_id)
                rule_ids.append(rule_id)
        self.sync()
        return rule_ids

    def _store_rule_locked(self, rule: dict) -> RuleId:
        # Caller must hold self._lock.
        stored = dict(rule)
        rule_id = stored.get("id") or self._generate_id()
        stored["id"] = rule_id
        self._rules[rule_id] = stored
        return rule_id

    def remove_rule(self, rule_id: RuleId) -> bool:
        """Remove a rule by id. Returns True if it existed."""
        with self._lock:
            existed = self._rules.pop(rule_id, None) is not None
            if existed:
                self._disconnect_rule_connections(rule_id)
                # Also remove from routed cache
                self._routed_cache.difference_update(
                    self._rule_connections.get(rule_id, set())
                )
        if existed:
            self.sync()
        return existed

    def clear_rules(self) -> int:
        """Remove all rules and disconnect their connections. Returns how many were removed."""
        with self._lock:
            count = len(self._rules)
            rule_ids = list(self._rules.keys())
            for rule_id in rule_ids:
                self._disconnect_rule_connections(rule_id)
            self._rules.clear()
            self._routed_cache.clear()
        if count > 0:
            self.sync()
        return count

    def rules(self) -> Dict[RuleId, dict]:
        with self._lock:
            return dict(self._rules)

    def _generate_id(self) -> RuleId:
        # Caller already holds self._lock.
        while True:
            candidate = "".join(secrets.choice(_ID_ALPHABET) for _ in range(_ID_LENGTH))
            if candidate not in self._rules:
                return candidate

    def _disconnect_rule_connections(self, rule_id: RuleId) -> None:
        """Disconnect all connections that were created by this rule."""
        connections = self._rule_connections.get(rule_id, set())
        if not connections:
            return

        logger.info(
            f"Disconnecting {len(connections)} connection(s) for rule {rule_id}"
        )
        for source_port, target_port in connections:
            try:
                self._graph.disconnect(source_port, target_port)
            except Exception as e:
                logger.warning(
                    f"Failed to disconnect {source_port} -> {target_port}: {e}"
                )

        self._rule_connections.pop(rule_id, None)

    # ---------- lifecycle: optional periodic safety-net sync ----------

    def start_polling(self, interval: float = 1.0) -> None:
        """
        Start a background thread that calls sync() periodically. This
        is a belt-and-suspenders fallback in case a change is missed;
        on_change-driven syncing (enabled by default) should already
        catch everything pw-dump reports.
        """
        if self._poll_thread is not None:
            return
        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, args=(interval,), daemon=True
        )
        self._poll_thread.start()

    def stop_polling(self, timeout: Optional[float] = 2.0) -> None:
        self._poll_stop.set()
        if self._poll_thread:
            self._poll_thread.join(timeout=timeout)
        self._poll_thread = None

    def _poll_loop(self, interval: float) -> None:
        while not self._poll_stop.is_set():
            try:
                self.sync()
            except Exception:
                logger.exception("sync() raised unexpectedly in poll loop")
            self._poll_stop.wait(interval)

    # ---------- reconciliation ----------

    def _on_graph_change(self, graph: PipewireGraph) -> None:
        self.sync()

    def sync(self) -> None:
        """Re-check every output stream against every rule."""
        graph = self._graph

        with self._lock:
            self._clean_cache()
            linked_pairs = graph.linked_pairs()
            rules = list(self._rules.values())

            for node_id, node_data in graph.nodes().items():
                props = node_data.get("info", {}).get("props", {})
                media_class = props.get("media.class")
                # Stream/Output/Audio: a normal app playing audio.
                # Audio/Sink: a sink's monitor ports mirror whatever's
                # being played into it - this is what lets a virtual
                # sink (e.g. a "PatchBay" null-audio-sink) act as a
                # rule *source* that gets routed onward to a real
                # hardware output.
                if media_class not in ("Stream/Output/Audio", "Audio/Sink"):
                    continue

                for rule in rules:
                    rule_id = rule.get("id")
                    source_filters = rule.get("sourceFilters", [])
                    if not any(
                        self._matches_filter(node_id, props, f) for f in source_filters
                    ):
                        continue
                    for target in rule.get("sinkFilters", []):
                        self._route(node_id, target, linked_pairs, rule_id)

    def _clean_cache(self) -> None:
        """Remove cache entries for ports that no longer exist."""
        current_ports = set(self._graph.ports().keys())
        self._routed_cache = {
            pair
            for pair in self._routed_cache
            if pair[0] in current_ports and pair[1] in current_ports
        }

    # ---------- filter / target matching ----------

    @staticmethod
    def _matches_filter(node_id: int, props: dict, filt: dict) -> bool:
        filt_id = filt.get("id")
        if filt_id is not None and filt_id != node_id:
            return False

        # name / nameRegex: nameRegex takes priority if both are set.
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

        # mediaName / mediaNameRegex: same priority rule.
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

        # description / descriptionRegex: same priority rule.
        # Looks at node.description, node.nick, or falls back to node.name
        # if neither is available (some nodes only have node.name).
        desc_regex = filt.get("descriptionRegex")
        filt_desc = filt.get("description")
        if desc_regex is not None or filt_desc is not None:
            haystack = (
                props.get("node.description")
                or props.get("node.nick")
                or props.get("node.name")
                or ""
            )
            if desc_regex is not None:
                pattern = _compile_regex(desc_regex)
                if pattern is None or not pattern.search(haystack):
                    return False
            elif filt_desc.lower() not in haystack.lower():
                return False

        return True

    def _find_node(
        self,
        node_id: Optional[int],
        name: Optional[str],
        name_regex: Optional[str] = None,
        description: Optional[str] = None,
        description_regex: Optional[str] = None,
    ) -> Optional[int]:
        if node_id is not None:
            return node_id if node_id in self._graph.nodes() else None

        # Try name-based matching first (priority: nameRegex > name)
        if name_regex is not None:
            pattern = _compile_regex(name_regex)
            if pattern is not None:
                for nid, node_data in self._graph.nodes().items():
                    props = node_data.get("info", {}).get("props", {})
                    haystack = props.get("node.name") or ""
                    if pattern.search(haystack):
                        return nid
            # If pattern was invalid, don't fall back to name - it was
            # explicitly set and failed to compile, so matching nothing
            # is the correct behavior.
            if name is None and description is None and description_regex is None:
                return None

        if name is not None:
            for nid, node_data in self._graph.nodes().items():
                props = node_data.get("info", {}).get("props", {})
                if props.get("node.name") == name:
                    return nid

        # Try description-based matching (priority: descriptionRegex > description)
        if description_regex is not None:
            pattern = _compile_regex(description_regex)
            if pattern is not None:
                for nid, node_data in self._graph.nodes().items():
                    props = node_data.get("info", {}).get("props", {})
                    haystack = (
                        props.get("node.description")
                        or props.get("node.nick")
                        or props.get("node.name")
                        or ""
                    )
                    if pattern.search(haystack):
                        return nid
            # If pattern was invalid, matching nothing is correct
            return None

        if description is not None:
            for nid, node_data in self._graph.nodes().items():
                props = node_data.get("info", {}).get("props", {})
                haystack = (
                    props.get("node.description")
                    or props.get("node.nick")
                    or props.get("node.name")
                    or ""
                )
                if description.lower() in haystack.lower():
                    return nid

        return None  # no filters matched

    # ---------- routing ----------

    def _route(
        self,
        source_node_id: int,
        target: dict,
        linked_pairs: Set[Tuple[int, int]],
        rule_id: Optional[str] = None,
    ) -> bool:
        """
        Returns True once source and target are fully connected for the
        selected port groups. Only logs when something actually changes.
        Caller must hold self._lock.
        """
        graph = self._graph

        target_node_id = self._find_node(
            target.get("id"),
            target.get("name"),
            target.get("nameRegex"),
            target.get("description"),
            target.get("descriptionRegex"),
        )
        if target_node_id is None:
            return False

        source_groups = self._port_groups_for_node(source_node_id, "out")
        target_groups = self._port_groups_for_node(target_node_id, "in")

        source_group = self._select_group(source_groups, None, source_node_id)
        target_group = self._select_group(
            target_groups, target.get("type"), target_node_id
        )
        if not source_group or not target_group:
            return False

        channels = sorted(set(source_group) & set(target_group))
        if not channels:
            return False

        pairs = [(source_group[ch], target_group[ch]) for ch in channels]

        if all(p in self._routed_cache for p in pairs):
            return True  # already done, nothing to check or log

        current_ports = set(graph.ports().keys())
        if any(p[0] not in current_ports or p[1] not in current_ports for p in pairs):
            return False

        needed = [p for p in pairs if p not in linked_pairs]

        if not needed:
            self._routed_cache.update(pairs)
            if rule_id:
                self._rule_connections.setdefault(rule_id, set()).update(pairs)
            return True

        try:
            created_any = False
            for pair in needed:
                created_any = graph.connect(*pair) or created_any

            self._routed_cache.update(pairs)

            # Track connections for this rule
            if rule_id:
                self._rule_connections.setdefault(rule_id, set()).update(pairs)

            if created_any:
                logger.info(
                    "Routed %s -> %s (%s)",
                    source_node_id,
                    target_node_id,
                    target.get("type") or "default",
                )
        except Exception as e:
            logger.warning(
                "Failed to route %s -> %s: %s", source_node_id, target_node_id, e
            )
            return False

        return True

    # ---------- port group helpers ----------

    def _port_groups_for_node(self, node_id: int, direction: str) -> PortGroups:
        """
        Group this node's ports (of the given port.direction, "in" or
        "out") by their port-name prefix, e.g. "probe_FL"/"probe_FR" ->
        group "probe" with channels {"FL": port_id, "FR": port_id}.
        """
        groups: PortGroups = {}
        for port_id, port_data in self._graph.ports_for_node(node_id).items():
            props = port_data.get("info", {}).get("props", {})
            if props.get("port.direction") != direction:
                continue
            channel = props.get("audio.channel")
            if not channel:
                continue
            port_name = props.get("port.name") or props.get("port.alias") or ""
            group_name = self._group_name(port_name, channel)
            groups.setdefault(group_name, {})[channel] = port_id
        return groups

    @staticmethod
    def _group_name(port_name: str, channel: str) -> str:
        suffix = f"_{channel}"
        if port_name.endswith(suffix):
            prefix = port_name[: -len(suffix)]
            if prefix:
                return prefix
        return "default"

    def _select_group(
        self, groups: PortGroups, type_filter: Optional[str], node_id: int
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
            "Node %s has multiple port groups (%s) - rule needs an explicit "
            "'type' to disambiguate",
            node_id,
            ", ".join(sorted(groups)),
        )
        return None
