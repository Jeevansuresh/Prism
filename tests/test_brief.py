"""The context pack rendered for an LLM project workspace."""
from __future__ import annotations

import pytest

from fixture_repo import JEEVAN
from prism.brief import generate_overview, generate_project, write_context_pack


@pytest.fixture(scope="module")
def overview(portfolio):
    return generate_overview(portfolio)


def test_overview_names_the_person_and_the_source_of_truth(overview):
    assert "Jeevan Suresh" in overview
    assert "No language model produced these facts" in overview


def test_overview_explains_what_blame_share_means(overview):
    """A model reading a percentage needs to know what it measures."""
    assert "Blame share" in overview
    assert "fraction" in overview


def test_overview_carries_the_limits(overview):
    """The caveats must travel with the data, not sit in a README."""
    assert "What this data cannot tell you" in overview
    assert "Blame sees only surviving code" in overview
    assert "Only five languages are parsed" in overview


def test_overview_has_the_technology_to_project_mapping(overview):
    """The table that answers the question the system exists for."""
    assert "Which projects demonstrate which technology" in overview
    assert "fastapi" in overview


def test_project_file_is_self_locating(portfolio):
    """A retrieved fragment must say which project it came from."""
    project = portfolio.repositories()[0]
    text = generate_project(portfolio, project)

    assert text.startswith(f"# Project: {project['name']}")
    # The project name is repeated in section headers, not only at the top,
    # so a chunk from the middle still identifies itself.
    assert text.count(project["name"]) >= 4


def test_project_file_cites_full_paths(portfolio):
    project = portfolio.repositories()[0]
    text = generate_project(portfolio, project)
    assert "app/auth.py:9-17" in text


def test_project_file_lists_every_symbol_not_a_truncated_sample(portfolio):
    """report.py truncates for humans; retrieval cannot find what was cut."""
    project = portfolio.repositories()[0]
    text = generate_project(portfolio, project)

    symbols = portfolio.authored_symbols()
    for symbol in symbols:
        assert symbol["qualified_name"] in text
    assert "not shown" not in text


def test_write_context_pack_emits_overview_plus_one_file_per_project(portfolio, tmp_path):
    written = write_context_pack(portfolio, tmp_path)
    projects = portfolio.repositories()

    assert len(written) == len(projects) + 1
    assert written[0].name == "00-portfolio-overview.md"
    assert all(p.exists() for p in written)


def test_pack_files_are_utf8_with_lf(portfolio, tmp_path):
    written = write_context_pack(portfolio, tmp_path)
    for path in written:
        assert b"\r\n" not in path.read_bytes()
        path.read_text(encoding="utf-8")


def test_source_excerpts_are_opt_in(portfolio, tmp_path):
    without = write_context_pack(portfolio, tmp_path / "a")
    text = (tmp_path / "a" / without[1].name).read_text(encoding="utf-8")
    assert "Implementation excerpts" not in text


def test_data_files_are_summarised_not_enumerated(portfolio):
    """2,000 training images must not bury a project's actual source."""
    project = portfolio.repositories()[0]
    text = generate_project(portfolio, project)
    # The fixture has no data files, so the summary line must be absent too.
    assert "committed data/binary files" not in text or "not listed individually" in text


# -- token budgeting -------------------------------------------------------

def test_budget_prunes_the_weakest_evidence_first(portfolio):
    """Tighter budgets shed short helpers, not the substantial work."""
    from prism.brief import BriefOptions, generate_project

    project = portfolio.repositories()[0]
    loose = generate_project(portfolio, project, options=BriefOptions())
    tight = generate_project(
        portfolio, project, options=BriefOptions(min_symbol_span=10)
    )
    assert len(tight) < len(loose)

    # The biggest symbol survives; a one-liner does not.
    symbols = portfolio.authored_symbols()
    biggest = max(symbols, key=lambda s: s["span_lines"])
    assert biggest["qualified_name"] in tight


def test_pruning_is_stated_never_silent(portfolio):
    """A reader must know the list is partial, and by how much."""
    from prism.brief import BriefOptions, generate_project

    project = portfolio.repositories()[0]
    text = generate_project(
        portfolio, project, options=BriefOptions(min_symbol_span=10)
    )
    assert "Showing" in text and "of" in text
    assert "% of the lines attributed" in text


def test_fit_to_budget_returns_something_under_the_budget(portfolio):
    from prism.brief import fit_to_budget

    options, size = fit_to_budget(portfolio, 4000, with_source=False)
    assert size <= 4000


def test_fit_to_budget_prefers_the_loosest_rung_that_fits(portfolio):
    """It should not over-prune: a generous budget keeps everything."""
    from prism.brief import BriefOptions, fit_to_budget

    options, _ = fit_to_budget(portfolio, 10_000_000, with_source=False)
    assert options.min_symbol_span == BriefOptions().min_symbol_span


def test_impossible_budget_reports_rather_than_silently_overruns(portfolio):
    from prism.brief import fit_to_budget

    _, size = fit_to_budget(portfolio, 1, with_source=False)
    assert size > 1, "caller must be able to see the budget was not met"


def test_overview_is_never_pruned(portfolio, tmp_path):
    """The matrix and the limits are what make the rest interpretable."""
    from prism.brief import write_context_pack

    written = write_context_pack(portfolio, tmp_path, budget_tokens=500)
    overview = written[0].read_text(encoding="utf-8")
    assert "Which projects demonstrate which technology" in overview
    assert "What this data cannot tell you" in overview
