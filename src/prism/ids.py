"""Canonical ID normalization.

Adapted from graphify's ``graphify/ids.py`` (Apache-2.0, Graphify Labs); see
the repository NOTICE file.

The recipe: iterate ``casefold`` then NFKC-normalize to a fixpoint, then replace
runs of non-word characters with a single underscore, collapse repeats, strip
the edges.

The fixpoint loop is not decoration. casefold and NFKC do not commute and
neither is a fixpoint of the other -- casefolding can *expand* a character into
a base letter plus a combining mark (``I`` with dot above -> ``i`` + U+0307) and
NFKC then recomposes what it can. A single pass therefore leaves
``normalize_id(s) != normalize_id(s.casefold())`` for some combining-mark
sequences, which is exactly how one entity splits into two ghost nodes.

``re.UNICODE`` on the non-word filter keeps CJK/Cyrillic/Arabic/accented-Latin
letters instead of collapsing a whole identifier to ``_``.
"""
from __future__ import annotations

import re
import unicodedata

__all__ = ["normalize_id", "make_id", "slugify_path"]

_MAX_FOLD_PASSES = 6


def normalize_id(s: str) -> str:
    r"""Normalize a string to its canonical ID form.

    Guarantees, all enforced by ``tests/test_ids.py``:

    - Idempotent: ``normalize_id(normalize_id(s)) == normalize_id(s)``
    - The result contains only ``\w`` characters and ``_``
    - Caseless-stable: ``normalize_id(s) == normalize_id(s.casefold())``
    """
    cur = s
    for _ in range(_MAX_FOLD_PASSES):
        nxt = unicodedata.normalize("NFKC", cur.casefold())
        if nxt == cur:
            break
        cur = nxt
    cur = re.sub(r"[^\w]+", "_", cur, flags=re.UNICODE)
    cur = re.sub(r"_+", "_", cur)
    return cur.strip("_")


def make_id(*parts: str) -> str:
    """Build a canonical ID from one or more name parts."""
    return normalize_id("_".join(p.strip("_.") for p in parts if p))


def slugify_path(path: str) -> str:
    """Normalize a repo-relative path for *matching* only.

    Prism stores paths verbatim (POSIX separators) in node IDs because they are
    already canonical and readable; this is for case-insensitive comparison on
    filesystems that fold case.
    """
    return path.replace("\\", "/").casefold()
