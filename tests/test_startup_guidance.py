import json
import logging

import pytest

from photos_mcp import logs


@pytest.mark.parametrize("role", ["photos", "photos-login", "photos-maintenance"])
def test_product_identity_preserves_the_native_process_role(role):
    data = json.loads(logs.JsonFormatter(role).format(logging.makeLogRecord({"msg": "synthetic"})))
    assert data["service"] == "photos-mcp" and data["service_"] == role


def test_http_refusal_names_missing_bearer(monkeypatch, capsys):
    from photos_mcp import server

    monkeypatch.setattr(logs, "setup", lambda *args, **kwargs: None)
    monkeypatch.delenv("MCP_BEARER_TOKEN", raising=False)
    with pytest.raises(SystemExit) as failure:
        server.main([])
    assert failure.value.code == 1
    data = json.loads(capsys.readouterr().err)
    assert "MCP_BEARER_TOKEN must be set" in data["msg"] and data["service"] == "photos-mcp"


def test_login_refusal_names_missing_token(monkeypatch, capsys):
    from photos_login import app

    monkeypatch.setattr(app, "setup", lambda *args, **kwargs: None)
    monkeypatch.delenv("PHOTOS_LOGIN_TOKEN", raising=False)
    with pytest.raises(SystemExit) as failure:
        app.main()
    assert failure.value.code == 1
    data = json.loads(capsys.readouterr().err)
    assert "PHOTOS_LOGIN_TOKEN" in data["msg"] and data["service_"] == "photos-login"
