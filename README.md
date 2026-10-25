# photos-mcp

[MCP](https://modelcontextprotocol.io) server for Google Photos libraries. An assistant can
**find** photos with Google Photos' own search (people, places, things, text in images,
dates), **look at** them, show them to the person as a gallery, **add** photos and videos
(from the person's phone or computer through a one-time upload page, from a link, or a file
the assistant has; [Uploading](#uploading)), **delete** them (to the trash, where Google keeps
them 60 days, and back again) and **organise albums** (create one, add photos, take them out,
delete it). The tools that take something away are marked destructive, so MCP clients ask the
person before each call; nothing empties the trash, archives, edits or shares a photo
([Safety](#safety-and-google-posture)). It serves
one library, or one per person behind an authenticating proxy ([Accounts](#accounts)), where
each person connects their own Google account through a sign-in link
([Signing in with a link](#signing-in-with-a-link)).

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
                                              │ sign-in links (optional, further accounts)
                                    photos-mcp-login: one browser profile per account ──▶ their snapshots
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
- `photos_download` (when `PHOTOS_PUBLIC_URL` is configured) makes JPEG copies for transfer
  to another tool. Each URL is a credential for one download within 10 minutes. Give it
  directly to the importing tool; opening it first consumes it. Copies default to 2048 px
  (up to 4096), contain no original EXIF metadata, and are not originals. Videos are refused.
  Outstanding copies are held in bounded memory (64 MiB); restarting revokes all links.
  A reverse proxy must forward only GET/HEAD of `/downloads/<43-character base64url token>`,
  without a query or Authorization header. WhatsApp clients with URL import can call
  `send_file(recipient=..., source_url=<returned url>)`; a plain Google Photos item URL
  opens a sign-in page and cannot substitute for a file download.
- With [sign-in links](#signing-in-with-a-link), the login service keeps each person's browser
  profile, which stays signed in to their **whole Google account** (not only Photos), and
  their snapshot. Whoever runs the server can use both: tell people so before they connect.
  They can end it any time in their Google Account (Security, Your devices).
- The state directory keeps a cache of thumbnails of your photos (up to
  `PHOTOS_CACHE_MAX_BYTES`, 2 GiB by default; `0` disables it). Treat it like the photos.
- The session directory and the browser profile give full access to the Google account.
- The server talks to nothing but Google's own photo hosts (see "Safety and Google
  posture"), except to fetch a file from a public link a caller gives `photos_upload`, and its
  logs carry no photo ids, URLs or cookies.
- A file being uploaded is held in an anonymous file under the state directory's `uploads/`
  only until it is sent (nothing is left there, even after a crash).

## Tools

All are read-only (`readOnlyHint`) except the album changes (`photos_album_create`,
`photos_album_add`, and `photos_album_remove`, the only `destructiveHint` one), `photos_upload`,
and the two that make links: `photos_connect` (a sign-in link, only where sign-in links are
configured) and `photos_upload_link` (an upload link, only with `PHOTOS_PUBLIC_URL`); all but
`photos_status` and the two link tools are `openWorldHint`.
Arguments that fail the schema come back as the typed `invalid_argument` error naming the
argument, never pydantic's raw text.

| Tool | Input | Output |
|---|---|---|
| `photos_search` | `query` (≤200), `media` all\|photos\|videos, `details`, `limit` 1–50 (20), `cursor` | items + `next_cursor` |
| `photos_list` | `source` library\|archive\|album\|favourites\|recently_added\|trash, `album_id` (iff album), `from_date`/`to_date` (library and archive), `media`, `details`, `limit`, `cursor` | items + `next_cursor` (Google pages of 100, sliced); with `source=album` also `album: {id, url, title, count}` |
| `photos_albums` | `limit`, `cursor` | `{id, url, title, count}`, plus `shared: true` for shared albums |
| `photos_get` | `ids` ≤10 (one batchexecute for all) | the item shape plus `uploaded_at` (UTC), lat/lon, camera, original width/height, size, duration, albums, `source`, `original_quality` |
| `photos_on_this_day` | `date` (today), `years` 1–20 (10), `per_year` 1–20 (6) | `{date, years: [{year, items, more}], years_checked}` |
| `photos_album_create` | `title` (≤200), `ids` ≤50 | the new album `{id, url, title, count, shared}`, `added` |
| `photos_album_add` | `album_id`, `ids` ≤50 (from search or list) | the album afterwards, `requested`, `added` (from its counts) |
| `photos_album_remove` | `album_id`, `ids` ≤50 (as the album's own listing gives them) | the album afterwards, `removed` (from its counts) and `not_in_album` (ids it does not hold, left alone) |
| `photos_album_delete` | `album_id` (one of the account's own albums) | the album and `deleted` (read back: the album no longer opens). Its photos stay in the library |
| `photos_delete` | `ids` ≤50 (from search or list) | `trashed`, `not_found` (not items of this library) and per id `in_trash` (read back from the trash) |
| `photos_restore` | `ids` ≤50 (as `photos_list(source=trash)` gives them) | `restored`, `not_in_trash` and per id `restored` (read back from the trash) |
| `photos_upload` | `url` (a public https link) or `data` (base64, ≤10 MiB), `filename`, `album_id` | the new item `{id, url, type, filename, taken_at, tz_known, width, height, uploaded_at}`, `already_in_library`, `size_bytes`, the album afterwards (`added`) |
| `photos_upload_link` | `album_id` | a one-time upload page for the caller's own account: `{url, expires_at, files_max, file_max_bytes, album, put_example, next_step}` ([Uploading](#uploading)) |
| `photos_show` | `items` ≤12 `{id, note}`, `title` | a gallery card for the person ([Photo cards](#photo-cards)); the same data as JSON for other clients |
| `photos_contact_sheet` | `ids` ≤24 | legend `n → id, date` + one JPEG grid ≤1568 px, numbered tiles, video badge |
| `photos_view` | `ids` ≤4, `max_px` 256–1568 (1024) | per id a label block (width/height of the returned JPEG) + a JPEG block (videos: poster frame) |
| `photos_download` | `ids` ≤10, `max_px` 256–4096 (2048); requires `PHOTOS_PUBLIC_URL` | one-time JPEG copy links, expiry, dimensions and size; no originals or video downloads |
| `photos_status` | – | account, generation, snapshot age, breaker, budgets left, cache, last error |
| `photos_canary` | – | `{ok, code, checked_at, cached, snapshot_age_s, generation}` (monitoring) |
| `photos_connect` | – | a one-time sign-in link for the caller's own account: `{url, expires_at, account_id, signed_in, next_step}` ([Signing in with a link](#signing-in-with-a-link)) |

Items have one shape in search, list, album and get, every key present (null when
unknown): `{id, url, type, taken_at, tz_known, filename, description, place, favourite,
archived}`. `url` opens the item on photos.google.com (inside its album when listed from
one); the person's own browser signs in, so it is also how they get an original. `taken_at` carries the item's UTC offset when `tz_known`; without a known
offset it is the instant in UTC with `Z` (never a fake `+00:00`). Search records carry no
place, so `place` is null there unless `details=true`, which reads each item's details
(one item-info RPC per item, ten to a request; `limit` up to 20) and adds `lat`, `lon` and
`camera`, filling `place` where the listing had none. `photos_get` adds `source` (how the
item reached the library: `mobile`, `web`, `shared`, `partnerShared`, `drive`, `pc`,
`gmail`, and `android` or `ios`) and `original_quality`. `archived` comes from the source for `photos_list`
library (false) and archive (true), because live library records end before the flag;
search and `photos_get` read it from the record; it is null where the record does not
say (album items). Filenames come from a second RPC (EWgK9e); if it fails for any reason other than
the session, the items go out with `filename: null`. Inline images are at most 1568 px;
`photos_download` exports JPEG copies up to 4096 px for file transfer. Original files
are not available. Listings carry `coverage` (`pagination_complete`,
`shared_partner: "not_covered"`, `archive_included` — false only for
`source=library` — and `cursor_recovered` / `dedupe_required` / `loop_detected` when they
apply) and `freshness` (`generation`, `session_verified_at`, `served_at`).

`photos_list` sources: `library` (newest first by taken date, archive excluded),
`archive`, `album`, `favourites` and `recently_added` (newest upload first; both are the web
app's own filtered views of the search RPC and include archived items), and `trash` (most
recently deleted first; what `photos_restore` can bring back). `from_date` /
`to_date` (`YYYY-MM-DD`, library and archive) bound the items' own local dates: the listing
starts Google's timeline at the end of the range and stops once it has passed the start.
`media=photos|videos` keeps one kind. A filtered listing reads up to 5 Google pages per call
to find matches, so a page can hold fewer than `limit` items while `next_cursor` goes on.

`photos_on_this_day` reads one library page per earlier year, starting at that day (up to 100
items; `more` says the day holds more than shown or than one page could tell: list it with
`from_date`=`to_date`), stops at the first year with nothing that old, and looks up all
filenames in one RPC. "Today" is in `PHOTOS_BUDGET_TZ`.

### Uploading

A photo or video reaches the library the way photos.google.com's own uploader sends it
([docs/PAYLOADS.md](docs/PAYLOADS.md#uploads)): photos-mcp first asks whether the library
already holds a file with the same SHA-1 (`swbisb`) and then sends nothing, else it sends the
bytes to Google's resumable upload endpoint in 16 MiB chunks and makes the item from the
upload token (`mdpdU`). Only photo and video formats Google Photos keeps are taken, told by
their first bytes (JPEG, PNG, GIF, WebP, HEIC, AVIF, BMP, TIFF and the raw formats built on
it; MP4, MOV, 3GP, AVI, MKV, WebM, WMV, MPEG), anything else is refused before it is stored.
Each file is at most `PHOTOS_UPLOAD_MAX_BYTES` (2 GiB) and costs one unit of the daily write
budget (two into an album) and three of the RPC budget whatever its size (the hash check, the
upload endpoint, making the item); the item keeps the file's own date (EXIF), else its
last-modified time. When the album step fails after the item was made, the answer still has the
item, and its `album` carries that error. At most 2 files are received or sent at once on a
server, by links and `photos_upload` alike: another one waits for a slot (up to 5 minutes on a
link, 1 minute for the tool) before its bytes are read.

- **The person's own files: `photos_upload_link`.** With `PHOTOS_PUBLIC_URL` set, the tool
  gives the caller a link, `PHOTOS_PUBLIC_URL/uploads/<id>`, to a page where they pick photos
  and videos on their phone or computer; each goes straight into their own library (and into
  the album the link names, checked when it is made) and the page says what became of it. The
  same address takes one file per `PUT` from a client that has it on disk (an image an
  assistant made in a code sandbox, say): `curl -T <file> -H 'X-File-Name: <name>' <url>`, which
  answers with the new item. A link works for 60 minutes and 100 files, adds only to the
  account that asked for it (not one removed and added again since), and needs no bearer: its
  256-bit id is the credential, so the reverse proxy must pass `/uploads/<id>` (GET and PUT, no
  query) through without one, as for sign-in links. A body that sends nothing for a minute is
  dropped. Links live in memory: a restart ends them.
- **A link: `photos_upload(url=...)`.** The server fetches the file itself, guarded so a link
  can never reach into the network the server runs in: https on port 443 only; every address
  the host resolves to must be public, and the request goes to that address only (curl's
  resolve pin, no proxy, not even one from the environment); redirects, at most 3, are checked
  the same way; no cookies or credentials are sent, and the whole fetch, headers included, must
  finish within 10 minutes.
- **Bytes the assistant has: `photos_upload(data=...)`**, base64 (standard or URL-safe, a
  `data:` URL prefix allowed), up to 10 MiB.

### Photo cards

`photos_show` gives claude.ai, ChatGPT and other [MCP Apps](https://modelcontextprotocol.io)
clients a gallery to draw for the person: thumbnail, date, the agent's note and a link that
opens each photo in Google Photos. The thumbnails (320 px, from the same paced and cached
image path as `photos_view`) travel inline in the result's `_meta["photos/view"]` as JPEG
`data:` URLs, which the model is not sent; the card (`ui://photos/view.html`,
`src/photos_mcp/view.html`) loads nothing else and opens links through the client. Clients
without MCP Apps get the same items as JSON.

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
| `not_signed_in` | a further account without a snapshot (never connected, or its browser was signed out), or not yet bound to a Google address | `photos_connect` gives the person a sign-in link |
| `account_not_connected` | the proxy named an account that is not configured, named none, or another bearer named one | the operator adds the account; never served from another account |
| `connect_unavailable` | `photos_connect` for the default account (its session is the operator's) | as named |
| `connect_limited` | more than 6 links an hour for one account | give the newest link |
| `upload_limited` | more than 30 upload links an hour for one account | give the newest link |
| `fetch_failed` | `photos_upload(url=...)`: the link did not answer, answered with an error, or took too long | nothing added |
| `outcome_unknown` | a change (album change, upload, trash) failed after it was sent (5xx or network): it is never repeated | read the album, `photos_albums`, `photos_list(source=recently_added)` or `photos_list(source=trash)` to see what happened |
| `refused` | Google refused a change (an album this account cannot edit, an upload it would not take, items it would not move) | nothing changed |

## Liveness

Only positive evidence marks a session dead: a redirect to `accounts.google.com` or to
Google's signed-out product page (`www.google.com/photos/about`), or another account than
`GOOGLE_PHOTOS_EXPECTED_EMAIL`. That generation is then never sent to Google again;
photos-mcp waits for a newer one. Everything else (a consent screen, an error page, a 5xx,
a bare 401, an app page without its signed-in data) is reported and retried later with
the same generation.

## Safety and Google posture

- **Album changes, uploads and the trash only.** Reads go through `GoogleTransport.rpc`, which
  refuses any RPC id outside `lcxiM EzkLib EWgK9e Z5xsfc snAcKc VrseUb fDcn4b EzwWhf swbisb
  zy0IHe` (`swbisb`: does the library hold a file with this hash; `zy0IHe`: the trash). Changes
  go through `GoogleTransport.write`, which refuses any outside `OXvT9d E1Cajb laUYf ycV3Nd
  LjmOue nV6Qv mdpdU XwAOJf` (create an album; add items to an album or a shared one; remove
  items from an album or a shared one; delete an album; make an item from an upload; move items
  to the trash or back) and never retries a request that may have reached Google
  (`outcome_unknown`, logged). Nothing deletes for good: no builder empties the trash or deletes
  from it, and `XwAOJf` is built only into the trash and back. An upload's bytes go through `GoogleTransport.upload_bytes` to Google's upload
  endpoint only (`/_/upload/uploadmedia/interactive`, and the session address it answers with,
  which must be the same endpoint); nothing is in the library until `mdpdU`, so a failure there
  changes nothing. No builder exists for emptying the trash, archiving, favouriting, editing
  dates, places or descriptions, sharing, the locked folder or downloads; `tests/test_safety.py`
  checks the source for other RPC ids and builders, other hosts and other HTTP libraries.
- **Removing and deleting are the client's to confirm.** `photos_album_remove`,
  `photos_album_delete` and `photos_delete` are annotated `destructiveHint`, and the
  instructions tell the model to ask the person first and show what goes; configure your MCP
  clients to ask before each call of these three (their approval setting per tool). Each acts
  in one call. `photos_album_remove` touches only the ids the album's own listing holds
  (Google's remove RPC names no album, so a key from another album would act there);
  `photos_delete` only the items of this library, by the dedup keys the trash RPC takes;
  `photos_album_delete` only the account's own albums (a shared one is left to the app, since
  deleting it changes it for everyone in it). Every change is read back (`added` / `removed` from the
  album's counts, `in_trash` / `restored` from the trash, `deleted` from the album no longer
  opening), counts against `PHOTOS_DAILY_WRITE_BUDGET` (each account's own, 500 a day; one per
  item, upload or album) and is appended to `audit.jsonl` in the account's state directory (ids
  and counts only).
- **Hosts:** HTTPS only, `photos.google.com`, `photos.fife.usercontent.google.com`,
  `lh[0-9].googleusercontent.com`. Redirects are followed by hand, at most 3, inside
  that list; sign-in, consent and `/sorry` redirects are classified, never followed. The one
  exception is `photos_upload(url=...)`, which fetches from the public host a caller names
  through `photos_mcp.fetch` ([Uploading](#uploading)), never with Google's cookies.
- **Pacing:** RPCs and the app page one at a time, ≥350 ms apart plus jitter; image
  fetches up to 4 at a time, starts ≥100 ms apart; uploads up to 2 files at a time in slots of
  their own (a long upload never holds the RPC slot); 10 s connect / 20 s read, ≤2 retries
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
| `GOOGLE_PHOTOS_EXPECTED_EMAIL` | – | required; the (default) account served |
| `MCP_BEARER_TOKEN` | – | required for `--transport http`; several tokens comma-separated (the first is the proxy's, [Accounts](#accounts)) |
| `PHOTOS_SESSION_DIR` | `/session` | the snapshot, read-only |
| `PHOTOS_STATE_DIR` | `/state` | `state.json` (budgets, breaker, dead generation, last error) and `cache/` |
| `PHOTOS_DAILY_RPC_BUDGET` | 3000 | RPCs plus app-page loads per day |
| `PHOTOS_DAILY_THUMB_BUDGET` | 2000 | image fetches per day (cache hits are free) |
| `PHOTOS_DAILY_WRITE_BUDGET` | 500 | changes per account and day (items added, removed, trashed or restored, albums created or deleted, files uploaded) |
| `PHOTOS_UPLOAD_MAX_BYTES` | 2 GiB | the largest file `photos_upload` and upload links take (1 MiB to 10 GiB) |
| `PHOTOS_BUDGET_TZ` | `UTC` | IANA time zone whose midnight resets the budgets |
| `PHOTOS_CACHE_MAX_BYTES` | 2 GiB | thumbnail cache, LRU; 0 disables it |
| `LOG_LEVEL` | `INFO` | photos-mcp's own log lines, JSON on stderr; FastMCP's startup lines and, over HTTP, the web server's access lines (with client addresses) are plain text |
| `PHOTOS_TRUSTED_ACCOUNT_HEADER` | – | the header the authenticating proxy names the account in ([Accounts](#accounts)); unset: every request is the default account's |
| `PHOTOS_DEFAULT_ACCOUNT` | `default` | the default account's id (what the proxy's header says for it) |
| `PHOTOS_ACCOUNT_SESSIONS_DIR` | `/sessions` | the further accounts' snapshots, one directory per account (written by the login service), read-only |
| `PHOTOS_PUBLIC_URL` | – | where people reach this server (the parent of `/mcp`, e.g. `https://example.com/photos`); turns on upload links (`photos_upload_link`), and with the next two `photos_connect` |
| `PHOTOS_LOGIN_URL` | – | the login service on a private network, e.g. `http://photos-mcp-login:8080` (host and port only) |
| `PHOTOS_LOGIN_TOKEN` | – | the login service's bearer (32+ characters), the same on both |

`photos-mcp [--transport http|stdio] [--host 127.0.0.1] [--port 8000]` (the image passes
`--host 0.0.0.0`): HTTP serves
stateless streamable HTTP with JSON replies on `/mcp` and liveness on `/health` (it never
calls Google).

## Accounts

One server can keep several people's libraries apart behind an authenticating reverse proxy
that knows who is calling. Each account has its own snapshot, state (budgets, last error, the
generation Google rejected), thumbnail cache, cursors and canary; Google's pacing and the
breaker after pushback are shared, because Google sees one client.

- The **default account** is the setup above: the snapshot in `PHOTOS_SESSION_DIR`,
  `GOOGLE_PHOTOS_EXPECTED_EMAIL`, its state right in `PHOTOS_STATE_DIR`. A one-account server
  needs nothing new. Its snapshot is the operator's to keep (with `photos-mcp-session` or
  anything else that writes the format); sign-in links are not offered for it.
- **Further accounts** live in `PHOTOS_STATE_DIR/accounts/<id>/` (ids are 1-32 lower-case
  letters, digits or dashes), with their snapshots in `PHOTOS_ACCOUNT_SESSIONS_DIR/<id>/`,
  which the login service writes after the person signed in through a link.
- **Budgets are split, not added.** `PHOTOS_DAILY_RPC_BUDGET` and `PHOTOS_DAILY_THUMB_BUDGET`
  stay the server's daily totals. Each further account has its own share; the default account
  gets what is left. A change applies on the next call. If the totals are lowered below the
  shares, every share shrinks in proportion (the default account then gets nothing).
- **Which account a request is for** comes from `PHOTOS_TRUSTED_ACCOUNT_HEADER`, read only on
  requests that carry the **first** token in `MCP_BEARER_TOKEN`: that one is the proxy's, so
  only the proxy may hold it, and the proxy must remove any copy of the header a client sends.
  With that token the header is required (a request the proxy did not name is refused, never
  served as the default account). Any other token acts for the default account, and a header
  with it is refused. A header naming an account that is not set up, or naming it twice, gets
  `account_not_connected`. Over stdio there is no header: always the default account.
- **An account serves one Google address.** It is bound by the first sign-in through its link
  (or given with `--email` when the account is added) and never changes: a sign-in as another
  Google account, or as one another account here serves, is refused. Until it is bound, the
  account serves nothing (`not_signed_in`). Only that address's library is ever served, even
  if the profile also holds another Google sign-in.
- **Removing and re-adding** an id gives a new account: nothing of the old one (state, cache,
  browser profile, snapshot) carries over; `admin account add` asks the login service to clear
  any leftover profile first.

```sh
docker exec photos-mcp photos-mcp admin account add second --rpc-budget 1500 --thumb-budget 1000
docker exec photos-mcp photos-mcp admin account budget second --rpc-budget 1000 --thumb-budget 800
docker exec photos-mcp photos-mcp admin account list      # addresses, shares, session and keeper state
docker exec photos-mcp photos-mcp admin account remove second   # also asks the login service to delete
                                                                # its browser profile and snapshot
```

## Signing in with a link

With the login service and a public address, a person connects their own Google account
without the operator: they ask their assistant to connect Google Photos, the assistant calls
`photos_connect` (the `not_signed_in` error points it there) and gives them the link, and they
sign in on Google's own page in their browser, phone or computer.

- **The login service** (`photos-mcp-login`, image `ghcr.io/benjamin-di/google-photos-mcp-login`,
  built from `Dockerfile.login`) keeps one browser profile per account in
  `PHOTOS_LOGIN_PROFILES` and writes the snapshots to `PHOTOS_LOGIN_SESSIONS` (the server's
  `PHOTOS_ACCOUNT_SESSIONS_DIR`, mounted read-only there). Run it beside the server on a
  private network with the same `PHOTOS_LOGIN_TOKEN`; it needs outbound access to Google.
  One browser runs at a time.
- **The window.** The page shows Google's sign-in page, running in that account's profile on a
  virtual display, through [noVNC](https://github.com/novnc/noVNC) relayed by the server.
  Nothing on the sign-in is automated: the person does every step, Google's own checks
  included. Passkeys do not work in a remote window: if Google offers one, choose another way, and
  sign in with the password and the code or prompt Google asks for.
- **The window is locked down**, because the login service keeps every further account's
  profile and snapshot: Chromium runs in kiosk mode (no address bar, tabs or menus) under
  [signin-window](https://github.com/benjamin-di/signin-window)'s managed policy (installed by
  the image) that refuses `file://`, `chrome://`, `view-source:`, `data:` and DevTools pages,
  file dialogs, downloads, printing and offers to save the password, and top-level pages are
  limited to Google's sign-in hosts. The service opens no window without the policy
  (`PHOTOS_LOGIN_UNMANAGED_BROWSER=1` overrides that, for trying it out on a computer of your
  own only). The live view works only while the profile is shown signed out on Google's
  sign-in page: a profile that is still signed in is checked, and exported, with nobody
  watching, and an account notice of a signed-in profile ends the window instead of showing it.
  Ctrl+V (Cmd+V) types the clipboard's text into the window (a small add-on to noVNC's viewer
  from [signin-window](https://github.com/benjamin-di/signin-window), the library the sign-in
  window and its links are built on; up to 1000 characters the window's keyboard has, which
  covers ASCII); on a phone, noVNC's keyboard button opens the phone's keyboard.
- **Once Google Photos shows a signed-in account**, the window checks that this account may
  use it (its bound address, or any address no other account here serves), then writes the
  snapshot from the same browser, as `photos-mcp-session browser export` does (it loads the
  library and opens one photo, so the library must not be empty), and closes. The server then
  binds the account to that address (also when the person closed the page meanwhile). A
  refused sign-in wipes the profile.
- **The keeper** opens each signed-in profile every hour (`PHOTOS_LOGIN_REFRESH_SECONDS`) and
  writes a fresh snapshot, so Google's rotating cookies stay current. Only the browser decides
  that a session ended, and only on evidence seen on two refreshes in a row: Google's sign-in
  page, its signed-out page or a request to prove it is them again. Then the snapshot is
  withdrawn, and the person's next call says `not_signed_in` (a new link). Another Google
  account in the profile withdraws it at once. Anything else (an account notice, a slow page)
  keeps the snapshot and tries again after 10 minutes. When Google rejects a generation, the
  server asks the keeper to look again soon (at most every 5 minutes per account). The keeper
  never signs in: it holds no password. `keeper.json` beside each snapshot says what it last
  saw (`photos_status`: `keeper`).
- **For the caller's account only.** The link is made for the account the request is for;
  there is no way to ask for another account's link. A newer link replaces the older one; at
  most 6 are made per account an hour.
- **One sign-in, 20 minutes.** `<PHOTOS_PUBLIC_URL>/connect/<id>` (192 random bits) works until
  a sign-in succeeds or 20 minutes pass; a reload or "try again" opens a new window, up to 4
  times. The page names the account it connects and opens Google's sign-in only when the
  person continues (so a chat app's link preview starts nothing). Its pages are never cached
  and send no referrer. Requests for ids that do not exist are counted per address (the last
  `X-Forwarded-For` entry, which the proxy must add): after 20 in ten minutes that address gets 429
  instead of 404 (a real link keeps working). Nothing under `/connect` shows or returns a
  session.
- **Your proxy decides who can reach it.** `/connect/...` (the page, its calls, noVNC's files and
  the live view's WebSocket) is served without the bearer, because people open it in a
  browser; the link id is its credential. Route only `<prefix>/connect/...` to the server
  without authentication, never anything else, and do not add your bearer to it.

Login service settings: `PHOTOS_LOGIN_TOKEN` (required), `PHOTOS_LOGIN_PROFILES` (`/profiles`),
`PHOTOS_LOGIN_SESSIONS` (`/sessions`), `PHOTOS_LOGIN_MINUTES` (20, a window's time),
`PHOTOS_LOGIN_REFRESH_SECONDS` (3600), `PORT` (8080), `PHOTOS_LOGIN_UNMANAGED_BROWSER` (see
above). Its API (behind the bearer) never returns a session; `/health` is open. As a compose file, beside the server:

```yaml
services:
  photos-mcp:
    image: ghcr.io/benjamin-di/google-photos-mcp:latest
    read_only: true
    volumes:
      - ./session:/session:ro          # the default account's snapshot
      - ./state:/state
      - ./sessions:/sessions:ro        # the further accounts' snapshots
    environment:
      MCP_BEARER_TOKEN: ${PROXY_TOKEN}           # held by the authenticating proxy only
      GOOGLE_PHOTOS_EXPECTED_EMAIL: you@example.com
      PHOTOS_TRUSTED_ACCOUNT_HEADER: X-Account   # set by the proxy, never by clients
      PHOTOS_PUBLIC_URL: https://example.com/photos
      PHOTOS_LOGIN_URL: http://photos-mcp-login:8080
      PHOTOS_LOGIN_TOKEN: ${LOGIN_TOKEN}
    networks: [default, login]
  photos-mcp-login:
    image: ghcr.io/benjamin-di/google-photos-mcp-login:latest
    read_only: true
    shm_size: 512m
    tmpfs: [/tmp, /home/login]
    volumes:
      - ./profiles:/profiles           # each account's Google sign-in: keep it private
      - ./sessions:/sessions
    environment:
      PHOTOS_LOGIN_TOKEN: ${LOGIN_TOKEN}
    networks: [login]                  # plus a network that reaches Google
networks:
  login: {}
```

## Operations

| Symptom | Action |
|---|---|
| `session_expired` / `session_unavailable` | export a fresh snapshot (`photos-mcp-session browser export`); if the profile is signed out, `browser login` again. photos-mcp recovers by itself on the next generation |
| `blocked` | wait out the breaker (`photos_status`); look for runaway use; lower `PHOTOS_DAILY_RPC_BUDGET` |
| `schema_changed` | Google changed a response: open an issue with the tool and `photos_status`' last error (the fix is a parser update in `rpc.py`, checked against GPTK's latest `parser.ts`) |
| `media_session_required` repeating | the snapshot lacks the image host's cookies: open a photo in the profile, export again |
| `account_mismatch` | stop; the snapshot or `GOOGLE_PHOTOS_EXPECTED_EMAIL` names the wrong account; never auto-corrected |
| `not_signed_in` for a further account | the person connects again with `photos_connect`; `photos_status.keeper` (or `admin account list`) says whether the keeper saw a signed-out profile or an error |
| `account_not_connected` | the proxy named an account that is not set up (`admin account add`), or sent no header with its token |

Kill switch: stop the server and delete the session directory; sign the profile out of
Google (or revoke the session in your Google account's security settings). For a further
account: `admin account remove <id>` (its profile and snapshot go too), or the person signs
the server's browser out under Security, Your devices.

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

Layout: `src/photos_mcp/` — `server.py` (tools, transports, the account registry),
`client.py` (tool logic, paging, thumbnails), `transport.py` (HTTP policy), `session.py`
(snapshot reader, cookies), `session_export.py` (`photos-mcp-session`), `state.py` (budgets,
breaker), `accounts.py` (further accounts), `connect.py` (sign-in links, on signin-window's
relay), `login_client.py` (the login service's API: signin-window's client plus the account
routes), `admin.py`, `config.py` (settings), `errors.py` (error codes),
`cursors.py`, `cache.py`, `images.py`, `rpc.py` (response parsers), `capture.py` (recorded
fixtures), `auth.py`, `logs.py`, `vendor/gpwc/` (request framing, read payloads).
`src/photos_login/` — the login service, on signin-window's: `app.py` (API), `windows.py` (the
sign-in window's runner),
`window.py` (the browser), `keeper.py`, `profiles.py`. `docs/PAYLOADS.md` maps every RPC and
field path to its source. `scripts/login_e2e.py` checks sign-in links end to end with real
browsers and a stand-in page (the release runs it in the login image).

Contributions: see `CONTRIBUTING.md`. Report security problems privately as described in
`SECURITY.md`, not in public issues.

## Licence and provenance

MIT (`LICENSE`). Based on [vsmelov/google-photos-mcp](https://github.com/vsmelov/google-photos-mcp),
used under the MIT licence with its author's permission;
vendors a modified subset of [gpwc](https://github.com/xob0t/google_photos_web_client) and
derives its parser fixtures from [Google-Photos-Toolkit](https://github.com/xob0t/Google-Photos-Toolkit),
both MIT. See `NOTICE` and `THIRD_PARTY_NOTICES`.

Optional operator-owned account upkeep is documented in [Account maintenance](docs/MAINTENANCE.md).
