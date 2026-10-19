"""The human-readable report.

Written for a human reading about someone's work -- so it leads with what they
built and what they built it with, and keeps the commit log at the bottom where
it belongs. Every claim carries a ``path:line`` so the reader can go look.
"""
from __future__ import annotations

from pathlib import Path

from .query import PortfolioGraph

__all__ = ["generate", "write_report"]

_MAX_ROWS = 25


def _pct(share: float | None) -> str:
    if share is None:
        return "-"
    return f"{round(share * 100)}%"


def _table(headers: list[str], rows: list[list[str]]) -> list[str]:
    if not rows:
        return ["_None._", ""]
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "|".join("---" for _ in headers) + "|",
    ]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    lines.append("")
    return lines


def _truncated(total: int, shown: int) -> list[str]:
    if total > shown:
        return [f"_{total - shown} more not shown._", ""]
    return []


def generate(graph: PortfolioGraph) -> str:
    """Render the full report as a Markdown string."""
    summary = graph.summary()
    person = summary.get("person") or (summary.get("emails") or ["unknown"])[0]
    repo = summary.get("repository") or "repository"

    out: list[str] = []
    out.append(f"# {repo} - {person}")
    out.append("")

    emails = ", ".join(summary.get("emails") or [])
    first = (summary.get("first_commit_at") or "")[:10]
    last = (summary.get("last_commit_at") or "")[:10]
    out.append(
        f"**{summary['commits']} commits** | {first} to {last} | "
        f"**{summary['files_touched']} source files** touched | "
        f"**{summary['symbols_authored']} symbols** authored | "
        f"**{summary['lines_attributed']} lines** attributed at HEAD"
    )
    out.append("")
    if graph.is_portfolio:
        out.append(f"<{emails}>")
    else:
        out.append(f"<{emails}> | `{(summary.get('head_sha') or '')[:12]}`")
    out.append("")

    if summary.get("data_files"):
        out.append(
            f"_Plus {summary['data_files']:,} committed data/binary files "
            f"(datasets, models, media) -- tracked as contributions, not counted "
            f"as authored source._"
        )
        out.append("")

    warnings = graph.meta.get("warnings") or []
    if warnings:
        out.append("> **Caveats**")
        for warning in warnings[:8]:
            out.append(f"> - {warning}")
        if len(warnings) > 8:
            out.append(f"> - _and {len(warnings) - 8} more_")
        out.append("")

    # -- Projects (portfolio only) ---------------------------------------
    if graph.is_portfolio:
        out.append("## Projects")
        out.append("")
        out.append("_Ordered by lines still attributed at HEAD, not by commit count._")
        out.append("")
        project_rows = []
        for project in graph.repositories():
            window = f"{(project['first_at'] or '')[:7]} → {(project['last_at'] or '')[:7]}"
            project_rows.append(
                [
                    f"**{project['name']}**",
                    str(project["commits"]),
                    str(project["files"]),
                    str(project["symbols"]),
                    f"{project['blame_lines']:,}",
                    window,
                    ", ".join(project["languages"]) or "-",
                ]
            )
        out += _table(
            ["Project", "Commits", "Files", "Symbols", "Lines", "Active", "Languages"],
            project_rows,
        )

    # -- Technologies ----------------------------------------------------
    out.append("## Technologies demonstrated")
    out.append("")
    out.append(
        "_Counted only where the evidence sits in a file this person touched._"
    )
    out.append("")
    technologies = graph.technologies()
    rows = []
    for tech in technologies[:_MAX_ROWS]:
        citation = str(tech.evidence[0]) if tech.evidence else "-"
        row = [f"`{tech.name}`", tech.category]
        if graph.is_portfolio:
            row.append(str(len(tech.repositories)))
        row += [str(tech.files), str(tech.commits), ", ".join(tech.signals) or "-", citation]
        rows.append(row)
    headers = ["Technology", "Category"]
    if graph.is_portfolio:
        headers.append("Projects")
    headers += ["Files", "Commits", "Signals", "First evidence"]
    out += _table(headers, rows)
    out += _truncated(len(technologies), _MAX_ROWS)

    # -- Symbols ---------------------------------------------------------
    out.append("## Symbols authored")
    out.append("")
    out.append("_Blame share is of the symbol's own line span at HEAD._")
    out.append("")
    symbols = graph.authored_symbols()
    rows = []
    for symbol in symbols[:_MAX_ROWS]:
        location = f"{symbol['path']}:{symbol['start_line']}-{symbol['end_line']}"
        calls = ", ".join(f"`{c}`" for c in symbol["calls"][:3]) or "-"
        decorators = " ".join(symbol.get("decorators") or [])
        name = f"`{symbol['qualified_name']}`"
        if decorators:
            name += f"<br>`{decorators[:60]}`"
        rows.append(
            [
                name,
                symbol["kind"],
                location,
                f"{_pct(symbol['blame_share'])} ({symbol['blame_lines']}/{symbol['span_lines']})",
                calls,
            ]
        )
    out += _table(["Symbol", "Kind", "Location", "Blame share", "Calls"], rows)
    out += _truncated(len(symbols), _MAX_ROWS)

    # -- Files -----------------------------------------------------------
    out.append("## Files")
    out.append("")
    files = graph.contributed_files()
    rows = []
    for row in files[:_MAX_ROWS]:
        marker = "" if row["exists_at_head"] else " _(deleted)_"
        rows.append(
            [
                f"`{row['path']}`{marker}",
                row["language"] or "-",
                str(row["commits"]),
                f"+{row['insertions']}/-{row['deletions']}",
                f"{_pct(row['blame_share'])} ({row['blame_lines']}/{row['loc']})",
            ]
        )
    out += _table(["File", "Language", "Commits", "Churn", "Blame share"], rows)
    out += _truncated(len(files), _MAX_ROWS)

    # -- Commits ---------------------------------------------------------
    out.append("## Commits")
    out.append("")
    commits = graph.commits()
    for commit in commits[:40]:
        when = (commit.get("authored_at") or "")[:10]
        out.append(
            f"- `{commit.get('short_sha')}` {when} {commit.get('subject')} "
            f"(+{commit.get('insertions')}/-{commit.get('deletions')}, "
            f"{commit.get('files_changed')} files)"
        )
    if len(commits) > 40:
        out.append(f"- _and {len(commits) - 40} more_")
    out.append("")

    # -- Provenance footer ------------------------------------------------
    counts = graph.meta.get("counts", {})
    out.append("---")
    out.append("")
    out.append(
        f"Graph: {counts.get('nodes', 0)} nodes, {counts.get('edges', 0)} edges. "
        f"Generated by Prism from git history and AST analysis only - "
        f"no language models were involved in producing this report."
    )
    out.append("")

    return "\n".join(out)


def write_report(graph: PortfolioGraph, path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(generate(graph))
    return target
