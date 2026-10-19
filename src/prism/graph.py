"""Stage 7 -- assemble the graph.

A ``MultiDiGraph``, not a ``DiGraph``: two nodes can be related in more than one
way at once (a file both imports a technology and declares it in a manifest),
and collapsing those into one edge would throw away exactly the distinction
that makes the evidence legible.

Direction is carried by the graph type, not by arc order. graphify writes
``"directed": false`` and recovers direction from the order of each link's
endpoints, which needs ``_src``/``_tgt`` markers to survive a round trip; Prism
writes a genuinely directed graph instead and sidesteps the whole problem.

Every edge added here goes through :func:`_add_edge`, which requires a
:class:`~prism.schema.Provenance`. There is no code path that produces an edge
without one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import networkx as nx

from .attribute import Attribution
from .history import Commit
from .languages import language_for_path
from .parse import ParseResult, Symbol
from .schema import (
    Provenance,
    commit_id,
    file_id,
    person_id,
    repo_id,
    span,
    symbol_id,
    tech_id,
)
from .tech import TechResult
from .touched import TouchedSet, is_source_candidate

__all__ = ["BuildInputs", "build_graph"]


@dataclass
class BuildInputs:
    """Everything the earlier stages produced, in one place."""

    repo_slug: str
    repo_path: str
    head_sha: str
    branch: str
    identity: Any
    commits: list[Commit]
    touched: TouchedSet
    parsed: ParseResult
    attribution: Attribution
    technologies: TechResult
    line_counts: dict[str, int]
    warnings: list[str]


def _add_edge(
    G: nx.MultiDiGraph,
    source: str,
    target: str,
    relation: str,
    provenance: Provenance,
    **attributes: Any,
) -> None:
    """Add one relationship. The provenance argument is not optional by design."""
    G.add_edge(
        source,
        target,
        key=relation,
        relation=relation,
        provenance=provenance.to_dict(),
        **attributes,
    )


def _symbol_node_id(slug: str, symbol: Symbol) -> str:
    return symbol_id(slug, symbol.path, symbol.kind, symbol.qualified_name)


def _key_to_node_id(slug: str, key: tuple[str, str, str]) -> str:
    path, kind, qualified = key
    return symbol_id(slug, path, kind, qualified)


def build_graph(inputs: BuildInputs) -> nx.MultiDiGraph:
    """Turn the stage outputs into the portfolio graph."""
    G = nx.MultiDiGraph()
    slug = inputs.repo_slug
    identity = inputs.identity

    # -- Person ---------------------------------------------------------
    person = person_id(identity.primary_email)
    G.add_node(
        person,
        type="Person",
        names=list(identity.names),
        emails=list(identity.emails),
        handles=list(getattr(identity, "handles", ()) or ()),
        commit_count=identity.commit_count,
        first_commit_at=identity.first_at,
        last_commit_at=identity.last_at,
    )

    # -- Repository -----------------------------------------------------
    repo = repo_id(slug)
    languages = sorted(
        {
            language_for_path(f.path)
            for f in inputs.touched.at_head
            if language_for_path(f.path) != "unknown"
        }
    )
    G.add_node(
        repo,
        type="Repository",
        name=slug,
        abs_path=inputs.repo_path,
        head_sha=inputs.head_sha,
        default_branch=inputs.branch,
        first_commit_at=inputs.commits[0].authored_at if inputs.commits else "",
        last_commit_at=inputs.commits[-1].authored_at if inputs.commits else "",
        languages=languages,
    )

    # -- Commits --------------------------------------------------------
    for commit in inputs.commits:
        node = commit_id(commit.sha)
        G.add_node(
            node,
            type="Commit",
            sha=commit.sha,
            short_sha=commit.short_sha,
            authored_at=commit.authored_at,
            committed_at=commit.committed_at,
            subject=commit.subject,
            insertions=commit.insertions,
            deletions=commit.deletions,
            files_changed=commit.files_changed,
            is_merge=commit.is_merge,
        )
        _add_edge(
            G, person, node, "AUTHORED",
            Provenance(kind="git_log", commit=commit.sha, confidence="EXTRACTED"),
            role="author",
            authored_at=commit.authored_at,
        )
        _add_edge(
            G, node, repo, "IN_REPOSITORY",
            Provenance(kind="git_log", commit=commit.sha, confidence="EXTRACTED"),
        )

    # -- Files ----------------------------------------------------------
    canonical = inputs.touched.canonical
    for touched in inputs.touched:
        node = file_id(slug, touched.path)
        G.add_node(
            node,
            type="File",
            path=touched.path,
            ext=("." + touched.path.rsplit(".", 1)[-1]) if "." in touched.path.rsplit("/", 1)[-1] else "",
            language=language_for_path(touched.path),
            loc=inputs.line_counts.get(touched.path, 0),
            exists_at_head=touched.exists_at_head,
            touched_by_person=True,
            # A committed dataset or model is a real contribution, but it is
            # not engineering work, and 2,000 training images would otherwise
            # dominate every file count in the portfolio. Marked rather than
            # dropped, so the provenance stays complete and the caller chooses.
            is_source=is_source_candidate(touched.path),
            aliases=list(touched.aliases),
        )
        _add_edge(
            G, node, repo, "IN_REPOSITORY",
            Provenance(kind="git_log", commit=inputs.head_sha, source=touched.path,
                       confidence="EXTRACTED"),
        )

    # Context files: reached only by an import from a touched file. They keep
    # IMPORTS edges from dangling and show the architecture the person worked
    # inside, but they are explicitly not claimed as their work.
    for path in inputs.parsed.context_files:
        node = file_id(slug, path)
        if node in G:
            continue
        G.add_node(
            node,
            type="File",
            path=path,
            ext=("." + path.rsplit(".", 1)[-1]) if "." in path.rsplit("/", 1)[-1] else "",
            language=language_for_path(path),
            loc=inputs.line_counts.get(path, 0),
            exists_at_head=True,
            touched_by_person=False,
            is_source=True,
            aliases=[],
        )
        _add_edge(
            G, node, repo, "IN_REPOSITORY",
            Provenance(kind="ast", source=path, confidence="INFERRED"),
        )

    # -- Commit -> File --------------------------------------------------
    for commit in inputs.commits:
        for change in commit.changes:
            final = canonical.get(change.path, change.path)
            node = file_id(slug, final)
            if node not in G:
                continue  # filtered out as vendored or generated
            _add_edge(
                G, commit_id(commit.sha), node, "CHANGED",
                Provenance(kind="git_log", commit=commit.sha, source=change.path,
                           confidence="EXTRACTED"),
                change_type=change.change_type,
                insertions=change.insertions,
                deletions=change.deletions,
                old_path=change.old_path,
            )

    # -- Symbols ---------------------------------------------------------
    for symbol in inputs.parsed.symbols:
        node = _symbol_node_id(slug, symbol)
        G.add_node(
            node,
            type="Symbol",
            name=symbol.name,
            qualified_name=symbol.qualified_name,
            kind=symbol.kind,
            start_line=symbol.start_line,
            end_line=symbol.end_line,
            signature=symbol.signature,
            language=symbol.language,
            decorators=list(symbol.decorators),
            path=symbol.path,
        )
        file_node = file_id(slug, symbol.path)
        if file_node in G:
            _add_edge(
                G, file_node, node, "DEFINES",
                Provenance(kind="ast", commit=inputs.head_sha, source=symbol.path,
                           location=span(symbol.start_line, symbol.end_line),
                           confidence="EXTRACTED"),
            )

    # Containment, once every symbol node exists.
    by_qualified = {
        (s.path, s.qualified_name): s for s in inputs.parsed.symbols
    }
    for symbol in inputs.parsed.symbols:
        if symbol.parent is None:
            continue
        parent = by_qualified.get((symbol.path, symbol.parent))
        if parent is None:
            continue
        _add_edge(
            G, _symbol_node_id(slug, symbol), _symbol_node_id(slug, parent), "MEMBER_OF",
            Provenance(kind="ast", commit=inputs.head_sha, source=symbol.path,
                       location=span(symbol.start_line), confidence="EXTRACTED"),
        )

    # -- Calls ------------------------------------------------------------
    for call in inputs.parsed.calls:
        source = _key_to_node_id(slug, call.caller)
        target = _key_to_node_id(slug, call.callee)
        if source not in G or target not in G:
            continue
        _add_edge(
            G, source, target, "CALLS",
            Provenance(kind="ast", commit=inputs.head_sha, source=call.path,
                       location=span(call.line), confidence=call.confidence),
            resolution=call.resolution,
        )

    # -- Imports ----------------------------------------------------------
    for imported in inputs.parsed.internal_imports:
        source = file_id(slug, imported.path)
        target = file_id(slug, imported.target_path)
        if source not in G or target not in G:
            continue
        _add_edge(
            G, source, target, "IMPORTS",
            Provenance(kind="ast", commit=inputs.head_sha, source=imported.path,
                       location=span(imported.line), confidence=imported.confidence),
            module=imported.module,
        )

    # -- Technologies ------------------------------------------------------
    for technology in inputs.technologies.technologies:
        G.add_node(
            tech_id(technology.name),
            type="Technology",
            name=technology.name,
            category=technology.category,
            aliases=list(technology.aliases),
        )

    #: Deduplicate: one edge per (source node, technology, signal), citing the
    #: first occurrence. Forty imports of fastapi in one file is one fact.
    seen_usage: set[tuple[str, str, str]] = set()
    for usage in inputs.technologies.usages:
        target = tech_id(usage.tech)
        if target not in G:
            continue

        category = G.nodes[target].get("category")
        if usage.signal == "import":
            source = file_id(slug, usage.source)
            relation = "IMPORTS_EXTERNAL"
        elif usage.scope == "file" and category != "language":
            source = file_id(slug, usage.source)
            relation = "USES"
        else:
            # Language usage is anchored at the repository. Per-file language
            # edges would restate File.language once per file -- the same fact,
            # thirty-six times, for a graph that is supposed to be evidence.
            # The rollup below still counts every file, from the usage records.
            source = repo
            relation = "USES"

        if source not in G:
            continue
        marker = (source, target, usage.signal)
        if marker in seen_usage:
            continue
        seen_usage.add(marker)

        attributes: dict[str, Any] = {"signal": usage.signal}
        if relation == "IMPORTS_EXTERNAL":
            attributes["module"] = usage.detail or usage.tech
        elif usage.detail:
            attributes["version_spec"] = usage.detail

        _add_edge(
            G, source, target, relation,
            Provenance(
                kind="manifest" if usage.signal == "manifest" else "ast",
                commit=inputs.head_sha,
                source=usage.source,
                location=span(usage.line),
                confidence="EXTRACTED" if usage.signal != "config" else "INFERRED",
            ),
            **attributes,
        )

    # -- Derived: Person -> File -------------------------------------------
    blame_by_path = {f.path: f for f in inputs.attribution.files}
    for touched in inputs.touched:
        node = file_id(slug, touched.path)
        if node not in G:
            continue
        blame = blame_by_path.get(touched.path)
        _add_edge(
            G, person, node, "CONTRIBUTED_TO",
            Provenance(
                kind="git_blame" if blame else "git_log",
                commit=inputs.head_sha if blame else touched.commits[-1],
                source=touched.path,
                location=span(1, inputs.line_counts.get(touched.path, 0)) if blame else None,
                confidence="EXTRACTED",
            ),
            commits=len(touched.commits),
            insertions=touched.insertions,
            deletions=touched.deletions,
            blame_lines=blame.blame_lines if blame else 0,
            blame_total=blame.total_lines if blame else 0,
            blame_share=blame.blame_share if blame else None,
            first_at=touched.first_at,
            last_at=touched.last_at,
        )

    # -- Derived: Person -> Symbol -----------------------------------------
    for attributed in inputs.attribution.symbols:
        node = _key_to_node_id(slug, attributed.key)
        if node not in G:
            continue
        path = attributed.key[0]
        symbol = by_qualified.get((path, attributed.key[2]))
        _add_edge(
            G, person, node, "AUTHORED_SYMBOL",
            Provenance(
                kind="git_blame", commit=inputs.head_sha, source=path,
                location=span(symbol.start_line, symbol.end_line) if symbol else None,
                confidence="EXTRACTED",
            ),
            blame_lines=attributed.blame_lines,
            span_lines=attributed.span_lines,
            blame_share=attributed.blame_share,
        )

    # -- Derived: Person -> Technology --------------------------------------
    _add_demonstrates(G, inputs, person, slug)

    return G


def _add_demonstrates(G: nx.MultiDiGraph, inputs: BuildInputs, person: str, slug: str) -> None:
    """Roll technology evidence up to the person.

    Only evidence anchored in a file they touched counts. A dependency listed
    in a manifest they never edited describes the project, not them.

    These edges carry counts and no score. Ranking is a Phase 2 question, and
    freezing a guess at it into the data contract would be the wrong kind of
    convenient.
    """
    touched_paths = {f.path for f in inputs.touched}
    file_commits: dict[str, tuple[str, ...]] = {f.path: f.commits for f in inputs.touched}
    file_dates: dict[str, tuple[str, str]] = {
        f.path: (f.first_at, f.last_at) for f in inputs.touched
    }
    symbols_by_path: dict[str, int] = {}
    for attributed in inputs.attribution.symbols:
        symbols_by_path[attributed.key[0]] = symbols_by_path.get(attributed.key[0], 0) + 1

    rollup: dict[str, dict[str, Any]] = {}
    for usage in inputs.technologies.usages:
        if usage.source not in touched_paths:
            continue
        entry = rollup.setdefault(
            usage.tech,
            {
                "files": set(), "commits": set(), "symbols": 0,
                "signals": set(), "first": "", "last": "",
                "source": usage.source, "line": usage.line,
            },
        )
        entry["files"].add(usage.source)
        entry["commits"].update(file_commits.get(usage.source, ()))
        entry["signals"].add(usage.signal)
        first, last = file_dates.get(usage.source, ("", ""))
        if first and (not entry["first"] or first < entry["first"]):
            entry["first"] = first
        if last and (not entry["last"] or last > entry["last"]):
            entry["last"] = last
        # Prefer an import citation: it points at a real line of their code.
        if usage.signal == "import" and usage.line is not None:
            entry["source"], entry["line"] = usage.source, usage.line

    for name, entry in sorted(rollup.items()):
        target = tech_id(name)
        if target not in G:
            continue
        symbol_count = sum(symbols_by_path.get(path, 0) for path in entry["files"])
        _add_edge(
            G, person, target, "DEMONSTRATES",
            Provenance(
                kind="ast", commit=inputs.head_sha, source=entry["source"],
                location=span(entry["line"]), confidence="INFERRED",
            ),
            files=len(entry["files"]),
            commits=len(entry["commits"]),
            symbols=symbol_count,
            signals=sorted(entry["signals"]),
            # Carried so a portfolio merge can union these rather than
            # overwrite one repository's evidence with another's.
            repositories=[slug],
            first_at=entry["first"],
            last_at=entry["last"],
        )
