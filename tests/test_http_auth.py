"""Bearer auth through the real HTTP app (same options main() serves with); /health is open."""

import pytest
from starlette.testclient import TestClient

import photos_mcp
from photos_mcp import server
from photos_mcp.auth import AuthError, bearer_auth_from_env

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}},
}
TOOLS = {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}


def _app(service):
    auth = bearer_auth_from_env({"MCP_BEARER_TOKEN": " proxy-test-token , client-test-token "})
    return server.create_server(service, auth=auth).http_app(**server.HTTP_OPTIONS)


def test_mcp_requires_a_bearer_and_health_is_open(service):
    headers = {"Accept": "application/json, text/event-stream"}
    with TestClient(_app(service)) as client:
        assert client.post("/mcp", headers=headers, json=INITIALIZE).status_code == 401
        wrong = {**headers, "Authorization": "Bearer wrong"}
        assert client.post("/mcp", headers=wrong, json=INITIALIZE).status_code == 401
        good = {**headers, "Authorization": "Bearer proxy-test-token"}
        response = client.post("/mcp", headers=good, json=INITIALIZE)
        assert response.status_code == 200, response.text
        assert "mcp-session-id" not in response.headers  # stateless
        assert response.json()["result"]["serverInfo"]["version"] == photos_mcp.__version__
        assert response.headers["content-type"].startswith("application/json")
        good["mcp-protocol-version"] = "2025-03-26"
        for token in ("proxy-test-token", "client-test-token"):
            listed = client.post("/mcp", json=TOOLS, headers={**good, "Authorization": f"Bearer {token}"})
            assert listed.status_code == 200
            assert "photos_search" in {t["name"] for t in listed.json()["result"]["tools"]}
        # Host headers of a reverse proxy's internal network are not rejected.
        assert client.post("/mcp", json=TOOLS, headers={**good, "Host": "photos-mcp:8000"}).status_code == 200
        health = client.get("/health")
        assert health.status_code == 200 and health.json() == {"status": "healthy"}


def test_serving_http_without_a_token_refuses(monkeypatch, capsys):
    for env in ({}, {"MCP_BEARER_TOKEN": " , "}):
        with pytest.raises(AuthError):
            bearer_auth_from_env(env)
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    monkeypatch.setenv("GOOGLE_PHOTOS_EXPECTED_EMAIL", "owner@example.com")
    with pytest.raises(SystemExit) as caught:
        server.main([])
    assert caught.value.code == 1 and "MCP_BEARER_TOKEN" in capsys.readouterr().err


def test_bad_configuration_exits_before_serving(monkeypatch, capsys):
    monkeypatch.setenv("MCP_BEARER_TOKEN", "t")
    monkeypatch.setenv("GOOGLE_PHOTOS_EXPECTED_EMAIL", "owner@example.com")
    monkeypatch.setenv("PHOTOS_BUDGET_TZ", "Mars/Olympus_Mons")
    for transport in ("http", "stdio"):
        with pytest.raises(SystemExit) as caught:
            server.main(["--transport", transport])
        assert caught.value.code == 1 and "PHOTOS_BUDGET_TZ" in capsys.readouterr().err


def test_main_requires_the_expected_email(monkeypatch):
    from photos_mcp.config import ConfigError, Settings

    with pytest.raises(ConfigError):
        Settings.from_env({})
    settings = Settings.from_env({"GOOGLE_PHOTOS_EXPECTED_EMAIL": " Owner@Example.com "})
    assert settings.expected_email == "owner@example.com"
    assert str(settings.session_dir) == "/session" and str(settings.cache_dir) == "/state/cache"
    assert (settings.daily_rpc_budget, settings.daily_thumb_budget) == (3000, 2000)
    assert settings.cache_max_bytes == 2 * 1024**3
    assert str(settings.budget_tz) == "UTC"
    zoned = Settings.from_env({"GOOGLE_PHOTOS_EXPECTED_EMAIL": "a@b", "PHOTOS_BUDGET_TZ": "Europe/Paris"})
    assert str(zoned.budget_tz) == "Europe/Paris"
    with pytest.raises(ConfigError):
        Settings.from_env({"GOOGLE_PHOTOS_EXPECTED_EMAIL": "a@b", "PHOTOS_DAILY_RPC_BUDGET": "lots"})
