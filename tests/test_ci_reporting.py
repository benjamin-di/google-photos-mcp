"""Exercise the gate's reports and exits with small real pytest runs and fake external tools."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)
    path.chmod(0o755)


@pytest.fixture
def gate(tmp_path):
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("test.sh", "ci-log.py", "bootstrap.sh"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
        (scripts / name).chmod(0o755)
    (root / "source-marker.txt").write_text("initial")
    (scripts / "source-identity.py").write_text(
        "import json\nfrom pathlib import Path\nprint(json.dumps({'source': Path('source-marker.txt').read_text()}))\n"
    )
    tools = root / "tools"
    executable(tools / "git", "#!/bin/sh\nexit 0\n")
    executable(
        tools / "uv",
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "if sys.argv[1] == 'sync':\n"
        " print('bootstrap output')\n"
        " sys.exit(int(os.environ.get('BOOTSTRAP_EXIT', '0')))\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "output.write_text('stale' if os.environ.get('STALE_LOCK') else 'locked')\n",
    )
    executable(
        root / ".venv/bin/ruff",
        f"#!{sys.executable}\n"
        "import os, pathlib, sys\n"
        "if os.environ.get('CHANGE_SOURCE'): pathlib.Path('source-marker.txt').write_text('changed')\n"
        "print('lint or format output')\n"
        "sys.exit(int(os.environ.get('LINT_EXIT', '0')))\n",
    )
    executable(root / ".venv/bin/python", f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    for name in ("requirements-runtime.lock", "requirements-login.lock"):
        (root / name).write_text("locked")
    (root / "test_sample.py").write_text("def test_ok(): pass\ndef test_broken(): assert False, 'synthetic failure'\n")
    environment = dict(os.environ, PATH=str(tools) + os.pathsep + os.environ["PATH"])
    # Nested pytest must not inherit its caller's report, selector or plugin configuration.
    for key in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS", "BOOTSTRAP_EXIT", "LINT_EXIT", "CHANGE_SOURCE", "STALE_LOCK"):
        environment.pop(key, None)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    return root, environment


def run_gate(gate, *arguments, **environment):
    root, inherited = gate
    return subprocess.run(
        ["bash", "scripts/test.sh", *arguments],
        cwd=root,
        env=inherited | environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=30,
    )


def receipts(gate):
    return sorted((gate[0] / ".artifacts").glob("check-*"))


def test_real_test_failures_and_repeated_invocations_keep_separate_reports(gate):
    inherited_report = gate[0] / "stage-default.xml"
    failed = run_gate(gate, "test_sample.py", PYTEST_ADDOPTS=f"--junitxml={inherited_report}")
    assert failed.returncode == 1, failed.stdout
    first = receipts(gate)[0]
    suite = ET.parse(first / "junit.xml")
    assert {case.attrib["name"] for case in suite.findall(".//testcase")} == {"test_ok", "test_broken"}
    assert len(suite.findall(".//failure")) == 1
    assert not inherited_report.exists(), "the repository destination must override the stage default"
    assert "synthetic failure" in (first / "tests.log").read_text()
    before = (first / "junit.xml").read_bytes()
    passed = run_gate(gate, "test_sample.py::test_ok")
    assert passed.returncode == 0, passed.stdout
    runs = receipts(gate)
    assert len(runs) == 2
    assert (first / "junit.xml").read_bytes() == before
    second = next(run for run in runs if run != first)
    assert [case.attrib["name"] for case in ET.parse(second / "junit.xml").findall(".//testcase")] == ["test_ok"]
    assert (first / "exit-code.txt").read_text().strip() == "1"
    assert (second / "exit-code.txt").read_text().strip() == "0"


@pytest.mark.parametrize("environment,expected", [({"BOOTSTRAP_EXIT": "17"}, 17), ({"LINT_EXIT": "23"}, 23)])
def test_failures_before_pytest_keep_command_status_and_source_evidence(gate, environment, expected):
    result = run_gate(gate, "test_sample.py", **environment)
    assert result.returncode == expected, result.stdout
    report = receipts(gate)[0]
    assert (report / "exit-code.txt").read_text().strip() == str(expected)
    assert (report / "source.json").read_bytes() == (report / "final-source.json").read_bytes()
    assert not (report / "junit.xml").exists(), "a command failure is not a fabricated test case"
    assert "bootstrap output" in (report / "bootstrap.log").read_text()


def test_lock_consistency_is_still_a_required_check(gate):
    result = run_gate(gate, "test_sample.py::test_ok", STALE_LOCK="1")
    assert result.returncode == 1, result.stdout
    assert "requirements-runtime.lock is stale" in result.stdout
    assert not (receipts(gate)[0] / "junit.xml").exists()


@pytest.mark.parametrize("pytest_exit", [0, 1, 5])
def test_source_change_fails_a_pass_and_preserves_an_existing_pytest_exit(gate, pytest_exit):
    root, _ = gate
    executable(root / ".venv/bin/python", f"#!/bin/sh\nexit {pytest_exit}\n")
    result = run_gate(gate, CHANGE_SOURCE="1")
    assert result.returncode == (pytest_exit or 1), result.stdout
    report = receipts(gate)[0]
    assert (report / "source.json").read_bytes() != (report / "final-source.json").read_bytes()
    assert "Source changed" in result.stdout


@pytest.mark.parametrize("broken_log", [False, True])
def test_command_log_is_bounded_and_an_unwritable_log_is_advisory(tmp_path, broken_log):
    destination = tmp_path / "command.log"
    if broken_log:
        destination.mkdir()
    payload = b"old output\n" + b"x" * (320 * 1024) + b"\nlast failure\n"
    result = subprocess.run(
        [sys.executable, ROOT / "scripts/ci-log.py", destination],
        input=payload,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == payload
    if broken_log:
        assert b"Command log unavailable" in result.stderr
    else:
        assert destination.read_bytes() == payload[-256 * 1024 :]


@pytest.mark.parametrize("command_exit", [0, 29, 124])
@pytest.mark.parametrize("cached", [False, True, "broken"])
def test_connections_exit_cleanup_and_report_mount_are_isolated(tmp_path, monkeypatch, command_exit, cached):
    root = tmp_path / "project"
    scripts = root / "scripts"
    scripts.mkdir(parents=True)
    for name in ("check-connections.sh", "ci-log.py"):
        shutil.copyfile(ROOT / "scripts" / name, scripts / name)
    tools = root / "tools"
    executable(tools / "uv", "#!/bin/sh\necho 'uv 0.13.5 (synthetic-platform)'\n")
    monkeypatch.delenv("FLEET_TOOLS", raising=False)
    cache = root / "container-downloads"
    if cached:
        refused = "import sys; sys.exit('Foreign download-cache entry refused')"
        native = refused if cached == "broken" else f"print({str(cache)!r})"
        executable(tools / "native.py", native + "\n")
        monkeypatch.setenv("FLEET_TOOLS", str(tools))
    broken, cached = cached == "broken", cached is True
    monkeypatch.setenv("UV_CACHE_DIR", str(root / "host-cache"))
    audit = root / "docker.jsonl"
    executable(
        tools / "docker",
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['DOCKER_AUDIT'], 'a') as audit: audit.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1] == 'run':\n"
        " print('synthetic container failure output')\n"
        " sys.exit(int(os.environ['COMMAND_EXIT']))\n",
    )
    pocket = root / "pocket-id"
    pocket.write_text("disposable binary placeholder")
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("DOCKER_AUDIT", str(audit))
    monkeypatch.setenv("COMMAND_EXIT", str(command_exit))
    monkeypatch.setenv("POCKET_ID_BIN", str(pocket))
    for _ in range(2):
        result = subprocess.run(
            ["bash", "scripts/check-connections.sh"], cwd=root, capture_output=True, text=True, timeout=15
        )
        assert result.returncode == command_exit, result.stdout + result.stderr
        assert ("using disposable downloads" in result.stderr) == broken
    reports = list((root / ".artifacts").glob("connections-*"))
    assert len(reports) == 2
    for report in reports:
        assert (report / "exit-code.txt").read_text().strip() == str(command_exit)
        assert "synthetic container failure output" in (report / "command.log").read_text()
    calls = [json.loads(line) for line in audit.read_text().splitlines()]
    runs = [call for call in calls if call[0] == "run"]
    cleanups = [call for call in calls if call[0] == "rm"]
    assert len(runs) == len(cleanups) == 2
    assert runs[0][runs[0].index("--name") + 1] != runs[1][runs[1].index("--name") + 1]
    for run, cleanup in zip(runs, cleanups, strict=True):
        assert cleanup == ["rm", "--force", run[run.index("--name") + 1]]
        assert str(root) + ":/workspace:ro" in run
        assert str(pocket) + ":/tmp/pocket-id:ro" in run
        assert any(value.startswith("python:3.13-slim-trixie@sha256:") for value in run)
        for bucket, target in (("uv", "uv-cache"), ("pip", "pip-cache"), ("browsers", "browsers")):
            assert (f"type=bind,src={cache / bucket},dst=/tmp/{target}" in run) == cached
        assert not any("host-cache" in arg for arg in run)
        command = run[-1]
        parsed = subprocess.run(["bash", "-n"], input=command, capture_output=True, text=True, check=False)
        assert parsed.returncode == 0, parsed.stderr
        assert "uv sync --locked --all-extras" in command
        assert "signin_window.browser install-policy" in command
        assert "5066103959ef4e9b10f37e5a148627360dd8414e4cf8a7db92bdbd022e728aaa" in command
        assert "--junitxml=/reports/junit.xml" in command
        assert "tests/test_connections_acceptance.py" in command
        assert "-e UV_PROJECT_ENVIRONMENT=/tmp/venv" in " ".join(run)
        assert "-e XDG_CONFIG_HOME=/tmp/browser-settings" in " ".join(run)


@pytest.mark.parametrize("fails", [False, True])
def test_connections_scenario_is_a_real_named_test_with_failure_details(tmp_path, fails):
    (tmp_path / "tests").mkdir()
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "tests/test_connections_acceptance.py", tmp_path / "tests/test_connections_acceptance.py")
    (tmp_path / "scripts/connections_e2e.py").write_text(
        "raise AssertionError('synthetic connections failure')\n" if fails else "print('synthetic connections pass')\n"
    )
    environment = {key: value for key, value in os.environ.items() if key not in ("PYTEST_ADDOPTS", "PYTEST_PLUGINS")}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--junitxml=connections.xml", "tests/test_connections_acceptance.py"],
        cwd=tmp_path,
        env=environment | {"PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1"},
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == int(fails), result.stdout + result.stderr
    suite = ET.parse(tmp_path / "connections.xml")
    assert [case.attrib["name"] for case in suite.findall(".//testcase")] == ["test_disposable_connections"]
    assert bool(suite.findall(".//failure")) == fails
    if fails:
        assert "synthetic connections failure" in suite.find(".//failure").text


@pytest.mark.parametrize("command_exit", [0, 5])
def test_missing_or_malformed_junit_does_not_replace_the_command_exit(gate, command_exit):
    root, _ = gate
    executable(
        root / ".venv/bin/python",
        f"#!/bin/sh\nprintf '<broken-report' > \"${{3#--junitxml=}}\"\nexit {command_exit}\n",
    )
    result = run_gate(gate)
    assert result.returncode == command_exit, result.stdout
    report = receipts(gate)[0]
    assert (report / "exit-code.txt").read_text().strip() == str(command_exit)
    assert (report / "junit.xml").read_text() == "<broken-report"
