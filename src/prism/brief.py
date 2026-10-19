"""Emit the graph as a context pack for an LLM project workspace.

The graph is the source of truth; this is a rendering of it aimed at
retrieval rather than at a human reading top to bottom.

Three constraints shape the output, and they are not the same ones that shape
``report.py``:

1. **Every chunk must stand alone.** Retrieval returns fragments, not files.
   A line saying "87% of ``login()``" is useless if the fragment it arrived in
   never names the project, so the project name is repeated in section
   headers and every citation carries its full path.

2. **Completeness beats brevity.** ``report.py`` truncates to 25 rows because
   a human will not read 467 symbols. Retrieval will, and a symbol that was
   truncated away simply cannot be found.

3. **The limits travel with the data.** A model reading this will be asked to
   make claims about someone's work. The caveats about what blame can and
   cannot see belong in the pack, not in a README nobody attached.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .query import PortfolioGraph

__all__ = [
    "write_context_pack", "generate_overview", "generate_project",
    "BriefOptions", "fit_to_budget", "estimate_tokens",
]

#: Rough chars-per-token. Prose and markdown tables sit near 4; this is only
#: used to aim at a budget, never reported as an exact count.
_CHARS_PER_TOKEN = 4


def estimate_tokens(text: str) -> int:
    return len(text) // _CHARS_PER_TOKEN


@dataclass(frozen=True)
class BriefOptions:
    """What to include. Tightening these trades completeness for tokens.

    The pruning order is deliberate: source excerpts go first because they are
    69% of the bytes, then the long tail of small symbols, then low-churn
    files. The overview, the technology-to-project matrix and the limits are
    never pruned -- they are what makes the rest interpretable.
    """

    min_symbol_span: int = 1        # drop symbols shorter than this
    min_file_blame: int = 0         # drop files with fewer attributed lines
    max_excerpts: int = 0           # source excerpts per project
    excerpt_lines: int = 120        # truncate an excerpt to this many lines
    include_commits: bool = True


#: Tightness ladder, loosest first. fit_to_budget walks it until the pack fits.
_LADDER: tuple[BriefOptions, ...] = (
    BriefOptions(max_excerpts=12, excerpt_lines=120),
    BriefOptions(max_excerpts=8, excerpt_lines=80),
    BriefOptions(max_excerpts=6, excerpt_lines=60, min_symbol_span=5),
    BriefOptions(max_excerpts=5, excerpt_lines=45, min_symbol_span=10, min_file_blame=10),
    BriefOptions(max_excerpts=4, excerpt_lines=35, min_symbol_span=15, min_file_blame=25),
    BriefOptions(max_excerpts=3, excerpt_lines=30, min_symbol_span=20, min_file_blame=50),
    BriefOptions(max_excerpts=2, excerpt_lines=25, min_symbol_span=30, min_file_blame=50),
    BriefOptions(max_excerpts=0, min_symbol_span=30, min_file_blame=50),
    BriefOptions(max_excerpts=0, min_symbol_span=50, min_file_blame=50, include_commits=False),
)


def _slug(name: str) -> str:
    out = "".join(c if c.isalnum() or c in "-_" else "-" for c in (name or "project"))
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-").lower() or "project"


def _pct(share: float | None) -> str:
    return "-" if share is None else f"{round(share * 100)}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    return (
        ["| " + " | ".join(headers) + " |",
         "|" + "|".join("---" for _ in headers) + "|"]
        + ["| " + " | ".join(r) + " |" for r in rows]
        + [""]
    )


# --------------------------------------------------------------------------
# Overview
# --------------------------------------------------------------------------

def generate_overview(graph: PortfolioGraph) -> str:
    """The entry point document: who, what projects, which technologies where."""
    summary = graph.summary()
    person = summary.get("person") or "the author"
    projects = graph.repositories()
    technologies = graph.technologies()

    out: list[str] = []
    out.append(f"# Engineering portfolio - {person}")
    out.append("")
    out.append(
        "This pack describes software **" + person + "** has written, derived "
        "from git history and AST analysis of their repositories. Every claim "
        "traces to a commit SHA or a `file:line`. No language model produced "
        "these facts."
    )
    out.append("")

    emails = ", ".join(summary.get("emails") or [])
    out.append(f"- **Identities**: {emails}")
    out.append(f"- **Projects**: {len(projects)}")
    out.append(f"- **Commits**: {summary['commits']}")
    out.append(f"- **Source files touched**: {summary['files_touched']}")
    out.append(f"- **Symbols authored**: {summary['symbols_authored']}")
    out.append(
        f"- **Lines still attributed at HEAD**: {summary['lines_attributed']:,}"
    )
    out.append(
        f"- **Active**: {(summary.get('first_commit_at') or '')[:10]} to "
        f"{(summary.get('last_commit_at') or '')[:10]}"
    )
    out.append("")

    # -- how to read this pack ------------------------------------------
    out.append("## How to read this pack")
    out.append("")
    out.append(
        "- One file per project, named `project-<name>.md`. Each is "
        "self-contained: it names its own project in every section."
    )
    out.append(
        "- **Blame share** is the fraction of a file's or a symbol's lines at "
        "HEAD still attributed to this person. 100% means sole authorship of "
        "the surviving code."
    )
    out.append(
        "- **Confidence** on a relationship is `EXTRACTED` (stated in the "
        "source, e.g. an import or a direct call) or `INFERRED` (a reasonable "
        "deduction, e.g. a uniquely-named cross-file call)."
    )
    out.append("")

    # -- projects --------------------------------------------------------
    out.append("## Projects at a glance")
    out.append("")
    out.append("_Ordered by lines still attributed at HEAD, not by commit count._")
    out.append("")
    rows = []
    for p in projects:
        rows.append([
            p["name"] or "-",
            str(p["commits"]),
            str(p["files"]),
            str(p["symbols"]),
            f"{p['blame_lines']:,}",
            f"{(p['first_at'] or '')[:7]} to {(p['last_at'] or '')[:7]}",
            ", ".join(p["languages"]) or "-",
        ])
    out += _table(
        ["Project", "Commits", "Source files", "Symbols", "Lines", "Active", "Languages"],
        rows,
    )

    # -- technology x project matrix -------------------------------------
    # This is the table that answers "which projects demonstrate X", which is
    # the question the whole system exists to serve.
    out.append("## Which projects demonstrate which technology")
    out.append("")
    out.append(
        "_Counted only where the evidence sits in a file this person touched. "
        "A dependency declared in a manifest they never edited describes the "
        "project, not them._"
    )
    out.append("")
    rows = []
    for tech in technologies:
        if not tech.repositories:
            continue
        citation = str(tech.evidence[0]) if tech.evidence else "-"
        rows.append([
            f"`{tech.name}`",
            tech.category,
            ", ".join(sorted(tech.repositories)),
            str(tech.files),
            ", ".join(tech.signals) or "-",
            citation,
        ])
    out += _table(
        ["Technology", "Category", "Projects", "Files", "Signals", "Example evidence"],
        rows,
    )

    # -- limits ----------------------------------------------------------
    out.append("## What this data cannot tell you")
    out.append("")
    out.append(
        "State these limits rather than working around them if a question "
        "runs past the evidence:"
    )
    out.append("")
    out.append(
        "- **Blame sees only surviving code.** Work later rewritten by someone "
        "else is invisible in the blame shares. The commit record "
        "(insertions/deletions, including for deleted files) is kept "
        "alongside, so that contribution is not lost, only un-blamed."
    )
    out.append(
        "- **Only five languages are parsed** for symbols: Python, JavaScript, "
        "TypeScript/TSX, Go, Java. Files in other languages appear with no "
        "symbols, which is not evidence that none exist."
    )
    out.append(
        "- **Committed data and binary files** (datasets, models, media) are "
        "recorded as contributions but never counted as authored lines."
    )
    out.append(
        "- **Commit subjects are the author's own words**, not verified "
        "descriptions of what the commit did."
    )
    out.append(
        "- Unresolved calls are counted, never guessed at. An absent `CALLS` "
        "relationship does not mean the call is absent from the code."
    )
    warnings = graph.meta.get("warnings") or []
    if warnings:
        out.append("")
        out.append("### Caveats recorded during this build")
        out.append("")
        for warning in warnings:
            out.append(f"- {warning}")
    out.append("")

    return "\n".join(out)


# --------------------------------------------------------------------------
# Per project
# --------------------------------------------------------------------------

def generate_project(
    graph: PortfolioGraph,
    project: dict[str, Any],
    *,
    source_lookup=None,
    options: BriefOptions | None = None,
) -> str:
    """One self-contained document per project."""
    name = project["name"] or "project"
    person = (graph.summary().get("person")) or "the author"
    repo_id = project["id"]

    # Restrict every section to this project.
    def in_project(node_id: str) -> bool:
        return graph.repo_of(node_id) == repo_id

    options = options or BriefOptions()

    all_symbols = [s for s in graph.authored_symbols() if in_project(s["id"])]
    symbols = [s for s in all_symbols if s["span_lines"] >= options.min_symbol_span]
    all_files = [f for f in graph.contributed_files() if in_project(f.get("id", ""))]
    source_files = [f for f in all_files if f.get("is_source", True)]
    files = [f for f in source_files if (f["blame_lines"] or 0) >= options.min_file_blame]
    data_files = [f for f in all_files if not f.get("is_source", True)]
    commits = [c for c in graph.commits() if in_project(c["id"])]

    out: list[str] = []
    out.append(f"# Project: {name}")
    out.append("")
    out.append(
        f"Part of **{person}**'s engineering portfolio. All figures below are "
        f"for this project only."
    )
    out.append("")
    out.append(f"- **Commits by {person}**: {project['commits']}")
    out.append(f"- **Source files touched**: {project['files']}")
    if project.get("data_files"):
        out.append(
            f"- **Data/binary files committed**: {project['data_files']} "
            f"(not counted as authored lines)"
        )
    out.append(f"- **Symbols authored**: {len(symbols)}")
    out.append(f"- **Lines attributed at HEAD**: {project['blame_lines']:,}")
    out.append(
        f"- **Active**: {(project['first_at'] or '')[:10]} to "
        f"{(project['last_at'] or '')[:10]}"
    )
    out.append(f"- **Languages**: {', '.join(project['languages']) or '-'}")
    out.append(f"- **HEAD**: `{(project.get('head_sha') or '')[:12]}`")
    out.append("")

    # -- technologies ----------------------------------------------------
    out.append(f"## Technologies used in {name}")
    out.append("")
    rows = []
    for tech in graph.technologies():
        if name not in (tech.repositories or []):
            continue
        local = [e for e in tech.evidence if graph.repo_of(_file_node(graph, e.source, repo_id)) == repo_id]
        citation = str(local[0]) if local else (str(tech.evidence[0]) if tech.evidence else "-")
        rows.append([f"`{tech.name}`", tech.category, ", ".join(tech.signals) or "-", citation])
    out += _table(["Technology", "Category", "Signals", "Evidence"], rows)

    # -- what they built -------------------------------------------------
    out.append(f"## Symbols {person} authored in {name}")
    out.append("")
    out.append(
        "_Blame share is of each symbol's own line span at HEAD. `Calls` lists "
        "what the symbol invokes, which is the clearest signal of how it was "
        "implemented._"
    )
    out.append("")
    rows = []
    for s in symbols:
        decorators = " ".join(s.get("decorators") or [])
        label = f"`{s['qualified_name']}`"
        if decorators:
            label += f" {decorators}"
        rows.append([
            label,
            s["kind"],
            f"{s['path']}:{s['start_line']}-{s['end_line']}",
            f"{_pct(s['blame_share'])} ({s['blame_lines']}/{s['span_lines']})",
            ", ".join(f"`{c}`" for c in s["calls"][:4]) or "-",
        ])
    out += _table(["Symbol", "Kind", "Location", "Blame share", "Calls"], rows)

    if len(symbols) < len(all_symbols):
        shown = sum(s["blame_lines"] for s in symbols)
        total = sum(s["blame_lines"] for s in all_symbols) or 1
        out.append(
            f"_Showing {len(symbols)} of {len(all_symbols)} authored symbols "
            f"(those spanning {options.min_symbol_span}+ lines), covering "
            f"{round(100 * shown / total)}% of the lines attributed in this "
            f"project. The remainder are short helpers._"
        )
        out.append("")

    # -- optional source excerpts ----------------------------------------
    if source_lookup is not None and options.max_excerpts > 0:
        excerpts = [
            s for s in symbols if (s["blame_share"] or 0) >= 0.5
        ][:options.max_excerpts]
        if excerpts:
            out.append(f"## Implementation excerpts from {name}")
            out.append("")
            out.append(
                "_Source at HEAD for the symbols above with majority "
                "authorship. Included so implementation questions can be "
                "answered from the code rather than from its shape._"
            )
            out.append("")
            for s in excerpts:
                text = source_lookup(project, s, options.excerpt_lines)
                if not text:
                    continue
                out.append(
                    f"### `{s['qualified_name']}` - "
                    f"{s['path']}:{s['start_line']}-{s['end_line']}"
                )
                out.append("")
                out.append(f"```{s.get('language') or ''}")
                out.append(text.rstrip("\n"))
                out.append("```")
                out.append("")

    # -- files -----------------------------------------------------------
    out.append(f"## Files {person} contributed to in {name}")
    out.append("")
    rows = []
    for f in files:
        marker = "" if f["exists_at_head"] else " (deleted)"
        rows.append([
            f"`{f['path']}`{marker}",
            f["language"] or "-",
            str(f["commits"]),
            f"+{f['insertions']}/-{f['deletions']}",
            f"{_pct(f['blame_share'])} ({f['blame_lines']}/{f['loc']})",
        ])
    out += _table(["File", "Language", "Commits", "Churn", "Blame share"], rows)

    if len(files) < len(source_files):
        out.append(
            f"_Showing {len(files)} of {len(source_files)} source files "
            f"(those with {options.min_file_blame}+ attributed lines)._"
        )
        out.append("")

    if data_files:
        # Listing 2,000 training images individually would bury the project's
        # actual source under noise no retrieval query wants to match.
        deleted = sum(1 for f in data_files if not f["exists_at_head"])
        out.append(
            f"Plus **{len(data_files):,} committed data/binary files** "
            f"(datasets, models, media{', ' + str(deleted) + ' since deleted' if deleted else ''}). "
            f"Real contributions to the repository, but not authored source, "
            f"so they are not listed individually and not counted as lines."
        )
        out.append("")

    # -- commit narrative --------------------------------------------------
    if not options.include_commits:
        return "\n".join(out)

    out.append(f"## Commit history for {name}")
    out.append("")
    out.append("_The author's own commit subjects, in order. Not independently verified._")
    out.append("")
    for c in commits:
        out.append(
            f"- `{c.get('short_sha')}` {(c.get('authored_at') or '')[:10]} - "
            f"{c.get('subject')} (+{c.get('insertions')}/-{c.get('deletions')}, "
            f"{c.get('files_changed')} files)"
        )
    out.append("")

    return "\n".join(out)


def _file_node(graph: PortfolioGraph, path: str, repo_id: str) -> str:
    """Best-effort File node ID for a path inside a given repository."""
    for node in graph.of_type("File"):
        if node.get("path") == path and graph.repo_of(node["id"]) == repo_id:
            return node["id"]
    return ""


# --------------------------------------------------------------------------
# Writing the pack
# --------------------------------------------------------------------------

def _git_source_lookup(
    project: dict[str, Any], symbol: dict[str, Any], max_lines: int = 120
) -> str:
    """Read a symbol's lines from the repository at the recorded revision."""
    from .gitio import show_blob

    repo_path = project.get("abs_path")
    if not repo_path or not Path(repo_path).exists():
        return ""
    blob = show_blob(repo_path, symbol["path"], project.get("head_sha") or "HEAD")
    if blob is None:
        return ""
    try:
        lines = blob.decode("utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    start = max(1, symbol["start_line"]) - 1
    end = min(len(lines), symbol["end_line"])
    excerpt = lines[start:end]
    # A very long function is summarised rather than pasted whole.
    if len(excerpt) > max_lines:
        excerpt = excerpt[:max_lines] + [f"... ({len(excerpt) - max_lines} more lines)"]
    return "\n".join(excerpt)


def _render_all(
    graph: PortfolioGraph, options: BriefOptions, lookup
) -> list[tuple[str, str]]:
    """Render the whole pack in memory as (filename, text) pairs."""
    rendered = [("00-portfolio-overview.md", generate_overview(graph))]
    for index, project in enumerate(graph.repositories(), start=1):
        node = graph.nodes.get(project["id"], {})
        project = {**project, "abs_path": node.get("abs_path")}
        rendered.append((
            f"{index:02d}-project-{_slug(project['name'])}.md",
            generate_project(graph, project, source_lookup=lookup, options=options),
        ))
    return rendered


def fit_to_budget(
    graph: PortfolioGraph, budget_tokens: int, *, with_source: bool = True
) -> tuple[BriefOptions, int]:
    """Find the most complete settings that fit inside a token budget.

    Walks a fixed ladder from loosest to tightest and stops at the first rung
    that fits, so the result keeps the strongest evidence and sheds the long
    tail. Returns the chosen options and the estimated token count -- which
    may still exceed the budget if even the tightest rung is too large, in
    which case the caller is told rather than handed a silent overrun.
    """
    lookup = _git_source_lookup if with_source else None
    chosen, size = _LADDER[-1], 0
    for options in _LADDER:
        if not with_source and options.max_excerpts:
            options = replace(options, max_excerpts=0)
        total = sum(
            estimate_tokens(text) for _, text in _render_all(graph, options, lookup)
        )
        chosen, size = options, total
        if total <= budget_tokens:
            break
    return chosen, size


def write_context_pack(
    graph: PortfolioGraph,
    out_dir: str | Path,
    *,
    with_source: bool = False,
    options: BriefOptions | None = None,
    budget_tokens: int | None = None,
) -> list[Path]:
    """Write the overview plus one file per project. Returns the paths written."""
    if budget_tokens is not None:
        options, _ = fit_to_budget(graph, budget_tokens, with_source=with_source)
    options = options or BriefOptions(max_excerpts=12 if with_source else 0)

    target = Path(out_dir)
    target.mkdir(parents=True, exist_ok=True)
    lookup = _git_source_lookup if with_source else None

    written: list[Path] = []
    for filename, text in _render_all(graph, options, lookup):
        path = target / filename
        path.write_text(text, encoding="utf-8", newline="\n")
        written.append(path)
    return written
