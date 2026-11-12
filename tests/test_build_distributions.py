"""Distribution acceptance rejects stale application bytes and stamps in either artifact."""

import json
import runpy
import shutil
import subprocess
import sys
import tarfile
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

from photos_mcp import __version__

ROOT = Path(__file__).resolve().parents[1]
REVISION = "a123456789" * 4


def test_built_release_artifacts_include_the_ignored_generated_stamp(tmp_path):
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE", "THIRD_PARTY_NOTICES", ".gitignore"):
        shutil.copyfile(ROOT / name, tmp_path / name)
    shutil.copytree(ROOT / "src", tmp_path / "src", ignore=shutil.ignore_patterns("__pycache__"))
    embed = runpy.run_path(str(ROOT / "scripts/embed-build-identity.py"))["embed"]
    embed(tmp_path, __version__, REVISION)
    result = subprocess.run(
        ["uv", "build", "--offline", "--python", sys.executable],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    check = runpy.run_path(str(ROOT / "scripts/check-build-distributions.py"))["check"]
    check(tmp_path, __version__, REVISION)


@pytest.mark.parametrize("artifact", ["wheel", "sdist"])
@pytest.mark.parametrize("change", ["source", "identity"])
def test_distribution_acceptance_rejects_changed_source_or_identity(tmp_path, artifact, change):
    check = runpy.run_path(str(ROOT / "scripts/check-build-distributions.py"))["check"]
    source = tmp_path / "src"
    files = {}
    for package in ("photos_mcp", "photos_login", "photos_maintenance"):
        path = source / package / "__init__.py"
        path.parent.mkdir(parents=True)
        path.write_text(f'PACKAGE = "{package}"\n')
        files[f"{package}/__init__.py"] = path.read_bytes()
    stamp = {"service": "photos-mcp", "version": __version__, "source_revision": REVISION}
    files["photos_mcp/build_identity.json"] = json.dumps(stamp).encode()
    dist = tmp_path / "dist"
    dist.mkdir()

    def write_artifacts(modified):
        for kind in ("wheel", "sdist"):
            content = modified if kind == artifact else files
            if kind == "wheel":
                with zipfile.ZipFile(dist / "app.whl", "w") as archive:
                    for name, data in content.items():
                        archive.writestr(name, data)
            else:
                with tarfile.open(dist / "app.tar.gz", "w:gz") as archive:
                    for name, data in content.items():
                        member = tarfile.TarInfo("app/src/" + name)
                        member.size = len(data)
                        archive.addfile(member, BytesIO(data))

    write_artifacts(files)
    check(tmp_path, __version__, REVISION)
    changed = dict(files)
    if change == "source":
        changed["photos_login/__init__.py"] = b'PACKAGE = "stale-source"\n'
    else:
        changed["photos_mcp/build_identity.json"] = json.dumps({**stamp, "source_revision": "f" * 40}).encode()
    write_artifacts(changed)
    with pytest.raises(ValueError, match="application bytes|build identity"):
        check(tmp_path, __version__, REVISION)
