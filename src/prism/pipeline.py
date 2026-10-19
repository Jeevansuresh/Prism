"""The pipeline: one function that runs every stage in order.

    identities -> history -> touched -> parse -> attribute -> tech -> graph

Each stage is a pure function over the previous stage's output, so this module
is deliberately boring -- it wires them together, collects warnings, and owns
nothing else. If logic creeps in here it belongs in a stage.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable

import networkx as nx

from . import attribute as attribute_stage
from . import tech as tech_stage
from .gitio import current_branch, file_line_count, head_sha, ls_files, repo_root
from .graph import BuildInputs, build_graph
from .history import Commit, walk
from .identity import MergedIdentity, detect_identities, resolve
from .languages import available_languages
from .parse import ParseResult, parse_files
from .touched import TouchedSet, resolve_touched

__all__ = ["BuildOptions", "BuildOutcome", "build"]

Progress = Callable[[str], None]


@dataclass
class BuildOptions:
    authors: list[str] = field(default_factory=list)
    handles: tuple[str, ...] = ()
    rev: str = "HEAD"
    since: str | None = None
    until: str | None = None
    include_merges: bool = False
    include_vendored: bool = False
    max_blame_files: int | None = 2000
    skip_blame: bool = False
    #: Force the Person node's identity. resolve() otherwise picks whichever
    #: matched address has the most commits *in this repository*, which makes
    #: the same human a different node in each one -- see build_portfolio.
    canonical_email: str | None = None


@dataclass
class BuildOutcome:
    graph: nx.MultiDiGraph
    identity: MergedIdentity
    commits: list[Commit]
    touched: TouchedSet
    parsed: ParseResult
    warnings: list[str]
    repo_slug: str
    head_sha: str


def _repo_slug(root: Path) -> str:
    """Prefer the remote repository name over the local directory name.

    A clone in ``~/work/tmp2`` is still the same project, and a portfolio that
    names it after the directory it happens to sit in is unhelpful.
    """
    from .gitio import GitError, git

    try:
        url = git(root, "config", "--get", "remote.origin.url").strip()
    except GitError:
        url = ""
    if url:
        name = url.rstrip("/").rsplit("/", 1)[-1]
        name = name.removesuffix(".git")
        if name:
            return name
    return root.name


def build(
    repo_path: str | Path,
    options: BuildOptions,
    *,
    progress: Progress | None = None,
) -> BuildOutcome:
    """Run the full Phase 1 pipeline for one repository and one person."""
    say: Progress = progress or (lambda _message: None)
    warnings: list[str] = []

    root = repo_root(repo_path)
    slug = _repo_slug(root)
    rev = options.rev
    sha = head_sha(root)
    if not sha:
        raise ValueError(f"{root} has no commits at {rev}")

    # 1 -- identity
    say("detecting identities")
    identities = detect_identities(root, include_merges=options.include_merges)
    if not identities:
        raise ValueError(f"{root} has no commit history to read identities from")
    identity = resolve(identities, options.authors, handles=options.handles)
    if options.canonical_email and options.canonical_email != identity.primary_email:
        # Only the Person node's identity changes. `emails` stays the set that
        # actually committed here, because that is what attribution matches on.
        identity = replace(identity, primary_email=options.canonical_email)
    say(f"selected {identity} ({identity.commit_count} commits)")

    # 2 -- history
    say("walking history")
    commits = walk(
        root,
        identity,
        include_merges=options.include_merges,
        since=options.since,
        until=options.until,
        rev=rev,
    )
    if not commits:
        warnings.append(f"no commits by {identity} in {rev}")
    say(f"{len(commits)} commits")

    # 3 -- touched set
    touched = resolve_touched(
        root, commits, rev=rev, include_vendored=options.include_vendored
    )
    parseable = touched.parseable()
    say(f"{len(touched)} files touched, {len(parseable)} parseable at {rev}")

    # 4 -- parse
    tracked = ls_files(root, rev)
    say("parsing")
    parsed = parse_files(
        root, [f.path for f in parseable], rev=rev, known_files=tracked
    )
    for path, reason in sorted(parsed.skipped.items()):
        warnings.append(f"skipped {path}: {reason}")
    say(
        f"{len(parsed.symbols)} symbols, {len(parsed.calls)} calls, "
        f"{len(parsed.internal_imports)} internal imports"
    )

    missing_grammars = set(l for l in ("python", "javascript", "typescript", "go", "java"))
    missing_grammars -= set(available_languages())
    touched_languages = {
        f.path.rsplit(".", 1)[-1] for f in parseable if "." in f.path
    }
    if missing_grammars and touched_languages:
        warnings.append(
            "grammars unavailable: " + ", ".join(sorted(missing_grammars))
        )

    # Line counts for every file that ends up in the graph. Computed before
    # attribution because file_line_count() returns 0 for anything holding a
    # NUL byte, which is a free content-based binary check -- it catches the
    # files an extension list alone would miss.
    line_counts: dict[str, int] = {}
    for path in {f.path for f in touched.at_head} | set(parsed.context_files):
        line_counts[path] = file_line_count(root, path, rev)

    # 5 -- attribution
    if options.skip_blame:
        attribution = attribute_stage.Attribution((), (), (), ())
        warnings.append("blame skipped: no line-level attribution in this graph")
    else:
        say("blaming")
        # Only plausibly hand-written text is blamed. A checked-in model file
        # or vector index is a real contribution to the repository, but it is
        # not authored lines, and counting it as such swamps everything else.
        blame_targets = [
            f.path for f in touched.blameable() if line_counts.get(f.path, 0) > 0
        ]
        excluded = len(touched.at_head) - len(blame_targets)
        if excluded:
            warnings.append(
                f"{excluded} file(s) excluded from blame as non-source "
                f"(binary, data, or generated)"
            )
        attribution = attribute_stage.attribute(
            root,
            identity,
            blame_targets,
            parsed.symbols,
            rev=rev,
            max_files=options.max_blame_files,
        )
        if attribution.failed_paths:
            listed = ", ".join(attribution.failed_paths[:5])
            more = "..." if len(attribution.failed_paths) > 5 else ""
            warnings.append(
                f"{len(attribution.failed_paths)} file(s) could not be blamed: "
                f"{listed}{more}"
            )
        say(
            f"{len(attribution.files)} files and {len(attribution.symbols)} "
            f"symbols attributed"
        )

    # 6 -- technologies
    say("detecting technologies")
    technologies = tech_stage.detect(
        root,
        tracked_files=tracked,
        touched_paths={f.path for f in touched},
        external_imports=parsed.external_imports,
        rev=rev,
    )
    say(f"{len(technologies.technologies)} technologies")

    # 7 -- graph
    say("building graph")
    graph = build_graph(
        BuildInputs(
            repo_slug=slug,
            repo_path=str(root).replace("\\", "/"),
            head_sha=sha,
            branch=current_branch(root),
            identity=identity,
            commits=commits,
            touched=touched,
            parsed=parsed,
            attribution=attribution,
            technologies=technologies,
            line_counts=line_counts,
            warnings=warnings,
        )
    )
    say(f"{graph.number_of_nodes()} nodes, {graph.number_of_edges()} edges")

    return BuildOutcome(
        graph=graph,
        identity=identity,
        commits=commits,
        touched=touched,
        parsed=parsed,
        warnings=warnings,
        # The human-readable slug. Normalisation happens inside the ID
        # constructors; it has no business leaking into display names.
        repo_slug=slug,
        head_sha=sha,
    )
