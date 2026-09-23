"""Tests for session_repair: validating and fixing a saved session JSON."""

from session_repair import (
    ERROR,
    WARNING,
    repair,
    validate,
)


def _config(nodes, edges, groups=None):
    return {"nodes": nodes, "edges": edges, "groups": groups or []}


def test_validate_flags_missing_endpoint_and_bad_port():
    cfg = _config(
        nodes={
            "a": {"type": "splitter", "params": {}},
            "b": {"type": "inverse_switcher", "params": {}},
        },
        edges=[
            {"from": "a", "to": "does_not_exist"},
            {"from": "a", "to": "b", "to_port": "nope"},
        ],
    )
    codes = {(i.code, i.where) for i in validate(cfg)}
    assert ("missing-endpoint", "a->does_not_exist") in codes
    assert any(c == "bad-port" for c, _ in codes)


def test_main_strict_check_rejects_a_config_that_needs_repairs(tmp_path):
    """`--check` alone reports the problems and exits 0 when the repair pass
    fixed them (a dropped bad edge is a "fix", not a failure).  A declarative
    config - what the Nix module generates - must be valid as written, which
    is what `--strict` is for: it fails on the *input*, so Nix can't silently
    load a graph different from the one it declared."""
    import json

    import session_repair

    broken = _config(
        nodes={
            "kick": {"type": "button", "params": {}},
            "vol": {"type": "volume", "params": {}},
        },
        # An impulse output into an audio input: an error the repairer drops.
        edges=[{"from": "kick", "to": "vol"}],
    )
    path = tmp_path / "session.json"
    path.write_text(json.dumps(broken))

    assert session_repair.main([str(path), "--check"]) == 0
    assert session_repair.main([str(path), "--check", "--strict"]) == 1

    valid = _config(
        nodes={
            "kick": {"type": "button", "params": {}},
            "boom": {"type": "sound_effect", "params": {}},
        },
        edges=[{"from": "kick", "to": "boom"}],
    )
    path.write_text(json.dumps(valid))
    assert session_repair.main([str(path), "--check", "--strict"]) == 0


def test_repair_normalizes_legacy_switch_ports():
    # inverse_switcher inputs are on/off; an old session used a/b.
    cfg = _config(
        nodes={
            "sw": {"type": "splitter", "params": {}},
            "tog": {"type": "inverse_switcher", "params": {}},
        },
        edges=[{"from": "sw", "to": "tog", "to_port": "a"}],
    )
    result = repair(cfg)
    assert result.config["edges"] == [
        {"from": "sw", "to": "tog", "to_port": "on"}
    ]
    assert any("normalized ports" in f for f in result.fixes)
    assert result.issues == []


def test_repair_drops_unknown_node_and_its_edges():
    cfg = _config(
        nodes={
            "good": {"type": "splitter", "params": {}},
            "weird": {"type": "totally_made_up", "params": {}},
        },
        edges=[{"from": "good", "to": "weird"}],
    )
    result = repair(cfg)
    assert "weird" not in result.config["nodes"]
    assert result.config["edges"] == []
    assert any("unknown type" in f for f in result.fixes)


def test_repair_drops_duplicate_edges():
    cfg = _config(
        nodes={
            "a": {"type": "splitter", "params": {}},
            "b": {"type": "splitter", "params": {}},
        },
        edges=[{"from": "a", "to": "b"}, {"from": "a", "to": "b"}],
    )
    result = repair(cfg)
    assert result.config["edges"] == [{"from": "a", "to": "b"}]
    assert any("duplicate edge" in f for f in result.fixes)


def test_repair_drops_boolean_audio_mismatch():
    cfg = _config(
        nodes={
            "sound": {"type": "splitter", "params": {}},
            "gate": {"type": "gate", "params": {}},
        },
        # A gate's ctrl input is boolean; feeding it audio must not survive.
        edges=[{"from": "sound", "to": "gate", "to_port": "ctrl"}],
    )
    result = repair(cfg)
    assert result.config["edges"] == []
    assert any("boolean/audio" in f for f in result.fixes)


def test_repair_collapses_overlapping_groups():
    cfg = _config(
        nodes={
            "a": {"type": "splitter", "params": {}},
            "b": {"type": "splitter", "params": {}},
        },
        edges=[{"from": "a", "to": "b"}],
        groups=[
            {"id": "g1", "label": "G1", "nodes": ["a", "b"]},
            {"id": "g2", "label": "G2", "nodes": ["b"]},
        ],
    )
    result = repair(cfg, dedupe_groups=True)
    assert result.config["groups"] == [{"id": "g1", "label": "G1", "nodes": ["a", "b"]}]


def test_repair_keeps_overlapping_groups_by_default():
    """Overlapping group membership can be intentional, so it is not
    fixed unless asked."""
    cfg = _config(
        nodes={"a": {"type": "splitter", "params": {}}},
        edges=[],
        groups=[
            {"id": "g1", "label": "G1", "nodes": ["a"]},
            {"id": "g2", "label": "G2", "nodes": ["a"]},
        ],
    )
    assert len(repair(cfg).config["groups"]) == 2


def test_validate_flags_duplicate_builtin_line_nodes():
    cfg = _config(
        nodes={
            "line1": {"type": "patchspace_mic_device", "params": {}},
            "line2": {"type": "patchspace_mic_device", "params": {}},
        },
        edges=[],
    )
    issues = validate(cfg)
    assert any(i.code == "duplicate-line" and i.severity == WARNING for i in issues)


def test_repair_collapse_duplicate_lines_keeps_busiest():
    cfg = _config(
        nodes={
            "src": {"type": "splitter", "params": {}},
            "line_busy": {"type": "patchspace_mic_device", "params": {}},
            "line_idle": {"type": "patchspace_mic_device", "params": {}},
        },
        edges=[{"from": "src", "to": "line_busy"}],
    )
    result = repair(cfg, collapse_duplicate_lines=True)
    assert "line_busy" in result.config["nodes"]
    assert "line_idle" not in result.config["nodes"]
    assert result.config["edges"] == [{"from": "src", "to": "line_busy"}]
    assert any("collapsed duplicate" in f and "Patch Space Mic" in f for f in result.fixes)


def test_virtual_speakers_are_not_treated_as_builtin_duplicates():
    """virtual_speaker/virtual_mic are independent named devices, so two
    of them are two real devices, not a duplicate of the built-in."""
    cfg = _config(
        nodes={
            "vs1": {"type": "virtual_speaker", "params": {"backing_node_name": "a"}},
            "vs2": {"type": "virtual_speaker", "params": {"backing_node_name": "b"}},
        },
        edges=[],
    )
    assert not [i for i in validate(cfg) if i.code == "duplicate-line"]


def test_repair_drop_orphans_is_opt_in():
    cfg = _config(
        nodes={
            "used": {"type": "splitter", "params": {}},
            "orphan": {"type": "splitter", "params": {}},
        },
        edges=[],
    )
    assert "orphan" in repair(cfg).config["nodes"]
    assert "orphan" not in repair(cfg, drop_orphans=True).config["nodes"]


def test_validate_clean_config_has_no_errors():
    cfg = _config(
        nodes={
            "src": {"type": "regex_input", "params": {"pattern": ".*"}},
            "dst": {"type": "regex_output", "params": {"pattern": ".*"}},
        },
        edges=[{"from": "src", "to": "dst"}],
    )
    assert not [i for i in validate(cfg) if i.severity == ERROR]


def test_repair_returns_a_copy_not_the_input():
    cfg = _config(
        nodes={"a": {"type": "splitter", "params": {}}},
        edges=[],
    )
    original = {"nodes": dict(cfg["nodes"]), "edges": list(cfg["edges"]), "groups": []}
    repair(cfg, drop_orphans=True)
    assert cfg == original
