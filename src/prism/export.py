"""Stage 8 -- serialise the graph.

Determinism is a hard requirement here, not a nicety. Two builds of an
unchanged repository must produce byte-identical ``graph.json``, because that
is what makes golden-file tests possible and what makes "what changed since
last time" a trivial diff rather than a parsing exercise.

So: sorted keys, sorted node and edge arrays, no timestamps except the one the
caller passes in explicitly, and LF line endings regardless of platform.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import networkx as nx

from .schema import SCHEMA_VERSION, validate_graph

__all__ = ["to_document", "write_json", "read_json", "DEFAULT_BUILT_AT"]

#: A fixed value so a graph is reproducible unless the caller asks otherwise.
DEFAULT_BUILT_AT = "1970-01-01T00:00:00Z"


def _edge_sort_key(edge: dict[str, Any]) -> tuple:
    provenance = edge.get("provenance") or {}
    return (
        edge.get("source", ""),
        edge.get("relation", ""),
        edge.get("target", ""),
        str(provenance.get("location") or ""),
    )


def to_document(
    G: nx.MultiDiGraph,
    *,
    repository: str,
    head_sha: str,
    selected_author: str,
    built_at: str = DEFAULT_BUILT_AT,
    stats: dict[str, Any] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Render the graph as the exportable document."""
    nodes: list[dict[str, Any]] = []
    for node_id, data in G.nodes(data=True):
        row = {"id": node_id}
        row.update({k: v for k, v in data.items()})
        nodes.append(row)
    nodes.sort(key=lambda n: n["id"])

    edges: list[dict[str, Any]] = []
    for source, target, _key, data in G.edges(keys=True, data=True):
        row = {"source": source, "target": target}
        row.update({k: v for k, v in data.items()})
        edges.append(row)
    edges.sort(key=_edge_sort_key)

    type_counts: dict[str, int] = {}
    for node in nodes:
        node_type = node.get("type", "?")
        type_counts[node_type] = type_counts.get(node_type, 0) + 1

    relation_counts: dict[str, int] = {}
    for edge in edges:
        relation = edge.get("relation", "?")
        relation_counts[relation] = relation_counts.get(relation, 0) + 1

    return {
        "schema_version": SCHEMA_VERSION,
        "directed": True,
        "multigraph": True,
        "meta": {
            "built_at": built_at,
            "repository": repository,
            "head_sha": head_sha,
            "selected_author": selected_author,
            "counts": {
                "nodes": len(nodes),
                "edges": len(edges),
                "by_type": dict(sorted(type_counts.items())),
                "by_relation": dict(sorted(relation_counts.items())),
            },
            "stats": dict(sorted((stats or {}).items())),
            "warnings": list(warnings or []),
        },
        "nodes": nodes,
        "edges": edges,
    }


def write_json(document: dict[str, Any], path: str | Path, *, validate: bool = True) -> Path:
    """Write the document deterministically. Raises if it does not validate."""
    if validate:
        errors = validate_graph(document)
        if errors:
            listed = "\n  ".join(errors[:20])
            more = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
            raise ValueError(f"graph failed validation:\n  {listed}{more}")

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False)
    # newline="" keeps json's "\n" from becoming "\r\n" on Windows.
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.write("\n")
    return target


def read_json(path: str | Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
