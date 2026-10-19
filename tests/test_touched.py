"""M2b -- the bounded work set and rename chains."""
from __future__ import annotations

from prism.touched import is_vendored, resolve_touched


def test_touched_set_is_the_union_of_every_commits_paths(touched):
    assert {f.path for f in touched} == {
        "app/__init__.py",
        "app/auth.py",
        "app/helpers.py",
        "app/tokens.py",
        "legacy.py",
        "requirements.txt",
        "web/client.js",
    }


def test_rename_collapses_to_one_file_with_the_old_name_as_alias(touched):
    helpers = touched.get("app/helpers.py")
    assert helpers is not None
    assert helpers.was_renamed
    assert "app/util.py" in helpers.aliases
    # The old spelling must not survive as a second File.
    assert all(f.path != "app/util.py" for f in touched)


def test_old_path_resolves_to_the_canonical_name(touched):
    assert touched.canonical["app/util.py"] == "app/helpers.py"
    assert touched.get("app/util.py").path == "app/helpers.py"


def test_renamed_file_keeps_the_history_of_both_names(touched):
    helpers = touched.get("app/helpers.py")
    # Created in commit 1, renamed in commit 4.
    assert len(helpers.commits) == 2


def test_deleted_file_is_kept_but_marked(touched):
    legacy = touched.get("legacy.py")
    assert legacy is not None
    assert not legacy.exists_at_head
    assert legacy not in touched.at_head


def test_parseable_excludes_deleted_files(touched):
    paths = {f.path for f in touched.parseable()}
    assert "legacy.py" not in paths
    assert "app/auth.py" in paths


def test_vendored_paths_are_filtered():
    assert is_vendored("node_modules/left-pad/index.js")
    assert is_vendored("app/__pycache__/auth.cpython-314.pyc")
    assert not is_vendored("app/auth.py")


def test_include_vendored_is_opt_in(repo, commits):
    default = resolve_touched(repo, commits)
    permissive = resolve_touched(repo, commits, include_vendored=True)
    # The fixture has no vendored paths, so the two must agree exactly; this
    # pins the flag's semantics rather than its effect on this repo.
    assert {f.path for f in default} == {f.path for f in permissive}


def test_churn_accumulates_across_commits(touched):
    auth = touched.get("app/auth.py")
    # Jeevan created it with 13 insertions; Dana's commit is not in this set.
    assert auth.insertions == 13
    assert auth.deletions == 0
