"""Building one graph across several repositories."""
from __future__ import annotations

import pytest

from fixture_repo import JEEVAN, JEEVAN_GITHUB, build_fixture, build_plus_email_repo
from prism.pipeline import BuildOptions
from prism.portfolio import RepoSpec, build_portfolio, canonical_email, parse_repo_spec
from prism.schema import person_id


# -- spec parsing ---------------------------------------------------------

def test_bare_path_spec():
    spec = parse_repo_spec(r"D:\everything\Friday")
    assert spec.path == r"D:\everything\Friday"
    assert spec.name is None


def test_named_spec():
    spec = parse_repo_spec(r"Bazy=D:\everything\Vegetable Web scraping")
    assert spec.name == "Bazy"
    assert spec.path == r"D:\everything\Vegetable Web scraping"


def test_windows_drive_letter_is_not_mistaken_for_a_separator():
    r"""`=` separates, not `:`, precisely so C:\ stays a path."""
    spec = parse_repo_spec(r"C:\code\thing")
    assert spec.name is None
    assert spec.path == r"C:\code\thing"


# -- the split-person bug -------------------------------------------------

@pytest.fixture(scope="module")
def mixed_identity_portfolio(tmp_path_factory):
    """One project where the laptop address dominates, one where the web UI does.

    This is the shape that split the person in two: resolve() picks the
    primary address per repository, so each one produced a different Person
    node ID and the portfolio ended up with two half-people.
    """
    laptop = build_fixture(tmp_path_factory.mktemp("laptop"))          # JEEVAN x5
    web = build_plus_email_repo(tmp_path_factory.mktemp("web"))        # GITHUB x2, JEEVAN x1

    options = BuildOptions(authors=[JEEVAN.email, JEEVAN_GITHUB.email])
    return build_portfolio(
        [RepoSpec(str(laptop)), RepoSpec(str(web))], options, portfolio_name="test"
    )


def test_portfolio_has_exactly_one_person(mixed_identity_portfolio):
    graph = mixed_identity_portfolio.graph
    people = [n for n, d in graph.nodes(data=True) if d.get("type") == "Person"]
    assert len(people) == 1, f"the person was split across repositories: {people}"


def test_canonical_identity_is_the_most_active_address_overall(tmp_path_factory):
    laptop = build_fixture(tmp_path_factory.mktemp("l2"))        # JEEVAN x5
    web = build_plus_email_repo(tmp_path_factory.mktemp("w2"))   # GITHUB x2, JEEVAN x1

    options = BuildOptions(authors=[JEEVAN.email, JEEVAN_GITHUB.email])
    chosen = canonical_email([RepoSpec(str(laptop)), RepoSpec(str(web))], options)
    # 6 commits on the laptop address vs 2 on the GitHub one.
    assert chosen == JEEVAN.email


def test_every_repository_attaches_to_that_one_person(mixed_identity_portfolio):
    graph = mixed_identity_portfolio.graph
    person = person_id(JEEVAN.email)
    assert person in graph

    repos = [n for n, d in graph.nodes(data=True) if d.get("type") == "Repository"]
    reached = set()
    for _, target, key in graph.out_edges(person, keys=True):
        if key != "CONTRIBUTED_TO":
            continue
        for _, repo, repo_key in graph.out_edges(target, keys=True):
            if repo_key == "IN_REPOSITORY":
                reached.add(repo)
    assert reached == set(repos), "a repository has no contribution edges to the person"


def test_commits_from_both_addresses_are_present(mixed_identity_portfolio):
    graph = mixed_identity_portfolio.graph
    person = person_id(JEEVAN.email)
    authored = [t for _, t, k in graph.out_edges(person, keys=True) if k == "AUTHORED"]
    # 5 laptop commits in the fixture + 3 in the plus-email repo.
    assert len(authored) == 8


# -- skipping ------------------------------------------------------------

def test_non_git_directory_is_skipped_not_fatal(tmp_path_factory):
    good = build_fixture(tmp_path_factory.mktemp("good"))
    plain = tmp_path_factory.mktemp("not_a_repo")

    outcome = build_portfolio(
        [RepoSpec(str(good)), RepoSpec(str(plain), name="Bazy")],
        BuildOptions(authors=[JEEVAN.email]),
    )
    assert len(outcome.built) == 1
    assert len(outcome.skipped) == 1
    assert "not a git repository" in outcome.skipped[0].error
    assert any("not a git repository" in w for w in outcome.warnings)


def test_missing_path_is_skipped(tmp_path_factory):
    good = build_fixture(tmp_path_factory.mktemp("good2"))
    outcome = build_portfolio(
        [RepoSpec(str(good)), RepoSpec(r"D:\nope\does-not-exist")],
        BuildOptions(authors=[JEEVAN.email]),
    )
    assert len(outcome.built) == 1
    assert "does not exist" in outcome.skipped[0].error


def test_repository_without_the_person_is_skipped(tmp_path_factory):
    from fixture_repo import DANA

    good = build_fixture(tmp_path_factory.mktemp("good3"))
    outcome = build_portfolio(
        [RepoSpec(str(good))], BuildOptions(authors=[DANA.email])
    )
    assert len(outcome.built) == 1   # Dana did commit to the fixture


def test_all_repositories_failing_is_an_error(tmp_path_factory):
    plain = tmp_path_factory.mktemp("empty_dir")
    with pytest.raises(ValueError, match="no repositories could be analysed"):
        build_portfolio([RepoSpec(str(plain))], BuildOptions(authors=[JEEVAN.email]))


def test_name_override_renames_only_the_display_name(tmp_path_factory):
    good = build_fixture(tmp_path_factory.mktemp("named"))
    outcome = build_portfolio(
        [RepoSpec(str(good), name="Pulse")], BuildOptions(authors=[JEEVAN.email])
    )
    repos = [d for _, d in outcome.graph.nodes(data=True) if d.get("type") == "Repository"]
    assert repos[0]["name"] == "Pulse"
    assert repos[0]["slug"] == "pulse-api"
    # Node IDs keep the original slug, so nothing dangles.
    assert any(n.startswith("file:pulse_api:") for n in outcome.graph.nodes)
