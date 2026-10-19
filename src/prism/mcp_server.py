"""An MCP server over a built Prism graph.

The context-pack approach puts the whole portfolio into a model's context and
pays for all of it on every turn. This does the opposite: the graph stays on
disk and the model asks for exactly what a question needs, which is typically
a few hundred tokens rather than tens of thousands.

Two rules shape every tool here:

1. **Return citations, not dumps.** Each line of output carries the project,
   the path and the line range that justifies it, so an answer built from it
   can be checked. A tool that returned raw nodes would just move the token
   problem rather than solve it.

2. **Say what is missing.** Where a result is capped or a technology is
   absent, the output says so. A model that cannot tell "no evidence" from
   "nothing returned" will confidently invent the difference.

Everything is read-only. ``read_source`` is the only tool that touches disk,
and it can only reach files the graph already lists.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .query import PortfolioGraph

__all__ = ["build_server", "serve"]

#: Default caps. Generous enough to answer a real question, small enough that
#: a careless call cannot dump the whole graph into a context window.
_DEFAULT_LIMIT = 20
_MAX_LIMIT = 100
_MAX_SOURCE_LINES = 200


def _pct(share: float | None) -> str:
    return "-" if share is None else f"{round(share * 100)}%"


def _truncation_note(total: int, shown: int, what: str) -> str:
    if total <= shown:
        return ""
    return f"\n({shown} of {total} {what} shown; raise `limit` for more.)"


def build_server(graph_path: str | Path, *, name: str = "prism"):
    """Create the MCP server for one graph file."""
    from mcp.server.mcpserver import MCPServer

    graph = PortfolioGraph.load(graph_path)
    summary = graph.summary()
    person = summary.get("person") or summary.get("emails", ["someone"])[0]

    server = MCPServer(
        name=name,
        instructions=(
            f"Evidence-backed record of software written by {person}, derived "
            f"from git history and AST analysis of {summary.get('repositories', 1)} "
            f"repositories. Every figure traces to a commit SHA or a file:line.\n\n"
            "Start with `overview` for orientation, then `find_work` to answer "
            "'which projects demonstrate X'. Use `read_source` to see how "
            "something was actually implemented.\n\n"
            "Blame share is the fraction of a file's or symbol's lines at HEAD "
            "still attributed to this person; it only sees surviving code, so "
            "work later rewritten by someone else is invisible. Report that "
            "limit rather than working around it."
        ),
    )

    # -- helpers ---------------------------------------------------------

    def project_of(node_id: str) -> str:
        repo = graph.repo_of(node_id or "")
        return (graph.nodes.get(repo, {}) or {}).get("name", "") if repo else ""

    def clamp(limit: int | None) -> int:
        if not limit or limit < 1:
            return _DEFAULT_LIMIT
        return min(limit, _MAX_LIMIT)

    # -- tools -----------------------------------------------------------

    @server.tool(
        description=(
            "Orientation: who this portfolio belongs to, how large it is, and "
            "what the data cannot tell you. Call this first."
        )
    )
    def overview() -> str:
        projects = graph.repositories()
        lines = [
            f"# {person}",
            "",
            f"- Identities: {', '.join(summary.get('emails') or [])}",
            f"- Projects: {len(projects)}",
            f"- Commits: {summary['commits']}",
            f"- Source files touched: {summary['files_touched']}",
            f"- Symbols authored: {summary['symbols_authored']}",
            f"- Lines attributed at HEAD: {summary['lines_attributed']:,}",
            f"- Active: {(summary.get('first_commit_at') or '')[:10]} to "
            f"{(summary.get('last_commit_at') or '')[:10]}",
            "",
            "## Projects (by lines attributed)",
        ]
        for p in projects:
            lines.append(
                f"- {p['name']}: {p['commits']} commits, {p['files']} source files, "
                f"{p['symbols']} symbols, {p['blame_lines']:,} lines, "
                f"{', '.join(p['languages']) or 'n/a'}"
            )
        lines += [
            "",
            "## Limits",
            "- Blame sees only surviving code; work later rewritten by others is invisible.",
            "- Symbols are parsed for Python, JavaScript, TypeScript/TSX, Go and Java only.",
            "- Committed data/binary files are contributions but never counted as lines.",
            "- Commit subjects are the author's own words, not verified descriptions.",
        ]
        warnings = graph.meta.get("warnings") or []
        if warnings:
            lines.append("")
            lines.append("## Caveats from this build")
            lines += [f"- {w}" for w in warnings[:10]]
        return "\n".join(lines)

    @server.tool(
        description=(
            "Which projects demonstrate a given technology, strongest evidence "
            "first, with the files and symbols that prove it. This is the main "
            "tool for portfolio questions such as 'which projects show backend "
            "API work'."
        )
    )
    def find_work(technology: str, limit: int | None = None) -> str:
        cap = clamp(limit)
        rows = graph.projects_using(technology)
        if not rows:
            known = ", ".join(
                sorted(t.name for t in graph.technologies())[:40]
            )
            return (
                f"No evidence for '{technology}' in this portfolio.\n\n"
                f"This means no file the person touched imports or declares it "
                f"-- not that they have never used it.\n\n"
                f"Technologies with evidence: {known}"
            )

        out = [f"# Work demonstrating '{technology}'", ""]
        for row in rows:
            out.append(
                f"## {row['repository']} - {row['blame_lines']:,} lines attributed"
            )
            out.append("")
            out.append(f"Files: {', '.join(row['files'][:8])}")
            if row["evidence"]:
                out.append(f"Evidence: {row['evidence'][0]}")
            symbols = row["symbols"][:cap]
            if symbols:
                out.append("")
                out.append("Symbols authored here:")
                for s in symbols:
                    out.append(
                        f"- `{s['qualified_name']}` ({s['kind']}) "
                        f"{s['path']}:{s['start_line']}-{s['end_line']} "
                        f"[{_pct(s['blame_share'])} of {s['span_lines']} lines]"
                        + (f" calls {', '.join(s['calls'][:3])}" if s["calls"] else "")
                    )
                out.append(_truncation_note(len(row["symbols"]), len(symbols), "symbols").strip())
            out.append("")
        out.append(
            "Use `read_source` on any symbol above to see how it was implemented."
        )
        return "\n".join(l for l in out if l is not None)

    @server.tool(
        description=(
            "Every technology with evidence in this portfolio, and which "
            "projects demonstrate each. Optionally filter by category "
            "(language, framework, library, database, infra, tool)."
        )
    )
    def list_technologies(category: str | None = None, limit: int | None = None) -> str:
        cap = clamp(limit)
        techs = graph.technologies()
        if category:
            techs = [t for t in techs if t.category == category.lower()]
            if not techs:
                cats = sorted({t.category for t in graph.technologies()})
                return f"No technologies in category '{category}'. Known: {', '.join(cats)}"
        shown = techs[:cap]
        out = ["| Technology | Category | Projects | Files | Evidence |", "|---|---|---|---|---|"]
        for t in shown:
            citation = str(t.evidence[0]) if t.evidence else "-"
            out.append(
                f"| {t.name} | {t.category} | {', '.join(sorted(t.repositories)) or '-'} "
                f"| {t.files} | {citation} |"
            )
        return "\n".join(out) + _truncation_note(len(techs), len(shown), "technologies")

    @server.tool(
        description=(
            "Everything about one project: scale, technologies, the symbols "
            "this person authored in it, and its commit history."
        )
    )
    def project_detail(name: str, limit: int | None = None) -> str:
        cap = clamp(limit)
        projects = graph.repositories()
        match = next(
            (p for p in projects if (p["name"] or "").lower() == name.lower()), None
        ) or next(
            (p for p in projects if name.lower() in (p["name"] or "").lower()), None
        )
        if match is None:
            return (
                f"No project named '{name}'. Known projects: "
                + ", ".join(p["name"] for p in projects)
            )

        repo_id = match["id"]
        symbols = [s for s in graph.authored_symbols() if graph.repo_of(s["id"]) == repo_id]
        techs = [t for t in graph.technologies() if match["name"] in (t.repositories or [])]

        out = [
            f"# {match['name']}",
            "",
            f"- Commits: {match['commits']}",
            f"- Source files touched: {match['files']}",
            f"- Symbols authored: {len(symbols)}",
            f"- Lines attributed at HEAD: {match['blame_lines']:,}",
            f"- Active: {(match['first_at'] or '')[:10]} to {(match['last_at'] or '')[:10]}",
            f"- Languages: {', '.join(match['languages']) or 'n/a'}",
            "",
            "## Technologies",
            ", ".join(f"{t.name} ({t.category})" for t in techs) or "none detected",
            "",
            f"## Symbols authored (top {min(cap, len(symbols))} by lines)",
        ]
        for s in symbols[:cap]:
            out.append(
                f"- `{s['qualified_name']}` ({s['kind']}) "
                f"{s['path']}:{s['start_line']}-{s['end_line']} "
                f"[{_pct(s['blame_share'])} of {s['span_lines']} lines]"
            )
        out.append(_truncation_note(len(symbols), min(cap, len(symbols)), "symbols").strip())

        commits = [c for c in graph.commits() if graph.repo_of(c["id"]) == repo_id]
        out += ["", "## Commits (author's own subjects)"]
        for c in commits[:cap]:
            out.append(
                f"- {c.get('short_sha')} {(c.get('authored_at') or '')[:10]} "
                f"{c.get('subject')} (+{c.get('insertions')}/-{c.get('deletions')})"
            )
        return "\n".join(l for l in out if l is not None)


    @server.tool(
        description=(
            "Find symbols, files or technologies by name across the portfolio. "
            "Use when you have a partial name and need its exact location."
        )
    )
    def search(query: str, limit: int | None = None) -> str:
        cap = clamp(limit)
        needle = query.strip().lower()
        if len(needle) < 2:
            return "Query must be at least 2 characters."

        hits: list[str] = []
        for s in graph.authored_symbols():
            if needle in (s["qualified_name"] or "").lower():
                hits.append(
                    f"- symbol `{s['qualified_name']}` [{project_of(s['id'])}] "
                    f"{s['path']}:{s['start_line']}-{s['end_line']} "
                    f"[{_pct(s['blame_share'])}]"
                )
        for f in graph.contributed_files():
            if needle in (f["path"] or "").lower():
                hits.append(
                    f"- file `{f['path']}` [{project_of(f.get('id', ''))}] "
                    f"{f['blame_lines']} lines attributed"
                )
        for t in graph.technologies():
            if needle in t.name.lower():
                hits.append(
                    f"- technology `{t.name}` ({t.category}) in "
                    f"{', '.join(sorted(t.repositories)) or 'n/a'}"
                )

        if not hits:
            return f"Nothing matching '{query}'."
        return "\n".join(hits[:cap]) + _truncation_note(len(hits), min(cap, len(hits)), "matches")

    @server.tool(
        description=(
            "Read the actual source of a symbol or line range, so "
            "implementation questions are answered from the code rather than "
            "from its shape. Reads at the revision the graph was built from."
        )
    )
    def read_source(
        project: str,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> str:
        from .gitio import show_blob

        projects = graph.repositories()
        match = next(
            (p for p in projects if (p["name"] or "").lower() == project.lower()), None
        )
        if match is None:
            return (
                f"No project named '{project}'. Known: "
                + ", ".join(p["name"] for p in projects)
            )

        # Allowlist: only paths the graph already records for this project are
        # readable. This is what keeps a crafted path from walking out of the
        # repository -- the server never joins untrusted input onto a root.
        node = next(
            (
                n for n in graph.of_type("File")
                if n.get("path") == path and graph.repo_of(n["id"]) == match["id"]
            ),
            None,
        )
        if node is None:
            return (
                f"'{path}' is not a file recorded in {match['name']}. "
                f"Use `project_detail` or `search` to find valid paths."
            )

        repo_node = graph.nodes.get(match["id"], {})
        repo_path = repo_node.get("abs_path")
        if not repo_path or not Path(repo_path).exists():
            return (
                f"The repository for {match['name']} is not available at "
                f"{repo_path!r}, so its source cannot be read. The graph's "
                f"metadata is still valid."
            )

        blob = show_blob(repo_path, path, repo_node.get("head_sha") or "HEAD")
        if blob is None:
            return f"Could not read {path} at the recorded revision."

        lines = blob.decode("utf-8", errors="replace").splitlines()
        start = max(1, start_line or 1)
        end = min(len(lines), end_line or len(lines))
        if end < start:
            return f"Invalid range {start}-{end}."

        excerpt = lines[start - 1:end]
        note = ""
        if len(excerpt) > _MAX_SOURCE_LINES:
            note = (
                f"\n... ({len(excerpt) - _MAX_SOURCE_LINES} more lines; "
                f"request a narrower range)"
            )
            excerpt = excerpt[:_MAX_SOURCE_LINES]

        header = f"{match['name']}/{path}:{start}-{start + len(excerpt) - 1}"
        return f"{header}\n\n```{node.get('language') or ''}\n" + "\n".join(excerpt) + f"\n```{note}"

    return server


def serve(graph_path: str | Path, *, transport: str = "stdio") -> None:
    """Run the MCP server. Blocks until the client disconnects."""
    build_server(graph_path).run(transport=transport)
