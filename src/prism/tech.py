"""Stage 6 -- what they built it with.

Three independent signals, each carrying its own provenance so a claim can be
traced back to the line that justifies it:

* ``manifest`` -- a declared dependency (requirements.txt, package.json, ...)
* ``import``   -- an actual import statement in a file they touched
* ``config``   -- a config/infra file whose presence implies a technology

The signals are deliberately not merged into a score. A dependency declared in
a manifest but never imported is a different kind of evidence from one imported
in forty files, and Phase 2 is where that difference gets weighed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .gitio import show_blob
from .ids import normalize_id
from .languages import language_for_path
from .parse import ExternalImport

__all__ = ["Technology", "TechUsage", "TechResult", "detect"]


@dataclass(frozen=True)
class Technology:
    name: str          # canonical display name
    category: str
    aliases: tuple[str, ...] = field(default=())

    @property
    def key(self) -> str:
        return normalize_id(self.name)


@dataclass(frozen=True)
class TechUsage:
    """One piece of evidence that a technology is in use."""

    tech: str          # canonical name
    signal: str        # manifest | import | config
    source: str        # repo-relative file
    line: int | None
    detail: str = ""   # version spec, module as written, etc.
    scope: str = "repository"   # repository | file


@dataclass
class TechResult:
    technologies: tuple[Technology, ...] = ()
    usages: tuple[TechUsage, ...] = ()

    def by_name(self, name: str) -> Technology | None:
        return next((t for t in self.technologies if t.name == name), None)


# --------------------------------------------------------------------------
# Canonicalisation
# --------------------------------------------------------------------------

#: Import root (or raw dependency name) -> canonical technology name. Only
#: entries where the two genuinely differ; anything absent is used as written.
_ALIASES: dict[str, str] = {
    # Python
    "discord": "discord.py",
    "dotenv": "python-dotenv",
    "yaml": "pyyaml",
    "cv2": "opencv",
    "sklearn": "scikit-learn",
    "PIL": "pillow",
    "bs4": "beautifulsoup4",
    "psycopg2": "psycopg2",
    "psycopg2_binary": "psycopg2",
    "pydantic_settings": "pydantic-settings",
    "apscheduler": "apscheduler",
    "jose": "python-jose",
    "jwt": "pyjwt",
    "dateutil": "python-dateutil",
    "attr": "attrs",
    "serial": "pyserial",
    "google": "google-cloud",
    "OpenGL": "pyopengl",
    "win32com": "pywin32",
    # JS / TS
    "react-dom": "react",
    "@types/node": "typescript",
}

#: Canonical name -> category. Everything unlisted falls back to "library",
#: which is the honest default: we know it is a dependency, not what kind.
_CATEGORIES: dict[str, str] = {
    # Frameworks
    "fastapi": "framework", "django": "framework", "flask": "framework",
    "starlette": "framework", "tornado": "framework", "sanic": "framework",
    "express": "framework", "next": "framework", "nuxt": "framework",
    "react": "framework", "vue": "framework", "svelte": "framework",
    "angular": "framework", "nestjs": "framework", "remix": "framework",
    "spring-boot": "framework", "gin": "framework", "echo": "framework",
    "discord.py": "framework", "discord.js": "framework", "aiogram": "framework",
    "apscheduler": "framework", "celery": "framework", "airflow": "framework",
    # Databases / data access
    "asyncpg": "database", "psycopg2": "database", "psycopg": "database",
    "sqlalchemy": "database", "alembic": "database", "pymongo": "database",
    "motor": "database", "redis": "database", "aioredis": "database",
    "mysqlclient": "database", "sqlite3": "database", "prisma": "database",
    "mongoose": "database", "postgresql": "database", "mongodb": "database",
    "elasticsearch": "database", "neo4j": "database", "duckdb": "database",
    # Infra
    "docker": "infra", "docker-compose": "infra", "kubernetes": "infra",
    "terraform": "infra", "ansible": "infra", "github-actions": "infra",
    "nginx": "infra", "aws": "infra", "boto3": "infra", "azure": "infra",
    "gcp": "infra", "vercel": "infra", "heroku": "infra",
    # Tools
    "pytest": "tool", "unittest": "tool", "jest": "tool", "vitest": "tool",
    "mypy": "tool", "ruff": "tool", "black": "tool", "flake8": "tool",
    "eslint": "tool", "prettier": "tool", "poetry": "tool", "uv": "tool",
    "pre-commit": "tool", "tox": "tool", "coverage": "tool", "make": "tool",
}

#: Filename (or suffix) -> technology implied by its mere presence.
_CONFIG_SIGNALS: tuple[tuple[str, str, str], ...] = (
    ("dockerfile", "docker", "infra"),
    ("docker-compose.yml", "docker-compose", "infra"),
    ("docker-compose.yaml", "docker-compose", "infra"),
    ("compose.yml", "docker-compose", "infra"),
    (".dockerignore", "docker", "infra"),
    ("makefile", "make", "tool"),
    ("procfile", "heroku", "infra"),
    ("vercel.json", "vercel", "infra"),
    ("nginx.conf", "nginx", "infra"),
    (".pre-commit-config.yaml", "pre-commit", "tool"),
    ("tsconfig.json", "typescript", "language"),
    ("schema.sql", "sql", "language"),
    ("pytest.ini", "pytest", "tool"),
    ("tox.ini", "tox", "tool"),
    ("alembic.ini", "alembic", "database"),
)

_LANGUAGE_CATEGORY = "language"


def canonical(name: str) -> str:
    raw = name.strip()
    if raw in _ALIASES:
        return _ALIASES[raw]
    lowered = raw.casefold()
    if lowered in _ALIASES:
        return _ALIASES[lowered]
    return lowered


def categorise(name: str) -> str:
    return _CATEGORIES.get(name.casefold(), "library")


# --------------------------------------------------------------------------
# Manifest parsers
# --------------------------------------------------------------------------

_REQUIREMENT = re.compile(r"^\s*([A-Za-z0-9._-]+)\s*(\[[^\]]*\])?\s*([<>=!~^].*)?$")


def _parse_requirements(text: str) -> list[tuple[str, str, int]]:
    """requirements.txt -> [(name, version_spec, line)]"""
    found: list[tuple[str, str, int]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        if line.startswith(("git+", "http://", "https://")):
            continue
        match = _REQUIREMENT.match(line)
        if not match:
            continue
        name, _extras, spec = match.groups()
        found.append((name, (spec or "").strip(), number))
    return found


def _parse_pyproject(text: str) -> list[tuple[str, str, int]]:
    try:
        try:
            import tomllib
        except ModuleNotFoundError:  # pragma: no cover - py3.10
            import tomli as tomllib
        data = tomllib.loads(text)
    except Exception:
        return []

    found: list[tuple[str, str, int]] = []
    lines = text.splitlines()

    def line_of(name: str) -> int:
        for number, raw in enumerate(lines, start=1):
            if name in raw:
                return number
        return 1

    project = data.get("project", {})
    for entry in project.get("dependencies", []) or []:
        match = _REQUIREMENT.match(str(entry))
        if match:
            found.append((match.group(1), (match.group(3) or "").strip(), line_of(str(entry))))
    for group in (project.get("optional-dependencies", {}) or {}).values():
        for entry in group or []:
            match = _REQUIREMENT.match(str(entry))
            if match:
                found.append((match.group(1), (match.group(3) or "").strip(), line_of(str(entry))))

    poetry = data.get("tool", {}).get("poetry", {})
    for name, spec in (poetry.get("dependencies", {}) or {}).items():
        if name.casefold() == "python":
            continue
        found.append((name, str(spec) if isinstance(spec, str) else "", line_of(name)))

    return found


def _parse_package_json(text: str) -> list[tuple[str, str, int]]:
    try:
        data = json.loads(text)
    except Exception:
        return []
    lines = text.splitlines()

    def line_of(name: str) -> int:
        needle = f'"{name}"'
        for number, raw in enumerate(lines, start=1):
            if needle in raw:
                return number
        return 1

    found: list[tuple[str, str, int]] = []
    for section in ("dependencies", "devDependencies", "peerDependencies"):
        for name, spec in (data.get(section) or {}).items():
            found.append((name, str(spec), line_of(name)))
    return found


def _parse_go_mod(text: str) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    in_block = False
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("require ("):
            in_block = True
            continue
        if in_block and line == ")":
            in_block = False
            continue
        if in_block or line.startswith("require "):
            parts = line.removeprefix("require ").split()
            if len(parts) >= 2 and not parts[0].startswith("//"):
                found.append((parts[0], parts[1], number))
    return found


def _parse_cargo(text: str) -> list[tuple[str, str, int]]:
    found: list[tuple[str, str, int]] = []
    section = ""
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line.strip("[]")
            continue
        if section.endswith("dependencies") and "=" in line and not line.startswith("#"):
            name = line.split("=", 1)[0].strip().strip('"')
            if name:
                found.append((name, line.split("=", 1)[1].strip(), number))
    return found


_MANIFEST_PARSERS = {
    "requirements.txt": _parse_requirements,
    "requirements-dev.txt": _parse_requirements,
    "requirements_dev.txt": _parse_requirements,
    "dev-requirements.txt": _parse_requirements,
    "pyproject.toml": _parse_pyproject,
    "package.json": _parse_package_json,
    "go.mod": _parse_go_mod,
    "cargo.toml": _parse_cargo,
}


# --------------------------------------------------------------------------
# Detection
# --------------------------------------------------------------------------

def detect(
    repo: Path | str,
    *,
    tracked_files: set[str],
    touched_paths: set[str],
    external_imports: tuple[ExternalImport, ...] = (),
    rev: str = "HEAD",
) -> TechResult:
    """Collect every technology signal for the repository.

    Manifests and config files are read repo-wide -- a dependency list describes
    the project regardless of who edited it -- while import evidence comes only
    from files the person touched, so the two scopes stay distinguishable.
    """
    registry: dict[str, Technology] = {}
    usages: list[TechUsage] = []

    def register(name: str, category: str, alias: str = "") -> str:
        canon = canonical(name)
        existing = registry.get(canon)
        aliases = set(existing.aliases) if existing else set()
        if alias and alias != canon:
            aliases.add(alias)
        # A specific category always beats the "library" fallback.
        chosen = category
        if existing and existing.category != "library" and category == "library":
            chosen = existing.category
        registry[canon] = Technology(
            name=canon, category=chosen, aliases=tuple(sorted(aliases))
        )
        return canon

    # 1. Manifests -----------------------------------------------------
    for path in sorted(tracked_files):
        filename = PurePosixPath(path).name.casefold()
        parser = _MANIFEST_PARSERS.get(filename)
        if parser is None:
            continue
        blob = show_blob(repo, path, rev)
        if blob is None:
            continue
        text = blob.decode("utf-8", errors="replace")
        for name, spec, line in parser(text):
            canon = register(name, categorise(canonical(name)), alias=name)
            usages.append(
                TechUsage(
                    tech=canon,
                    signal="manifest",
                    source=path,
                    line=line,
                    detail=spec,
                    scope="repository",
                )
            )

    # 2. Config / infra files -----------------------------------------
    for path in sorted(tracked_files):
        filename = PurePosixPath(path).name.casefold()
        suffix = PurePosixPath(path).suffix.casefold()
        for needle, name, category in _CONFIG_SIGNALS:
            if filename == needle or (needle == "dockerfile" and filename.startswith("dockerfile")):
                canon = register(name, category)
                usages.append(
                    TechUsage(tech=canon, signal="config", source=path, line=None, scope="repository")
                )
        if suffix == ".tf":
            canon = register("terraform", "infra")
            usages.append(
                TechUsage(tech=canon, signal="config", source=path, line=None, scope="repository")
            )
        if path.startswith(".github/workflows/") and suffix in {".yml", ".yaml"}:
            canon = register("github-actions", "infra")
            usages.append(
                TechUsage(tech=canon, signal="config", source=path, line=None, scope="repository")
            )

    # 3. Imports in touched files --------------------------------------
    for imported in external_imports:
        if imported.path not in touched_paths:
            continue
        canon = register(imported.root, categorise(canonical(imported.root)), alias=imported.root)
        usages.append(
            TechUsage(
                tech=canon,
                signal="import",
                source=imported.path,
                line=imported.line,
                detail=imported.module,
                scope="file",
            )
        )

    # 4. Languages, from the extensions of touched files ---------------
    for path in sorted(touched_paths):
        language = language_for_path(path)
        if language == "unknown":
            continue
        canon = register(language, _LANGUAGE_CATEGORY)
        usages.append(
            TechUsage(tech=canon, signal="config", source=path, line=None, scope="file")
        )

    technologies = tuple(sorted(registry.values(), key=lambda t: t.name))
    usages.sort(key=lambda u: (u.tech, u.signal, u.source, u.line or 0))
    return TechResult(technologies=technologies, usages=tuple(usages))
