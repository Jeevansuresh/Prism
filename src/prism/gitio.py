"""Thin, deterministic wrapper around the ``git`` CLI.

Prism talks to git through subprocess rather than a binding library. git is
already installed wherever a repository is, the plumbing output formats are
stable and documented, and this keeps the dependency surface to zero for the
half of the pipeline that matters most.

Everything here is read-only. No command in this module can modify a
repository.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "GitError", "NotARepository", "git", "git_lines", "is_repository",
    "repo_root", "head_sha", "current_branch", "ls_files", "show_blob",
    "file_line_count",
    "UNIT", "RECORD",
]

# ASCII field/record separators. Safe inside commit subjects and paths in a way
# that newlines and tabs are not.
UNIT = "\x1f"
RECORD = "\x1e"

_TIMEOUT_SECONDS = 600


class GitError(RuntimeError):
    """A git invocation failed."""


class NotARepository(GitError):
    """The given path is not inside a git work tree."""


@dataclass(frozen=True)
class GitResult:
    returncode: int
    stdout: str
    stderr: str


def _git_executable() -> str:
    exe = shutil.which("git")
    if exe is None:
        raise GitError("git executable not found on PATH")
    return exe


def _clean_env() -> dict[str, str]:
    """A reproducible environment for git.

    Locale and pager settings leak into git's output formatting; pinning them
    is what lets two runs on the same repository produce byte-identical graphs.
    """
    env = dict(os.environ)
    env.update(
        {
            "GIT_PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
            "LANG": "C",
        }
    )
    # Credential helpers can block on network/UI; nothing here needs them.
    env.pop("GIT_ASKPASS", None)
    env.pop("SSH_ASKPASS", None)
    return env


def git(repo: Path | str, *args: str, check: bool = True) -> str:
    """Run a git command in ``repo`` and return stdout as text.

    ``core.quotepath=false`` is set on every call so non-ASCII paths come back
    as UTF-8 rather than octal escapes.
    """
    cmd = [_git_executable(), "-c", "core.quotepath=false", *args]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(repo),
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            env=_clean_env(),
        )
    except FileNotFoundError as exc:  # pragma: no cover - PATH race
        raise GitError(f"git not executable: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git timed out after {_TIMEOUT_SECONDS}s: git {' '.join(args)}") from exc

    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace").strip()
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {stderr}")
    return stdout


def git_lines(repo: Path | str, *args: str, check: bool = True) -> list[str]:
    """Run a git command and return stdout split into lines, trailing blank dropped."""
    out = git(repo, *args, check=check)
    if not out:
        return []
    lines = out.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def is_repository(path: Path | str) -> bool:
    try:
        return git(path, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def repo_root(path: Path | str) -> Path:
    """Resolve ``path`` to the root of its work tree."""
    target = Path(path).expanduser()
    if not target.exists():
        raise NotARepository(f"path does not exist: {target}")
    if not target.is_dir():
        target = target.parent
    try:
        out = git(target, "rev-parse", "--show-toplevel").strip()
    except GitError as exc:
        raise NotARepository(f"not a git repository: {target}") from exc
    if not out:
        raise NotARepository(f"not a git repository: {target}")
    return Path(out).resolve()


def head_sha(repo: Path | str) -> str:
    """Full SHA of HEAD, or the empty string for a repository with no commits."""
    try:
        return git(repo, "rev-parse", "HEAD").strip()
    except GitError:
        return ""


def current_branch(repo: Path | str) -> str:
    try:
        name = git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    except GitError:
        return ""
    return "" if name == "HEAD" else name


def ls_files(repo: Path | str, rev: str = "HEAD") -> set[str]:
    """Every tracked path at ``rev``, as POSIX-separated repo-relative strings."""
    try:
        lines = git_lines(repo, "ls-tree", "-r", "--name-only", rev)
    except GitError:
        return set()
    return {line.strip() for line in lines if line.strip()}


def show_blob(repo: Path | str, path: str, rev: str = "HEAD") -> bytes | None:
    """Raw bytes of a tracked file at ``rev``, or None if it is not there.

    Prism reads file content from the object database rather than the working
    tree so that a dirty checkout cannot change the graph. The parse, the blame
    and the line numbers in every citation all refer to the same revision.
    """
    exe = _git_executable()
    try:
        proc = subprocess.run(
            [exe, "-c", "core.quotepath=false", "show", f"{rev}:{path}"],
            cwd=str(repo),
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            env=_clean_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def file_line_count(repo: Path | str, path: str, rev: str = "HEAD") -> int:
    """Line count of a tracked file at ``rev``. Returns 0 for binary or missing.

    Counted over bytes rather than decoded text: a checked-in model file or
    vector index can be tens of megabytes, and decoding it to count newlines
    costs real time for a result that is going to be 0 anyway.

    Callers rely on the 0-for-binary behaviour as a content-based binary check,
    so the NUL scan must stay.
    """
    blob = show_blob(repo, path, rev)
    if not blob:
        return 0
    if b"\x00" in blob[:8192]:  # binary
        return 0
    count = blob.count(b"\n")
    # A final line without a trailing newline still counts.
    return count if blob.endswith(b"\n") else count + 1
