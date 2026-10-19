"""The command line surface."""
from __future__ import annotations

import json

import pytest

from fixture_repo import DANA, JEEVAN
from prism.cli import main


def test_identities_lists_both_authors(repo, capsys):
    assert main(["identities", str(repo)]) == 0
    out = capsys.readouterr().out
    assert JEEVAN.email in out
    assert DANA.email in out
    assert "COMMITS" in out


def test_build_writes_both_artifacts(repo, tmp_path, capsys):
    out_dir = tmp_path / "out"
    code = main(
        ["build", str(repo), "--author", JEEVAN.email, "--out", str(out_dir), "--quiet"]
    )
    assert code == 0
    assert (out_dir / "graph.json").exists()
    assert (out_dir / "REPORT.md").exists()

    document = json.loads((out_dir / "graph.json").read_text(encoding="utf-8"))
    assert document["meta"]["selected_author"] == JEEVAN.email


def test_build_is_reproducible_by_default(repo, tmp_path):
    """No wall-clock timestamp unless the caller asks for one."""
    first = tmp_path / "a"
    second = tmp_path / "b"
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(first), "-q"])
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(second), "-q"])
    assert (first / "graph.json").read_bytes() == (second / "graph.json").read_bytes()


def test_timestamp_flag_stamps_the_real_time(repo, tmp_path):
    out_dir = tmp_path / "t"
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(out_dir), "-q", "--timestamp"])
    document = json.loads((out_dir / "graph.json").read_text(encoding="utf-8"))
    assert not document["meta"]["built_at"].startswith("1970")


def test_no_blame_flag_warns_about_what_is_missing(repo, tmp_path):
    out_dir = tmp_path / "nb"
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(out_dir), "-q", "--no-blame"])
    document = json.loads((out_dir / "graph.json").read_text(encoding="utf-8"))
    assert any("blame skipped" in w for w in document["meta"]["warnings"])
    assert not [e for e in document["edges"] if e["relation"] == "AUTHORED_SYMBOL"]


def test_unknown_author_exits_with_a_usable_message(repo, capsys):
    assert main(["identities", str(repo)]) == 0
    code = main(["build", str(repo), "--author", "ghost@nowhere.com", "-o", "x", "-q"])
    assert code == 2
    assert "no author matching" in capsys.readouterr().err


def test_not_a_repository_fails_cleanly(tmp_path, capsys):
    code = main(["identities", str(tmp_path)])
    assert code == 1
    assert "error:" in capsys.readouterr().err


def test_query_tech_prints_files_and_symbols(repo, tmp_path, capsys):
    out_dir = tmp_path / "q"
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(out_dir), "-q"])
    capsys.readouterr()

    assert main(["query", str(out_dir / "graph.json"), "--tech", "fastapi"]) == 0
    out = capsys.readouterr().out
    assert "app/auth.py" in out
    assert "login" in out


def test_query_without_filters_prints_a_summary(repo, tmp_path, capsys):
    out_dir = tmp_path / "s"
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(out_dir), "-q"])
    capsys.readouterr()

    assert main(["query", str(out_dir / "graph.json")]) == 0
    out = capsys.readouterr().out
    assert "technologies demonstrated" in out
    assert "commits" in out


def test_report_subcommand_renders_from_a_saved_graph(repo, tmp_path, capsys):
    out_dir = tmp_path / "r"
    main(["build", str(repo), "-a", JEEVAN.email, "-o", str(out_dir), "-q"])
    capsys.readouterr()

    assert main(["report", str(out_dir / "graph.json")]) == 0
    assert "# pulse-api - Jeevan Suresh" in capsys.readouterr().out
