"""photos-mcp: read-only MCP access to one Google Photos library over the web app's private RPCs."""

from importlib.metadata import PackageNotFoundError, version

try:
    # Releases bump pyproject.toml only; read the installed distribution.
    __version__ = version("google-photos-mcp")
except PackageNotFoundError:  # pragma: no cover - source tree without an install
    __version__ = "0+unknown"
