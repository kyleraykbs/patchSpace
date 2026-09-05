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
                "mediaClass": None,     # OPTIONAL. Substring match
                                        # (case-insensitive) against the
                                        # node's own media.class (e.g.
                                        # "Audio/Source", "Stream/Output/
                                        # Audio", "Audio/Sink"). Use this to
                                        # match a whole category of nodes
                                        # (e.g. every microphone/capture
                                        # device) instead of naming them
                                        # individually. or None = any.
                "mediaClassRegex": None,# OPTIONAL. Same regex treatment as
                                        # nameRegex, but against media.class.
                                        # Takes priority over "mediaClass".
                "description": None,   # OPTIONAL. Substring match
                                        # (case-insensitive) against the
                                        # node's node.description (falling
                                        # back to node.nick if description
                                        # is unset). Useful for hardware
                                        # devices, whose node.name is often
                                        # a cryptic id (e.g.
                                        # "alsa_output.pci-0000_0a_00.4...")
                                        # while node.description is the
                                        # human-readable label shown in
                                        # _log_audio_devices(). or None = any.
                "descriptionRegex": None, # OPTIONAL. Same regex treatment
                                        # as nameRegex, but against
                                        # node.description/node.nick.
                                        # Takes priority over "description".
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
                "mediaClass": None,     # OPTIONAL. Substring match
                                        # (case-insensitive) against the
                                        # target node's media.class (e.g.
                                        # "Stream/Input/Audio" to mean
                                        # "every app currently capturing
                                        # audio"). Unlike "name", this
                                        # matches EVERY node that qualifies,
                                        # not just the first one found -
                                        # see "Fan-out" below. or None = any.
                "mediaClassRegex": None,# OPTIONAL. Same regex treatment as
                                        # nameRegex, but against media.class.
                                        # Takes priority over "mediaClass".
                "description": None,   # OPTIONAL. Substring match
                                        # (case-insensitive) against the
                                        # target node's node.description
                                        # (falling back to node.nick, then
                                        # node.name, if description is
                                        # unset). Useful for hardware
                                        # devices whose node.name is a
                                        # cryptic id - match the friendly
                                        # label shown in
                                        # _log_audio_devices() instead
                                        # (e.g. "TOZO" or "Ryzen HD Audio
                                        # Controller Analog Stereo").
                                        # or None = any.
                "descriptionRegex": None, # OPTIONAL. Same regex treatment
                                        # as nameRegex, but against
                                        # node.description/node.nick/
                                        # node.name. Takes priority over
                                        # "description".
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
sinks defined with different port "type"s on the same node). Within a
single filter entry (source or sink), every criterion you set (id, name/
nameRegex, mediaName/mediaNameRegex, mediaClass/mediaClassRegex) must all
match (AND) - use separate list entries to OR different criteria together.

Sources: sync() considers three kinds of nodes as candidate rule sources -
ordinary app playback streams (media.class == "Stream/Output/Audio"),
audio sinks (media.class == "Audio/Sink"), whose monitor ports mirror
whatever's currently playing into them, and hardware capture devices
(media.class == "Audio/Source", e.g. a physical microphone). The sink
case is what lets a virtual "dummy" sink (e.g. a null-audio-sink named
"PatchBay") act as a source in a rule: route apps INTO PatchBay via
PipeWire's normal device selection, then a rule with sourceFilters
matching "PatchBay" routes its monitor ports onward to a real hardware
output. The Audio/Source case is what lets a rule route a physical mic
straight into one or more apps that are capturing audio, without those
apps needing to select it as their input device themselves.

Fan-out: sinkFilters[].id / .name / .nameRegex / .mediaClass /
.mediaClassRegex / .description / .descriptionRegex identify the target
node(s). "id" and "name" match at most one node each (id is exact; name
is an exact match on node.name). "nameRegex", "mediaClass",
"mediaClassRegex", "description", and "descriptionRegex" can each match
several nodes at once, and the rule is routed to ALL of them - this is
what lets a single sinkFilters entry mean "every app currently capturing
audio" (e.g. {"mediaClass": "Stream/Input/Audio"}) rather than needing
to name each app and update the rule whenever a new one launches. If a
filter entry sets no identifying field at all (no id/name/nameRegex/
mediaClass/mediaClassRegex/description/descriptionRegex), it matches
nothing.

Note on multiple sources into one sink: if more than one node satisfies
a rule's sourceFilters (e.g. mediaClass="Audio/Source" matches two
physical microphones), each gets routed into every matched sink
independently - PipeWire simply mixes multiple sources landing on the
same input port. That's usually what you want for something like "route
my mic into every app that's listening", but be aware of it if you have
several capture devices and only meant one of them; pin the exact device
with "name"/"nameRegex" (or "id") in that case instead of "mediaClass".

sinkFilters[].id / .name / .nameRegex / .mediaClass / .mediaClassRegex /
.description / .descriptionRegex: at least one must be set to identify
target node(s). Priority when several are set on the SAME filter entry:
id > nameRegex > name for identity, with mediaClassRegex > mediaClass
and descriptionRegex > description each applied as an additional
(AND'd) filter on top of whichever identity field matched (or on their
own, if id/name/nameRegex are all unset - e.g. a filter that sets only
"description" matches every node whose description contains that
substring).

NOTE: sinkFilters that only set "description"/"descriptionRegex" (no
"mediaClass"/"mediaClassRegex") match every node with that description
regardless of direction - which can mean an Audio/Sink and an
Audio/Source sharing one physical device's name both match. Add a
mediaClass constraint (e.g. {"mediaClass": "Audio/Sink", "description":
"..."}) if you need to pin the direction.

sinkFilters[].type ("port group"): some nodes expose more than one
group of ports for the same direction - e.g. a webrtc echo-canceller's
sink typically has both "playback_FL/FR" (normal input) and
"probe_FL/FR" (echo-reference input) port groups. A port's group name
is derived from its port.name with the trailing "_<channel>" stripped
(so "probe_FL" -> group "probe", "playback_FL" -> group "playback").
"type" selects which group to connect into by name (case-insensitive
exact match). type=None means "any": if the node only has one input
group that's used automatically; if it has more than one, the rule is
ambiguous and is skipped (with a log message) until "type" is set. This
applies per-node - when a sinkFilters entry fans out to several target
nodes, each one is evaluated for ambiguity independently.

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

IMPORTANT re: upserts and PatchSpace - PatchSpace.sync() is a full
reconciliation pass that recomputes its ENTIRE desired rule set and
calls add_rules() on all of it every time any node/edge changes -
including rules whose content hasn't actually changed since last time.
Every one of those is technically an upsert here, which is why
_disconnect_rule_connections() below is the single place responsible
for keeping _routed_cache consistent with what it actually tears down
- if that bookkeeping lived in the callers instead, an upsert of an
*unrelated* rule could silently leave a different rule's connections
undone (a real bug this file used to have: the cache kept saying a
link existed after this method had already pw-link -d'd it, so the
very next sync() skipped reconnecting it, permanently).
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
    substring "name"/"mediaName"/"mediaClass" filters. Returns None
    (rather than raising) on an invalid pattern, so a typo in one rule
    can't take down sync() for every rule - the caller treats None as
    "never matches" and logs once via the warning below.
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

        This method now avoids disconnecting/reconnecting if the rule
        content is identical to the existing one (and connections already
        exist), preventing unnecessary churn on repeated syncs.
        """
        with self._lock:
            rule_id = rule.get("id") or self._generate_id()
            old_rule = self._rules.get(rule_id)

            # Store (copy)
            stored = dict(rule)
            stored["id"] = rule_id
            self._rules[rule_id] = stored

            # If this is an upsert and the content is unchanged,
            # and we already have connections for this rule,
            # skip the disconnect/sync entirely.
            if (
                old_rule is not None
                and old_rule == stored
                and rule_id in self._rule_connections
            ):
                return rule_id

            # If this is an upsert and content changed (or no prior
            # connections), disconnect old ones first.
            if old_rule is not None and rule_id in self._rule_connections:
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

        Also avoids disconnecting/reconnecting for rules whose content
        hasn't changed.
        """
        with self._lock:
            rule_ids = []
            changed = False
            for rule in rules:
                rule_id = rule.get("id") or self._generate_id()
                old_rule = self._rules.get(rule_id)

                stored = dict(rule)
                stored["id"] = rule_id
                self._rules[rule_id] = stored

                # If unchanged and connections exist, nothing to do.
                if (
                    old_rule is not None
                    and old_rule == stored
                    and rule_id in self._rule_connections
                ):
                    rule_ids.append(rule_id)
                    continue

                # If changed (or new), disconnect old connections if any.
                if old_rule is not None and rule_id in self._rule_connections:
                    self._disconnect_rule_connections(rule_id)
                    changed = True

                rule_ids.append(rule_id)

        if changed:
            self.sync()
        return rule_ids

    def _store_rule_locked(self, rule: dict) -> RuleId:
        """Deprecated helper, retained for backward compatibility."""
        # This method is no longer used by add_rule/add_rules; kept in case
        # external code calls it directly.
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
                # _disconnect_rule_connections() purges _routed_cache
                # itself now - no need (and no ability, since it pops
                # _rule_connections[rule_id] as part of disconnecting)
                # to do it again here afterward.
                self._disconnect_rule_connections(rule_id)
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
            # _disconnect_rule_connections() already removed every
            # rule's pairs from _routed_cache above, but clear
            # unconditionally too in case any stale entries (e.g. from
            # ports that vanished without a matching disconnect) were
            # left over - clear_rules() should always leave a fully
            # empty slate.
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
        connections = self._rule_connections.pop(rule_id, None)
        if not connections:
            return

        logger.info(
            f"Disconnecting {len(connections)} connection(s) for rule {rule_id}"
        )
        # Remove from cache first so that sync() won't skip reconnecting them
        # if we re-add a rule later.
        self._routed_cache.difference_update(connections)

        # Disconnect each pair, but be tolerant if the link is already gone
        for source_port, target_port in list(connections):
            try:
                self._graph.disconnect(source_port, target_port)
            except Exception as e:
                # If the link is already removed, that's fine – we just log
                # at debug level to avoid noise.
                logger.debug(
                    f"Failed to disconnect {source_port} -> {target_port}: {e}"
                )

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
                # Audio/Source: a hardware capture device (e.g. a
                # physical microphone) - this is what lets a rule
                # route a mic directly into one or more apps that are
                # capturing audio.
                if media_class not in (
                    "Stream/Output/Audio",
                    "Audio/Sink",
                    "Audio/Source",
                ):
                    continue

                for rule in rules:
                    rule_id = rule.get("id")
                    source_filters = rule.get("sourceFilters", [])
                    if not any(
                        self._matches_filter(node_id, props, f) for f in source_filters
                    ):
                        continue
                    for target in rule.get("sinkFilters", []):
                        for target_node_id in self._find_nodes(target):
                            self._route(
                                node_id, target_node_id, target, linked_pairs, rule_id
                            )

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

        # mediaName / mediaNameRegex: same priority rule. Matches
        # media.name (e.g. a track/window title), NOT the node's own
        # media.class - see mediaClass/mediaClassRegex below for that.
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

        # mediaClass / mediaClassRegex: same priority rule, but against
        # the node's own media.class (e.g. "Audio/Source",
        # "Stream/Output/Audio"). Lets a filter match a whole category
        # of nodes instead of naming them individually.
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

        # description / descriptionRegex: same priority rule, but
        # against node.description (falling back to node.nick, then
        # node.name). Useful for hardware devices whose node.name is a
        # cryptic id.
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

        return True

    @staticmethod
    def _node_matches_target(node_id: int, props: dict, target: dict) -> bool:
        """
        Like _matches_filter, but for sinkFilters target identification:
        "name" is an EXACT match on node.name (not a substring match
        against application.name/node.name like sourceFilters uses),
        matching the existing sink-lookup behavior. Returns False (no
        match) if the target entry sets no identifying field at all.
        """
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
            return False  # nothing to identify by - matches nothing

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

    def _find_nodes(self, target: dict) -> List[int]:
        """
        Resolve a sinkFilters entry to every currently-matching node id.
        "id" and exact "name" naturally resolve to at most one node
        each; "nameRegex", "mediaClass", and "mediaClassRegex" can each
        match several nodes at once, and all of them are returned - see
        the "Fan-out" note in the module docstring.
        """
        target_id = target.get("id")
        if target_id is not None:
            return [target_id] if target_id in self._graph.nodes() else []

        matches = []
        for nid, node_data in self._graph.nodes().items():
            props = node_data.get("info", {}).get("props", {})
            if self._node_matches_target(nid, props, target):
                matches.append(nid)
        return matches

    # ---------- routing ----------

    def _route(
        self,
        source_node_id: int,
        target_node_id: int,
        target: dict,
        linked_pairs: Set[Tuple[int, int]],
        rule_id: Optional[str] = None,
    ) -> bool:
        """
        Returns True once source and target are fully connected for the
        selected port groups. Always records the connection pairs for the
        rule if a route is found, so that remove_rule() can later clean
        them up reliably.
        """
        graph = self._graph

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

        # Validate that all ports still exist
        current_ports = set(graph.ports().keys())
        if any(p[0] not in current_ports or p[1] not in current_ports for p in pairs):
            return False

        # ALWAYS record these pairs for this rule, regardless of whether
        # they are already linked or not. This ensures remove_rule() can
        # clean them up even if they were already in _routed_cache.
        if rule_id:
            self._rule_connections.setdefault(rule_id, set()).update(pairs)
        self._routed_cache.update(pairs)

        # Only connect the ones that aren't already linked in the live graph
        needed = [p for p in pairs if p not in linked_pairs]
        if needed:
            try:
                created_any = False
                for pair in needed:
                    created_any = graph.connect(*pair) or created_any
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
                # Even on failure, we've already recorded the pairs, but
                # they may not actually be linked. That's okay – the next
                # sync will try again. We do NOT remove them from the cache
                # because we want to keep trying; removing them would cause
                # repeated attempts.
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
