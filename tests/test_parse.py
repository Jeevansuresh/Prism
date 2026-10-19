"""M3 -- symbols, spans, calls and imports."""
from __future__ import annotations

import pytest

from prism.languages import available_languages, language_for_path


def _symbol(parsed, path, qualified):
    return next(
        (s for s in parsed.symbols if s.path == path and s.qualified_name == qualified),
        None,
    )


# -- symbols --------------------------------------------------------------

def test_python_functions_are_found_with_exact_spans(parsed):
    issue = _symbol(parsed, "app/tokens.py", "issue_token")
    assert issue is not None
    assert issue.kind == "function"
    assert (issue.start_line, issue.end_line) == (4, 6)


def test_login_span_is_exact(parsed):
    login = _symbol(parsed, "app/auth.py", "login")
    assert login is not None
    # The def line through the final return, decorator excluded.
    assert (login.start_line, login.end_line) == (9, 17)


def test_decorators_are_captured(parsed):
    login = _symbol(parsed, "app/auth.py", "login")
    assert login.decorators == ('@router.post("/login")',)


def test_signature_is_recorded(parsed):
    login = _symbol(parsed, "app/auth.py", "login")
    assert login.signature == "login(email: str, password: str)"


def test_every_symbol_has_a_sane_span(parsed):
    for symbol in parsed.symbols:
        assert symbol.start_line >= 1
        assert symbol.end_line >= symbol.start_line


@pytest.mark.skipif(
    "javascript" not in available_languages(), reason="javascript grammar not installed"
)
def test_javascript_class_and_method_are_found(parsed):
    client = _symbol(parsed, "web/client.js", "AuthClient")
    assert client is not None and client.kind == "class"
    login = _symbol(parsed, "web/client.js", "AuthClient.login")
    assert login is not None and login.kind == "method"
    assert login.parent == "AuthClient"


@pytest.mark.skipif(
    "javascript" not in available_languages(), reason="javascript grammar not installed"
)
def test_arrow_function_bound_to_a_name_becomes_a_symbol(parsed):
    assert _symbol(parsed, "web/client.js", "buildStore") is not None


def test_language_detection(parsed):
    assert language_for_path("app/auth.py") == "python"
    assert language_for_path("web/client.js") == "javascript"
    assert language_for_path("requirements.txt") == "unknown"


# -- calls ----------------------------------------------------------------

def _call(parsed, caller_name, callee_name):
    return next(
        (
            c for c in parsed.calls
            if c.caller[2] == caller_name and c.callee[2] == callee_name
        ),
        None,
    )


def test_cross_file_call_resolves_through_the_import(parsed):
    call = _call(parsed, "login", "issue_token")
    assert call is not None
    assert call.resolution == "import_resolved"
    assert call.confidence == "EXTRACTED"
    assert call.line == 16


def test_builtin_calls_are_not_edges(parsed):
    callees = {c.callee[2] for c in parsed.calls}
    assert "len" not in callees
    assert "print" not in callees


def test_unresolved_calls_are_counted_not_invented(parsed):
    # HTTPException(...) is imported from fastapi, so there is no symbol for
    # it in this repository and no edge may be fabricated.
    callees = {c.callee[2] for c in parsed.calls}
    assert "HTTPException" not in callees
    assert parsed.unresolved_calls > 0


def test_module_level_calls_are_counted_separately(parsed):
    # router = APIRouter(...) sits outside any function.
    assert parsed.module_level_calls >= 1


def test_every_call_has_a_known_resolution_tier(parsed):
    allowed = {"same_file", "import_resolved", "cross_file_unique"}
    assert all(c.resolution in allowed for c in parsed.calls)


# -- imports --------------------------------------------------------------

def test_internal_import_resolves_to_a_repo_file(parsed):
    internal = next(
        (i for i in parsed.internal_imports if i.path == "app/auth.py"), None
    )
    assert internal is not None
    assert internal.target_path == "app/tokens.py"
    assert internal.module == "app.tokens"
    assert internal.line == 3


def test_external_import_is_recorded_with_its_root(parsed):
    external = next(
        (i for i in parsed.external_imports if i.module == "fastapi"), None
    )
    assert external is not None
    assert external.root == "fastapi"
    assert external.path == "app/auth.py"
    assert external.line == 1


def test_stdlib_imports_are_not_technologies(parsed):
    # app/tokens.py imports hashlib; it must not become a dependency.
    roots = {i.root for i in parsed.external_imports}
    assert "hashlib" not in roots


@pytest.mark.skipif(
    "javascript" not in available_languages(), reason="javascript grammar not installed"
)
def test_javascript_package_import_is_external(parsed):
    roots = {i.root for i in parsed.external_imports}
    assert "redux" in roots


def test_nothing_was_skipped_in_the_fixture(parsed):
    assert parsed.skipped == {}
