"""M7 -- querying the built graph.

This milestone exists to prove the Phase 1 graph can answer the eventual
product's question shape -- "which work demonstrates X, and how was it
implemented" -- without yet being that product.
"""
from __future__ import annotations

from fixture_repo import JEEVAN, LOGIN_LINES_BY_DANA
from prism.query import PortfolioGraph


def test_summary_reports_the_person_and_the_work(portfolio):
    summary = portfolio.summary()
    assert summary["person"] == JEEVAN.name
    assert summary["emails"] == [JEEVAN.email]
    assert summary["repository"] == "pulse-api"
    assert summary["commits"] == 5
    assert summary["files_touched"] == 7


def test_technologies_are_returned_with_evidence(portfolio):
    fastapi = next(t for t in portfolio.technologies() if t.name == "fastapi")
    assert fastapi.category == "framework"
    assert set(fastapi.signals) == {"import", "manifest"}
    sources = {e.source for e in fastapi.evidence}
    assert {"app/auth.py", "requirements.txt"} <= sources


def test_files_using_a_technology(portfolio):
    rows = portfolio.files_using("fastapi")
    paths = {r["path"] for r in rows}
    assert "app/auth.py" in paths

    auth = next(r for r in rows if r["path"] == "app/auth.py")
    assert auth["blame_lines"] == 17 - LOGIN_LINES_BY_DANA
    assert auth["evidence"].location == "L1"


def test_symbols_using_a_technology_carry_their_citation(portfolio):
    rows = portfolio.symbols_using("fastapi")
    login = next(r for r in rows if r["qualified_name"] == "login")
    assert (login["path"], login["start_line"], login["end_line"]) == ("app/auth.py", 9, 17)
    assert login["signature"] == "login(email: str, password: str)"
    assert login["decorators"] == ['@router.post("/login")']


def test_symbol_rows_expose_what_they_call(portfolio):
    """'How did I implement it' starts with what the code reaches for."""
    login = next(
        r for r in portfolio.authored_symbols() if r["qualified_name"] == "login"
    )
    assert "issue_token" in login["calls"]


def test_lookup_is_case_insensitive(portfolio):
    assert portfolio.files_using("FastAPI") == portfolio.files_using("fastapi")


def test_unknown_technology_returns_nothing_rather_than_guessing(portfolio):
    assert portfolio.files_using("kubernetes") == []


def test_min_share_filters_weak_attribution(portfolio):
    strong = portfolio.authored_symbols(min_share=0.9)
    names = {r["qualified_name"] for r in strong}
    assert "issue_token" in names        # 100%
    assert "login" not in names          # 5/9


def test_contributed_files_include_the_deleted_one(portfolio):
    rows = portfolio.contributed_files()
    legacy = next(r for r in rows if r["path"] == "legacy.py")
    assert legacy["exists_at_head"] is False
    assert legacy["blame_share"] is None   # nothing survives to blame


def test_commits_are_chronological(portfolio):
    timestamps = [c["authored_at"] for c in portfolio.commits()]
    assert timestamps == sorted(timestamps)


def test_graph_loads_from_a_saved_file(document, tmp_path):
    from prism.export import write_json

    path = write_json(document, tmp_path / "graph.json")
    loaded = PortfolioGraph.load(path)
    assert loaded.summary() == PortfolioGraph(document).summary()


def test_the_product_question_shape_is_answerable(portfolio):
    """A dry run of the Phase 2 query, using only Phase 1 data.

    "Which work demonstrates backend API development, and how did I implement
    it?" -> a technology, the files, the symbols, and a line citation for each.
    """
    rows = portfolio.files_using("fastapi")
    assert rows, "no files matched"

    symbols = portfolio.symbols_using("fastapi")
    assert symbols, "no symbols matched"

    for symbol in symbols:
        assert symbol["path"] and symbol["start_line"] and symbol["end_line"]
        assert symbol["blame_share"] > 0
