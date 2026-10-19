"""M0 -- a synthetic git repository with pinned SHAs.

Every test below M0 asserts against this repository, so it has to be identical
on every machine and every run. That means pinning everything git would
otherwise take from the environment: author and committer dates, author and
committer identity, the initial branch name, line endings, and signing.

Two authors in the main fixture, deliberately: one author cannot catch the bug
class that matters most here -- code credited to the wrong person.

``build_plus_email_repo`` is a separate, smaller repository for the one bug that
needs an author address containing a ``+``.
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "build_fixture", "build_plus_email_repo", "JEEVAN", "JEEVAN_GITHUB",
    "DANA", "FILES", "LOGIN_LINES_BY_DANA",
]


@dataclass(frozen=True)
class Author:
    name: str
    email: str


JEEVAN = Author("Jeevan Suresh", "jeevansuresh2508@gmail.com")
DANA = Author("Dana Okafor", "dana@example.com")

#: The same human as JEEVAN, committing through the GitHub web UI. The "+" is
#: not decoration: git's --author is a POSIX basic regex by default, and an
#: escaped "\+" there means "one or more", so an address like this silently
#: matched zero commits until walk() switched to --fixed-strings. Every GitHub
#: noreply address has this shape, so the regression test needs a real one.
JEEVAN_GITHUB = Author("jeevan suresh", "70627624+Jeevansuresh@users.noreply.github.com")

#: Dana rewrites exactly this many lines inside login(), so the expected blame
#: split is derivable rather than magic.
LOGIN_LINES_BY_DANA = 4


# --------------------------------------------------------------------------
# File content
# --------------------------------------------------------------------------

REQUIREMENTS = """\
fastapi==0.115.0
pytest==8.3.0
python-dotenv>=1.0.1
"""

TOKENS_PY = """\
import hashlib


def issue_token(email: str) -> str:
    digest = hashlib.sha256(email.encode()).hexdigest()
    return digest[:32]


def revoke_token(token: str) -> bool:
    return bool(token)
"""

UTIL_PY = """\
def slugify(text: str) -> str:
    return text.strip().lower().replace(" ", "-")
"""

LEGACY_PY = """\
def old_thing() -> None:
    pass
"""

AUTH_PY_V1 = """\
from fastapi import APIRouter, HTTPException

from app.tokens import issue_token

router = APIRouter(prefix="/auth")


@router.post("/login")
def login(email: str, password: str) -> dict:
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    token = issue_token(email)
    return {"token": token}
"""

#: Same file after Dana's commit. The four lines between the markers are hers;
#: everything else stays byte-identical so blame attributes it to Jeevan.
AUTH_PY_V2 = """\
from fastapi import APIRouter, HTTPException

from app.tokens import issue_token

router = APIRouter(prefix="/auth")


@router.post("/login")
def login(email: str, password: str) -> dict:
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    if not password:
        raise HTTPException(status_code=400, detail="password required")
    if len(password) < 8:
        raise HTTPException(status_code=400, detail="password too short")
    token = issue_token(email)
    return {"token": token}
"""

CLIENT_JS = """\
import { createStore } from "redux";

export class AuthClient {
  constructor(baseUrl) {
    this.baseUrl = baseUrl;
  }

  async login(email, password) {
    const response = await fetch(`${this.baseUrl}/auth/login`);
    return response.json();
  }
}

const buildStore = () => createStore(() => ({}));
"""


@dataclass(frozen=True)
class ExpectedFile:
    path: str
    exists_at_head: bool
    language: str


FILES = (
    ExpectedFile("app/__init__.py", True, "python"),
    ExpectedFile("app/auth.py", True, "python"),
    ExpectedFile("app/helpers.py", True, "python"),
    ExpectedFile("app/tokens.py", True, "python"),
    ExpectedFile("legacy.py", False, "python"),
    ExpectedFile("requirements.txt", True, "unknown"),
    ExpectedFile("web/client.js", True, "javascript"),
)


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------

def _run(repo: Path, *args: str, author: Author | None = None, when: str | None = None) -> None:
    env_args: list[str] = []
    if author is not None:
        env_args += [
            "-c", f"user.name={author.name}",
            "-c", f"user.email={author.email}",
        ]
    env = {
        "GIT_AUTHOR_DATE": when or "",
        "GIT_COMMITTER_DATE": when or "",
        # An identity must exist even for non-commit commands.
        "GIT_AUTHOR_NAME": (author or JEEVAN).name,
        "GIT_AUTHOR_EMAIL": (author or JEEVAN).email,
        "GIT_COMMITTER_NAME": (author or JEEVAN).name,
        "GIT_COMMITTER_EMAIL": (author or JEEVAN).email,
    }
    import os

    full_env = dict(os.environ)
    full_env.update({k: v for k, v in env.items() if v})
    full_env["GIT_CONFIG_NOSYSTEM"] = "1"
    full_env["HOME"] = str(repo)

    result = subprocess.run(
        ["git", *env_args, *args],
        cwd=str(repo),
        capture_output=True,
        env=full_env,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed:\n{result.stderr.decode('utf-8', 'replace')}"
        )


def _write(repo: Path, relative: str, content: str) -> None:
    target = repo / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    # newline="" keeps the LF endings the content literals already have; a CRLF
    # translation here would change every blob hash on Windows.
    with open(target, "w", encoding="utf-8", newline="") as handle:
        handle.write(content)


def _commit(repo: Path, author: Author, when: str, subject: str) -> None:
    _run(repo, "add", "-A", author=author, when=when)
    _run(repo, "commit", "-m", subject, "--no-gpg-sign", author=author, when=when)


def build_fixture(root: Path, *, name: str = "pulse-api", variant: str = "") -> Path:
    """Create the ``pulse-api`` fixture repository under ``root``.

    ``variant`` makes a genuinely different second project -- different
    content and a different year, so different SHAs. Without it the fixture is
    byte-identical every time, which is what the determinism tests rely on, so
    the default path must stay untouched.
    """
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    year = "2027" if variant else "2026"

    _run(repo, "init", "-b", "main")
    _run(repo, "config", "core.autocrlf", "false")
    _run(repo, "config", "commit.gpgsign", "false")
    _run(repo, "config", "gc.auto", "0")

    # 1 -- Jeevan lays down the project.
    _write(repo, "requirements.txt", REQUIREMENTS)
    _write(repo, "app/__init__.py", "")
    _write(repo, "app/tokens.py", TOKENS_PY)
    _write(repo, "app/util.py", UTIL_PY)
    _write(repo, "legacy.py", LEGACY_PY)
    if variant:
        _write(repo, "VARIANT.md", "# " + variant + "\n")
    _commit(repo, JEEVAN, f"{year}-02-10T09:14:22+00:00", "Initial project skeleton")

    # 2 -- Jeevan writes the login endpoint.
    _write(repo, "app/auth.py", AUTH_PY_V1)
    _commit(repo, JEEVAN, f"{year}-02-12T11:47:05+00:00", "Add JWT login endpoint")

    # 3 -- Dana hardens it, taking four lines of login() with her.
    _write(repo, "app/auth.py", AUTH_PY_V2)
    _commit(repo, DANA, f"{year}-02-12T15:30:00+00:00", "Harden login validation")

    # 4 -- Jeevan renames a module.
    _run(repo, "mv", "app/util.py", "app/helpers.py", author=JEEVAN)
    _commit(repo, JEEVAN, f"{year}-02-13T10:00:00+00:00", "Rename util to helpers")

    # 5 -- Jeevan deletes a module.
    _run(repo, "rm", "-q", "legacy.py", author=JEEVAN)
    _commit(repo, JEEVAN, f"{year}-02-13T12:00:00+00:00", "Drop legacy module")

    # 6 -- Jeevan adds a second language.
    _write(repo, "web/client.js", CLIENT_JS)
    _commit(repo, JEEVAN, f"{year}-02-14T18:02:41+00:00", "Add web client")

    return repo


def build_plus_email_repo(root: Path) -> Path:
    """A tiny repo whose author address contains a ``+``.

    Kept separate from the main fixture so the blame-split and line-number
    assertions there stay undisturbed. This exists purely to pin the
    ``--fixed-strings`` behaviour in :func:`prism.history.walk`.
    """
    repo = root / "plus-email"
    repo.mkdir(parents=True, exist_ok=True)

    _run(repo, "init", "-b", "main")
    _run(repo, "config", "core.autocrlf", "false")
    _run(repo, "config", "commit.gpgsign", "false")

    _write(repo, "app.py", "def run() -> None:\n    pass\n")
    _commit(repo, JEEVAN_GITHUB, "2026-03-01T10:00:00+00:00", "Via the GitHub web UI")

    _write(repo, "extra.py", "def helper() -> int:\n    return 1\n")
    _commit(repo, JEEVAN_GITHUB, "2026-03-02T10:00:00+00:00", "Second web commit")

    _write(repo, "local.py", "def local() -> None:\n    pass\n")
    _commit(repo, JEEVAN, "2026-03-03T10:00:00+00:00", "From a laptop")

    return repo
