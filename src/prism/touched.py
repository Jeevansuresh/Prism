"""Stage 3 -- the bounded work set.

This is the module that makes Prism cheap. Graphify parses a whole corpus;
Prism parses only what the selected person's commits actually reached, which on
a shared repository is a small fraction of it.

Two jobs:

1. Follow rename chains so ``old/auth.py`` and ``app/auth.py`` are one File,
   credited with the full history of both.
2. Separate paths that still exist at HEAD (parseable, blameable) from paths
   that do not (still real contributions, still graphed, but evidence is
   limited to the commit record).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from .gitio import ls_files
from .history import Commit

__all__ = ["TouchedFile", "TouchedSet", "resolve_touched"]

#: Extensions we never attempt to parse or blame, regardless of who touched them.
_NON_SOURCE_SUFFIXES = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".bmp", ".tiff",
        ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z", ".rar",
        ".mp3", ".mp4", ".wav", ".mov", ".avi", ".webm", ".ogg",
        ".ttf", ".otf", ".woff", ".woff2", ".eot",
        ".so", ".dll", ".dylib", ".exe", ".bin", ".o", ".a", ".class", ".pyc",
        ".lock", ".pack", ".idx",
        # Models, embeddings and datasets. These are the ones that actually
        # distorted a portfolio: a pickled model and a FAISS index have no NUL
        # byte early enough for git to call them binary, so blame happily
        # reported tens of thousands of "authored lines" for a file nobody
        # typed. Checking in a 50MB .pkl is not writing 50,000 lines of code.
        ".pkl", ".pickle", ".joblib", ".npy", ".npz", ".h5", ".hdf5",
        ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".tflite", ".pb",
        ".index", ".faiss", ".bin2", ".model", ".vec", ".kv",
        ".parquet", ".feather", ".arrow", ".db", ".sqlite", ".sqlite3",
        ".dat", ".dump", ".sav",
    }
)

#: Directory segments whose contents are never a person's own work.
_VENDOR_SEGMENTS = frozenset(
    {
        "node_modules", "vendor", "third_party", "thirdparty", "dist", "build",
        "__pycache__", ".venv", "venv", "env", ".tox", ".mypy_cache",
        ".pytest_cache", "site-packages", "target", "bin", "obj",
        ".git", ".idea", ".vscode", "coverage", ".next", ".nuxt",
    }
)


@dataclass(frozen=True)
class TouchedFile:
    """A path the person touched, after rename resolution."""

    path: str                       # canonical path (at HEAD if it survives)
    exists_at_head: bool
    commits: tuple[str, ...]        # SHAs, chronological
    insertions: int
    deletions: int
    first_at: str
    last_at: str
    aliases: tuple[str, ...] = field(default=())   # earlier names, chronological
    is_binary: bool = False

    @property
    def was_renamed(self) -> bool:
        return bool(self.aliases)


@dataclass(frozen=True)
class TouchedSet:
    files: tuple[TouchedFile, ...]
    #: Every historical path spelling -> canonical path. Lets any later stage
    #: resolve a raw git path without re-deriving the chain.
    canonical: dict[str, str]

    def __iter__(self):
        return iter(self.files)

    def __len__(self) -> int:
        return len(self.files)

    @property
    def at_head(self) -> tuple[TouchedFile, ...]:
        return tuple(f for f in self.files if f.exists_at_head)

    def parseable(self) -> tuple[TouchedFile, ...]:
        """Files at HEAD that are worth handing to the parser."""
        return tuple(f for f in self.at_head if is_source_candidate(f.path) and not f.is_binary)

    def blameable(self) -> tuple[TouchedFile, ...]:
        """Files at HEAD worth blaming.

        The same predicate as :meth:`parseable`, and deliberately so: a file
        that is not plausibly hand-written source should not contribute
        authored lines either. They are separate methods because applying the
        filter to parsing but not to blame is exactly the bug that let a
        checked-in model file count as 19,407 lines of someone's work.
        """
        return self.parseable()

    def get(self, path: str) -> TouchedFile | None:
        canonical = self.canonical.get(path, path)
        for touched in self.files:
            if touched.path == canonical:
                return touched
        return None


def is_vendored(path: str) -> bool:
    """True if any path segment marks generated, vendored, or tooling output."""
    return any(segment in _VENDOR_SEGMENTS for segment in path.split("/"))


def is_source_candidate(path: str) -> bool:
    """True if the path could plausibly hold a person's own source or config."""
    if is_vendored(path):
        return False
    return Path(path).suffix.lower() not in _NON_SOURCE_SUFFIXES


def _build_rename_chain(commits: list[Commit]) -> dict[str, str]:
    """Map every historical spelling to the final one.

    Commits arrive oldest-first, so following each rename forward and then
    re-pointing everything that already resolved to the old name keeps the map
    flat -- one lookup, no chain walking at read time.
    """
    canonical: dict[str, str] = {}
    for commit in commits:
        for change in commit.changes:
            if not change.is_rename or change.old_path is None:
                continue
            old, new = change.old_path, change.path
            for key, value in list(canonical.items()):
                if value == old:
                    canonical[key] = new
            canonical[old] = new
    return canonical


def resolve_touched(
    repo: Path | str,
    commits: list[Commit],
    *,
    rev: str = "HEAD",
    include_vendored: bool = False,
) -> TouchedSet:
    """Collapse a commit list into the set of files the person actually worked on."""
    canonical = _build_rename_chain(commits)
    head_files = ls_files(repo, rev)

    accumulated: dict[str, dict] = {}
    for commit in commits:
        for change in commit.changes:
            final = canonical.get(change.path, change.path)
            entry = accumulated.setdefault(
                final,
                {
                    "commits": [],
                    "insertions": 0,
                    "deletions": 0,
                    "first": commit.authored_at,
                    "last": commit.authored_at,
                    "aliases": [],
                    "binary": False,
                },
            )
            entry["commits"].append(commit.sha)
            entry["insertions"] += change.insertions
            entry["deletions"] += change.deletions
            entry["last"] = commit.authored_at
            entry["binary"] = entry["binary"] or change.is_binary

            # Record every spelling other than the canonical one.
            for spelling in (change.path, change.old_path):
                if spelling and spelling != final and spelling not in entry["aliases"]:
                    entry["aliases"].append(spelling)

    files: list[TouchedFile] = []
    for path, entry in accumulated.items():
        if not include_vendored and is_vendored(path):
            continue
        files.append(
            TouchedFile(
                path=path,
                exists_at_head=path in head_files,
                commits=tuple(entry["commits"]),
                insertions=entry["insertions"],
                deletions=entry["deletions"],
                first_at=entry["first"],
                last_at=entry["last"],
                aliases=tuple(entry["aliases"]),
                is_binary=entry["binary"],
            )
        )

    files.sort(key=lambda f: f.path)

    # Every spelling resolves, including ones that were never renamed.
    full_map = dict(canonical)
    for touched in files:
        full_map.setdefault(touched.path, touched.path)

    return TouchedSet(files=tuple(files), canonical=full_map)
