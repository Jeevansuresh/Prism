"""Shared fixtures.

The fixture repository is built once per session and reused. Building it is a
dozen git invocations; doing that per test would dominate the suite's runtime
for no benefit, since nothing in the suite mutates it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from fixture_repo import DANA, JEEVAN, build_fixture
from prism.attribute import attribute
from prism.gitio import ls_files
from prism.history import walk
from prism.identity import detect_identities, resolve
from prism.parse import parse_files
from prism.pipeline import BuildOptions, build
from prism.touched import resolve_touched


@pytest.fixture(scope="session")
def repo(tmp_path_factory) -> Path:
    return build_fixture(tmp_path_factory.mktemp("fixtures"))


@pytest.fixture(scope="session")
def identities(repo):
    return detect_identities(repo)


@pytest.fixture(scope="session")
def jeevan(identities):
    return resolve(identities, [JEEVAN.email], handles=("jeevansuresh",))


@pytest.fixture(scope="session")
def dana(identities):
    return resolve(identities, [DANA.email])


@pytest.fixture(scope="session")
def commits(repo, jeevan):
    return walk(repo, jeevan)


@pytest.fixture(scope="session")
def touched(repo, commits):
    return resolve_touched(repo, commits)


@pytest.fixture(scope="session")
def parsed(repo, touched):
    return parse_files(
        repo, [f.path for f in touched.parseable()], known_files=ls_files(repo)
    )


@pytest.fixture(scope="session")
def attribution(repo, jeevan, touched, parsed):
    return attribute(
        repo, jeevan, [f.path for f in touched.at_head], parsed.symbols
    )


@pytest.fixture(scope="session")
def outcome(repo, jeevan):
    return build(
        repo,
        BuildOptions(authors=[JEEVAN.email], handles=("jeevansuresh",)),
    )


@pytest.fixture(scope="session")
def document(outcome):
    from prism.export import to_document

    return to_document(
        outcome.graph,
        repository=outcome.repo_slug,
        head_sha=outcome.head_sha,
        selected_author=outcome.identity.primary_email,
        warnings=outcome.warnings,
    )


@pytest.fixture(scope="session")
def portfolio(document):
    from prism.query import PortfolioGraph

    return PortfolioGraph(document)


@pytest.fixture(scope="session")
def anyio_backend():
    """Run the async MCP tests on asyncio only, not trio as well."""
    return "asyncio"
