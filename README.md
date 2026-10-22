# photos-mcp

Read-only [MCP](https://modelcontextprotocol.io) server for one Google Photos library. An
assistant can **find** photos with Google Photos' own search (people, places, things, text
in images, dates) and **look at** them, and nothing more: there is no write path.

Google's official Library API only sees media created by the calling app since
2025-03-31, so photos-mcp speaks the private `batchexecute` RPCs that photos.google.com
itself uses, with the cookies of a signed-in browser.

> **Unofficial.** photos-mcp is not affiliated with or endorsed by Google. It relies on an
> undocumented web API that can change at any time, and automated access may conflict with
> Google's terms of service: use it only with your own account, at your own risk. The
> session snapshot it reads carries **full authority over that Google account**; keep it
> private (the tools create it 0600 in a 0700 directory).

The repository and the PyPI package are [`google-photos-mcp`](https://pypi.org/project/google-photos-mcp/); its
commands are `google-photos-mcp` (also installed as `photos-mcp`) and `photos-mcp-session`. It is developed and tested
on Linux (Python 3.12+).

```
your browser profile ──photos-mcp-session──▶ session dir {cookies.txt, meta.json}
                                                      │ read-only
MCP client (stdio, or HTTP + bearer) ◀──▶ photos-mcp ─┘   state dir: budgets, breaker, thumbnail cache
```

## Quick start

1. **Install** (the `session` extra adds the browser exporter):

   ```sh
   uv tool install "google-photos-mcp[session]"
   photos-mcp-session browser install   # once: Playwright's Chromium (--with-deps also installs
                                        # its Linux system libraries, which needs root)
   ```

   or use the image `ghcr.io/benjamin-di/google-photos-mcp` for the server.

2. **Create a session snapshot** in a private directory, one of two ways:

   - *Cookies from your own browser* (schema 1). In Chrome or Chromium, open
     photos.google.com and one photo, export the cookies as a Netscape `cookies.txt`
     (any cookie-export extension), then:

     ```sh
     photos-mcp-session import --cookies cookies.txt --email you@example.com \
         --user-agent "<navigator.userAgent of that browser>" --out ~/.photos-mcp/session
     ```

     Delete the exported `cookies.txt` afterwards. Signing out of that browser ends the
     session, and the snapshot is never refreshed: import again when photos-mcp reports
     `session_expired` (its `photos_canary` calls an imported snapshot `snapshot_stale`
     after 3 h; that is expected).

   - *Dedicated browser profile* (**experimental**: schema 2, the browser's real request
     headers, refreshable unattended). Sign in by hand once, then export:

     ```sh
     photos-mcp-session browser login  --profile ~/.photos-mcp/profile
     photos-mcp-session browser export --profile ~/.photos-mcp/profile --out ~/.photos-mcp/session \
         --expected-email you@example.com
     ```

     Both commands open a browser window, so they need a display (on a server: `xvfb-run -a
     photos-mcp-session ...`). If Google refuses the sign-in in Playwright's Chromium, use an
     installed Google Chrome with `--channel chrome`, for `login` and `export` alike. One
     command at a time per profile (the browser locks it). `export` never signs in; when the
     profile is signed out, run `login` again.

3. **Run the server.** For a local client (Claude Desktop, Claude Code, ...) over stdio,
   with the absolute path of `photos-mcp` (`command -v photos-mcp`; desktop apps may not
   share your shell's `PATH`):

   ```json
   {
     "mcpServers": {
       "photos": {
         "command": "/home/you/.local/bin/photos-mcp",
         "args": ["--transport", "stdio"],
         "env": {
           "GOOGLE_PHOTOS_EXPECTED_EMAIL": "you@example.com",
           "PHOTOS_SESSION_DIR": "/home/you/.photos-mcp/session",
           "PHOTOS_STATE_DIR": "/home/you/.photos-mcp/state"
         }
       }
     }
   }
   ```

   Or over HTTP (`/mcp`, bearer required; `/health` is open). The image listens on port
   8000; publish it on localhost and run it as your own uid so it can read the snapshot:

   ```sh
   mkdir -p ~/.photos-mcp/state
   TOKEN="$(openssl rand -hex 32)"; echo "bearer token: $TOKEN"
   docker run -d --name photos-mcp -p 127.0.0.1:8000:8000 --read-only --user "$(id -u):$(id -g)" \
     -e MCP_BEARER_TOKEN="$TOKEN" -e GOOGLE_PHOTOS_EXPECTED_EMAIL=you@example.com \
     -v ~/.photos-mcp/session:/session:ro -v ~/.photos-mcp/state:/state \
     ghcr.io/benjamin-di/google-photos-mcp
   ```

   Clients send `Authorization: Bearer <token>` to `http://127.0.0.1:8000/mcp`. Put a TLS
   reverse proxy in front before exposing it beyond localhost. Without Docker,
   `photos-mcp` serves HTTP on 127.0.0.1:8000 (`--host`, `--port`).

4. **Keep the session fresh.** Google rotates session cookies. With a browser profile,
   re-run `browser export` regularly, for example hourly from cron:

   ```
   0 * * * * flock -n ~/.photos-mcp/export.lock xvfb-run -a photos-mcp-session browser export --profile ~/.photos-mcp/profile --out ~/.photos-mcp/session --expected-email you@example.com
   ```

   photos-mcp picks up each new generation by itself. `photos_status` shows the
   snapshot's age.

## Privacy

- The images and metadata the tools return go to your MCP client and, through it, to the
  model provider it uses.
- The state directory keeps a cache of thumbnails of your photos (up to
  `PHOTOS_CACHE_MAX_BYTES`, 2 GiB by default; `0` disables it). Treat it like the photos.
- The session directory and the browser profile give full access to the Google account.
- The server talks to nothing but Google's own photo hosts (see "Safety and Google
  posture"), and its logs carry no photo ids, URLs or cookies.

## Tools

All are read-only (`readOnlyHint`); all but `photos_status` are `openWorldHint`.
Arguments that fail the schema come back as the typed `invalid_argument` error naming the
argument, never pydantic's raw text.

| Tool | Input | Output |
|---|---|---|
| `photos_search` | `query` (≤200), `limit` 1–50 (20), `cursor` | items + `next_cursor` |
| `photos_list` | `source` library\|archive\|album, `album_id` (iff album), `limit`, `cursor` | items + `next_cursor` (library and archive: Google pages of 100, sliced); with `source=album` also `album: {id, title, count}` |
| `photos_albums` | `limit`, `cursor` | `{id, title, count}`, plus `shared: true` for shared albums |
| `photos_get` | `ids` ≤10 (one batchexecute for all) | the item shape plus `uploaded_at` (UTC), lat/lon, camera, original width/height, size, duration, albums |
| `photos_contact_sheet` | `ids` ≤24 | legend `n → id, date` + one JPEG grid ≤1568 px, numbered tiles, video badge |
| `photos_view` | `ids` ≤4, `max_px` 256–1568 (1024) | per id a label block (width/height of the returned JPEG) + a JPEG block (videos: poster frame) |
| `photos_status` | – | account, generation, snapshot age, breaker, budgets left, cache, last error |
| `photos_canary` | – | `{ok, code, checked_at, cached, snapshot_age_s, generation}` (monitoring) |

Items have one shape in search, list, album and get, every key present (null when
unknown): `{id, type, taken_at, tz_known, filename, description, place, favourite,
archived}`. `taken_at` carries the item's UTC offset when `tz_known`; without a known
offset it is the instant in UTC with `Z` (never a fake `+00:00`). Search records carry no
place, so `place` is null there. `archived` comes from the source for `photos_list`
library (false) and archive (true), because live library records end before the flag;
search and `photos_get` read it from the record; it is null where the record does not
say (album items). Filenames come from a second RPC (EWgK9e); if it fails for any reason other than
the session, the items go out with `filename: null`. Images are at most 1568 px; there is
no original or download path. Listings carry `coverage` (`pagination_complete`,
`shared_partner: "not_covered"`, `archive_included` — false only for
`source=library` — and `cursor_recovered` / `dedupe_required` / `loop_detected` when they
apply) and `freshness` (`generation`, `session_verified_at`, `served_at`).

`photos_canary` is the real-path check for monitoring: one real check (an
upstream library page of 1 item plus that item's thumbnail, bypassing the thumbnail
cache, through the normal RPC, parser and image path, budgets and breaker); calls in
between return the cached verdict with `cached: true`, unless a newer snapshot was published
since: a verdict belongs to the generation it checked. An ok result and lasting failures
(session, block, consent, budget, schema, `media_session_required`) are cached 55 min; transient ones
(`upstream_error`, `busy`, `stale_token`, `item_unavailable`, `internal_error`) are
re-checked after 5 min. `ok` is false on any error code, and with code `snapshot_stale`
when the snapshot is older than 3 h (`snapshot_age_s` > 10800) or its `verified_at` lies
more than 5 min in the future (negative `snapshot_age_s`); it never raises and never
logs ids.

`photos_status.last_error` records real failures, item-level ones included (for example
`media_session_required` on one thumbnail), but not the agent's own `invalid_argument` or
`invalid_cursor`.

Cursors are server-side, scope-bound (query, source or album) and live 1 h. An expired
cursor, or one from before a restart, is recovered by re-reading from the start and
skipping what was delivered (at most 5 upstream pages: 500 items for the library and the
archive, which use fixed Google pages of 100); the result says `dedupe_required`.

### Errors

Errors are tool errors whose text is `{"error": {"code", "next_step", "retryable"}}`:

| Code | Detected by | Reaction |
|---|---|---|
| `session_unavailable` | no `meta.json`, unreadable or malformed snapshot | publish a snapshot |
| `session_expired` | redirect to `accounts.google.com` or Google's signed-out `google.com/photos/about` (the generation is marked dead: no Google calls until a newer one); a bare 401 (reported, generation kept) | wait for a newer generation |
| `account_mismatch` | `meta.email` or `oPEP7c` ≠ `GOOGLE_PHOTOS_EXPECTED_EMAIL` (the generation is marked dead) | refuse; never auto-corrected |
| `blocked` | `/sorry`, 429, captcha | breaker: no Google calls for 30 min, then 2 h, then 8 h (persisted) |
| `consent_required` | redirect to `consent.google.com` from the app page or batchexecute | reported and retryable later; the generation is kept |
| `stale_token` | batchexecute 400/403 after one globals rebuild and retry | retry later |
| `schema_changed` | no `wrb.fr` envelope, all `er`, undecodable data, parser invariant | that tool fails |
| `media_session_required` | image request redirected to sign-in or consent (or HTML/401); never affects the session | that item fails |
| `item_unavailable` | 403/404 for one item, not an image, bomb, unknown id | that item only |
| `budget_exhausted` | daily RPC or thumbnail budget spent | until midnight in `PHOTOS_BUDGET_TZ` |
| `busy` | waited 30 s for the single request slot | retryable |
| `upstream_error` | 5xx or network after 2 retries (a body must finish within 30 s), an app page without `_gd`, HTML or a redirect inside Google from batchexecute | retryable; the generation is kept |
| `invalid_argument`, `invalid_cursor`, `cursor_expired`, `internal_error` | our own checks | as named |

## Liveness

Only positive evidence marks a session dead: a redirect to `accounts.google.com` or to
Google's signed-out product page (`www.google.com/photos/about`), or another account than
`GOOGLE_PHOTOS_EXPECTED_EMAIL`. That generation is then never sent to Google again;
photos-mcp waits for a newer one. Everything else (a consent screen, an error page, a 5xx,
a bare 401, an app page without its signed-in data) is reported and retried later with
the same generation.

## Safety and Google posture

- **No write path.** Only the read builders of gpwc are vendored; the transport refuses
  any RPC id outside `lcxiM EzkLib EWgK9e Z5xsfc snAcKc VrseUb fDcn4b EzwWhf`;
  `tests/test_safety.py` checks the source for other RPC ids, write builders, other
  hosts and other HTTP libraries.
- **Hosts:** HTTPS only, `photos.google.com`, `photos.fife.usercontent.google.com`,
  `lh[0-9].googleusercontent.com`. Redirects are followed by hand, at most 3, inside
  that list; sign-in, consent and `/sorry` redirects are classified, never followed.
- **Pacing:** RPCs and the app page one at a time, ≥350 ms apart plus jitter; image
  fetches up to 4 at a time, starts ≥100 ms apart; 10 s connect / 20 s read, ≤2 retries
  on 5xx and network errors only. Daily budgets
  `PHOTOS_DAILY_RPC_BUDGET` (3000) and `PHOTOS_DAILY_THUMB_BUDGET` (2000).
- **Request shape:** the browser behind the snapshot is the source of truth. Each request uses
  `meta.json`'s header values for its kind (`page`, `rpc`, `image`; or the UA-synthesised
  template where that kind fell back, see below), plus the request's cookie, referer and
  `accept-encoding`, all in Chrome's per-kind wire order (`session.CANONICAL_ORDER`).
  curl_cffi impersonates the newest `chromeN` profile with N ≤ `chromium_major` (the
  nearest one when none is older) with `default_headers=False`, so it adds no headers of
  its own. `tests/test_curl_sender.py` captures what goes on the wire and fails when the
  sample's Chrome is more than 4 majors past curl_cffi's newest profile (then `make lock`
  a newer curl_cffi).
- **Untrusted bytes:** RPC bodies ≤8 MiB and images ≤15 MiB, streamed; magic bytes
  before Pillow; a decompression-bomb warning is an error; JPEGs are `draft()`-reduced
  before `load()`.
- **Hygiene:** tool output never carries exception text; logs carry RPC ids, host,
  status, bytes and ms only. Cookies stay in memory: allowlisted `Set-Cookie` updates
  are merged there and never written back. photos-mcp never rotates cookies. Tool errors
  are logged as one line, never with a traceback: INFO for the agent's own mistakes
  (`invalid_argument`, `invalid_cursor`), WARNING otherwise (the ToolError's `log_level`,
  which FastMCP and the tool-call log follow). An unexpected crash logs `tool crashed:
  <type>` at ERROR and answers `internal_error`.

## Session snapshot

`PHOTOS_SESSION_DIR` (default `/session`, mounted read-only) holds:

- `cookies.txt`, Netscape format. Host-only rows have include-subdomains `FALSE` and
  no leading dot (the CDN's own `OSID` on `photos.fife.usercontent.google.com`);
  domain rows have `TRUE` and a leading dot. Rows for domains other than
  `google.com`, `photos.google.com` and `photos.fife.usercontent.google.com` are ignored.
- `meta.json` (schema 2): `{"schema": 2, "generation": <int, monotonic>, "email",
  "account_index": 0, "user_agent", "chromium_major", "verified_at": <ISO 8601>,
  "headers": {"page": [[name, value], …], "rpc": […], "image": […]}}`. It is written
  last, so it is the commit point. Each `headers.<kind>` is the ordered header list
  Chrome sent for one real request of that kind while the library loaded: names
  lower-case, at most 32 pairs, single-line values ≤ 512 chars, `user-agent` equal to
  `user_agent`, and only `user-agent`, `accept`, `accept-language`, `content-type` (rpc
  only), `origin`, `x-same-domain`, `upgrade-insecure-requests`, `priority`, `sec-ch-ua*`
  and `sec-fetch-*`. Other names are ignored, structural errors make the snapshot
  `session_unavailable`, and `chromium_major` must match the User-Agent. A kind without
  `sec-fetch-site`, `sec-fetch-mode` and `sec-fetch-dest`, or whose dest contradicts the
  kind (page `document`, rpc `empty`, image `image`), uses the synthesised template below
  for that kind only, logged once per generation without values; `photos-mcp-session`
  applies the same rule when it writes a snapshot.
  Values come from the browser, order from photos-mcp: a CDP capture reports
  names sorted, so every template is put in Chrome's per-kind wire order
  (`session.CANONICAL_ORDER`, recorded from a real Chrome with `--log-net-log`;
  `tests/fixtures/chrome_wire_order.json` keeps those names). The order covers the headers
  the transport adds too: `referer`, `accept-encoding` and `cookie` go last, before
  `priority`, as Chrome sends them; an unknown name goes just before `sec-fetch-site`.
- Schema 1 (no `headers`; what `photos-mcp-session import` writes) is read too: the
  templates are then synthesised from the User-Agent (its Chrome major in `sec-ch-ua`, its
  platform, Chrome's per-kind `sec-fetch-*` values), so they never contradict it.

`meta.json` is stat-ed on every call; a newer generation rebuilds the HTTP session and
re-reads the page globals. A generation Google rejected is not retried. A checked-in
sample lives in `tests/fixtures/session/` (`meta.json` schema 2, `meta.schema1.json`
schema 1). Anything that writes this format can keep the session for photos-mcp; nothing
flows back from photos-mcp to it.

## Configuration

| Variable | Default | |
|---|---|---|
| `GOOGLE_PHOTOS_EXPECTED_EMAIL` | – | required; the only account served |
| `MCP_BEARER_TOKEN` | – | required for `--transport http`; several tokens comma-separated |
| `PHOTOS_SESSION_DIR` | `/session` | the snapshot, read-only |
| `PHOTOS_STATE_DIR` | `/state` | `state.json` (budgets, breaker, dead generation, last error) and `cache/` |
| `PHOTOS_DAILY_RPC_BUDGET` | 3000 | RPCs plus app-page loads per day |
| `PHOTOS_DAILY_THUMB_BUDGET` | 2000 | image fetches per day (cache hits are free) |
| `PHOTOS_BUDGET_TZ` | `UTC` | IANA time zone whose midnight resets the budgets |
| `PHOTOS_CACHE_MAX_BYTES` | 2 GiB | thumbnail cache, LRU; 0 disables it |
| `LOG_LEVEL` | `INFO` | photos-mcp's own log lines, JSON on stderr; FastMCP's startup lines and, over HTTP, the web server's access lines (with client addresses) are plain text |

`photos-mcp [--transport http|stdio] [--host 127.0.0.1] [--port 8000]` (the image passes
`--host 0.0.0.0`): HTTP serves
stateless streamable HTTP with JSON replies on `/mcp` and liveness on `/health` (it never
calls Google).

## Operations

| Symptom | Action |
|---|---|
| `session_expired` / `session_unavailable` | export a fresh snapshot (`photos-mcp-session browser export`); if the profile is signed out, `browser login` again. photos-mcp recovers by itself on the next generation |
| `blocked` | wait out the breaker (`photos_status`); look for runaway use; lower `PHOTOS_DAILY_RPC_BUDGET` |
| `schema_changed` | Google changed a response: open an issue with the tool and `photos_status`' last error (the fix is a parser update in `rpc.py`, checked against GPTK's latest `parser.ts`) |
| `media_session_required` repeating | the snapshot lacks the image host's cookies: open a photo in the profile, export again |
| `account_mismatch` | stop; the snapshot or `GOOGLE_PHOTOS_EXPECTED_EMAIL` names the wrong account; never auto-corrected |

Kill switch: stop the server and delete the session directory; sign the profile out of
Google (or revoke the session in your Google account's security settings).

## Development

`make bootstrap`, then `make check` (ruff, format, lock consistency, pytest; see
`AGENTS.md`). `make lock` after changing dependencies in `pyproject.toml`. Tests never
reach Google.

Recorded fixtures: `python -m photos_mcp.capture OUT_DIR [--query Q]`
(`scripts/capture-fixtures` is the same entry point) records anonymised real answers
through the normal transport, budgets and breaker, including the no-cookie signed-out
answer. Run it with the server's configuration and state directory (for example
`docker exec photos-mcp python -m photos_mcp.capture /state/recorded`; both keep budgets
and the breaker in `state.json`, so keep the run short), review the output, check it in under
`tests/fixtures/recorded/` (`tests/test_recorded.py` and `tests/test_fixture_privacy.py`
pick it up), then delete the output directory. The anonymiser replaces every string and
every number about the account (see `capture.py`).

Layout: `src/photos_mcp/` — `server.py` (tools, transports), `client.py` (tool logic,
paging, thumbnails), `transport.py` (HTTP policy), `session.py` (snapshot reader, cookies),
`session_export.py` (`photos-mcp-session`), `state.py` (budgets, breaker), `config.py`
(settings), `errors.py` (error codes), `cursors.py`, `cache.py`, `images.py`, `rpc.py`
(response parsers), `capture.py` (recorded fixtures), `auth.py`, `logs.py`, `vendor/gpwc/`
(request framing, read payloads). `docs/PAYLOADS.md` maps every RPC and field path to its
source.

Contributions: see `CONTRIBUTING.md`. Report security problems privately as described in
`SECURITY.md`, not in public issues.

## Licence and provenance

MIT (`LICENSE`). Based on [vsmelov/google-photos-mcp](https://github.com/vsmelov/google-photos-mcp),
used under the MIT licence with its author's permission;
vendors a modified subset of [gpwc](https://github.com/xob0t/google_photos_web_client) and
derives its parser fixtures from [Google-Photos-Toolkit](https://github.com/xob0t/Google-Photos-Toolkit),
both MIT. See `NOTICE` and `THIRD_PARTY_NOTICES`.
