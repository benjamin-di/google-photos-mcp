"""Artifact identity stays strict and never comes from mutable runtime settings."""

import json
import os
import runpy
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from importlib.metadata import PackageNotFoundError
from pathlib import Path
from types import SimpleNamespace

import pytest

from photos_mcp import __version__, build_identity

REVISION = "a123456789" * 4


def test_publisher_requires_the_product_version_and_full_source_revision(tmp_path):
    (tmp_path / "src/photos_mcp").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion="{__version__}"\n')
    embed = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/embed-build-identity.py"))["embed"]
    path = tmp_path / "src/photos_mcp/build_identity.json"
    for version, revision in (("wrong", REVISION), (__version__, REVISION[:12]), (__version__, "unknown")):
        with pytest.raises(ValueError):
            embed(tmp_path, version, revision)
        assert not path.exists()
    embed(tmp_path, "v" + __version__, REVISION.upper())
    assert build_identity.load(path) == build_identity.BuildIdentity("photos-mcp", __version__, REVISION)


@pytest.mark.parametrize("revision", [REVISION, "b" * 64])
def test_valid_embedded_identity_is_immutable(tmp_path, revision):
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"service": "photos-mcp", "version": __version__, "source_revision": revision}))
    identity = build_identity.load(path)
    assert (identity.service, identity.version, identity.source_revision) == ("photos-mcp", __version__, revision)
    with pytest.raises(AttributeError):
        identity.version = "changed"


@pytest.mark.parametrize(
    "change",
    [
        {"service": "google-photos-mcp"},
        {"version": "0.0.0"},
        {"source_revision": REVISION[:12]},
        {"source_revision": "unknown"},
        {"source_revision": REVISION.upper()},
        {"source_revision": 12},
        {"extra": True},
    ],
)
def test_invalid_or_conflicting_stamp_is_rejected_even_in_editable_source(tmp_path, monkeypatch, change):
    path = tmp_path / "identity.json"
    path.write_text(
        json.dumps({"service": "photos-mcp", "version": __version__, "source_revision": REVISION, **change})
    )
    monkeypatch.setattr(
        build_identity, "distribution", lambda _: SimpleNamespace(read_text=lambda _: '{"dir_info":{"editable":true}}')
    )
    with pytest.raises(ValueError, match="Invalid embedded"):
        build_identity.load(path)


@pytest.mark.parametrize("contents", ["[]", "null", "{}", "{broken"])
def test_malformed_stamp_is_rejected(tmp_path, contents):
    path = tmp_path / "identity.json"
    path.write_text(contents)
    with pytest.raises(ValueError):
        build_identity.load(path)


@pytest.mark.parametrize("direct", [None, "{}", "[]", "invalid", '{"dir_info":{"editable":false}}'])
def test_installed_package_requires_its_stamp(tmp_path, monkeypatch, direct):
    def installed(name):
        assert name == "google-photos-mcp"
        return SimpleNamespace(read_text=lambda _: direct)

    monkeypatch.setattr(build_identity, "distribution", installed)
    with pytest.raises(ValueError, match="Missing embedded"):
        build_identity.load(tmp_path / "missing.json")


def test_uninstalled_and_editable_source_report_unknown(tmp_path, monkeypatch):
    path = tmp_path / "missing.json"

    def not_installed(name):
        assert name == "google-photos-mcp"
        raise PackageNotFoundError(name)

    monkeypatch.setattr(build_identity, "distribution", not_installed)
    assert build_identity.load(path).source_revision == "unknown"
    monkeypatch.setattr(
        build_identity, "distribution", lambda _: SimpleNamespace(read_text=lambda _: '{"dir_info":{"editable":true}}')
    )
    assert build_identity.load(path) == build_identity.BuildIdentity("photos-mcp", __version__, "unknown")


def test_environment_overrides_are_ignored_and_each_process_gets_one_instance(tmp_path):
    env = dict(os.environ, VERSION="0.0.0", VCS_REF="b" * 40, SOURCE_REVISION="c" * 40, PHOTOS_VERSION="bad")
    code = (
        "import json; from photos_mcp.build_identity import log_context; "
        "print(json.dumps([log_context(),log_context()]))"
    )
    outputs = [
        json.loads(subprocess.check_output([sys.executable, "-c", code], cwd=tmp_path, env=env, text=True, timeout=30))
        for _ in range(2)
    ]
    first, second = outputs[0]
    assert first == second
    assert first["version"] == __version__ and first["source_revision"] == build_identity.BUILD.source_revision
    assert first["service"] == "photos-mcp"
    assert first["instance"] != outputs[1][0]["instance"]


def test_threads_share_the_process_instance():
    with ThreadPoolExecutor(max_workers=8) as pool:
        contexts = list(pool.map(lambda _: build_identity.log_context(), range(100)))
    assert all(context == contexts[0] for context in contexts)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process instance check")
def test_forked_worker_has_a_new_instance_and_the_same_artifact():
    read, write = os.pipe()
    parent = build_identity.log_context()
    child = os.fork()
    if child == 0:
        os.close(read)
        os.write(write, json.dumps(build_identity.log_context()).encode())
        os.close(write)
        os._exit(0)
    os.close(write)
    with os.fdopen(read) as pipe:
        context = json.load(pipe)
    assert os.waitpid(child, 0)[1] == 0
    assert context.pop("instance") != parent.pop("instance")
    assert context == parent
