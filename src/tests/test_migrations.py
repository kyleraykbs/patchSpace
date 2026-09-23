"""Legacy config migration: the old regex/media-class/description filter
leaves become bundle pipeline nodes, in memory, idempotently."""

import migrations
from session_repair import repair


def _config(nodes, edges, groups=None):
    cfg = {"nodes": nodes, "edges": edges, "groups": groups or []}
    return cfg


def _types(cfg):
    return {nid: node["type"] for nid, node in cfg["nodes"].items()}


def test_migrate_regex_input_to_bundle_pipeline():
    cfg = _config(
        nodes={
            "in1": {"type": "regex_input", "params": {"pattern": "Firefox"}},
            "out1": {"type": "app_output", "params": {"app_name": "x"}},
        },
        edges=[{"from": "in1", "to": "out1"}],
    )
    migrated, fixes = migrations.migrate(cfg)
    types = _types(migrated)
    assert migrated["schema_version"] == migrations.SCHEMA_VERSION
    assert "in1" not in migrated["nodes"]
    assert "all_inputs" in migrated["nodes"]
    classifier = next(n for n, t in types.items() if t == "regex_classifier")
    filter_id = next(n for n, t in types.items() if t == "filter")
    assert migrated["nodes"][classifier]["params"]["pattern"] == "Firefox"
    # The old edge now leaves the filter.
    assert {"from": filter_id, "to": "out1"} in migrated["edges"]
    # Preset -> filter and classifier -> filter wiring exists.
    assert {"from": "all_inputs", "to": filter_id, "to_port": "in"} in migrated["edges"]
    assert {
        "from": classifier, "to": filter_id, "to_port": "filter"
    } in migrated["edges"]
    assert any("regex_input" in f for f in fixes)


def test_migrate_regex_output_to_bundle_terminal():
    cfg = _config(
        nodes={
            "src": {"type": "app_input", "params": {"app_name": "x"}},
            "o1": {"type": "media_class_output",
                   "params": {"media_class": "Audio/Sink"}},
        },
        edges=[{"from": "src", "to": "o1"}],
    )
    migrated, _ = migrations.migrate(cfg)
    types = _types(migrated)
    terminal = next(n for n, t in types.items() if t == "bundle_output")
    filter_id = next(n for n, t in types.items() if t == "filter")
    classifier = next(n for n, t in types.items() if t == "media_class_classifier")
    assert "all_outputs" in migrated["nodes"]
    # The source now feeds the terminal.
    assert {"from": "src", "to": terminal} in migrated["edges"]
    assert {"from": filter_id, "to": terminal, "to_port": "bundle"} in migrated["edges"]
    assert {"from": "all_outputs", "to": filter_id, "to_port": "in"} in migrated["edges"]
    assert {
        "from": classifier, "to": filter_id, "to_port": "filter"
    } in migrated["edges"]


def test_migrate_is_idempotent():
    cfg = _config(
        nodes={"in1": {"type": "regex_input", "params": {"pattern": "a"}}},
        edges=[],
    )
    once, _ = migrations.migrate(cfg)
    snapshot = {n: dict(v) for n, v in once["nodes"].items()}
    twice, fixes = migrations.migrate(once)
    assert fixes == []
    assert twice["nodes"] == snapshot


def test_migrate_preserves_panel_prefix():
    cfg = _config(
        nodes={
            "kit::in1": {"type": "regex_input", "params": {"pattern": "a"}},
        },
        edges=[],
    )
    migrated, _ = migrations.migrate(cfg)
    # The generated classifier/filter land in the same panel...
    generated = [nid for nid in migrated["nodes"] if nid not in ("all_inputs",)]
    assert generated and all(nid.startswith("kit::") for nid in generated)
    # ...while the shared preset stays at the root so panels reuse it.
    assert "all_inputs" in migrated["nodes"]


def test_repair_migrates_and_keeps_schema_version():
    cfg = _config(
        nodes={"in1": {"type": "description_input", "params": {"description": "d"}}},
        edges=[],
    )
    result = repair(cfg)
    assert result.config["schema_version"] == migrations.SCHEMA_VERSION
    assert "in1" not in result.config["nodes"]
    assert any("migration:" in f for f in result.fixes)
    # The caller's dict is not mutated.
    assert "in1" in cfg["nodes"]


def test_repair_is_clean_after_migration():
    cfg = _config(
        nodes={
            "in1": {"type": "regex_input", "params": {"pattern": "Firefox"}},
            "sink": {"type": "app_output", "params": {"app_name": "x"}},
        },
        edges=[{"from": "in1", "to": "sink"}],
    )
    result = repair(cfg)
    # No bad-port/unknown-type errors from the rewritten pipeline.
    assert result.errors == []
