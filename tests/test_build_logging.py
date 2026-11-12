"""Retained diagnostics identify the process without weakening sink privacy or channels."""

import json
import logging
import subprocess
import sys
import textwrap
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from photos_mcp import logs
from photos_mcp.build_identity import log_context


def record(**fields):
    result = logging.LogRecord("photos.synthetic", logging.WARNING, "", 0, "safe event", (), None)
    result.__dict__.update(fields)
    return result


def identity(payload):
    assert {key: payload[key] for key in log_context()} == log_context()
    assert payload["level"] == "WARNING"
    assert payload["component"] == "photos.synthetic"


def test_default_formatter_preserves_flat_typed_fields_and_all_collisions():
    fields = {
        "count": 3,
        "nested": {"ready": True, "items": [1, None]},
        "version": {"custom": True},
        "version_": 7,
        "service": False,
        "component": [2],
        "level": 5,
        "source_revision": ["caller"],
        "instance": 99,
        "msg": ["custom"],
        "msg_": {"another": True},
        "ts": 11,
    }
    payload = json.loads(logs.JsonFormatter().format(record(extras=fields)))
    identity(payload)
    assert payload["count"] == 3 and payload["nested"] == fields["nested"]
    assert payload["version_"] == 7 and payload["version__"] == {"custom": True}
    assert payload["service_"] is False and payload["level_"] == 5
    assert payload["source_revision_"] == ["caller"] and payload["instance_"] == 99
    assert payload["component_"] == [2]
    assert payload["ts"] == 11 and isinstance(payload["ts_"], str)
    assert sorted([payload["msg"], payload["msg_"], payload["msg__"]], key=str) == sorted(
        ["safe event", ["custom"], {"another": True}], key=str
    )


@pytest.mark.parametrize("structured", [False, True])
def test_original_formatter_is_authoritative_redaction(structured):
    class Redacting(logging.Formatter):
        def format(self, record):
            if structured:
                return json.dumps({"msg": "redacted", "count": 4, "nested": [False, None], "instance": {"old": 1}})
            return "redacted"

    event = record(extras={"secret": "SENTINEL"})
    event.msg = "SENTINEL"
    event.exc_info = (ValueError, ValueError("SENTINEL"), None)
    rendered = logs.JsonFormatter(original=Redacting()).format(event)
    assert "SENTINEL" not in rendered
    payload = json.loads(rendered)
    identity(payload)
    assert payload["msg"] == "redacted"
    if structured:
        assert payload["count"] == 4 and payload["nested"] == [False, None]
        assert payload["instance_"] == {"old": 1}


def test_setup_preserves_sinks_filters_levels_rich_quiet_and_last_resort():
    script = """
        import io, json, logging, os
        from rich.console import Console
        from rich.logging import RichHandler
        from photos_mcp import logs
        from photos_mcp.build_identity import log_context
        os.environ['LOG_LEVEL'] = 'DEBUG'
        root = logging.getLogger()
        root.handlers.clear()
        root_stream = io.StringIO()
        root_handler = logging.StreamHandler(root_stream)
        root_handler.setLevel(logging.ERROR)
        root.addHandler(root_handler)
        names = ['asyncio', 'photos', 'photos_login', 'photos-login', 'photos_maintenance',
                 'signin_window', 'mcp', 'fastmcp', 'uvicorn']
        streams = []
        for name in names:
            stream = io.StringIO()
            logger = logging.getLogger(name + '.synthetic')
            logger.handlers.clear()
            logger.propagate = False
            logger.setLevel(logging.DEBUG)
            handler = logging.StreamHandler(stream)
            handler.setLevel(logging.WARNING)
            handler.addFilter(lambda rec: rec.msg != 'filtered')
            handler.setFormatter(logging.Formatter('redacted %(levelname)s'))
            logger.addHandler(handler)
            streams.append((logger, stream, handler))
        rich_stream = io.StringIO()
        console = Console(file=rich_stream, quiet=True)
        rich_logger = logging.getLogger('fastmcp.rich')
        rich_logger.handlers.clear()
        rich_logger.propagate = False
        rich_logger.setLevel(logging.DEBUG)
        rich = RichHandler(console=console, show_time=False, show_level=False, show_path=False)
        rich.setLevel(logging.WARNING)
        rich.addFilter(lambda rec: rec.msg != 'filtered')
        rich.setFormatter(logging.Formatter('redacted %(levelname)s'))
        rich_logger.addHandler(rich)
        fallback = io.StringIO()
        logging.lastResort = logging.StreamHandler(fallback)
        logs.setup()
        logs.setup()
        for logger, stream, handler in streams:
            assert logger.handlers == [handler]
            logger.info('SENTINEL')
            logger.warning('filtered')
            logger.warning('SENTINEL', extra={'secret': 'SENTINEL'})
            rows = [json.loads(line) for line in stream.getvalue().splitlines()]
            assert len(rows) == 1 and rows[0]['msg'] == 'redacted WARNING'
            assert {key: rows[0][key] for key in log_context()} == log_context()
            assert 'SENTINEL' not in stream.getvalue()
        rich_logger.warning('SENTINEL')
        assert not rich_stream.getvalue()
        console.quiet = False
        rich_logger.info('SENTINEL')
        rich_logger.warning('filtered')
        rich_logger.warning('SENTINEL')
        rows = [json.loads(line) for line in rich_stream.getvalue().splitlines()]
        assert len(rows) == 1 and rows[0]['msg'].strip() == 'redacted WARNING'
        console.quiet = True
        rich_logger.error('SENTINEL')
        assert len(rich_stream.getvalue().splitlines()) == 1
        logging.warning('not retained')
        logging.error('retained')
        assert len(root_stream.getvalue().splitlines()) == 1
        assert json.loads(root_stream.getvalue())['msg'] == 'retained'
        orphan = logging.getLogger('photos.orphan')
        orphan.propagate = False
        orphan.warning('fallback')
        assert json.loads(fallback.getvalue())['instance'] == log_context()['instance']
        for name in ('uvicorn.access', 'mcp.server.lowlevel.server', 'httpx', 'httpx2', 'httpcore', 'httpcore2'):
            assert logging.getLogger(name).getEffectiveLevel() >= logging.WARNING
    """
    result = subprocess.run([sys.executable, "-c", textwrap.dedent(script)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not result.stdout


@pytest.mark.parametrize("arguments", [["--port", "SENTINEL"], ["--port", "999999"], ["--SENTINEL"]])
def test_server_bad_arguments_keep_stdout_clean_and_hide_values(arguments, monkeypatch, capsys):
    from photos_mcp import server

    monkeypatch.setattr(logs, "setup", lambda *args, **kwargs: None)
    with pytest.raises(SystemExit) as caught:
        server.main(arguments)
    captured = capsys.readouterr()
    assert caught.value.code == 2 and not captured.out
    assert "SENTINEL" not in captured.err
    payload = json.loads(captured.err)
    assert payload["instance"] == log_context()["instance"] and payload["level"] == "ERROR"


@pytest.mark.parametrize("service", ["reader", "login", "recovery"])
def test_startup_configuration_failures_are_safe_and_identified(service, monkeypatch, capsys):
    from photos_login import app as login
    from photos_maintenance import human
    from photos_mcp import server
    from photos_mcp.config import ConfigError

    monkeypatch.setattr(logs, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(login, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(human, "setup", lambda *args, **kwargs: None)
    if service == "reader":

        def fail(*args, **kwargs):
            raise ConfigError("SENTINEL")

        monkeypatch.setattr(server, "_main", fail)
        command = server.main
    elif service == "login":
        monkeypatch.setenv("PHOTOS_LOGIN_TOKEN", "x" * 32)
        monkeypatch.setenv("PORT", "SENTINEL")
        command = login.main
    else:
        monkeypatch.setenv("PHOTOS_DEFAULT_LOGIN_PORT", "SENTINEL")

        def command():
            return human.main([])

    with pytest.raises(SystemExit) as caught:
        command()
    captured = capsys.readouterr()
    assert caught.value.code == 1 and not captured.out and "SENTINEL" not in captured.err
    payload = json.loads(captured.err)
    assert payload["instance"] == log_context()["instance"] and payload["level"] == "ERROR"


def test_http_runner_preserves_logging_and_disables_url_access_logs(monkeypatch):
    from photos_mcp import deployment, server

    calls = []
    monkeypatch.setattr(server.logs, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "bearer_auth_from_env", lambda: object())
    monkeypatch.setattr(server.Settings, "from_env", lambda: object())
    monkeypatch.setattr(deployment, "Gate", lambda *args: SimpleNamespace(start=lambda: object()))
    monkeypatch.setattr(
        server, "create_server", lambda **kwargs: SimpleNamespace(run=lambda **kwargs: calls.append(kwargs))
    )
    server.main([])
    assert calls[0]["uvicorn_config"] == {"access_log": False, "log_config": None}
    assert calls[0]["show_banner"] is False


def test_recovery_stdout_remains_a_command_result(monkeypatch, capsys):
    from photos_maintenance import human
    from photos_mcp import deployment

    monkeypatch.setattr(human, "setup", lambda *args, **kwargs: None)
    monkeypatch.setattr(human.Config, "from_env", lambda: SimpleNamespace(root="synthetic"))
    gate = SimpleNamespace(start=lambda: gate, work=nullcontext)
    monkeypatch.setattr(deployment, "Gate", lambda *args: gate)
    monkeypatch.setattr(human, "recover", lambda config: True)
    monkeypatch.delenv("PHOTOS_DEFAULT_LOGIN_PORT", raising=False)
    human.main(["--recover-only"])
    captured = capsys.readouterr()
    assert json.loads(captured.out) == {"status": "recovered"} and not captured.err


@pytest.mark.parametrize("kind", ["login", "recovery"])
def test_login_http_runners_keep_framework_sinks_and_access_urls_private(kind, monkeypatch):
    import threading

    import uvicorn

    from photos_login import app as login
    from photos_maintenance import human
    from photos_mcp import deployment

    calls = []
    application = SimpleNamespace(add_middleware=lambda *args, **kwargs: None)
    gate = SimpleNamespace(start=lambda: gate)
    monkeypatch.setattr(deployment, "Gate", lambda *args: gate)
    monkeypatch.setattr(deployment, "guarded_windows", lambda *args: None)
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setenv("PHOTOS_LOGIN_TOKEN", "x" * 32)
    if kind == "login":
        monkeypatch.setattr(login, "setup", lambda *args, **kwargs: None)
        monkeypatch.setattr(login, "policy_installed", lambda: True)
        monkeypatch.setattr(login, "Windows", lambda *args, **kwargs: object())
        monkeypatch.setattr(login, "Keeper", lambda *args, **kwargs: SimpleNamespace(run=None))
        monkeypatch.setattr(threading, "Thread", lambda *args, **kwargs: SimpleNamespace(start=lambda: None))
        monkeypatch.setattr(login, "build_app", lambda *args, **kwargs: application)
        monkeypatch.setenv("PORT", "8001")
        login.main()
    else:
        monkeypatch.setattr(human, "setup", lambda *args, **kwargs: None)
        monkeypatch.setattr(human.Config, "from_env", lambda: SimpleNamespace(root="synthetic"))
        monkeypatch.setattr(human, "build_app", lambda *args, **kwargs: application)
        monkeypatch.setenv("PHOTOS_DEFAULT_LOGIN_PORT", "8001")
        human.main([])
    assert calls == [{"host": "0.0.0.0", "port": 8001, "log_level": "warning", "log_config": None, "access_log": False}]
