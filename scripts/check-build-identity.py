"""Verify the installed application and native MCP identity without provider access."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
from importlib.metadata import distribution, version
from pathlib import Path

from fastmcp import Client

from photos_mcp.build_identity import BUILD
from photos_mcp.config import Settings
from photos_mcp.server import VIEW_HTML, Accounts, create_server


def check(product_version: str, revision: str) -> None:
    direct = json.loads(distribution("google-photos-mcp").read_text("direct_url.json") or "{}")
    assert not direct.get("dir_info", {}).get("editable"), "Probe requires an installed release artifact"
    assert BUILD.service == "photos-mcp"
    assert BUILD.version == version("google-photos-mcp") == product_version
    assert BUILD.source_revision == revision
    assert f"Google Photos v{product_version}" in VIEW_HTML and f'title="{revision}"' in VIEW_HTML
    with tempfile.TemporaryDirectory() as directory:
        settings = Settings(
            expected_email="smoke@example.com", session_dir=Path(directory) / "session", state_dir=Path(directory)
        )
        server = create_server(Accounts.from_settings(settings))

        async def native_identity():
            for mode in ("legacy", "auto"):
                async with Client(server, mode=mode) as client:
                    assert client.server_info.version == product_version

        asyncio.run(native_identity())
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("PHOTOS_", "GOOGLE_PHOTOS_", "MCP_", "PYTHONPATH"))
        }
        environment.update(VERSION="0.0.0", VCS_REF="f" * 40)
        for module in (
            "photos_mcp",
            "photos_mcp.session_export",
            "photos_mcp.capture",
            "photos_mcp.deployment",
            "photos_mcp.connections_auth",
            "photos_maintenance.cli",
            "photos_maintenance.human",
        ):
            args = ["--invalid=synthetic-private-argument"]
            if module == "photos_maintenance.cli":
                args.insert(0, "status")
            result = subprocess.run(
                [sys.executable, "-m", module, *args],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            assert result.returncode == 2 and not result.stdout, (module, result.returncode)
            assert "synthetic-private-argument" not in result.stderr
            events = [json.loads(line) for line in result.stderr.splitlines()]
            assert events, f"Missing retained diagnostic for {module}"
            for event in events:
                assert event["service"] == BUILD.service
                assert event["version"] == product_version and event["source_revision"] == revision
                assert event["instance"] and event["level"] and event["component"]
        result = subprocess.run(
            [sys.executable, "-m", "photos_login"],
            cwd=directory,
            env=environment,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 1 and not result.stdout
        events = [json.loads(line) for line in result.stderr.splitlines()]
        assert events
        assert all(event["version"] == product_version and event["source_revision"] == revision for event in events)


if __name__ == "__main__":
    check(*sys.argv[1:])
