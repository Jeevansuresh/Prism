"""M5 -- technology detection from manifests, imports and config."""
from __future__ import annotations

import pytest

from prism.gitio import ls_files
from prism.tech import canonical, categorise, detect


@pytest.fixture(scope="module")
def technologies(repo, touched, parsed):
    return detect(
        repo,
        tracked_files=ls_files(repo),
        touched_paths={f.path for f in touched},
        external_imports=parsed.external_imports,
    )


def _usages(technologies, name, signal=None):
    return [
        u for u in technologies.usages
        if u.tech == name and (signal is None or u.signal == signal)
    ]


def test_manifest_dependencies_are_detected(technologies):
    names = {t.name for t in technologies.technologies}
    assert {"fastapi", "pytest", "python-dotenv"} <= names


def test_manifest_usage_cites_the_requirements_line(technologies):
    usage = _usages(technologies, "fastapi", "manifest")[0]
    assert usage.source == "requirements.txt"
    assert usage.line == 1
    assert usage.detail == "==0.115.0"


def test_import_usage_cites_the_import_line(technologies):
    usage = _usages(technologies, "fastapi", "import")[0]
    assert usage.source == "app/auth.py"
    assert usage.line == 1


def test_one_technology_two_independent_signals(technologies):
    """fastapi is declared *and* imported; both facts survive as evidence."""
    signals = {u.signal for u in _usages(technologies, "fastapi")}
    assert signals == {"manifest", "import"}
    # ...but it is still a single node.
    assert len([t for t in technologies.technologies if t.name == "fastapi"]) == 1


def test_category_is_assigned(technologies):
    assert technologies.by_name("fastapi").category == "framework"
    assert technologies.by_name("pytest").category == "tool"
    assert technologies.by_name("python").category == "language"


def test_unknown_dependency_falls_back_to_library():
    # The honest default: we know it is a dependency, not what kind.
    assert categorise("some-unknown-package") == "library"


def test_import_root_is_canonicalised():
    assert canonical("discord") == "discord.py"
    assert canonical("dotenv") == "python-dotenv"
    assert canonical("pydantic_settings") == "pydantic-settings"


def test_canonicalisation_merges_alias_and_manifest_name(technologies):
    """python-dotenv is declared under that name; nothing else may duplicate it."""
    names = [t.name for t in technologies.technologies]
    assert names.count("python-dotenv") == 1
    assert "dotenv" not in names


def test_languages_are_technologies(technologies):
    names = {t.name for t in technologies.technologies}
    assert "python" in names
    assert "javascript" in names


def test_stdlib_is_not_a_technology(technologies):
    names = {t.name for t in technologies.technologies}
    assert "hashlib" not in names


def test_technologies_are_sorted(technologies):
    names = [t.name for t in technologies.technologies]
    assert names == sorted(names)


def test_usages_only_count_imports_from_touched_files(repo, parsed):
    """An import in a file the person never touched is not their evidence."""
    result = detect(
        repo,
        tracked_files=ls_files(repo),
        touched_paths=set(),          # nothing touched
        external_imports=parsed.external_imports,
    )
    assert _usages(result, "fastapi", "import") == []
    # The manifest signal is repo-wide, so it survives.
    assert _usages(result, "fastapi", "manifest") != []
