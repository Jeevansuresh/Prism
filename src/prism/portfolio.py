"""Building one graph across several repositories.

Phase 1 answered "what did this person build in *this* repository". A
portfolio answers "which of their projects best demonstrates X" -- which needs
every repository in one graph, sharing one Person node and one node per
Technology.

This module is the per-repository pipeline run N times plus
:mod:`prism.merge`. It deliberately keeps going when one repository fails: a
directory that is not a git repository, or one holding none of this person's
commits, is reported and skipped rather than taking the whole portfolio down
with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import networkx as nx

from .gitio import GitError, repo_root
from .identity import IdentityError, detect_identities, resolve
from .merge import MergeReport, merge_graphs
from .pipeline import BuildOptions, BuildOutcome, build

__all__ = ["RepoSpec", "RepoResult", "PortfolioOutcome", "build_portfolio", "parse_repo_spec"]

Progress = Callable[[str], None]


@dataclass(frozen=True)
class RepoSpec:
    """One repository to include, with an optional display name override."""

    path: str
    name: str | None = None

    def __str__(self) -> str:
        return f"{self.name}={self.path}" if self.name else self.path


def parse_repo_spec(raw: str) -> RepoSpec:
    """Parse ``NAME=PATH`` or a bare ``PATH``.

    ``=`` separates rather than ``:`` because a Windows path starts with a
    drive letter and a colon, which would make ``C:\\x`` ambiguous.
    """
    if "=" in raw:
        name, _, path = raw.partition("=")
        name, path = name.strip(), path.strip()
        if name and path:
            return RepoSpec(path=path, name=name)
    return RepoSpec(path=raw.strip())


@dataclass
class RepoResult:
    """The outcome for one repository -- built, or skipped with a reason."""

    spec: RepoSpec
    outcome: BuildOutcome | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.outcome is not None

    @property
    def slug(self) -> str:
        if self.outcome is not None:
            return self.outcome.repo_slug
        return self.spec.name or Path(self.spec.path).name


@dataclass
class PortfolioOutcome:
    graph: nx.MultiDiGraph
    results: list[RepoResult]
    identity: object | None
    warnings: list[str] = field(default_factory=list)
    merge_report: MergeReport | None = None
    portfolio_name: str = "portfolio"

    @property
    def built(self) -> list[RepoResult]:
        return [r for r in self.results if r.ok]

    @property
    def skipped(self) -> list[RepoResult]:
        return [r for r in self.results if not r.ok]

    @property
    def total_commits(self) -> int:
        return sum(len(r.outcome.commits) for r in self.built)


def _preflight(spec: RepoSpec) -> str | None:
    """Return a human-readable reason this repository cannot be analysed."""
    path = Path(spec.path).expanduser()
    if not path.exists():
        return "path does not exist"
    if not path.is_dir():
        return "not a directory"
    try:
        repo_root(path)
    except GitError:
        return "not a git repository (no commit history to read)"
    return None


def canonical_email(specs: list[RepoSpec], options: BuildOptions) -> str | None:
    """Pick one address to be this person's identity across the whole portfolio.

    Without this, ``resolve()`` chooses whichever matched address has the most
    commits *in each repository* -- so someone who commits from a laptop in one
    project and through the GitHub web UI in another becomes **two Person
    nodes**, and every portfolio query silently sees only half their work.

    The winner is the address with the most commits summed across every
    repository, ties broken alphabetically so the choice is deterministic.
    """
    totals: dict[str, int] = {}
    for spec in specs:
        if _preflight(spec) is not None:
            continue
        try:
            available = detect_identities(spec.path, include_merges=options.include_merges)
        except GitError:
            continue
        for selector in options.authors:
            try:
                identity = resolve(available, [selector])
            except IdentityError:
                continue
            totals[identity.primary_email] = (
                totals.get(identity.primary_email, 0) + identity.commit_count
            )
    if not totals:
        return None
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]


def build_portfolio(
    specs: list[RepoSpec],
    options: BuildOptions,
    *,
    portfolio_name: str = "portfolio",
    progress: Progress | None = None,
) -> PortfolioOutcome:
    """Build and merge a graph for every repository in ``specs``."""
    say: Progress = progress or (lambda _m: None)

    # Settle the person's identity before building anything, so every
    # repository contributes to the same Person node.
    primary = options.canonical_email or canonical_email(specs, options)
    if primary:
        options = BuildOptions(**{**vars(options), "canonical_email": primary})
        say(f"portfolio identity: {primary}")
    results: list[RepoResult] = []
    warnings: list[str] = []
    graphs: list[nx.MultiDiGraph] = []
    identity = None

    for index, spec in enumerate(specs, start=1):
        prefix = f"[{index}/{len(specs)}] {spec.name or Path(spec.path).name}"

        reason = _preflight(spec)
        if reason is not None:
            say(f"{prefix}: SKIPPED -- {reason}")
            warnings.append(f"{spec.path}: {reason}")
            results.append(RepoResult(spec=spec, error=reason))
            continue

        # An author selector that matches in one repository may be absent from
        # another; that is a skip for that repository, not a failed portfolio.
        try:
            available = detect_identities(spec.path, include_merges=options.include_merges)
            matched = _selectors_present(available, options.authors)
            if not matched:
                reason = "none of the selected identities committed here"
                say(f"{prefix}: SKIPPED -- {reason}")
                warnings.append(f"{spec.path}: {reason}")
                results.append(RepoResult(spec=spec, error=reason))
                continue

            repo_options = BuildOptions(**{**vars(options), "authors": matched})
            say(f"{prefix}: building")
            outcome = build(spec.path, repo_options)
        except (GitError, IdentityError, ValueError) as exc:
            say(f"{prefix}: FAILED -- {exc}")
            warnings.append(f"{spec.path}: {exc}")
            results.append(RepoResult(spec=spec, error=str(exc)))
            continue

        if spec.name:
            _rename_repository(outcome, spec.name)

        if not outcome.commits:
            reason = "no commits by the selected identity"
            say(f"{prefix}: SKIPPED -- {reason}")
            warnings.append(f"{spec.path}: {reason}")
            results.append(RepoResult(spec=spec, error=reason))
            continue

        identity = identity or outcome.identity
        for warning in outcome.warnings:
            warnings.append(f"{outcome.repo_slug}: {warning}")

        say(
            f"{prefix}: {len(outcome.commits)} commits, "
            f"{outcome.graph.number_of_nodes()} nodes"
        )
        results.append(RepoResult(spec=spec, outcome=outcome))
        graphs.append(outcome.graph)

    if not graphs:
        raise ValueError("no repositories could be analysed; see the warnings above")

    say(f"merging {len(graphs)} repositories")
    merged, report = merge_graphs(graphs)
    say(f"{merged.number_of_nodes()} nodes, {merged.number_of_edges()} edges")

    return PortfolioOutcome(
        graph=merged,
        results=results,
        identity=identity,
        warnings=warnings,
        merge_report=report,
        portfolio_name=portfolio_name,
    )


def _selectors_present(available, selectors: list[str]) -> list[str]:
    """Keep only the selectors that actually match an identity in this repo."""
    present: list[str] = []
    for selector in selectors:
        try:
            resolve(available, [selector])
        except IdentityError:
            continue
        present.append(selector)
    return present


def _rename_repository(outcome: BuildOutcome, name: str) -> None:
    """Apply a display-name override to an already-built graph.

    Only the Repository node's ``name`` changes. Node IDs keep the slug they
    were built with, because rewriting every File and Symbol ID to match a
    cosmetic rename is how a graph ends up with dangling edges.
    """
    from .schema import repo_id

    node = repo_id(outcome.repo_slug)
    if node in outcome.graph:
        outcome.graph.nodes[node]["name"] = name
        outcome.graph.nodes[node]["slug"] = outcome.repo_slug
