"""Compare release distributions with their stamped source, without installing them."""

import json
import sys
import tarfile
import zipfile
from pathlib import Path


def check(root: Path, product_version: str, revision: str) -> None:
    expected = {
        path.relative_to(root / "src").as_posix(): path.read_bytes()
        for path in (root / "src").rglob("*")
        if path.is_file() and path.suffix in {".py", ".html"}
    }
    identity = {"service": "photos-mcp", "version": product_version, "source_revision": revision}
    for pattern in ("*.whl", "*.tar.gz"):
        artifacts = list((root / "dist").glob(pattern))
        if len(artifacts) != 1:
            raise ValueError("Release needs exactly one wheel and source distribution")
        path = artifacts[0]
        if path.suffix == ".whl":
            with zipfile.ZipFile(path) as archive:
                files = {name: archive.read(name) for name in archive.namelist() if not name.endswith("/")}
        else:
            with tarfile.open(path) as archive:
                files = {
                    member.name.split("/src/", 1)[1]: archive.extractfile(member).read()
                    for member in archive.getmembers()
                    if member.isfile() and "/src/" in member.name
                }
        if json.loads(files.get("photos_mcp/build_identity.json", b"null")) != identity:
            raise ValueError("Distribution build identity differs from the selected source")
        packaged = {
            name: data
            for name, data in files.items()
            if name.split("/", 1)[0] in {"photos_mcp", "photos_login", "photos_maintenance"}
            and Path(name).suffix in {".py", ".html"}
        }
        if packaged != expected:
            raise ValueError("Distribution application bytes differ from the selected source")


if __name__ == "__main__":
    check(Path.cwd(), *sys.argv[1:])
