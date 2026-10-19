"""Merging per-repository graphs into one portfolio graph.

The ID scheme does most of the work. ``File``, ``Symbol`` and ``Commit`` IDs
are already repository-scoped (or globally unique, for a SHA), so they cannot
collide. ``Person`` and ``Technology`` IDs are deliberately *not* scoped -- one
person and one ``tech:fastapi`` across every repository is the entire point of
a portfolio graph.

That deliberate sharing is also the hazard. ``networkx`` keys a MultiDiGraph
edge by ``(source, target, key)``, and Prism uses the relation as the key, so
a second repository's ``person -DEMONSTRATES-> tech:python`` edge would
silently **overwrite** the first one's counts rather than add to them. Every
shared node and edge therefore needs an explicit combine rule, and anything
without one is left alone rather than guessed at.
"""
from __future__ import annotations

import copy
from typing import Any

import networkx as nx

__all__ = ["merge_graphs", "MergeReport"]


class MergeReport:
    """What the merge did, for the caller to report or assert on."""

    def __init__(self) -> None:
        self.nodes_added = 0
        self.nodes_merged = 0
        self.edges_added = 0
        self.edges_merged = 0
        self.type_conflicts: list[str] = []

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<MergeReport nodes +{self.nodes_added}/~{self.nodes_merged} "
            f"edges +{self.edges_added}/~{self.edges_merged}>"
        )


def _union_list(existing: Any, incoming: Any) -> list:
    """Order-preserving union of two list-valued properties."""
    out = list(existing or [])
    for item in (incoming or []):
        if item not in out:
            out.append(item)
    return out


def _min_date(a: str, b: str) -> str:
    candidates = [d for d in (a, b) if d]
    return min(candidates) if candidates else ""


def _max_date(a: str, b: str) -> str:
    candidates = [d for d in (a, b) if d]
    return max(candidates) if candidates else ""


def _merge_person(existing: dict, incoming: dict) -> None:
    """One human, several repositories.

    Commit counts add; the active window is the span of both; names, emails
    and handles union because the same person legitimately commits under
    several of each.
    """
    existing["names"] = _union_list(existing.get("names"), incoming.get("names"))
    existing["emails"] = _union_list(existing.get("emails"), incoming.get("emails"))
    existing["handles"] = _union_list(existing.get("handles"), incoming.get("handles"))
    existing["commit_count"] = (existing.get("commit_count") or 0) + (incoming.get("commit_count") or 0)
    existing["first_commit_at"] = _min_date(
        existing.get("first_commit_at", ""), incoming.get("first_commit_at", "")
    )
    existing["last_commit_at"] = _max_date(
        existing.get("last_commit_at", ""), incoming.get("last_commit_at", "")
    )


def _merge_technology(existing: dict, incoming: dict) -> None:
    """One technology, however many repositories use it.

    A specific category always beats the ``library`` fallback: if any
    repository's detection knew that ``fastapi`` is a framework, the portfolio
    node keeps that rather than being flattened by a less specific guess.
    """
    existing["aliases"] = _union_list(existing.get("aliases"), incoming.get("aliases"))
    if existing.get("category") == "library" and incoming.get("category") not in (None, "library"):
        existing["category"] = incoming["category"]


def _merge_demonstrates(existing: dict, incoming: dict) -> None:
    """The edge that makes a portfolio answerable.

    ``person -DEMONSTRATES-> tech`` exists once per repository before the
    merge. Counts add up, signals and repositories union, and the active
    window spans both -- so "6 files across 3 projects" is a real sum rather
    than whichever repository happened to be merged last.

    The citation is kept from whichever side has the broader evidence, because
    a single ``path:line`` can only point at one repository and the more
    substantial one is the more useful thing to show.
    """
    incoming_files = incoming.get("files") or 0
    existing_files = existing.get("files") or 0
    if incoming_files > existing_files:
        existing["provenance"] = copy.deepcopy(incoming.get("provenance"))

    for field in ("files", "commits", "symbols"):
        existing[field] = (existing.get(field) or 0) + (incoming.get(field) or 0)

    existing["signals"] = sorted(set(_union_list(existing.get("signals"), incoming.get("signals"))))
    existing["repositories"] = sorted(
        set(_union_list(existing.get("repositories"), incoming.get("repositories")))
    )
    existing["first_at"] = _min_date(existing.get("first_at", ""), incoming.get("first_at", ""))
    existing["last_at"] = _max_date(existing.get("last_at", ""), incoming.get("last_at", ""))


#: Node types whose IDs are shared across repositories and so need a rule.
_NODE_MERGERS = {
    "Person": _merge_person,
    "Technology": _merge_technology,
}

#: Relations that can legitimately appear between the same two nodes from more
#: than one repository. Everything else is repo-scoped on at least one end.
_EDGE_MERGERS = {
    "DEMONSTRATES": _merge_demonstrates,
}


def merge_graphs(graphs: list[nx.MultiDiGraph]) -> tuple[nx.MultiDiGraph, MergeReport]:
    """Combine per-repository graphs into one portfolio graph."""
    merged = nx.MultiDiGraph()
    report = MergeReport()

    for graph in graphs:
        for node_id, data in graph.nodes(data=True):
            if node_id not in merged:
                merged.add_node(node_id, **copy.deepcopy(data))
                report.nodes_added += 1
                continue

            existing = merged.nodes[node_id]
            if existing.get("type") != data.get("type"):
                # Two different kinds of thing claiming one ID is a bug in the
                # ID scheme, not something to paper over by picking a winner.
                report.type_conflicts.append(
                    f"{node_id}: {existing.get('type')} vs {data.get('type')}"
                )
                continue

            merger = _NODE_MERGERS.get(data.get("type"))
            if merger is not None:
                merger(existing, data)
                report.nodes_merged += 1

        for source, target, key, data in graph.edges(keys=True, data=True):
            if not merged.has_edge(source, target, key=key):
                merged.add_edge(source, target, key=key, **copy.deepcopy(data))
                report.edges_added += 1
                continue

            existing = merged[source][target][key]
            merger = _EDGE_MERGERS.get(data.get("relation"))
            if merger is not None:
                merger(existing, data)
                report.edges_merged += 1

    if report.type_conflicts:
        raise ValueError(
            "node ID collision across repositories:\n  "
            + "\n  ".join(report.type_conflicts[:10])
        )

    return merged, report
