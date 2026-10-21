"""The MCP layer in process: tool surface, annotations, compact JSON, image blocks, typed
errors, and no exception text in tool output."""

import asyncio
import base64
import json

import pytest
from conftest import load
from fastmcp import Client
from fastmcp.exceptions import ToolError

from photos_mcp.errors import NEXT_STEP, PhotosError
from photos_mcp.server import INSTRUCTIONS, create_server

TOOLS = {
    "photos_search",
    "photos_list",
    "photos_albums",
    "photos_get",
    "photos_contact_sheet",
    "photos_view",
    "photos_status",
    "photos_canary",
}


def run(coro):
    return asyncio.run(coro)


async def _list(server):
    async with Client(server) as client:
        return await client.list_tools()


async def _call(server, name, args, raise_on_error=True):
    async with Client(server) as client:
        return await client.call_tool(name, args, raise_on_error=raise_on_error)


def test_tool_surface_and_annotations(service):
    tools = run(_list(create_server(service)))
    assert {tool.name for tool in tools} == TOOLS
    for tool in tools:
        assert tool.annotations.read_only_hint is True, tool.name
        assert tool.annotations.destructive_hint is False, tool.name
        assert tool.annotations.open_world_hint is (tool.name != "photos_status"), tool.name


def test_instructions_cover_flow_and_every_error_code():
    assert "photos_search -> photos_contact_sheet -> photos_view" in INSTRUCTIONS
    assert "read" in INSTRUCTIONS.lower()
    for code in NEXT_STEP:
        assert code in INSTRUCTIONS


def test_search_returns_compact_json(service):
    result = run(_call(create_server(service), "photos_search", {"query": "beach", "limit": 2}))
    text = result.content[0].text
    body = json.loads(text)
    assert len(body["items"]) == 2 and body["coverage"]["shared_partner"] == "not_covered"
    assert text == json.dumps(body, separators=(",", ":"), ensure_ascii=False)


def test_view_returns_label_then_jpeg(service):
    key = load("VrseUb")[0][0]
    result = run(_call(create_server(service), "photos_view", {"ids": [key], "max_px": 256}))
    label, image = result.content
    assert json.loads(label.text)["video_poster"] is True
    assert image.type == "image" and image.mime_type == "image/jpeg"
    assert base64.b64decode(image.data)[:3] == b"\xff\xd8\xff"


def test_contact_sheet_returns_legend_then_jpeg(service):
    keys = [r[0] for r in load("lcxiM")[0]]
    result = run(_call(create_server(service), "photos_contact_sheet", {"ids": keys}))
    legend, image = result.content
    assert [e["n"] for e in json.loads(legend.text)["legend"]] == [1, 2, 3, 4]
    assert image.mime_type == "image/jpeg"


def test_status_tool(service):
    result = run(_call(create_server(service), "photos_status", {}))
    body = json.loads(result.content[0].text)
    assert body["session"]["state"] == "ok" and body["read_only"] is True


def test_typed_error_payload(service, transport):
    transport.responses["EzkLib"] = PhotosError("blocked", log_detail="secret detail")
    with pytest.raises(ToolError) as caught:
        run(_call(create_server(service), "photos_search", {"query": "dog"}))
    error = json.loads(str(caught.value))["error"]
    assert error == {"code": "blocked", "next_step": NEXT_STEP["blocked"], "retryable": False}
    assert "secret detail" not in str(caught.value)
    assert service.status()["last_error"]["code"] == "blocked"


def test_unexpected_exception_text_never_reaches_the_model(service, transport):
    def boom(payload):
        raise RuntimeError("cookie SID=fake-sid-value leaked")

    transport.responses["EzkLib"] = boom
    result = run(_call(create_server(service), "photos_search", {"query": "dog"}, raise_on_error=False))
    assert result.is_error
    text = " ".join(getattr(block, "text", "") for block in result.content)
    assert "fake-sid-value" not in text and "RuntimeError" not in text
    assert json.loads(text)["error"]["code"] == "internal_error"


def test_input_validation_is_enforced_by_the_schema(service, transport):
    server = create_server(service)
    for name, args in (
        ("photos_search", {"query": "x" * 201}),
        ("photos_search", {"query": "dog", "limit": 51}),
        ("photos_view", {"ids": ["a"] * 5}),
        ("photos_view", {"ids": ["AF1QipFAKE01"], "max_px": 1569}),
        ("photos_contact_sheet", {"ids": ["AF1QipFAKE01"] * 25}),
        ("photos_get", {"ids": []}),
        ("photos_list", {"source": "trash"}),
    ):
        result = run(_call(server, name, args, raise_on_error=False))
        assert result.is_error, (name, args)
        text = " ".join(getattr(block, "text", "") for block in result.content)
        error = json.loads(text)["error"]  # the typed format, not pydantic's raw text
        assert error["code"] == "invalid_argument" and error["retryable"] is False, (name, text)
        assert "pydantic" not in text.lower() and "x" * 50 not in text
    assert transport.calls == []
    assert service.status()["last_error"] is None  # agent mistakes are not last_error


def test_validation_error_names_the_argument(service):
    result = run(_call(create_server(service), "photos_search", {"query": "dog", "limit": 51}, raise_on_error=False))
    error = json.loads(result.content[0].text)["error"]
    assert error["next_step"].startswith("limit: ") and error["next_step"].endswith(NEXT_STEP["invalid_argument"])


def test_canary_tool(service, transport):
    result = run(_call(create_server(service), "photos_canary", {}))
    body = json.loads(result.content[0].text)
    assert set(body) == {"ok", "code", "checked_at", "cached", "snapshot_age_s", "generation"}
    assert body["generation"] == 7 and body["cached"] is False
    assert transport.rpcids[0] == ["lcxiM"] and len(transport.gets) == 1


def test_instructions_state_image_limits_and_archive_rules():
    assert "1568" in INSTRUCTIONS and "originals" in INSTRUCTIONS
    assert "returned JPEG" in INSTRUCTIONS
    assert "source=library" in INSTRUCTIONS and "archived" in INSTRUCTIONS
    assert "photos_canary" in INSTRUCTIONS


def test_tool_errors_log_one_line_at_a_level_that_fits(service, transport, caplog):
    """An agent's own mistake is routine (INFO), a Google-side failure a WARNING; neither
    is an ERROR with a traceback (FastMCP's and ToolCallLog's lines follow the ToolError's
    log_level)."""
    import logging

    caplog.set_level(logging.INFO)
    server = create_server(service)
    transport.responses["EzkLib"] = PhotosError("blocked")
    run(_call(server, "photos_search", {"query": "dog", "limit": 51}, raise_on_error=False))
    run(_call(server, "photos_search", {"query": "dog", "cursor": "garbage"}, raise_on_error=False))
    run(_call(server, "photos_search", {"query": "dog"}, raise_on_error=False))
    tool_lines = [r for r in caplog.records if r.getMessage() in ("tool error", "Error calling tool 'photos_search'")]
    assert tool_lines and all(not r.exc_info for r in tool_lines), [(r.name, r.levelno, r.exc_info) for r in tool_lines]
    assert all(r.levelno <= logging.WARNING for r in tool_lines)
    assert any(r.levelno == logging.INFO for r in tool_lines)  # the two agent mistakes
