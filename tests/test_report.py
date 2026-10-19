"""The human-readable report."""
from __future__ import annotations

import pytest

from prism.report import generate


@pytest.fixture(scope="module")
def text(portfolio):
    return generate(portfolio)


def test_leads_with_the_person_and_the_project(text):
    assert text.startswith("# pulse-api - Jeevan Suresh")


def test_headline_numbers_are_present(text):
    assert "**5 commits**" in text
    assert "**7 source files** touched" in text


def test_technologies_section_cites_a_location(text):
    assert "## Technologies demonstrated" in text
    assert "`fastapi`" in text
    assert "app/auth.py:L1" in text


def test_symbols_section_cites_line_ranges(text):
    assert "## Symbols authored" in text
    assert "app/auth.py:9-17" in text


def test_blame_share_is_shown_as_a_fraction_of_the_span(text):
    # login(): 5 of its 9 lines.
    assert "(5/9)" in text


def test_deleted_file_is_marked(text):
    assert "`legacy.py` _(deleted)_" in text


def test_commits_are_listed_with_subjects(text):
    assert "## Commits" in text
    assert "Add JWT login endpoint" in text


def test_states_that_no_model_was_involved(text):
    assert "no language models were involved" in text


def test_report_is_written_as_utf8_lf(portfolio, tmp_path):
    from prism.report import write_report

    path = write_report(portfolio, tmp_path / "REPORT.md")
    assert b"\r\n" not in path.read_bytes()
    path.read_text(encoding="utf-8")   # must decode cleanly
