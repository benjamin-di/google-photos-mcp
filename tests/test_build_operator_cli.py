"""Operator diagnostics retain artifact identity without retaining private inputs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRIVATE = "fake-private-cookie-token"


def run_cli(source: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=ROOT,
        env={**os.environ, "LOG_LEVEL": "CRITICAL"},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )


def diagnostic(result: subprocess.CompletedProcess[str], code: int) -> dict:
    assert result.returncode == code, result.stderr
    assert result.stdout == ""
    assert PRIVATE not in result.stderr
    records = [json.loads(line) for line in result.stderr.splitlines()]
    assert len(records) == 1
    record = records[0]
    assert record["service"] == "photos-mcp"
    assert record["version"]
    assert record["source_revision"]
    assert record["instance"]
    assert record["msg"]
    return record


@pytest.mark.parametrize(
    "source",
    [
        f"from photos_mcp.admin import main; main(['account', 'add', '{PRIVATE}'])",
        f"from photos_mcp.session_export import main; main(['browser', '{PRIVATE}'])",
        f"from photos_mcp.capture import main; main(['fake-output', '--max-items', '{PRIVATE}'])",
        f"import sys; sys.argv = ['deployment', 'reader', '{PRIVATE}']; from photos_mcp.deployment import main; main()",
    ],
)
def test_argument_failures_are_attributed_without_echoing_private_values(source):
    assert "--help" in diagnostic(run_cli(source), 2)["msg"]


@pytest.mark.parametrize(
    "module,invocation",
    [
        ("admin", "main(['--help'])"),
        ("session_export", "main(['--help'])"),
        ("capture", "main(['--help'])"),
        ("deployment", "main()"),
    ],
)
def test_help_remains_on_stdout(module, invocation):
    result = run_cli(f"import sys; sys.argv = ['cli', '--help']; from photos_mcp.{module} import main; {invocation}")
    assert result.returncode == 0
    assert "usage:" in result.stdout
    assert result.stderr == ""


@pytest.mark.parametrize(
    "source,code,message",
    [
        (
            "from photos_mcp import admin\n"
            f"def fail(): raise admin.ConfigError('{PRIVATE}')\n"
            "admin.Settings.from_env = fail\n"
            "raise SystemExit(admin.main(['account', 'list']))",
            1,
            "configuration invalid",
        ),
        (
            "from photos_mcp import admin\n"
            "admin.Settings.from_env = lambda: None\n"
            f"def fail(*args, **kwargs): raise admin.accounts.AccountConfigError('{PRIVATE}')\n"
            "admin.accounts.set_budget = fail\n"
            "raise SystemExit(admin.main(['account', 'budget', 'fake-account', "
            "'--rpc-budget', '1', '--thumb-budget', '1']))",
            1,
            "budget shares exceed server limits",
        ),
        (
            "from photos_mcp import session_export as cli\n"
            f"def fail(*args, **kwargs): raise RuntimeError('{PRIVATE}')\n"
            "cli.browser_login = fail\n"
            "raise SystemExit(cli.main(['browser', 'login', '--profile', 'fake-profile']))",
            1,
            "operation failed",
        ),
        (
            "from photos_mcp import session_export as cli\n"
            f"def fail(*args, **kwargs): raise cli.ExportError('{PRIVATE}')\n"
            "cli.browser_login = fail\n"
            "raise SystemExit(cli.main(['browser', 'login', '--profile', 'fake-profile']))",
            1,
            "session export failed",
        ),
        (
            "from photos_mcp import capture as cli\n"
            f"def fail(*args, **kwargs): raise OSError('{PRIVATE}')\n"
            "cli.rescrub = fail\n"
            "raise SystemExit(cli.main(['fake-output', '--rescrub']))",
            1,
            "fixture capture failed",
        ),
        (
            "import sys\n"
            "from photos_mcp import deployment as cli\n"
            f"def fail(*args, **kwargs): raise cli.Busy('{PRIVATE}')\n"
            "cli.Gate = fail\n"
            "sys.argv = ['deployment', 'reader', 'status']\n"
            "raise SystemExit(cli.main())",
            73,
            "preserve original work",
        ),
    ],
)
def test_operator_failures_keep_codes_and_never_echo_exception_text(source, code, message):
    assert message in diagnostic(run_cli(source), code)["msg"]


@pytest.mark.parametrize(
    "source,expected",
    [
        (
            "from photos_mcp import admin\n"
            "admin.Settings.from_env = lambda: None\n"
            "admin._list = lambda settings: [{'id': 'fake-account'}]\n"
            "raise SystemExit(admin.main(['account', 'list']))",
            [{"id": "fake-account"}],
        ),
        (
            "from photos_mcp import session_export as cli\n"
            "cli.browser_export = lambda *args, **kwargs: {'schema': 2, 'generation': 7}\n"
            "raise SystemExit(cli.main(['browser', 'export', '--profile', 'fake-profile', '--out', 'fake-out']))",
            {"schema": 2, "generation": 7},
        ),
        (
            "from photos_mcp import capture as cli\n"
            "cli.rescrub = lambda directory: ['fake-recording.json']\n"
            "raise SystemExit(cli.main(['fake-output', '--rescrub']))",
            {"rewritten": ["fake-recording.json"]},
        ),
        (
            "import sys\n"
            "from photos_mcp import deployment as cli\n"
            "class FakeGate:\n"
            " def __init__(self, *args): pass\n"
            " def status(self): return {'hold': None}\n"
            "cli.Gate = FakeGate\n"
            "sys.argv = ['deployment', 'reader', 'status']\n"
            "raise SystemExit(cli.main())",
            {"hold": None},
        ),
    ],
)
def test_operator_result_stdout_stays_machine_readable(source, expected):
    result = run_cli(source)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected
    assert result.stderr == ""


def test_browser_sign_in_instruction_is_useful_and_attributed():
    result = run_cli(
        "from contextlib import nullcontext\n"
        "from types import SimpleNamespace\n"
        "from photos_mcp import session_export as cli\n"
        "page = SimpleNamespace(url=cli.APP_URL, goto=lambda *args, **kwargs: None, "
        'content=lambda: \'<script data-id="_gd">{"oPEP7c": "user@example.com"}</script>\')\n'
        "context = SimpleNamespace(pages=[page], close=lambda: None)\n"
        "cli._playwright = lambda: nullcontext(None)\n"
        "cli._launch = lambda *args, **kwargs: context\n"
        "raise SystemExit(cli.main(['browser', 'login', '--profile', 'fake-profile']))"
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"profile": "fake-profile", "account": "user@example.com"}
    record = json.loads(result.stderr)
    assert "Sign in to Google in the browser window" in record["msg"]
    assert record["service"] == "photos-mcp"
    assert record["source_revision"] and record["instance"]
    assert record["level"] == "INFO"
    assert "user@example.com" not in result.stderr
