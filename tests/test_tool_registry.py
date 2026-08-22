"""The MCP tool surface is the project's public contract — assert its shape."""

import pytest

from daisy import mcp_server


def _handler_names() -> set[str]:
    """Tool names the call_tool dispatcher knows how to route."""
    src = (mcp_server.__file__ or "")
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    start = text.index("    handlers = {")
    end = text.index("    }", start)
    block = text[start:end]
    return {
        line.split('"')[1]
        for line in block.splitlines()
        if line.strip().startswith('"daisy_')
    }


def test_expected_tools_are_registered():
    assert {t.name for t in mcp_server.TOOLS} == {
        "daisy_projects",
        "daisy_new_project",
        "daisy_switch_project",
        "daisy_archive_project",
        "daisy_intake",
        "daisy_search",
        "daisy_fetch",
        "daisy_extract",
        "daisy_add_fact",
        "daisy_facts",
        "daisy_confirm",
        "daisy_report",
        "daisy_sources",
        "daisy_fetch_summary",
        "daisy_fetch_log",
        "daisy_build_graph",
        "daisy_graph",
    }


def test_tool_names_are_unique():
    names = [t.name for t in mcp_server.TOOLS]
    assert len(names) == len(set(names))


def test_every_tool_has_a_handler():
    handlers = _handler_names()
    declared = {t.name for t in mcp_server.TOOLS}
    declared.add(mcp_server._CRAWL4AI_FETCH_CLEAN_TOOL.name)
    assert declared - handlers == set()


@pytest.mark.parametrize(
    "tool",
    mcp_server.TOOLS + [mcp_server._CRAWL4AI_FETCH_CLEAN_TOOL],
    ids=lambda t: t.name,
)
def test_tool_schema_is_well_formed(tool):
    schema = tool.inputSchema
    assert schema["type"] == "object"
    assert tool.description.strip()
    # Every required field must actually be declared in properties.
    assert set(schema.get("required", [])) <= set(schema["properties"])


def test_fetch_clean_is_conditional_on_crawl4ai():
    """daisy_fetch_clean is only advertised when Crawl4AI is configured."""
    assert mcp_server._CRAWL4AI_FETCH_CLEAN_TOOL not in mcp_server.TOOLS


async def test_list_tools_respects_crawl4ai_flag(monkeypatch):
    monkeypatch.setattr(mcp_server, "_crawl4ai_available", False)
    assert "daisy_fetch_clean" not in {t.name for t in await mcp_server.list_tools()}

    monkeypatch.setattr(mcp_server, "_crawl4ai_available", True)
    assert "daisy_fetch_clean" in {t.name for t in await mcp_server.list_tools()}


async def test_unknown_tool_is_reported_not_raised():
    result = await mcp_server.call_tool("daisy_not_a_tool", {})
    assert "Unknown tool" in result[0].text


async def test_methodology_resource_is_served():
    resources = await mcp_server.list_resources()
    assert [str(r.uri) for r in resources] == ["daisy://methodology"]
    assert await mcp_server.read_resource("daisy://methodology")
