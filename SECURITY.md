# Security

photos-mcp handles a session that carries full authority over a Google account, so please
report vulnerabilities privately: use GitHub's "Report a vulnerability" (Security tab →
Advisories) on this repository. Do not open a public issue, and never post cookies,
`meta.json`, logs with account details or screenshots of a library.

In scope: anything that lets photos-mcp write without admitted authority or its documented guards, reach a host outside the
allowlist (README "Safety and Google posture"), leak the session, cookies or photos (in
tool output, logs, files or network traffic), or serve `/mcp` without a valid bearer.
Google changing its private web API is not a vulnerability; open a normal issue.

Human OIDC account management, MCP gateway grants and native Google authority are
separate. Wrong-owner viewer access, cross-account publication, a stale attempt reviving
after disconnect, or browser consent expanding an agent grant are in scope. This includes
the retained native default profile and further-account login profiles. See
[permission boundaries and recovery](docs/CONNECTIONS.md).

Only the latest release is supported.
