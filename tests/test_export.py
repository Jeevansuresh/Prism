"""M6 -- export, schema validation and byte-level determinism."""
from __future__ import annotations

import json

import pytest

from fixture_repo import JEEVAN
from prism.export import to_document, write_json
from prism.pipeline import BuildOptions, build
from prism.schema import SCHEMA_VERSION, validate_graph


def test_document_validates_against_the_schema(document):
    assert validate_graph(document) == []


def test_document_header(document):
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["directed"] is True
    assert document["multigraph"] is True


def test_meta_records_the_selection(document):
    meta = document["meta"]
    assert meta["selected_author"] == JEEVAN.email
    assert meta["repository"] == "pulse-api"
    assert len(meta["head_sha"]) == 40


def test_counts_match_the_arrays(document):
    counts = document["meta"]["counts"]
    assert counts["nodes"] == len(document["nodes"])
    assert counts["edges"] == len(document["edges"])
    assert sum(counts["by_type"].values()) == counts["nodes"]
    assert sum(counts["by_relation"].values()) == counts["edges"]


def test_nodes_and_edges_are_sorted(document):
    ids = [n["id"] for n in document["nodes"]]
    assert ids == sorted(ids)
    keys = [(e["source"], e["relation"], e["target"]) for e in document["edges"]]
    assert keys == sorted(keys)


def test_written_file_round_trips(document, tmp_path):
    path = write_json(document, tmp_path / "graph.json")
    assert json.loads(path.read_text(encoding="utf-8")) == document


def test_written_file_uses_lf_endings(document, tmp_path):
    path = write_json(document, tmp_path / "graph.json")
    assert b"\r\n" not in path.read_bytes()


def test_two_builds_are_byte_identical(repo, tmp_path):
    """The determinism guarantee, asserted end to end."""
    options = BuildOptions(authors=[JEEVAN.email])

    def render() -> bytes:
        outcome = build(repo, options)
        document = to_document(
            outcome.graph,
            repository=outcome.repo_slug,
            head_sha=outcome.head_sha,
            selected_author=outcome.identity.primary_email,
            warnings=outcome.warnings,
        )
        return write_json(document, tmp_path / "g.json").read_bytes()

    assert render() == render()


def test_write_refuses_an_invalid_document(tmp_path):
    broken = {
        "schema_version": SCHEMA_VERSION,
        "directed": True,
        "multigraph": True,
        "meta": {
            "built_at": "", "repository": "", "head_sha": "",
            "selected_author": "", "counts": {},
        },
        "nodes": [{"id": "a", "type": "Person"}],
        "edges": [
            {
                "source": "a", "target": "ghost", "relation": "AUTHORED",
                "provenance": {
                    "kind": "git_log", "confidence": "EXTRACTED",
                    "commit": None, "source": None, "location": None,
                },
            }
        ],
    }
    with pytest.raises(ValueError, match="dangling edge target"):
        write_json(broken, tmp_path / "bad.json")


def test_validator_catches_duplicate_node_ids():
    doc = {
        "schema_version": SCHEMA_VERSION,
        "directed": True,
        "multigraph": True,
        "meta": {
            "built_at": "", "repository": "", "head_sha": "",
            "selected_author": "", "counts": {},
        },
        "nodes": [{"id": "a", "type": "Person"}, {"id": "a", "type": "File"}],
        "edges": [],
    }
    assert any("duplicate node id" in e for e in validate_graph(doc))
