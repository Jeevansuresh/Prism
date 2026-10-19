"""Querying a built graph.

This is the thin traversal layer that proves the Phase 1 graph can answer the
questions the eventual product asks -- "which work demonstrates X, and how was
it implemented" -- without yet being that product. No ranking model, no
explanations, no LLM: just traversals that return evidence.

It reads the exported document rather than the in-memory ``networkx`` graph, so
a saved ``graph.json`` is queryable on its own.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from .export import read_json

__all__ = ["PortfolioGraph", "Evidence", "TechSummary"]


@dataclass(frozen=True)
class Evidence:
    """A citation: a file, a line range, and why it is being shown."""

    source: str
    location: str | None
    detail: str = ""
    confidence: str = "EXTRACTED"

    def __str__(self) -> str:
        where = f"{self.source}:{self.location}" if self.location else self.source
        return f"{where} ({self.detail})" if self.detail else where


@dataclass
class TechSummary:
    name: str
    category: str
    files: int
    commits: int
    symbols: int
    signals: list[str]
    first_at: str
    last_at: str
    repositories: list[str] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)


class PortfolioGraph:
    """Indexed read access to an exported Prism graph."""

    def __init__(self, document: dict[str, Any]):
        self.document = document
        self.meta: dict[str, Any] = document.get("meta", {})
        self.nodes: dict[str, dict[str, Any]] = {
            node["id"]: node for node in document.get("nodes", [])
        }
        self.edges: list[dict[str, Any]] = list(document.get("edges", []))

        self._out: dict[str, list[dict]] = {}
        self._in: dict[str, list[dict]] = {}
        for edge in self.edges:
            self._out.setdefault(edge["source"], []).append(edge)
            self._in.setdefault(edge["target"], []).append(edge)

    # -- construction ----------------------------------------------------

    @classmethod
    def load(cls, path: str | Path) -> "PortfolioGraph":
        return cls(read_json(path))

    @classmethod
    def from_networkx(cls, G, **meta: Any) -> "PortfolioGraph":
        from .export import to_document

        return cls(to_document(G, **meta))

    # -- primitives ------------------------------------------------------

    def out_edges(self, node_id: str, relation: str | None = None) -> list[dict]:
        edges = self._out.get(node_id, [])
        return [e for e in edges if relation is None or e["relation"] == relation]

    def in_edges(self, node_id: str, relation: str | None = None) -> list[dict]:
        edges = self._in.get(node_id, [])
        return [e for e in edges if relation is None or e["relation"] == relation]

    def of_type(self, node_type: str) -> list[dict]:
        return [n for n in self.nodes.values() if n.get("type") == node_type]

    @property
    def person(self) -> dict[str, Any] | None:
        people = self.of_type("Person")
        return people[0] if people else None

    @property
    def repository(self) -> dict[str, Any] | None:
        repos = self.of_type("Repository")
        return repos[0] if repos else None

    @property
    def is_portfolio(self) -> bool:
        return len(self.of_type("Repository")) > 1

    def repo_of(self, node_id: str) -> str | None:
        """The Repository node a File, Symbol or Commit belongs to."""
        node = self.nodes.get(node_id)
        if node is None:
            return None
        if node.get("type") == "Repository":
            return node_id
        if node.get("type") == "Symbol":
            # A Symbol reaches its repository through the File that defines it.
            for edge in self.in_edges(node_id, "DEFINES"):
                return self.repo_of(edge["source"])
            return None
        for edge in self.out_edges(node_id, "IN_REPOSITORY"):
            return edge["target"]
        return None

    # -- queries ---------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        person = self.person or {}
        repo = self.repository or {}
        contributed = self.out_edges(person.get("id", ""), "CONTRIBUTED_TO")
        authored_symbols = self.out_edges(person.get("id", ""), "AUTHORED_SYMBOL")
        blame_lines = sum(e.get("blame_lines") or 0 for e in contributed)
        repos = self.of_type("Repository")
        return {
            "person": person.get("names", [None])[0] if person.get("names") else None,
            "emails": person.get("emails", []),
            "repository": repo.get("name") if len(repos) == 1 else f"{len(repos)} projects",
            "repositories": len(repos),
            "head_sha": repo.get("head_sha"),
            "commits": len(self.of_type("Commit")),
            "files_touched": sum(
                1 for e in contributed
                if self.nodes.get(e["target"], {}).get("is_source", True)
            ),
            "data_files": sum(
                1 for e in contributed
                if not self.nodes.get(e["target"], {}).get("is_source", True)
            ),
            "symbols_authored": len(authored_symbols),
            "lines_attributed": blame_lines,
            "technologies": len(self.of_type("Technology")),
            "first_commit_at": person.get("first_commit_at"),
            "last_commit_at": person.get("last_commit_at"),
        }

    def technologies(self, *, demonstrated_only: bool = True) -> list[TechSummary]:
        """Technologies, each with the person's demonstrated usage and citations.

        Ordering is by breadth of evidence (files, then commits, then symbols),
        with the name as a tiebreak. This is a stable presentation order, not a
        relevance score -- scoring is Phase 2.
        """
        person = self.person
        if person is None:
            return []

        demonstrated = {
            edge["target"]: edge for edge in self.out_edges(person["id"], "DEMONSTRATES")
        }

        summaries: list[TechSummary] = []
        for node in self.of_type("Technology"):
            edge = demonstrated.get(node["id"])
            if edge is None and demonstrated_only:
                continue

            evidence: list[Evidence] = []
            for usage in self.in_edges(node["id"]):
                if usage["relation"] not in {"IMPORTS_EXTERNAL", "USES"}:
                    continue
                provenance = usage.get("provenance", {})
                evidence.append(
                    Evidence(
                        source=provenance.get("source") or "",
                        location=provenance.get("location"),
                        detail=usage.get("module") or usage.get("version_spec") or usage.get("signal", ""),
                        confidence=provenance.get("confidence", "EXTRACTED"),
                    )
                )
            evidence.sort(key=lambda e: (e.source, e.location or ""))

            summaries.append(
                TechSummary(
                    name=node.get("name", ""),
                    category=node.get("category", "library"),
                    files=(edge or {}).get("files", 0),
                    commits=(edge or {}).get("commits", 0),
                    symbols=(edge or {}).get("symbols", 0),
                    signals=list((edge or {}).get("signals", [])),
                    first_at=(edge or {}).get("first_at", ""),
                    last_at=(edge or {}).get("last_at", ""),
                    repositories=list((edge or {}).get("repositories", [])),
                    evidence=evidence,
                )
            )

        summaries.sort(
            key=lambda t: (-len(t.repositories), -t.files, -t.commits, -t.symbols, t.name)
        )
        return summaries

    def repositories(self) -> list[dict[str, Any]]:
        """Every project in the graph with the person's footprint in each.

        Ordered by lines attributed, so the project someone actually built the
        most of comes first -- not the one with the most commits, which a
        flurry of one-line fixes would win.
        """
        person = self.person
        contributed = {
            edge["target"]: edge
            for edge in (self.out_edges(person["id"], "CONTRIBUTED_TO") if person else [])
        }

        rows: list[dict[str, Any]] = []
        for repo in self.of_type("Repository"):
            files = [e["source"] for e in self.in_edges(repo["id"], "IN_REPOSITORY")
                     if self.nodes.get(e["source"], {}).get("type") == "File"]
            commits = [e["source"] for e in self.in_edges(repo["id"], "IN_REPOSITORY")
                       if self.nodes.get(e["source"], {}).get("type") == "Commit"]

            symbols = 0
            for file_id in files:
                symbols += len(self.out_edges(file_id, "DEFINES"))

            blame_lines = sum(
                (contributed.get(f, {}).get("blame_lines") or 0) for f in files
            )
            insertions = sum(
                (contributed.get(f, {}).get("insertions") or 0) for f in files
            )
            touched = [f for f in files if contributed.get(f) is not None]
            source_files = [
                f for f in touched if self.nodes.get(f, {}).get("is_source", True)
            ]

            dates = [
                self.nodes[c].get("authored_at", "") for c in commits
                if self.nodes.get(c)
            ]
            technologies = sorted({
                self.nodes[e["target"]].get("name")
                for f in files
                for e in self.out_edges(f, "IMPORTS_EXTERNAL")
                if e["target"] in self.nodes
            } | {
                self.nodes[e["target"]].get("name")
                for e in self.out_edges(repo["id"], "USES")
                if e["target"] in self.nodes
            })

            rows.append({
                "id": repo["id"],
                "name": repo.get("name"),
                "slug": repo.get("slug", repo.get("name")),
                "languages": repo.get("languages", []),
                "head_sha": repo.get("head_sha"),
                "commits": len(commits),
                # Source files only. Committed datasets and models stay in the
                # graph but do not inflate "files built".
                "files": len(source_files),
                "data_files": len(touched) - len(source_files),
                "symbols": symbols,
                "blame_lines": blame_lines,
                "insertions": insertions,
                "first_at": min(dates) if dates else "",
                "last_at": max(dates) if dates else "",
                "technologies": [t for t in technologies if t],
            })

        rows.sort(key=lambda r: (-r["blame_lines"], -r["commits"], r["name"] or ""))
        return rows

    def projects_using(self, technology: str) -> list[dict[str, Any]]:
        """Which projects demonstrate a technology, strongest evidence first.

        This is the portfolio question in one call: it returns the project,
        how much of it the person wrote, and the symbols and citations that
        back the claim.
        """
        target = self._find_tech(technology)
        if target is None:
            return []

        files = self.files_using(technology)
        symbols = self.symbols_using(technology)

        by_repo: dict[str, dict[str, Any]] = {}
        for row in files:
            repo_id = self.repo_of(row["id"]) if row.get("id") else None
            if repo_id is None:
                continue
            bucket = by_repo.setdefault(repo_id, {
                "repository": self.nodes[repo_id].get("name"),
                "files": [], "symbols": [], "blame_lines": 0, "evidence": [],
            })
            bucket["files"].append(row["path"])
            bucket["blame_lines"] += row["blame_lines"] or 0
            if row.get("evidence"):
                bucket["evidence"].append(row["evidence"])

        for row in symbols:
            repo_id = self.repo_of(row["id"])
            if repo_id in by_repo:
                by_repo[repo_id]["symbols"].append(row)

        rows = list(by_repo.values())
        for row in rows:
            row["symbols"].sort(key=lambda s: -s["blame_lines"])
        rows.sort(key=lambda r: (-r["blame_lines"], -len(r["files"])))
        return rows

    def files_using(self, technology: str) -> list[dict[str, Any]]:
        """Files that import or declare ``technology``, with the person's share."""
        target = self._find_tech(technology)
        if target is None:
            return []

        person = self.person
        shares = {
            edge["target"]: edge
            for edge in (self.out_edges(person["id"], "CONTRIBUTED_TO") if person else [])
        }

        rows: list[dict[str, Any]] = []
        for edge in self.in_edges(target, "IMPORTS_EXTERNAL") + self.in_edges(target, "USES"):
            node = self.nodes.get(edge["source"])
            if node is None or node.get("type") != "File":
                continue
            contribution = shares.get(node["id"], {})
            rows.append(
                {
                    "id": node["id"],
                    "path": node.get("path"),
                    "language": node.get("language"),
                    "loc": node.get("loc"),
                    "blame_share": contribution.get("blame_share"),
                    "blame_lines": contribution.get("blame_lines", 0),
                    "commits": contribution.get("commits", 0),
                    "signal": edge.get("signal"),
                    "evidence": Evidence(
                        source=edge.get("provenance", {}).get("source", ""),
                        location=edge.get("provenance", {}).get("location"),
                        detail=edge.get("module", ""),
                    ),
                }
            )
        rows.sort(key=lambda r: (-(r["blame_lines"] or 0), r["path"] or ""))
        return rows

    def symbols_using(self, technology: str) -> list[dict[str, Any]]:
        """Symbols the person authored inside files that use ``technology``."""
        paths = {row["path"] for row in self.files_using(technology)}
        return [row for row in self.authored_symbols() if row["path"] in paths]

    def authored_symbols(self, *, min_share: float = 0.0) -> list[dict[str, Any]]:
        """Every symbol the person is blamed for, strongest attribution first."""
        person = self.person
        if person is None:
            return []
        rows: list[dict[str, Any]] = []
        for edge in self.out_edges(person["id"], "AUTHORED_SYMBOL"):
            node = self.nodes.get(edge["target"])
            if node is None:
                continue
            share = edge.get("blame_share") or 0.0
            if share < min_share:
                continue
            rows.append(
                {
                    "id": node["id"],
                    "name": node.get("name"),
                    "qualified_name": node.get("qualified_name"),
                    "kind": node.get("kind"),
                    "path": node.get("path"),
                    "start_line": node.get("start_line"),
                    "end_line": node.get("end_line"),
                    "signature": node.get("signature"),
                    "language": node.get("language"),
                    "decorators": node.get("decorators", []),
                    "blame_share": share,
                    "blame_lines": edge.get("blame_lines", 0),
                    "span_lines": edge.get("span_lines", 0),
                    "calls": self._call_targets(node["id"]),
                }
            )
        rows.sort(key=lambda r: (-r["blame_lines"], r["path"] or "", r["start_line"] or 0))
        return rows

    def contributed_files(self, *, min_share: float = 0.0) -> list[dict[str, Any]]:
        person = self.person
        if person is None:
            return []
        rows: list[dict[str, Any]] = []
        for edge in self.out_edges(person["id"], "CONTRIBUTED_TO"):
            node = self.nodes.get(edge["target"])
            if node is None:
                continue
            share = edge.get("blame_share")
            if share is not None and share < min_share:
                continue
            rows.append(
                {
                    "id": node["id"],
                    "path": node.get("path"),
                    "language": node.get("language"),
                    "loc": node.get("loc"),
                    "exists_at_head": node.get("exists_at_head", True),
                    "is_source": node.get("is_source", True),
                    "blame_share": share,
                    "blame_lines": edge.get("blame_lines", 0),
                    "commits": edge.get("commits", 0),
                    "insertions": edge.get("insertions", 0),
                    "deletions": edge.get("deletions", 0),
                    "first_at": edge.get("first_at", ""),
                    "last_at": edge.get("last_at", ""),
                }
            )
        rows.sort(key=lambda r: (-(r["blame_lines"] or 0), r["path"] or ""))
        return rows

    def commits(self) -> list[dict[str, Any]]:
        rows = [dict(node) for node in self.of_type("Commit")]
        rows.sort(key=lambda c: (c.get("authored_at", ""), c.get("sha", "")))
        return rows

    # -- helpers ---------------------------------------------------------

    def _find_tech(self, name: str) -> str | None:
        needle = name.strip().casefold()
        for node in self.of_type("Technology"):
            if node.get("name", "").casefold() == needle:
                return node["id"]
        for node in self.of_type("Technology"):
            if needle in node.get("name", "").casefold():
                return node["id"]
        return None

    def _call_targets(self, symbol_id: str) -> list[str]:
        names: list[str] = []
        for edge in self.out_edges(symbol_id, "CALLS"):
            target = self.nodes.get(edge["target"])
            if target is not None:
                names.append(target.get("qualified_name") or target.get("name") or "")
        return sorted(n for n in names if n)
