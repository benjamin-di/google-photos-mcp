"""Bearer auth for the HTTP transport.

``MCP_BEARER_TOKEN`` holds the accepted token, or several separated by commas (for example
one for a reverse proxy and one for direct clients, so either can be rotated alone). Only
``/mcp`` is protected; ``/health`` stays open. The stdio transport needs no token.
"""

from __future__ import annotations

import os
from collections.abc import Mapping

from fastmcp.server.auth.providers.jwt import StaticTokenVerifier

TOKEN_VAR = "MCP_BEARER_TOKEN"


class AuthError(Exception):
    """No token configured for a transport that requires one."""


def bearer_auth_from_env(env: Mapping[str, str] | None = None) -> StaticTokenVerifier:
    """A verifier accepting every configured token; raises AuthError when there is none."""
    env = os.environ if env is None else env
    tokens = [token.strip() for token in (env.get(TOKEN_VAR) or "").split(",") if token.strip()]
    if not tokens:
        raise AuthError(f"{TOKEN_VAR} must be set to serve over HTTP (comma-separated for several tokens)")
    return StaticTokenVerifier(
        tokens={token: {"client_id": f"token-{index}", "scopes": []} for index, token in enumerate(tokens, 1)}
    )
