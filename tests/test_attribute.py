"""M4 -- blame-based attribution.

The load-bearing assertion in this file is the split one: Dana rewrote four
lines inside ``login()``, so Jeevan's share of that symbol must be exactly
``(span - 4) / span`` -- not 100%, and not zero. Everything else in Prism is
downstream of getting that number right.
"""
from __future__ import annotations

from fixture_repo import DANA, JEEVAN, LOGIN_LINES_BY_DANA
from prism.attribute import attribute, blame_file


def _file(attribution, path):
    return next((f for f in attribution.files if f.path == path), None)


def _symbol(attribution, path, qualified):
    return next(
        (s for s in attribution.symbols if s.key[0] == path and s.key[2] == qualified),
        None,
    )


# -- raw blame ------------------------------------------------------------

def test_blame_returns_one_author_per_line(repo):
    authors = blame_file(repo, "app/auth.py")
    assert len(authors) == 17
    assert set(authors) == {JEEVAN.email, DANA.email}


def test_blame_credits_danas_lines_to_dana(repo):
    authors = blame_file(repo, "app/auth.py")
    # Lines 12-15 are the two validation blocks she added.
    assert authors[11] == DANA.email
    assert authors[14] == DANA.email
    assert authors[0] == JEEVAN.email


def test_blame_of_a_missing_file_is_empty_not_an_error(repo):
    assert blame_file(repo, "does/not/exist.py") == []


# -- file-level rollup ----------------------------------------------------

def test_sole_authored_file_is_fully_attributed(attribution):
    tokens = _file(attribution, "app/tokens.py")
    assert tokens.blame_share == 1.0
    assert tokens.blame_lines == tokens.total_lines


def test_shared_file_is_partially_attributed(attribution):
    auth = _file(attribution, "app/auth.py")
    assert auth.total_lines == 17
    assert auth.blame_lines == 17 - LOGIN_LINES_BY_DANA
    assert 0.0 < auth.blame_share < 1.0
    assert auth.is_primary_author


def test_renamed_file_is_attributed_under_its_new_name(attribution):
    assert _file(attribution, "app/helpers.py") is not None
    assert _file(attribution, "app/util.py") is None


def test_empty_files_are_not_reported_as_failures(attribution):
    assert "app/__init__.py" in attribution.empty_paths
    assert attribution.failed_paths == ()


# -- symbol-level rollup --------------------------------------------------

def test_sole_authored_symbol_is_fully_attributed(attribution):
    issue = _symbol(attribution, "app/tokens.py", "issue_token")
    assert issue.blame_share == 1.0
    assert issue.blame_lines == issue.span_lines == 3


def test_shared_symbol_blame_share_is_exact(attribution):
    """The whole point of the module, in one assertion."""
    login = _symbol(attribution, "app/auth.py", "login")
    assert login.span_lines == 9                      # lines 9-17
    assert login.blame_lines == 9 - LOGIN_LINES_BY_DANA
    assert login.blame_share == round(5 / 9, 3)


def test_symbol_attribution_is_scoped_to_its_own_span(attribution):
    """A symbol's share must come from its own lines, not the whole file."""
    login = _symbol(attribution, "app/auth.py", "login")
    auth_file = _file(attribution, "app/auth.py")
    assert login.blame_share != auth_file.blame_share


def test_dana_gets_her_own_lines_and_not_jeevans(repo, dana, touched, parsed):
    """Running the same pipeline for the other author must not overlap."""
    hers = attribute(
        repo, dana, [f.path for f in touched.at_head], parsed.symbols
    )
    login = _symbol(hers, "app/auth.py", "login")
    assert login.blame_lines == LOGIN_LINES_BY_DANA
    # She never touched tokens.py, so it must not appear at all.
    assert _file(hers, "app/tokens.py") is None


def test_shares_are_rounded_for_reproducibility(attribution):
    for row in attribution.symbols:
        assert row.blame_share == round(row.blame_share, 3)


def test_max_files_cap_is_deterministic(repo, jeevan, touched, parsed):
    paths = [f.path for f in touched.at_head]
    first = attribute(repo, jeevan, paths, parsed.symbols, max_files=2)
    second = attribute(repo, jeevan, paths, parsed.symbols, max_files=2)
    assert first.blamed_paths == second.blamed_paths


# -- regression: non-source files must not count as authored lines ---------

def test_binary_and_model_files_are_not_blamed(tmp_path):
    """A checked-in model or index is not thousands of lines of authored work.

    These files have no NUL byte early enough for git to call them binary, so
    `git blame` happily reports every "line" in them. One pickled model and a
    FAISS index contributed 120,000 phantom lines to a portfolio before the
    blame set was filtered the same way the parse set already was.
    """
    import subprocess

    from prism.pipeline import BuildOptions, build
    from prism.touched import is_source_candidate

    repo = tmp_path / "modelrepo"
    repo.mkdir()

    def run(*args):
        subprocess.run(
            ["git", *args], cwd=repo, capture_output=True, check=True,
            env={
                "PATH": __import__("os").environ["PATH"],
                "GIT_AUTHOR_NAME": JEEVAN.name, "GIT_AUTHOR_EMAIL": JEEVAN.email,
                "GIT_COMMITTER_NAME": JEEVAN.name, "GIT_COMMITTER_EMAIL": JEEVAN.email,
                "GIT_AUTHOR_DATE": "2026-05-01T10:00:00+00:00",
                "GIT_COMMITTER_DATE": "2026-05-01T10:00:00+00:00",
            },
        )

    run("init", "-b", "main")
    (repo / "app.py").write_text("def main():\n    return 1\n", encoding="utf-8")
    # No NUL bytes, so neither git nor a naive check calls these binary.
    (repo / "model.pkl").write_text("x" * 50 + "\n" * 5000, encoding="utf-8")
    (repo / "vectors.index").write_text("y" * 50 + "\n" * 5000, encoding="utf-8")
    run("add", "-A")
    run("commit", "-m", "add app and model", "--no-gpg-sign")

    assert not is_source_candidate("model.pkl")
    assert not is_source_candidate("vectors.index")

    outcome = build(repo, BuildOptions(authors=[JEEVAN.email]))
    person = next(
        n for n, d in outcome.graph.nodes(data=True) if d.get("type") == "Person"
    )
    blamed = {
        outcome.graph.nodes[t]["path"]: data.get("blame_lines")
        for _, t, k, data in outcome.graph.out_edges(person, keys=True, data=True)
        if k == "CONTRIBUTED_TO"
    }

    assert blamed.get("app.py") == 2
    # Still present as contributions -- just not credited as authored lines.
    assert blamed.get("model.pkl", 0) == 0
    assert blamed.get("vectors.index", 0) == 0
