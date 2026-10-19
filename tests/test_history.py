"""M2 -- commit walking, change types, and rename following."""
from __future__ import annotations

from fixture_repo import DANA, JEEVAN
from prism.history import walk


def test_only_the_selected_authors_commits(commits):
    assert len(commits) == 5
    assert all(c.author_email == JEEVAN.email for c in commits)


def test_danas_commit_is_excluded(commits):
    subjects = [c.subject for c in commits]
    assert "Harden login validation" not in subjects


def test_commits_are_chronological(commits):
    timestamps = [c.authored_at for c in commits]
    assert timestamps == sorted(timestamps)


def test_subjects_in_order(commits):
    assert [c.subject for c in commits] == [
        "Initial project skeleton",
        "Add JWT login endpoint",
        "Rename util to helpers",
        "Drop legacy module",
        "Add web client",
    ]


def test_added_files_are_marked_A(commits):
    initial = commits[0]
    assert {c.path for c in initial.changes} == {
        "app/__init__.py",
        "app/tokens.py",
        "app/util.py",
        "legacy.py",
        "requirements.txt",
    }
    assert all(c.change_type == "A" for c in initial.changes)


def test_line_counts_are_recorded(commits):
    auth_commit = next(c for c in commits if c.subject == "Add JWT login endpoint")
    change = next(c for c in auth_commit.changes if c.path == "app/auth.py")
    assert change.insertions == 13
    assert change.deletions == 0
    assert auth_commit.files_changed == 1


def test_rename_is_detected_with_both_paths(commits):
    rename_commit = next(c for c in commits if c.subject == "Rename util to helpers")
    change = next(c for c in rename_commit.changes if c.path == "app/helpers.py")
    assert change.change_type == "R"
    assert change.old_path == "app/util.py"
    assert change.is_rename


def test_deletion_is_marked_D(commits):
    delete_commit = next(c for c in commits if c.subject == "Drop legacy module")
    change = next(c for c in delete_commit.changes if c.path == "legacy.py")
    assert change.change_type == "D"
    assert change.deletions > 0


def test_author_filter_is_exact_not_substring(repo, identities):
    """A regex superset must never credit the wrong person.

    ``--author`` is passed to git as a regex for speed; the guard against it
    over-matching is the exact ``%ae`` comparison in walk().
    """
    from prism.identity import resolve

    dana = resolve(identities, [DANA.email])
    dana_commits = walk(repo, dana)
    assert len(dana_commits) == 1
    assert dana_commits[0].subject == "Harden login validation"


def test_merged_identity_sees_both_authors_commits(repo, identities):
    from prism.identity import resolve

    both = resolve(identities, [JEEVAN.email, DANA.email])
    assert len(walk(repo, both)) == 6


def test_since_filter_narrows_the_window(repo, jeevan):
    recent = walk(repo, jeevan, since="2026-02-13T00:00:00+00:00")
    assert [c.subject for c in recent] == [
        "Rename util to helpers",
        "Drop legacy module",
        "Add web client",
    ]


# -- regression: author addresses containing "+" ---------------------------

def test_author_email_with_plus_is_not_treated_as_a_regex(tmp_path):
    r"""git's --author is a POSIX basic regex unless told otherwise.

    An escaped ``\+`` there is the GNU "one or more" quantifier, not a literal
    plus, so a regex-escaped address silently matched ZERO commits. Every
    GitHub noreply address (``<id>+<user>@users.noreply.github.com``) and every
    ``user+tag@`` alias has this shape, and the failure was invisible: the
    graph just quietly omitted that person's work.
    """
    from fixture_repo import JEEVAN_GITHUB, build_plus_email_repo
    from prism.identity import detect_identities, resolve

    repo = build_plus_email_repo(tmp_path)
    identity = resolve(detect_identities(repo), [JEEVAN_GITHUB.email])

    commits = walk(repo, identity)
    assert len(commits) == 2, "commits from a '+' address were dropped"
    assert all(c.author_email == JEEVAN_GITHUB.email for c in commits)


def test_plus_address_merges_with_a_normal_one(tmp_path):
    from fixture_repo import JEEVAN, JEEVAN_GITHUB, build_plus_email_repo
    from prism.identity import detect_identities, resolve

    repo = build_plus_email_repo(tmp_path)
    merged = resolve(detect_identities(repo), [JEEVAN.email, JEEVAN_GITHUB.email])

    assert len(walk(repo, merged)) == 3


def test_author_email_matching_ignores_case(tmp_path):
    """Identities are stored casefolded; git stores the address as typed.

    Searching a casefolded address against a commit written with capitals
    matched nothing, dropping that author's commits silently -- the same
    failure mode as the "+" above, from a different direction.
    """
    from fixture_repo import JEEVAN_GITHUB, build_plus_email_repo
    from prism.identity import detect_identities, resolve

    repo = build_plus_email_repo(tmp_path)
    assert "J" in JEEVAN_GITHUB.email, "fixture must carry an uppercase letter"

    identity = resolve(detect_identities(repo), [JEEVAN_GITHUB.email])
    assert identity.primary_email == JEEVAN_GITHUB.email.casefold()
    assert len(walk(repo, identity)) == 2
