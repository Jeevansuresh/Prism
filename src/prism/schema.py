"""The data contract: node/edge types, ID construction, provenance, JSON Schema.

Every other module depends on this one and nothing in here depends on them.
If an ID is built anywhere outside this module, that is a bug -- the graph
splits a single entity into disconnected ghost nodes the moment two producers
disagree on an ID.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .ids import normalize_id

SCHEMA_VERSION = "prism/1.0"

# --------------------------------------------------------------------------
# Vocabulary
# --------------------------------------------------------------------------

NodeType = Literal["Person", "Repository", "Commit", "File", "Symbol", "Technology"]

NODE_TYPES: tuple[str, ...] = (
    "Person", "Repository", "Commit", "File", "Symbol", "Technology",
)

RELATIONS: tuple[str, ...] = (
    "AUTHORED",          # Person   -> Commit
    "IN_REPOSITORY",     # Commit   -> Repository, File -> Repository
    "CHANGED",           # Commit   -> File
    "DEFINES",           # File     -> Symbol
    "MEMBER_OF",         # Symbol   -> Symbol   (method -> class)
    "CALLS",             # Symbol   -> Symbol
    "IMPORTS",           # File     -> File     (resolved, internal)
    "IMPORTS_EXTERNAL",  # File     -> Technology
    "USES",              # File/Repository -> Technology
    "CONTRIBUTED_TO",    # Person   -> File       (derived)
    "AUTHORED_SYMBOL",   # Person   -> Symbol     (derived)
    "DEMONSTRATES",      # Person   -> Technology (derived)
)

# graphify's vocabulary, kept deliberately: it is already well understood.
Confidence = Literal["EXTRACTED", "INFERRED", "AMBIGUOUS"]
CONFIDENCES: tuple[str, ...] = ("EXTRACTED", "INFERRED", "AMBIGUOUS")

ProvenanceKind = Literal["git_log", "git_blame", "ast", "manifest"]
PROVENANCE_KINDS: tuple[str, ...] = ("git_log", "git_blame", "ast", "manifest")

SYMBOL_KINDS: tuple[str, ...] = (
    "function", "method", "class", "interface", "struct", "enum", "module",
)

TECH_CATEGORIES: tuple[str, ...] = (
    "language", "framework", "library", "database", "infra", "tool",
)


# --------------------------------------------------------------------------
# Provenance
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Provenance:
    """Where a relationship came from. Present on every single edge."""

    kind: ProvenanceKind
    confidence: Confidence = "EXTRACTED"
    commit: str | None = None
    source: str | None = None
    location: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "commit": self.commit,
            "source": self.source,
            "location": self.location,
            "confidence": self.confidence,
        }


def span(start: int | None, end: int | None = None) -> str | None:
    """Format a source location: ``L40-L92``, ``L7``, or None."""
    if start is None:
        return None
    if end is None or end == start:
        return f"L{start}"
    return f"L{start}-L{end}"


# --------------------------------------------------------------------------
# Node IDs -- the single source of truth
# --------------------------------------------------------------------------

def person_id(email: str) -> str:
    return f"person:{email.strip().casefold()}"


def repo_id(slug: str) -> str:
    return f"repo:{normalize_id(slug)}"


def commit_id(sha: str) -> str:
    return f"commit:{sha.strip().lower()}"


def posix(path: str) -> str:
    return path.replace("\\", "/")


def file_id(repo_slug: str, path: str) -> str:
    """Paths stay verbatim (POSIX separators) -- canonical and readable already."""
    return f"file:{normalize_id(repo_slug)}:{posix(path)}"


def symbol_id(repo_slug: str, path: str, kind: str, qualified_name: str) -> str:
    return f"{file_id(repo_slug, path)}#{kind}:{qualified_name}"


def tech_id(name: str) -> str:
    return f"tech:{normalize_id(name)}"


# --------------------------------------------------------------------------
# JSON Schema for the exported graph
# --------------------------------------------------------------------------

_PROVENANCE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["kind", "confidence", "commit", "source", "location"],
    "properties": {
        "kind": {"enum": list(PROVENANCE_KINDS)},
        "confidence": {"enum": list(CONFIDENCES)},
        "commit": {"type": ["string", "null"]},
        "source": {"type": ["string", "null"]},
        "location": {"type": ["string", "null"]},
    },
    "additionalProperties": False,
}

GRAPH_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Prism portfolio graph",
    "type": "object",
    "required": ["schema_version", "directed", "multigraph", "meta", "nodes", "edges"],
    "properties": {
        "schema_version": {"const": SCHEMA_VERSION},
        "directed": {"const": True},
        "multigraph": {"const": True},
        "meta": {
            "type": "object",
            "required": ["built_at", "repository", "head_sha", "selected_author", "counts"],
            "properties": {
                "built_at": {"type": "string"},
                "repository": {"type": "string"},
                "head_sha": {"type": "string"},
                "selected_author": {"type": "string"},
                "counts": {"type": "object"},
                "stats": {"type": "object"},
                "warnings": {"type": "array", "items": {"type": "string"}},
            },
        },
        "nodes": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id", "type"],
                "properties": {
                    "id": {"type": "string", "minLength": 1},
                    "type": {"enum": list(NODE_TYPES)},
                },
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["source", "target", "relation", "provenance"],
                "properties": {
                    "source": {"type": "string", "minLength": 1},
                    "target": {"type": "string", "minLength": 1},
                    "relation": {"enum": list(RELATIONS)},
                    "provenance": _PROVENANCE_SCHEMA,
                },
            },
        },
    },
}


def validate_graph(doc: dict[str, Any]) -> list[str]:
    """Validate an exported graph document. Returns a list of error strings.

    Beyond the JSON Schema this enforces the two referential invariants the
    schema cannot express: unique node IDs, and every edge endpoint resolving
    to a declared node.
    """
    import jsonschema

    errors: list[str] = []
    validator = jsonschema.Draft202012Validator(GRAPH_SCHEMA)
    for err in sorted(validator.iter_errors(doc), key=lambda e: list(e.path)):
        path = "/".join(str(p) for p in err.path) or "<root>"
        errors.append(f"{path}: {err.message}")

    seen: set[str] = set()
    for node in doc.get("nodes", []):
        nid = node.get("id")
        if nid in seen:
            errors.append(f"duplicate node id: {nid}")
        seen.add(nid)

    for edge in doc.get("edges", []):
        for endpoint in ("source", "target"):
            ref = edge.get(endpoint)
            if ref not in seen:
                errors.append(
                    f"dangling edge {endpoint}: {ref} "
                    f"({edge.get('relation')} {edge.get('source')} -> {edge.get('target')})"
                )
    return errors


__all__ = [
    "SCHEMA_VERSION", "NODE_TYPES", "RELATIONS", "CONFIDENCES", "PROVENANCE_KINDS",
    "SYMBOL_KINDS", "TECH_CATEGORIES", "Provenance", "span", "posix",
    "person_id", "repo_id", "commit_id", "file_id", "symbol_id", "tech_id",
    "GRAPH_SCHEMA", "validate_graph",
]
