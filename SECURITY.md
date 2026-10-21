# Security

photos-mcp handles a session that carries full authority over a Google account, so please
report vulnerabilities privately: use GitHub's "Report a vulnerability" (Security tab →
Advisories) on this repository. Do not open a public issue, and never post cookies,
`meta.json`, logs with account details or screenshots of a library.

In scope: anything that lets photos-mcp write to a library, reach a host outside the
allowlist (README "Safety and Google posture"), leak the session, cookies or photos (in
tool output, logs, files or network traffic), or serve `/mcp` without a valid bearer.
Google changing its private web API is not a vulnerability; open a normal issue.

Only the latest release is supported.
