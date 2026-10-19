"""The MCP server over a built graph.

These drive the tools through the real MCP client/server session rather than
calling the Python functions directly, so the registration, schemas and
serialisation are exercised too -- the parts that break without the Python
ever being wrong.
"""
from __future__ import annotations

import pytest

mcp = pytest.importorskip("mcp", reason="the mcp extra is not installed")

from fixture_repo import JEEVAN  # noqa: E402
from prism.export import write_json  # noqa: E402
from prism.mcp_server import build_server  # noqa: E402


@pytest.fixture(scope="module")
def graph_file(document, tmp_path_factory):
    path = tmp_path_factory.mktemp("mcp") / "graph.json"
    return write_json(document, path)


@pytest.fixture(scope="module")
def server(graph_file):
    return build_server(graph_file)


async def _call(server, tool: str, arguments: dict | None = None) -> str:
    """Dispatch a tool the way the protocol does, and return its text.

    Arguments go in a dict rather than as **kwargs because a tool is allowed
    its own parameter called ``name`` -- ``project_detail`` has one.
    """
    result = await server.call_tool(tool, arguments or {})
    return "\n".join(
        block.text for block in result.content if hasattr(block, "text")
    )


async def _tool_names(server) -> set[str]:
    return {t.name for t in await server.list_tools()}


# -- registration ---------------------------------------------------------

@pytest.mark.anyio
async def test_expected_tools_are_registered(server):
    names = await _tool_names(server)
    assert {
        "overview", "find_work", "list_technologies",
        "project_detail", "search", "read_source",
    } <= names


@pytest.mark.anyio
async def test_no_tool_can_write(server):
    """The server is read-only by construction; nothing mutating is exposed."""
    names = await _tool_names(server)
    forbidden = {"build", "write", "delete", "update", "set", "rebuild"}
    assert not any(any(f in n.lower() for f in forbidden) for n in names)


# -- orientation ----------------------------------------------------------

@pytest.mark.anyio
async def test_overview_states_the_limits(server):
    """A model answering from this must know what blame cannot see."""
    text = await _call(server, "overview")
    assert "Blame sees only surviving code" in text
    assert "Limits" in text


@pytest.mark.anyio
async def test_overview_is_compact(server):
    """The whole point is not spending 75k tokens; keep orientation cheap."""
    text = await _call(server, "overview")
    assert len(text) < 4000


# -- the headline query ---------------------------------------------------

@pytest.mark.anyio
async def test_find_work_returns_projects_with_citations(server):
    text = await _call(server, "find_work", {"technology": "fastapi"})
    assert "pulse-api" in text
    assert "app/auth.py:9-17" in text
    assert "login" in text


@pytest.mark.anyio
async def test_find_work_distinguishes_absent_from_empty(server):
    """'No evidence' must not read as 'nothing found, maybe try again'."""
    text = await _call(server, "find_work", {"technology": "kubernetes"})
    assert "No evidence" in text
    assert "not that they have never used it" in text
    # It should also say what IS known, so the model can redirect.
    assert "fastapi" in text


@pytest.mark.anyio
async def test_find_work_answer_is_small(server):
    """A typical answer should cost hundreds of tokens, not thousands."""
    text = await _call(server, "find_work", {"technology": "fastapi"})
    assert len(text) < 6000


# -- other tools ----------------------------------------------------------

@pytest.mark.anyio
async def test_list_technologies_shows_project_breadth(server):
    text = await _call(server, "list_technologies")
    assert "fastapi" in text
    assert "framework" in text


@pytest.mark.anyio
async def test_unknown_category_lists_the_real_ones(server):
    text = await _call(server, "list_technologies", {"category": "nonsense"})
    assert "Known:" in text


@pytest.mark.anyio
async def test_project_detail(server):
    text = await _call(server, "project_detail", {"name": "pulse-api"})
    assert "pulse-api" in text
    assert "Symbols authored" in text
    assert "Add JWT login endpoint" in text


@pytest.mark.anyio
async def test_unknown_project_lists_the_real_ones(server):
    text = await _call(server, "project_detail", {"name": "nope"})
    assert "No project named" in text
    assert "pulse-api" in text


@pytest.mark.anyio
async def test_search_finds_a_symbol(server):
    text = await _call(server, "search", {"query": "issue_token"})
    assert "issue_token" in text
    assert "app/tokens.py" in text


@pytest.mark.anyio
async def test_search_rejects_a_too_short_query(server):
    text = await _call(server, "search", {"query": "a"})
    assert "at least 2" in text


# -- read_source and its allowlist ----------------------------------------

@pytest.mark.anyio
async def test_read_source_returns_the_actual_code(server):
    text = await _call(server, "read_source", {
        "project": "pulse-api", "path": "app/auth.py",
        "start_line": 9, "end_line": 17,
    })
    assert "def login(" in text
    assert "issue_token(email)" in text
    assert "app/auth.py:9-17" in text


@pytest.mark.anyio
async def test_read_source_refuses_a_path_outside_the_graph(server):
    """The allowlist is the path-traversal guard: untrusted input is never
    joined onto a repository root, only matched against known File nodes."""
    for attempt in ("../../../etc/passwd", "/etc/passwd", "app/../../secret.py"):
        text = await _call(
            server, "read_source", {"project": "pulse-api", "path": attempt}
        )
        assert "not a file recorded" in text, attempt


@pytest.mark.anyio
async def test_read_source_refuses_an_unknown_project(server):
    text = await _call(
        server, "read_source", {"project": "elsewhere", "path": "app/auth.py"}
    )
    assert "No project named" in text
