"""Merging repositories into one portfolio graph.

The headline risk is silent and arithmetic: networkx keys a MultiDiGraph edge
by (source, target, key), Prism uses the relation as the key, and Person and
Technology IDs are shared across repositories on purpose. So a second
repository's DEMONSTRATES edge lands on the same key as the first one's and
would overwrite its counts unless the merge adds them. Several tests here
exist only to pin that down.
"""
from __future__ import annotations

import pytest

from fixture_repo import DANA, JEEVAN, build_fixture
from prism.merge import merge_graphs
from prism.pipeline import BuildOptions, build
from prism.schema import person_id, tech_id


@pytest.fixture(scope="module")
def second_repo(tmp_path_factory):
    """A genuinely different second project -- different content, different SHAs."""
    root = tmp_path_factory.mktemp("second")
    return build_fixture(root, name="atlas-api", variant="atlas")


@pytest.fixture(scope="module")
def two_outcomes(repo, second_repo):
    options = BuildOptions(authors=[JEEVAN.email], handles=("jeevansuresh",))
    return [build(repo, options), build(second_repo, options)]


@pytest.fixture(scope="module")
def merged(two_outcomes):
    graph, report = merge_graphs([o.graph for o in two_outcomes])
    return graph, report


def test_merge_produces_one_person(merged):
    graph, _ = merged
    people = [n for n, d in graph.nodes(data=True) if d.get("type") == "Person"]
    assert len(people) == 1


def test_merge_keeps_both_repositories(merged):
    graph, _ = merged
    repos = [d.get("name") for _, d in graph.nodes(data=True) if d.get("type") == "Repository"]
    assert sorted(repos) == ["atlas-api", "pulse-api"]


def test_person_commit_count_sums_across_repositories(merged, two_outcomes):
    graph, _ = merged
    person = graph.nodes[person_id(JEEVAN.email)]
    expected = sum(o.identity.commit_count for o in two_outcomes)
    assert person["commit_count"] == expected
    assert person["commit_count"] == 10   # 5 per fixture repo


def test_person_active_window_spans_both(merged):
    """Earliest commit in either project to latest in either."""
    graph, _ = merged
    person = graph.nodes[person_id(JEEVAN.email)]
    assert person["first_commit_at"].startswith("2026-02-10")   # pulse-api
    assert person["last_commit_at"].startswith("2027-02-14")    # atlas-api


def test_technology_is_one_node_not_two(merged):
    graph, _ = merged
    fastapi = [
        n for n, d in graph.nodes(data=True)
        if d.get("type") == "Technology" and d.get("name") == "fastapi"
    ]
    assert len(fastapi) == 1


def test_demonstrates_counts_add_rather_than_overwrite(merged, two_outcomes):
    """The bug this module exists to prevent."""
    graph, _ = merged
    edge = graph[person_id(JEEVAN.email)][tech_id("fastapi")]["DEMONSTRATES"]

    per_repo = []
    for outcome in two_outcomes:
        single = outcome.graph[person_id(JEEVAN.email)][tech_id("fastapi")]["DEMONSTRATES"]
        per_repo.append(single["files"])

    assert edge["files"] == sum(per_repo)
    assert edge["files"] > max(per_repo), "counts were overwritten, not summed"


def test_demonstrates_records_every_repository(merged):
    graph, _ = merged
    edge = graph[person_id(JEEVAN.email)][tech_id("fastapi")]["DEMONSTRATES"]
    assert sorted(edge["repositories"]) == ["atlas-api", "pulse-api"]


def test_demonstrates_signals_union(merged):
    graph, _ = merged
    edge = graph[person_id(JEEVAN.email)][tech_id("fastapi")]["DEMONSTRATES"]
    assert set(edge["signals"]) == {"import", "manifest"}


def test_merge_report_counts_what_it_did(merged):
    _, report = merged
    assert report.nodes_added > 0
    assert report.nodes_merged > 0     # Person + the shared technologies
    assert report.edges_merged > 0     # the DEMONSTRATES edges
    assert report.type_conflicts == []


def test_repo_scoped_nodes_do_not_collide(merged, two_outcomes):
    """File and Symbol IDs carry their repo slug, so nothing is lost."""
    graph, _ = merged
    files = [n for n, d in graph.nodes(data=True) if d.get("type") == "File"]
    expected = sum(
        len([n for n, d in o.graph.nodes(data=True) if d.get("type") == "File"])
        for o in two_outcomes
    )
    assert len(files) == expected


def test_commits_are_not_deduplicated_away(merged, two_outcomes):
    graph, _ = merged
    commits = [n for n, d in graph.nodes(data=True) if d.get("type") == "Commit"]
    # Distinct repositories, distinct SHAs -- all of them survive.
    expected = sum(
        len([n for n, d in o.graph.nodes(data=True) if d.get("type") == "Commit"])
        for o in two_outcomes
    )
    assert len(commits) == expected


def test_the_same_commit_in_two_clones_is_one_node(tmp_path_factory):
    """A SHA is content-addressed, so the same SHA really is the same commit.

    Two clones of one project (or a fork) must not inflate the commit count.
    The repositories stay separate nodes; the shared history does not double.
    """
    first = build_fixture(tmp_path_factory.mktemp("clone_a"))
    second = build_fixture(tmp_path_factory.mktemp("clone_b"))

    options = BuildOptions(authors=[JEEVAN.email])
    graphs = [build(first, options).graph, build(second, options).graph]
    merged_graph, _ = merge_graphs(graphs)

    commits = [n for n, d in merged_graph.nodes(data=True) if d.get("type") == "Commit"]
    assert len(commits) == 5, "identical SHAs were counted twice"


def test_merging_a_single_graph_changes_nothing(two_outcomes):
    original = two_outcomes[0].graph
    graph, report = merge_graphs([original])
    assert graph.number_of_nodes() == original.number_of_nodes()
    assert graph.number_of_edges() == original.number_of_edges()
    assert report.nodes_merged == 0


def test_merge_does_not_mutate_its_inputs(two_outcomes):
    """A portfolio build must leave the per-repo graphs usable."""
    first = two_outcomes[0].graph
    before = first[person_id(JEEVAN.email)][tech_id("fastapi")]["DEMONSTRATES"]["files"]
    merge_graphs([o.graph for o in two_outcomes])
    after = first[person_id(JEEVAN.email)][tech_id("fastapi")]["DEMONSTRATES"]["files"]
    assert before == after


def test_merged_graph_still_validates(merged):
    from prism.export import to_document
    from prism.schema import validate_graph

    graph, _ = merged
    document = to_document(
        graph, repository="portfolio", head_sha="", selected_author=JEEVAN.email
    )
    assert validate_graph(document) == []
