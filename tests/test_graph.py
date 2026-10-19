"""Cross-cutting invariants.

These do not test one stage; they test properties the whole graph must have no
matter which stage produced a node or an edge. They are the tests most likely
to catch a regression introduced somewhere else.
"""
from __future__ import annotations

from fixture_repo import JEEVAN, LOGIN_LINES_BY_DANA
from prism.schema import NODE_TYPES, PROVENANCE_KINDS, RELATIONS, CONFIDENCES


def _edges(document, relation):
    return [e for e in document["edges"] if e["relation"] == relation]


# -- invariants -----------------------------------------------------------

def test_every_edge_has_provenance(document):
    for edge in document["edges"]:
        provenance = edge.get("provenance")
        assert provenance is not None, edge
        assert provenance["kind"] in PROVENANCE_KINDS
        assert provenance["confidence"] in CONFIDENCES


def test_every_node_has_a_known_type(document):
    for node in document["nodes"]:
        assert node["type"] in NODE_TYPES


def test_every_relation_is_in_the_vocabulary(document):
    for edge in document["edges"]:
        assert edge["relation"] in RELATIONS


def test_no_dangling_endpoints(document):
    ids = {n["id"] for n in document["nodes"]}
    for edge in document["edges"]:
        assert edge["source"] in ids
        assert edge["target"] in ids


def test_node_ids_are_unique(document):
    ids = [n["id"] for n in document["nodes"]]
    assert len(ids) == len(set(ids))


def test_exactly_one_person_and_one_repository(document):
    assert len([n for n in document["nodes"] if n["type"] == "Person"]) == 1
    assert len([n for n in document["nodes"] if n["type"] == "Repository"]) == 1


# -- structure ------------------------------------------------------------

def test_person_authored_every_commit(document):
    commits = [n for n in document["nodes"] if n["type"] == "Commit"]
    assert len(commits) == 5
    assert len(_edges(document, "AUTHORED")) == 5


def test_commit_changed_edges_carry_the_change_type(document):
    changed = _edges(document, "CHANGED")
    assert changed
    types = {e["change_type"] for e in changed}
    assert {"A", "R", "D"} <= types
    for edge in changed:
        assert edge["provenance"]["kind"] == "git_log"
        assert edge["provenance"]["commit"]


def test_rename_edge_records_the_old_path(document):
    rename = next(e for e in _edges(document, "CHANGED") if e["change_type"] == "R")
    assert rename["old_path"] == "app/util.py"
    assert rename["target"].endswith("app/helpers.py")


def test_defines_edges_cite_the_symbol_span(document):
    for edge in _edges(document, "DEFINES"):
        assert edge["provenance"]["location"].startswith("L")
        assert edge["provenance"]["kind"] == "ast"


def test_calls_edge_is_directed_the_right_way(document):
    call = next(
        e for e in _edges(document, "CALLS")
        if e["source"].endswith("#function:login")
    )
    assert call["target"].endswith("#function:issue_token")
    assert call["resolution"] == "import_resolved"


def test_member_of_points_from_method_to_class(document):
    members = _edges(document, "MEMBER_OF")
    if not members:
        return  # javascript grammar absent
    edge = members[0]
    assert "#method:" in edge["source"] or "#function:" in edge["source"]
    assert "#class:" in edge["target"]


def test_internal_import_edge_connects_two_files(document):
    imports = _edges(document, "IMPORTS")
    assert any(
        e["source"].endswith("app/auth.py") and e["target"].endswith("app/tokens.py")
        for e in imports
    )


def test_deleted_file_is_present_but_marked(document):
    legacy = next(n for n in document["nodes"] if n["id"].endswith("legacy.py"))
    assert legacy["exists_at_head"] is False
    assert legacy["touched_by_person"] is True


def test_authored_symbol_share_matches_the_blame_split(document):
    edge = next(
        e for e in _edges(document, "AUTHORED_SYMBOL")
        if e["target"].endswith("#function:login")
    )
    assert edge["span_lines"] == 9
    assert edge["blame_lines"] == 9 - LOGIN_LINES_BY_DANA
    assert edge["provenance"]["kind"] == "git_blame"
    assert edge["provenance"]["location"] == "L9-L17"


def test_contributed_to_covers_every_touched_file(document):
    files = [n for n in document["nodes"] if n["type"] == "File"]
    contributed = _edges(document, "CONTRIBUTED_TO")
    touched = [f for f in files if f["touched_by_person"]]
    assert len(contributed) == len(touched)


def test_demonstrates_only_counts_touched_evidence(document):
    edge = next(
        e for e in _edges(document, "DEMONSTRATES") if e["target"] == "tech:fastapi"
    )
    assert edge["files"] >= 1
    assert "import" in edge["signals"]
    # Counts only, no score -- ranking is a Phase 2 decision.
    assert "score" not in edge
    assert "weight" not in edge


def test_language_usage_is_anchored_at_the_repository_not_per_file(document):
    """Language edges must not restate File.language once per file."""
    python_uses = [
        e for e in _edges(document, "USES") if e["target"] == "tech:python"
    ]
    assert len(python_uses) == 1
    assert python_uses[0]["source"].startswith("repo:")


def test_person_node_carries_the_handle(document):
    person = next(n for n in document["nodes"] if n["type"] == "Person")
    assert person["handles"] == ["jeevansuresh"]
    assert person["emails"] == [JEEVAN.email]
