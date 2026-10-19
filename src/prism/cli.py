"""Command line interface.

    prism identities <repo>
    prism build <repo> --author <email> [--out prism-out]
    prism report <graph.json>
    prism brief <graph.json> [--budget TOKENS] [--with-source]
    prism query <graph.json> [--tech NAME] [--symbols] [--files]
    prism serve <graph.json>
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from .export import DEFAULT_BUILT_AT, to_document, write_json
from .gitio import GitError, repo_root
from .identity import IdentityError, detect_identities
from .pipeline import BuildOptions, build
from .query import PortfolioGraph
from .report import generate as generate_report
from .report import write_report

__all__ = ["main"]

_DEFAULT_OUT = "prism-out"


def _eprint(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------
# identities
# --------------------------------------------------------------------------

def _cmd_identities(args: argparse.Namespace) -> int:
    # Resolve first, so "not a git repository" is reported as such rather than
    # surfacing later as the much less useful "no commits found".
    root = repo_root(args.repo)
    identities = detect_identities(root, include_merges=args.include_merges)
    if not identities:
        _eprint(f"error: {root} has no commits")
        return 1

    name_width = max((len(i.display_name) for i in identities), default=4)
    email_width = max((len(i.email) for i in identities), default=5)
    print(
        f"{'NAME'.ljust(name_width)}  {'EMAIL'.ljust(email_width)}  "
        f"{'COMMITS':>7}  FIRST       LAST"
    )
    for identity in identities:
        print(
            f"{identity.display_name.ljust(name_width)}  "
            f"{identity.email.ljust(email_width)}  "
            f"{identity.commit_count:>7}  "
            f"{identity.first_at[:10]}  {identity.last_at[:10]}"
        )
        if len(identity.names) > 1:
            print(f"{' ' * name_width}  also: {', '.join(identity.names[1:])}")
    return 0


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------

def _cmd_build(args: argparse.Namespace) -> int:
    options = BuildOptions(
        authors=args.author,
        handles=tuple(args.handle or ()),
        rev=args.rev,
        since=args.since,
        until=args.until,
        include_merges=args.include_merges,
        include_vendored=args.include_vendored,
        max_blame_files=args.max_blame_files,
        skip_blame=args.no_blame,
    )

    verbose = not args.quiet
    outcome = build(
        args.repo,
        options,
        progress=(lambda m: _eprint(f"  {m}")) if verbose else None,
    )

    built_at = DEFAULT_BUILT_AT
    if args.timestamp:
        built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    document = to_document(
        outcome.graph,
        repository=outcome.repo_slug,
        head_sha=outcome.head_sha,
        selected_author=outcome.identity.primary_email,
        built_at=built_at,
        stats={
            "commits": len(outcome.commits),
            "files_touched": len(outcome.touched),
            "files_at_head": len(outcome.touched.at_head),
            "symbols": len(outcome.parsed.symbols),
            "calls_resolved": len(outcome.parsed.calls),
            "calls_unresolved": outcome.parsed.unresolved_calls,
            "module_level_calls": outcome.parsed.module_level_calls,
            "internal_imports": len(outcome.parsed.internal_imports),
            "external_imports": len(outcome.parsed.external_imports),
        },
        warnings=outcome.warnings,
    )

    out_dir = Path(args.out)
    graph_path = write_json(document, out_dir / "graph.json")
    report_path = write_report(PortfolioGraph(document), out_dir / "REPORT.md")

    print(f"graph  -> {graph_path}")
    print(f"report -> {report_path}")
    counts = document["meta"]["counts"]
    print(f"{counts['nodes']} nodes, {counts['edges']} edges")
    if outcome.warnings and verbose:
        _eprint(f"{len(outcome.warnings)} warning(s); see the report")
    return 0


# --------------------------------------------------------------------------
# report / query
# --------------------------------------------------------------------------

def _cmd_portfolio(args: argparse.Namespace) -> int:
    from .portfolio import build_portfolio, parse_repo_spec

    specs = [parse_repo_spec(raw) for raw in args.repo]
    options = BuildOptions(
        authors=args.author,
        handles=tuple(args.handle or ()),
        rev=args.rev,
        since=args.since,
        until=args.until,
        include_merges=args.include_merges,
        include_vendored=args.include_vendored,
        max_blame_files=args.max_blame_files,
        skip_blame=args.no_blame,
    )

    verbose = not args.quiet
    outcome = build_portfolio(
        specs,
        options,
        portfolio_name=args.name,
        progress=(lambda m: _eprint(f"  {m}")) if verbose else None,
    )

    built_at = DEFAULT_BUILT_AT
    if args.timestamp:
        built_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    document = to_document(
        outcome.graph,
        repository=args.name,
        head_sha="",   # a portfolio spans many HEADs; each Repository node carries its own
        selected_author=(outcome.identity.primary_email if outcome.identity else ""),
        built_at=built_at,
        stats={
            "repositories_requested": len(specs),
            "repositories_built": len(outcome.built),
            "repositories_skipped": len(outcome.skipped),
            "commits": outcome.total_commits,
            "nodes_merged": outcome.merge_report.nodes_merged if outcome.merge_report else 0,
            "edges_merged": outcome.merge_report.edges_merged if outcome.merge_report else 0,
        },
        warnings=outcome.warnings,
    )

    out_dir = Path(args.out)
    graph_path = write_json(document, out_dir / "graph.json")
    report_path = write_report(PortfolioGraph(document), out_dir / "REPORT.md")

    print(f"graph  -> {graph_path}")
    print(f"report -> {report_path}")
    counts = document["meta"]["counts"]
    print(
        f"{len(outcome.built)}/{len(specs)} repositories · "
        f"{counts['nodes']} nodes, {counts['edges']} edges"
    )
    for result in outcome.skipped:
        _eprint(f"  skipped {result.spec.path}: {result.error}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    graph = PortfolioGraph.load(args.graph)
    text = generate_report(graph)
    if args.out:
        target = write_report(graph, args.out)
        print(f"report -> {target}")
    else:
        print(text)
    return 0


def _cmd_brief(args: argparse.Namespace) -> int:
    from .brief import estimate_tokens, fit_to_budget, write_context_pack

    graph = PortfolioGraph.load(args.graph)
    if args.budget:
        options, estimate = fit_to_budget(
            graph, args.budget, with_source=args.with_source
        )
        if estimate > args.budget:
            _eprint(
                f"  note: even the tightest settings estimate ~{estimate:,} tokens, "
                f"above the {args.budget:,} budget"
            )
        else:
            _eprint(f"  fitted to ~{estimate:,} tokens (budget {args.budget:,})")
        written = write_context_pack(
            graph, args.out, with_source=args.with_source, options=options
        )
    else:
        written = write_context_pack(graph, args.out, with_source=args.with_source)

    total = 0
    for path in written:
        size = path.stat().st_size
        total += size
        print(f"  {path}  ({size / 1024:.0f} KB)")
    tokens = estimate_tokens("x" * total)
    print(
        f"{len(written)} files, {total / 1024:.0f} KB, ~{tokens:,} tokens "
        f"-> {args.out}"
    )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    try:
        from .mcp_server import serve
    except ModuleNotFoundError as exc:
        _eprint(
            f"error: the MCP server needs the 'mcp' package ({exc.name} missing)."
        )
        _eprint("  pip install 'prism-portfolio[mcp]'   (or: pip install mcp)")
        return 1

    graph = Path(args.graph)
    if not graph.exists():
        _eprint(f"error: no graph at {graph}")
        return 1

    # stdio is the transport, so nothing may be printed to stdout -- it is the
    # protocol channel. Status goes to stderr.
    _eprint(f"prism MCP server on {graph} ({args.transport})")
    serve(graph, transport=args.transport)
    return 0


def _cmd_query(args: argparse.Namespace) -> int:
    graph = PortfolioGraph.load(args.graph)

    if args.tech:
        files = graph.files_using(args.tech)
        if not files:
            _eprint(f"no files found using {args.tech!r}")
            return 1
        # In a portfolio several projects legitimately have an "app.py", so a
        # bare path is ambiguous. Qualify every row with its project.
        def project_of(node_id: str) -> str:
            repo = graph.repo_of(node_id or "")
            return (graph.nodes.get(repo, {}) or {}).get("name", "") if repo else ""

        print(f"# Files using {args.tech}")
        for row in files:
            share = "-" if row["blame_share"] is None else f"{round(row['blame_share'] * 100)}%"
            where = project_of(row.get("id", ""))
            prefix = f"[{where}] " if where else ""
            print(
                f"  {prefix}{row['path']}  [{share} of {row['loc']} lines, "
                f"{row['commits']} commits]  <- {row['evidence']}"
            )
        symbols = graph.symbols_using(args.tech)
        if symbols:
            print(f"\n# Symbols authored in those files ({len(symbols)})")
            for row in symbols[:30]:
                where = project_of(row.get("id", ""))
                prefix = f"[{where}] " if where else ""
                print(
                    f"  {prefix}{row['qualified_name']}  "
                    f"{row['path']}:{row['start_line']}-{row['end_line']}  "
                    f"[{round(row['blame_share'] * 100)}%]"
                )
        return 0

    if args.symbols:
        for row in graph.authored_symbols(min_share=args.min_share)[:args.limit]:
            print(
                f"  {row['qualified_name']}  {row['path']}:"
                f"{row['start_line']}-{row['end_line']}  "
                f"[{round(row['blame_share'] * 100)}% of {row['span_lines']} lines]"
            )
        return 0

    if args.files:
        for row in graph.contributed_files(min_share=args.min_share)[:args.limit]:
            share = "-" if row["blame_share"] is None else f"{round(row['blame_share'] * 100)}%"
            print(f"  {row['path']}  [{share}, {row['commits']} commits]")
        return 0

    summary = graph.summary()
    width = max(len(k) for k in summary)
    for key, value in summary.items():
        print(f"  {key.ljust(width)}  {value}")
    print()
    print("  technologies demonstrated:")
    for tech in graph.technologies()[:15]:
        print(f"    {tech.name:<22} {tech.category:<10} {tech.files} files, {tech.commits} commits")
    return 0


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="prism",
        description="Engineering Portfolio Intelligence - what one person built, with evidence.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    identities = sub.add_parser(
        "identities", help="list the author identities found in a repository"
    )
    identities.add_argument("repo", help="path to a local git repository")
    identities.add_argument("--include-merges", action="store_true")
    identities.set_defaults(func=_cmd_identities)

    builder = sub.add_parser("build", help="build the portfolio graph for one person")
    builder.add_argument("repo", help="path to a local git repository")
    builder.add_argument(
        "--author", "-a", action="append", required=True,
        help="author email, name, or substring. Repeat to merge several identities.",
    )
    builder.add_argument(
        "--handle", action="append", default=[],
        help="an external handle for this person (e.g. a GitHub username)",
    )
    builder.add_argument("--out", "-o", default=_DEFAULT_OUT, help="output directory")
    builder.add_argument("--rev", default="HEAD", help="revision to analyse")
    builder.add_argument("--since", help="only commits after this date")
    builder.add_argument("--until", help="only commits before this date")
    builder.add_argument("--include-merges", action="store_true")
    builder.add_argument(
        "--include-vendored", action="store_true",
        help="do not filter node_modules, dist, __pycache__ and friends",
    )
    builder.add_argument(
        "--max-blame-files", type=int, default=2000,
        help="cap the number of files to blame (default 2000)",
    )
    builder.add_argument(
        "--no-blame", action="store_true",
        help="skip blame entirely; much faster, no line-level attribution",
    )
    builder.add_argument(
        "--timestamp", action="store_true",
        help="stamp the real build time instead of a fixed one "
             "(off by default so builds are byte-reproducible)",
    )
    builder.add_argument("--quiet", "-q", action="store_true")
    builder.set_defaults(func=_cmd_build)

    folio = sub.add_parser(
        "portfolio", help="build one graph across several repositories"
    )
    folio.add_argument(
        "repo", nargs="+",
        help="repository paths. Use NAME=PATH to override the displayed name.",
    )
    folio.add_argument(
        "--author", "-a", action="append", required=True,
        help="author email, name, or substring. Repeat to merge several identities.",
    )
    folio.add_argument("--handle", action="append", default=[])
    folio.add_argument("--name", default="portfolio", help="name for the portfolio itself")
    folio.add_argument("--out", "-o", default=_DEFAULT_OUT)
    folio.add_argument("--rev", default="HEAD")
    folio.add_argument("--since")
    folio.add_argument("--until")
    folio.add_argument("--include-merges", action="store_true")
    folio.add_argument("--include-vendored", action="store_true")
    folio.add_argument("--max-blame-files", type=int, default=2000)
    folio.add_argument("--no-blame", action="store_true")
    folio.add_argument("--timestamp", action="store_true")
    folio.add_argument("--quiet", "-q", action="store_true")
    folio.set_defaults(func=_cmd_portfolio)

    report = sub.add_parser("report", help="render a report from a built graph")
    report.add_argument("graph", help="path to graph.json")
    report.add_argument("--out", "-o", help="write to a file instead of stdout")
    report.set_defaults(func=_cmd_report)

    brief = sub.add_parser(
        "brief",
        help="render the graph as markdown context files for an LLM project",
    )
    brief.add_argument("graph", help="path to graph.json")
    brief.add_argument("--out", "-o", default="context", help="output directory")
    brief.add_argument(
        "--budget", type=int, metavar="TOKENS",
        help="prune to fit a token budget, shedding the weakest evidence "
             "first (short helper symbols, low-churn files, long excerpts)",
    )
    brief.add_argument(
        "--with-source", action="store_true",
        help="embed source excerpts for majority-authored symbols, so "
             "implementation questions can be answered from the code itself",
    )
    brief.set_defaults(func=_cmd_brief)

    serve_cmd = sub.add_parser(
        "serve",
        help="expose a built graph to an AI client over MCP (read-only)",
    )
    serve_cmd.add_argument("graph", help="path to graph.json")
    serve_cmd.add_argument(
        "--transport", default="stdio",
        choices=["stdio", "sse", "streamable-http"],
        help="stdio for a local client (Claude Desktop, Claude Code); "
             "streamable-http to host it for a remote connector",
    )
    serve_cmd.set_defaults(func=_cmd_serve)

    query = sub.add_parser("query", help="query a built graph")
    query.add_argument("graph", help="path to graph.json")
    query.add_argument("--tech", help="show the work that demonstrates a technology")
    query.add_argument("--symbols", action="store_true", help="list authored symbols")
    query.add_argument("--files", action="store_true", help="list contributed files")
    query.add_argument("--min-share", type=float, default=0.0, help="minimum blame share")
    query.add_argument("--limit", type=int, default=40)
    query.set_defaults(func=_cmd_query)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except IdentityError as exc:
        _eprint(f"error: {exc}")
        return 2
    except (GitError, ValueError, FileNotFoundError) as exc:
        _eprint(f"error: {exc}")
        return 1
    except KeyboardInterrupt:  # pragma: no cover
        _eprint("interrupted")
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
