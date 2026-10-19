"""Stage 5 -- how much of this is actually theirs.

A ``CHANGED`` edge from git log is weak evidence. A one-line typo fix and a
600-line subsystem look identical in a commit list, which is exactly how
portfolio claims become unfalsifiable.

So Prism blames every surviving touched file at HEAD and measures what fraction
of each file -- and of each symbol's span -- is still attributed to the selected
person. That turns "I worked on this repo" into "87% of the 53 lines of
``login()`` at ``app/auth.py:40-92``", which is a citation someone can check.

The limitation is explicit and worth stating plainly: blame at HEAD sees only
surviving code. Work later rewritten by someone else is invisible here. The
commit record (insertions/deletions, including for deleted files) is kept
alongside precisely so that contribution is not lost, only un-blamed.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitio import GitError, git, show_blob
from .parse import Symbol

__all__ = ["FileAttribution", "SymbolAttribution", "Attribution", "blame_file", "attribute"]

#: Blaming a very large file is slow and the result is rarely load-bearing.
_MAX_BLAME_LINES = 50_000


@dataclass(frozen=True)
class FileAttribution:
    path: str
    blame_lines: int          # lines at HEAD attributed to the person
    total_lines: int          # blameable lines at HEAD
    blame_share: float

    @property
    def is_primary_author(self) -> bool:
        return self.blame_share >= 0.5


@dataclass(frozen=True)
class SymbolAttribution:
    key: tuple[str, str, str]   # (path, kind, qualified_name)
    blame_lines: int
    span_lines: int
    blame_share: float


@dataclass(frozen=True)
class Attribution:
    files: tuple[FileAttribution, ...]
    symbols: tuple[SymbolAttribution, ...]
    blamed_paths: tuple[str, ...]
    failed_paths: tuple[str, ...]
    #: Files with no lines to blame. An empty ``__init__.py`` is not a failure,
    #: and reporting it as one trains the reader to ignore the warnings.
    empty_paths: tuple[str, ...] = ()

    @property
    def total_blame_lines(self) -> int:
        return sum(f.blame_lines for f in self.files)


def blame_file(repo: Path | str, path: str, rev: str = "HEAD") -> list[str]:
    """Author email for each line of ``path`` at ``rev``, 1-indexed into the result.

    Returns a list where index 0 corresponds to line 1. An empty list means the
    file could not be blamed (binary, missing, or a git failure).

    ``-w`` ignores whitespace-only changes so a reformat does not transfer
    authorship; ``-M`` follows moved lines within the file.
    """
    try:
        out = git(repo, "blame", "--line-porcelain", "-w", "-M", rev, "--", path)
    except GitError:
        return []

    authors: list[str] = []
    pending: str | None = None
    for line in out.split("\n"):
        if line.startswith("author-mail "):
            pending = line[len("author-mail "):].strip().strip("<>").casefold()
        elif line.startswith("\t"):
            # The content line closes one blame record.
            authors.append(pending or "")
            pending = None
            if len(authors) > _MAX_BLAME_LINES:
                break
    return authors


def _share(part: int, whole: int) -> float:
    if whole <= 0:
        return 0.0
    # Three decimals: enough to distinguish real differences, few enough that
    # float noise cannot make two runs of the same repo differ.
    return round(part / whole, 3)


def attribute(
    repo: Path | str,
    identity,
    paths: list[str],
    symbols: tuple[Symbol, ...] = (),
    *,
    rev: str = "HEAD",
    max_files: int | None = None,
) -> Attribution:
    """Blame ``paths`` and roll the result up to files and symbols.

    ``max_files`` caps the number of blame invocations; paths are processed in
    sorted order so a capped run is still deterministic.
    """
    ordered = sorted(set(paths))
    if max_files is not None:
        ordered = ordered[:max_files]

    symbols_by_path: dict[str, list[Symbol]] = {}
    for symbol in symbols:
        symbols_by_path.setdefault(symbol.path, []).append(symbol)

    file_rows: list[FileAttribution] = []
    symbol_rows: list[SymbolAttribution] = []
    blamed: list[str] = []
    failed: list[str] = []
    empty: list[str] = []

    for path in ordered:
        authors = blame_file(repo, path, rev)
        if not authors:
            # Tell "nothing to blame" apart from "blame did not work".
            blob = show_blob(repo, path, rev)
            if blob is not None and not blob.strip():
                empty.append(path)
            else:
                failed.append(path)
            continue
        blamed.append(path)

        mine = [identity.matches(email) for email in authors]
        blame_lines = sum(mine)
        total_lines = len(authors)

        if blame_lines:
            file_rows.append(
                FileAttribution(
                    path=path,
                    blame_lines=blame_lines,
                    total_lines=total_lines,
                    blame_share=_share(blame_lines, total_lines),
                )
            )

        for symbol in symbols_by_path.get(path, []):
            # Spans are 1-indexed and inclusive; clamp to what blame returned
            # in case the parse and the blame disagree on the final line.
            start = max(1, symbol.start_line)
            end = min(symbol.end_line, total_lines)
            if end < start:
                continue
            span = end - start + 1
            in_span = sum(mine[start - 1:end])
            if in_span:
                symbol_rows.append(
                    SymbolAttribution(
                        key=symbol.key,
                        blame_lines=in_span,
                        span_lines=span,
                        blame_share=_share(in_span, span),
                    )
                )

    file_rows.sort(key=lambda f: f.path)
    symbol_rows.sort(key=lambda s: s.key)

    return Attribution(
        files=tuple(file_rows),
        symbols=tuple(symbol_rows),
        blamed_paths=tuple(blamed),
        failed_paths=tuple(sorted(failed)),
        empty_paths=tuple(sorted(empty)),
    )
