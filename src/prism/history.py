"""Stage 2 -- the selected person's commits and what each one touched.

This is the spine of the whole system. Everything downstream (which files to
parse, which symbols to blame, which technologies to credit) is reachable only
because a commit of theirs reached it first.

Two git passes, joined on ``(sha, path)``:

* ``--numstat``     gives insertions/deletions but no change type
* ``--name-status`` gives the change type but no line counts

git has no single format that gives both cleanly for renames, and running two
plumbing commands is far cheaper and far more legible than reconstructing one
from ``--raw`` interleaved output.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from .gitio import RECORD, UNIT, git_lines
from .schema import posix

__all__ = ["FileChange", "Commit", "walk"]

#: ``pre{old => new}post`` -- git's compact rename form.
_BRACED_RENAME = re.compile(r"^(?P<pre>.*)\{(?P<old>.*) => (?P<new>.*)\}(?P<post>.*)$")


@dataclass(frozen=True)
class FileChange:
    """One path touched by one commit."""

    path: str                      # path AFTER the change
    change_type: str               # A | M | D | R | C | T
    insertions: int
    deletions: int
    old_path: str | None = None    # set for R/C
    is_binary: bool = False

    @property
    def is_rename(self) -> bool:
        return self.old_path is not None and self.old_path != self.path


@dataclass(frozen=True)
class Commit:
    """One commit authored by the selected person."""

    sha: str
    short_sha: str
    authored_at: str
    committed_at: str
    author_email: str
    author_name: str
    subject: str
    is_merge: bool
    changes: tuple[FileChange, ...] = field(default=())

    @property
    def insertions(self) -> int:
        return sum(c.insertions for c in self.changes)

    @property
    def deletions(self) -> int:
        return sum(c.deletions for c in self.changes)

    @property
    def files_changed(self) -> int:
        return len(self.changes)


def _split_braced_rename(field_text: str) -> tuple[str, str] | None:
    """Expand ``src/{old.py => new.py}`` into ``(src/old.py, src/new.py)``."""
    match = _BRACED_RENAME.match(field_text)
    if not match:
        return None
    pre, old, new, post = (match.group(g) for g in ("pre", "old", "new", "post"))
    # Collapse the double slash left behind when one side of the brace is empty.
    old_path = re.sub(r"//+", "/", f"{pre}{old}{post}")
    new_path = re.sub(r"//+", "/", f"{pre}{new}{post}")
    return old_path, new_path


def _parse_numstat_path(field_text: str) -> tuple[str, str | None]:
    """Return ``(new_path, old_path_or_None)`` from a numstat path field."""
    braced = _split_braced_rename(field_text)
    if braced is not None:
        old_path, new_path = braced
        return posix(new_path), posix(old_path)
    if " => " in field_text:
        old_path, _, new_path = field_text.partition(" => ")
        return posix(new_path.strip()), posix(old_path.strip())
    return posix(field_text), None


def _log_args(
    identity_emails: tuple[str, ...],
    *,
    include_merges: bool,
    since: str | None,
    until: str | None,
    rev: str,
) -> list[str]:
    # --fixed-strings, not a regex. git's --author defaults to POSIX basic
    # regex, where GNU reads an escaped "\+" as the "one or more" quantifier
    # rather than a literal plus -- so a Python re.escape()'d address silently
    # matched NOTHING for every email containing "+", which is every GitHub
    # noreply address (<id>+<user>@users.noreply.github.com) and every
    # "user+tag@" alias. Those commits vanished from the graph with no error.
    # --regexp-ignore-case because identities are stored casefolded while git
    # stores the address as typed: searching a casefolded "...jeevansuresh@..."
    # against a commit that says "...Jeevansuresh@..." matched nothing, which
    # dropped that author's commits just as silently as the "+" did.
    args = [
        "log", rev, "--root", "-M", "--date=iso-strict",
        "--fixed-strings", "--regexp-ignore-case",
    ]
    if not include_merges:
        args.append("--no-merges")
    if since:
        args.append(f"--since={since}")
    if until:
        args.append(f"--until={until}")
    for email in identity_emails:
        # Still a superset (substring match); the exact address is re-checked
        # against %ae in Python below.
        args.append(f"--author={email}")
    return args


_FORMAT = (
    RECORD + "%H" + UNIT + "%h" + UNIT + "%aI" + UNIT + "%cI"
    + UNIT + "%ae" + UNIT + "%an" + UNIT + "%P" + UNIT + "%s"
)


def _parse_header(line: str) -> dict | None:
    parts = line[len(RECORD):].split(UNIT)
    if len(parts) < 8:
        return None
    sha, short, authored, committed, email, name, parents, subject = parts[:8]
    return {
        "sha": sha.strip(),
        "short_sha": short.strip(),
        "authored_at": authored.strip(),
        "committed_at": committed.strip(),
        "author_email": email.strip(),
        "author_name": name.strip(),
        "is_merge": len(parents.split()) > 1,
        "subject": subject.strip(),
    }


def _collect_numstat(repo: Path | str, args: list[str]) -> dict[str, dict]:
    """sha -> {header..., 'stats': {path: (ins, dels, old_path, is_binary)}}"""
    commits: dict[str, dict] = {}
    current: dict | None = None

    for line in git_lines(repo, *args, "--numstat", f"--format={_FORMAT}", check=False):
        if line.startswith(RECORD):
            header = _parse_header(line)
            if header is None:
                current = None
                continue
            header["stats"] = {}
            commits[header["sha"]] = header
            current = header
            continue
        if not line.strip() or current is None:
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        raw_ins, raw_dels, path_field = parts[0], parts[1], "\t".join(parts[2:])
        is_binary = raw_ins == "-" or raw_dels == "-"
        insertions = 0 if is_binary else int(raw_ins or 0)
        deletions = 0 if is_binary else int(raw_dels or 0)
        new_path, old_path = _parse_numstat_path(path_field)
        current["stats"][new_path] = (insertions, deletions, old_path, is_binary)

    return commits


def _collect_name_status(repo: Path | str, args: list[str]) -> dict[str, dict[str, tuple[str, str | None]]]:
    """sha -> {path: (change_type, old_path_or_None)}"""
    statuses: dict[str, dict[str, tuple[str, str | None]]] = {}
    current: dict | None = None

    for line in git_lines(repo, *args, "--name-status", f"--format={_FORMAT}", check=False):
        if line.startswith(RECORD):
            header = _parse_header(line)
            if header is None:
                current = None
                continue
            current = statuses.setdefault(header["sha"], {})
            continue
        if not line.strip() or current is None:
            continue
        parts = line.split("\t")
        status = parts[0].strip()
        if not status:
            continue
        letter = status[0].upper()
        if letter in {"R", "C"} and len(parts) >= 3:
            current[posix(parts[2])] = (letter, posix(parts[1]))
        elif len(parts) >= 2:
            current[posix(parts[1])] = (letter, None)

    return statuses


def walk(
    repo: Path | str,
    identity,
    *,
    include_merges: bool = False,
    since: str | None = None,
    until: str | None = None,
    rev: str = "HEAD",
) -> list[Commit]:
    """Every commit in ``rev`` authored by ``identity``, oldest first.

    ``identity`` is a :class:`~prism.identity.MergedIdentity`. The ``--author``
    filter passed to git is a regex superset; each commit's ``%ae`` is then
    compared exactly, so an address that merely shares a substring with the
    selected one is never miscredited.
    """
    emails = tuple(identity.emails)
    args = _log_args(
        emails, include_merges=include_merges, since=since, until=until, rev=rev
    )

    numstat = _collect_numstat(repo, args)
    statuses = _collect_name_status(repo, args)

    commits: list[Commit] = []
    for sha, header in numstat.items():
        if not identity.matches(header["author_email"]):
            continue  # regex superset matched someone else

        status_map = statuses.get(sha, {})
        changes: list[FileChange] = []
        for path, (insertions, deletions, old_from_numstat, is_binary) in header["stats"].items():
            change_type, old_from_status = status_map.get(path, ("M", None))
            old_path = old_from_status or old_from_numstat
            changes.append(
                FileChange(
                    path=path,
                    change_type=change_type,
                    insertions=insertions,
                    deletions=deletions,
                    old_path=old_path,
                    is_binary=is_binary,
                )
            )
        changes.sort(key=lambda c: c.path)

        commits.append(
            Commit(
                sha=sha,
                short_sha=header["short_sha"],
                authored_at=header["authored_at"],
                committed_at=header["committed_at"],
                author_email=header["author_email"],
                author_name=header["author_name"],
                subject=header["subject"],
                is_merge=header["is_merge"],
                changes=tuple(changes),
            )
        )

    # Oldest first: chronological order is what rename-chain resolution needs,
    # and it makes the report read like a narrative.
    commits.sort(key=lambda c: (c.authored_at, c.sha))
    return commits
