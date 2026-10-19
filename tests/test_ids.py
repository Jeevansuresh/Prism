"""The ID normalization guarantees.

These are the properties the vendored graphify recipe exists to provide. They
look pedantic until two producers disagree on an ID and one entity silently
becomes two disconnected nodes.
"""
from __future__ import annotations

import re

import pytest

from prism.ids import make_id, normalize_id
from prism.schema import commit_id, file_id, person_id, symbol_id, tech_id

SAMPLES = [
    "simple",
    "Mixed Case",
    "with-dashes_and_underscores",
    "dots.in.the.name",
    "Islem Yap",
    "İslemYap",          # Turkish dotted capital I
    "café",
    "中文模块",  # CJK
    "служба",  # Cyrillic
    "trailing___",
    "!!!",
    "",
]


@pytest.mark.parametrize("value", SAMPLES)
def test_idempotent(value):
    once = normalize_id(value)
    assert normalize_id(once) == once


@pytest.mark.parametrize("value", SAMPLES)
def test_caseless_stable(value):
    assert normalize_id(value) == normalize_id(value.casefold())


@pytest.mark.parametrize("value", SAMPLES)
def test_only_word_characters_survive(value):
    assert re.fullmatch(r"[\w]*", normalize_id(value), flags=re.UNICODE)


def test_non_latin_scripts_are_not_collapsed():
    """A CJK identifier must not normalise to the empty string."""
    assert normalize_id("中文模块") != ""


def test_turkish_dotted_capital_is_stable():
    # The case graphify's fixpoint loop exists for.
    assert normalize_id("İslemYap") == normalize_id("İslemYap".casefold())


def test_make_id_joins_parts():
    assert make_id("app", "auth", "login") == "app_auth_login"
    assert make_id("app", "", "login") == "app_login"


# -- schema IDs ------------------------------------------------------------

def test_person_id_is_case_insensitive():
    assert person_id("A@B.com") == person_id("a@b.com")


def test_file_id_keeps_the_path_readable():
    assert file_id("pulse-api", "app/auth.py") == "file:pulse_api:app/auth.py"


def test_file_id_normalises_separators():
    assert file_id("x", "app\\auth.py") == file_id("x", "app/auth.py")


def test_symbol_id_extends_the_file_id():
    fid = file_id("x", "app/auth.py")
    sid = symbol_id("x", "app/auth.py", "function", "login")
    assert sid.startswith(fid + "#")
    assert sid.endswith("#function:login")


def test_symbol_ids_distinguish_kind_and_qualification():
    a = symbol_id("x", "f.py", "function", "run")
    b = symbol_id("x", "f.py", "method", "run")
    c = symbol_id("x", "f.py", "method", "Cls.run")
    assert len({a, b, c}) == 3


def test_commit_id_is_lowercased():
    assert commit_id("ABC123") == "commit:abc123"


def test_tech_id_folds_case_and_punctuation():
    assert tech_id("FastAPI") == tech_id("fastapi")
    assert tech_id("discord.py") == "tech:discord_py"
