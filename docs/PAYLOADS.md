# Payload map: MCP tools → Google Photos web RPCs

> **photos-mcp note.** This research predates photos-mcp (it was written for the project it is based on). Tool names map as
> follows: `list_library` → `photos_list`, `search` → `photos_search`, `list_albums` →
> `photos_albums`, `get_album` → `photos_list(source="album")`, `get_media_info` →
> `photos_get`, `get_media` → `photos_view` / `photos_contact_sheet`.
> `download_media` and `get_storage_quota` are gone (no tool); uploads (`photos_upload`,
> `photos_upload_link`) are photos-mcp's own, see "Uploads"; deleting (`photos_delete`, `photos_restore`,
> `photos_album_delete`) is in "Deleting". gpwc is vendored under
> `src/photos_mcp/vendor/gpwc/`; the transport now uses curl_cffi, not `requests`.
> RPC ids, payloads and field paths below are current; pacing, error handling and
> caching follow README.md, which is authoritative where they differ.

Every RPC id and request shape below was taken from source that was read for this
document, never guessed. Two references, both pinned:

| source | commit | files read |
|---|---|---|
| gpwc — `xob0t/google_photos_web_client` | `651aa268d50529cb6128c8a42c9cd4ce668f2189` (2026-06-04, main) | `gpwc/client.py`, `gpwc/payloads.py`, `gpwc/parser.py`, `gpwc/utils.py`, `README.md` |
| GPTK — `xob0t/Google-Photos-Toolkit` | `6db9ab355e4797327e4078cf87a2b6994d36cf73` (v3.3.0, 2026-08-20) | `src/api/api.ts`, `src/api/parser.ts`, `src/api/api-utils.ts`, `src/filters.ts`, `src/types/index.ts`, `tests/fixtures/parser/*.json` |

GPTK has been ported to TypeScript since the task was written, so `src/api/api.js`
and `src/api/parser.js` are now `api.ts` and `parser.ts`. Same code, typed.

Line numbers below refer to those two commits.

## How the transport works (what gpwc already does for us)

* `Client(cookies_txt, account_index=N)` loads the Netscape cookie jar, GETs
  `https://photos.google.com/` (or `/u/N/`) and reads `window.WIZ_global_data` from
  `<script data-id="_gd">` (`client.py` L53-65). That dict supplies `f.sid` (`FdrFJe`),
  `bl` (`cfb2h`), the CSRF token `at` (`SNlM0e`) and the RPC path prefix (`Im6cmf`).
* Every call is `POST https://photos.google.com{Im6cmf}/data/batchexecute?rpcids=…&source-path=…&f.sid=…&bl=…&rt=c`
  with body `f.req=[[[rpcid, json(data), null, payload_id], …]]&at=<token>` (`client.py` L83-110).
  Several payloads can share one POST: `client.send_api_request([p1, p2])`.
* The response is line-delimited; lines containing `wrb.fr` are JSON envelopes whose
  `[0][2]` is a JSON *string* holding the RPC result (`client.py` L112-134, GPTK `api.ts` L87-99).
* An expired session never reaches the envelope: the initial GET is redirected to
  `accounts.google.com` (no `_gd` script → gpwc raises `IndexError`), and a
  `batchexecute` POST comes back as an HTML login page / 401 with no `wrb.fr` lines
  (gpwc then returns an empty list). photos-mcp classifies these per README "Errors":
  only a sign-in or `google.com/photos/about` redirect (or an account mismatch) ends a
  generation; a page without `_gd` or HTML from batchexecute is `upstream_error`; a bare
  401 is `session_expired` but the generation is kept.

## Tool → RPC table

| MCP tool | RPC id | request `data` (exactly as sent) | exists in | response fields used |
|---|---|---|---|---|
| `list_library` | `lcxiM` | `[page_id, null, page_size, null, 1, 1]` — last element is the source: `1` library only, `2` archive, `3` both. We send `1`. | gpwc `payloads.GetLibraryPageByTakenDate` (`payloads.py` L26-45); GPTK `getItemsByTakenDate` (`api.ts` L114-136) | `data[0]` items (newest-first by taken date), `data[1]` next page id, `data[2]` last item timestamp (`parser.py` L68-80, `parser.ts` L61-67) |
| `list_library` / `search` / `get_album` — filenames | `EWgK9e` | `[[[[[k1], [k2], …]], [[null×24, [], null×10, []]]]]` (i.e. `[[[keys], [flags]]]` with `keys = [[k1], [k2], …]`) | gpwc `payloads.GetBatchMediaInfo` (`payloads.py` L142-160); GPTK `getBatchMediaInfo` (`api.ts` L657-675) | `data[0][1]` = list of `[media_key, [_, _, description, filename, _, _, taken_ts, tz_offset, upload_ts, size_bytes, …]]` (`parser.py` L521-545, `parser.ts` L222-235). List/search/album items carry no filename, so each page costs one extra POST with the page's keys. GPTK sends up to 5000 keys per call (`api-utils-default-presets.ts`); we send ≤ 50 per call and cache results per key; a failure leaves `filename` null. |
| `search` | `EzkLib` | `[query, null, page_id]` | gpwc `payloads.GetSearchPage` (`payloads.py` L66-82); GPTK `search` (`api.ts` L154-169) | `data[0]` items, `data[1]` next page id (`parser.py` L83-93). Items have the same layout as `lcxiM` items (`parser.ts` uses one `libraryItemParse` for both). This is Google's own search (people / places / things / text); no client-side filtering. |
| `list_albums` | `Z5xsfc` | `[page_id, null, null, null, 1, null, null, 100, [2], 5]` | gpwc `payloads.GetAlbumsPage` (`payloads.py` L328-344); GPTK `getAlbums` (`api.ts` L275-290) | `data[0]` albums, `data[1]` next page id. Album: `[0]` album key, `[1][0]` cover thumbnail base URL, `[-1]["72930366"][1]` title, `…[3]` item count, `…[4]` is shared, `…[5]` auth key (`parser.ts` L108-121, `parser.py` L241-268). `list_albums` follows `next page id` until exhausted (100 albums per POST). |
| `get_album` | `snAcKc` | `[album_key, page_id, null, auth_key]` | gpwc `payloads.GetAlbumPage` (`payloads.py` L347-364); GPTK `getAlbumPage` (`api.ts` L292-308) | `data[1]` items, `data[2]` next page id, `data[3]` album meta: `[3][1]` title, `[3][21]` item count, `[3][19]` auth key (`parser.py` L425-453, `parser.ts` L197-213). Album items share the list layout (`albumItemParse`, `parser.ts` L147-161). |
| `get_media_info` | `fDcn4b` **and** `VrseUb`, batched in **one** POST | `fDcn4b`: `[media_key, 1, null, null, 1]` · `VrseUb`: `[media_key, null, null, null, null]` | gpwc `payloads.GetItemInfoExt` (`payloads.py` L123-139) and `payloads.GetItemInfo` (L103-120); GPTK `getItemInfoExt` (`api.ts` L639-655) and `getItemInfo` (L621-637) | see the field map below |
| `get_media` | `VrseUb` → thumbnail base URL → plain `GET` on that URL with a size suffix | `[media_key, null, null, null, null]` | gpwc `GetItemInfo`; thumbnail URL at `data[12]` (`parser.py` L218, `parser.ts` L324) | see "Thumbnails and downloads" below |
| `download_media` | `VrseUb` + `fDcn4b` (one POST) → plain `GET` on the download URL | as above | gpwc `GetItemInfo` gives `data[7]` = original-file download URL and `data[1]` = download URL (`parser.py` L206-207, `parser.ts` L310-311); `fDcn4b` `data[0][2]` gives the filename to save as | see below |
| `get_storage_quota` | `EzwWhf` | `[]` | gpwc `payloads.GetStorageQuota` (`payloads.py` L429-443); GPTK `getStorageQuota` (`api.ts` L451-462) | `data[6][0]` total used bytes, `data[6][1]` total available bytes, `data[6][3]` bytes used by Photos (`parser.py` L548-560, `parser.ts` L342-348) |

Nothing has to be ported from GPTK: every RPC we need is already a gpwc payload class.
What we do take from GPTK: the extension-object lookup for `VrseUb` (`parser.ts`
L295-300), the `cameraInfo` fallback path (`parser.ts` L325), the `authKey` /
`takesUpSpace` fields gpwc does not read, and the thumbnail-URL sizing mechanism
(`parser.ts` L30-34, `filters.ts` L366).

### Request budget

| tool | POSTs to `batchexecute` | other requests |
|---|---|---|
| `list_library`, `search`, `get_album` | 2 per page (list + `EWgK9e` filenames) | — |
| `list_albums` | 1 per 100 albums | — |
| `photos_get` | 1 for all ids (`fDcn4b` + `VrseUb` per id) | — |
| `get_media` | 1 (skipped on cache hit) | 1 image GET (skipped on cache hit) |
| `download_media` | 1 | 1 file GET |
| `get_storage_quota` | 1 | — |

Plus one GET of `photos.google.com` per session generation (and after a stale token).
RPCs and the app page go one at a time, ≥ 350 ms apart plus jitter; images up to 4 at a
time, starts ≥ 100 ms apart; ≤ 2 retries on 5xx and network errors only; a 429 trips the
breaker (`blocked`).

## Item shape (identical for `list_library`, `search`, `get_album`)

Raw list item = `[media_key, [thumb_url, width, height, …], taken_ts_ms, dedup_key, tz_offset_ms, upload_ts_ms, …, {extensions}]`
(`parser.py` L40-65 `LibraryItem.from_data`, `parser.ts` L36-59).

| output field | source |
|---|---|
| `media_key` | `item[0]` |
| `filename` | `EWgK9e` row for that key, `[1][3]` |
| `taken_at` | `item[2]` (epoch ms) rendered as ISO 8601 in the offset `item[4]` (ms east of UTC; fixtures show `-18000000` = −05:00, `10800000` = +03:00) |
| `type` | `video` when the trailing extension dict has key `"76647426"` (video duration ms at `[0]`), else `photo` (`parser.py` L60, `parser.ts` L49). Live photos (`"146008172"`) stay `photo`. |
| `tz_known` | whether `item[4]` is a usable offset; without one `taken_at` is the instant in UTC with `Z` |
| `place` | ext `"129168200"[1][4][0][1][0][0]`. `lcxiM` records carry it; `EzkLib` (search) records carry no `"129168200"` extension in any fixture, so `place` is null on search. |
| `archived` | `item[13]` when it is a bool (`EzkLib`); live `lcxiM` records are 10 long, so the listing source decides (library false, archive true); `snAcKc` records are 10 long, so null there |
| `width`, `height` | `item[1][1]`, `item[1][2]` |
| `next_page_token` | `lcxiM`/`EzkLib`: `data[1]`; `snAcKc`: `data[2]` (empty string = last page in the `snAcKc` fixture) |

`limit` is honoured by slicing. `lcxiM` accepts a page size, `EzkLib` and `snAcKc` do not
(the server picks ~100 / whole album page), so a page may hold more than `limit`
items. photos-mcp always asks `lcxiM` (and `Z5xsfc`) for 100, so cursor recovery covers
the same 500 items whatever the agent's `limit`. The token we hand back encodes *(server page token that produced this page,
offset)*; the sliced remainder is served from a small in-memory page cache, so no
item is skipped and no extra POST is normally needed.

## `get_media_info` field map

`fDcn4b` result (`data[0]`, `parser.ts` L237-292, `parser.py` L343-393; fixture `fDcn4b.json`):

| output | path |
|---|---|
| `filename` | `[0][2]` |
| `description` | `[0][1]` |
| `taken_at` | `[0][3]` ms + `[0][4]` tz offset ms |
| `size_bytes` | `[0][5]` |
| `width`, `height` | `[0][6]`, `[0][7]` |
| `location.lat`, `location.lng` | `[0][9][0]` or `[0][13][0]` — a pair of E7 integers (`407696000, -739964000` in the fixture = 40.7696, −73.9964, the album is titled "ny"). We divide by 1e7. Neither upstream converts; GPTK types them as plain numbers. |
| `location.name` | `[0][13][2][0][1][0][0]` |
| `albums[]` | `[0][19]` list of album records, same layout as `Z5xsfc` albums (`albumParse`) → `album_key`, `title`, `item_count` |
| `camera` | `[0][23]` — GPTK exposes it as `cameraInfo: unknown`; gpwc as `camera_info: str | int | None`. It is `null` in the only recorded fixture (a video), so its internal layout is **not documented anywhere we read**. photos-mcp returns its readable strings (at most 6 of ≤ 80 chars, deduplicated) joined by spaces, or null (`client._camera`). |
| `source` | `[0][27][0]` mapped `1 mobile, 2 web, 3 shared, 4 partnerShared, 7 drive, 8 pc, 11 gmail`; `[0][27][1][2]` mapped `1 android, 3 ios` (`parser.ts` L240-257) |
| `takes_up_space`, `space_used_bytes`, `is_original_quality` | `[0][30][0] == 1`, `[0][30][1]`, `[0][30][2] == 2` |

`VrseUb` result (`parser.ts` L294-327; fixtures `VrseUb.json`, `VrseUb_short.json`):

| output | path |
|---|---|
| `uploaded_at` | `[0][5]` (creation timestamp, ms) — not present in `fDcn4b`, which is why both RPCs are needed |
| `is_favorite` | ext `"163238866"[0]` |
| `is_archived` | `[0][13]` |
| `duration_ms` (videos) | ext `"76647426"[0]` |
| `camera` (fallback) | `[0][1][8][4]` (`parser.ts` L325) when `fDcn4b` `[0][23]` is null |
| `thumbnail_url` | `[12]` — the base URL used by `get_media` |
| `download_original_url`, `download_url` | `[7]`, `[1]` — used by `download_media` |

The extension dict inside `[0]` sits at index 15 for library items but at index 9 for
items reached through an album (`parser.ts` L296-300, both fixtures). GPTK finds it by
looking for the known keys; gpwc hard-codes 15 and misses the album case. We do it
GPTK's way.

## Thumbnails and downloads (how `get_media` gets bytes for a media key)

1. `VrseUb` for the key → `data[12]` is a `photos.fife.usercontent.google.com/pw/…`
   base URL (`parser.ts` L324). List/search/album items carry the same kind of URL at
   `item[1][0]`, but `get_media` takes a bare key, so it asks `VrseUb`.
2. Append a size suffix and GET it with the same cookie jar. GPTK documents the
   suffix in `parser.ts` L30-34: *"Add `=w417-h174-k-no?authuser=0` to thumbnail URL to
   set custom size, remove 'video' watermark, remove auth requirement"*, and its
   similarity filter fetches `item.thumb + "=h${imageHeight}"` with
   `credentials: 'include'` (`filters.ts` L366-371). We request
   `{thumb}=w{max_px}-h{max_px}-k-no`, then Pillow re-encodes to JPEG q80 with the
   longest side ≤ `max_px` regardless of what the server returned.
3. For a video the same base URL yields the poster frame (GPTK hashes video thumbs the
   same way); the tool adds a text note that the item is a video with its duration.
4. Cache: `$PHOTOS_STATE_DIR/cache/<id>/<px>.jpg` (+ `<px>.json`). A cache hit costs zero requests.

`download_media` uses `VrseUb` `data[7]` (original) and falls back to `data[1]`
(`parser.ts` L310-311 `downloadOriginalUrl` / `downloadUrl`). In the recorded video
fixture only `[1]` is present, which is why the fallback exists. The file is streamed
to `dest_dir/<filename from fDcn4b>`.

Two other download routes exist and are deliberately **not** used:

* `yCLA7` (`GetDownloadToken`) + `dnv2s` (`CheckDownloadToken`, polled until status 4)
  produce a **zip** (`file_0.zip` in `dnv2s.json`) — the bulk-export path.
* `pLFTfd` — gpwc calls it `GetUnsupportedVideoDownloadUrl` with `[keys, null, null, 1]`
  (`payloads.py` L539-555); GPTK calls it `getDownloadUrl` with `[keys, null, auth_key]`
  (`api.ts` L464-474). The two sources disagree on the payload, so it stays out of v1.

## Encoding quirks observed in the recorded fixtures

* **Sparse tails are objects keyed by 1-based field number.** `snAcKc.json` items end
  with `{"15": 53578, "76647426": […]}` at index 9 while `lcxiM.json` items carry the
  same value positionally at index 14; `VrseUb.json` is a 5-element array whose last
  element is `{"11": "", "13": "<thumb URL>"}` where GPTK reads the description at
  `[10]` and the thumbnail at `[12]`. The parser therefore reads index *i* positionally
  and, when the array is shorter, from the trailing object under key `str(i + 1)`.
* Timestamps and timezone offsets are milliseconds. Coordinates are E7 integers.
* `Z5xsfc` returns `next page id` as a string; `snAcKc` returns `""` on the last page;
  `nMFwOc`/`zy0IHe` return `null`. All are treated as "no more pages".

## Uploads

Neither gpwc nor GPTK uploads (gpwc has the hash check only). photos-mcp's upload follows what
photos.google.com's own uploader sent for "Import photos from your computer", recorded from the web
app (2026-10, build `boq_photosuiserver_20261019`) with a small synthetic PNG; the code is
`src/photos_mcp/upload.py` and `GoogleTransport.upload_bytes`.

1. **Already there?** `swbisb` (gpwc `payloads.GetRemoteMatchesByHash`, `payloads.py` L85-100;
   GPTK `getRemoteMatchesByHash`, `api.ts` L171-186) with `[[sha1_base64], null, 3, 0]`, the file's
   SHA-1 in standard base64 with padding. The answer is `[]` when no item has it, else
   `[[ [hash, [media_key, [thumb_url, width, height, …], taken_ts, dedup_key, tz_offset_ms,
   uploaded_ts, …]], … ]]` (gpwc `parser.RemoteMatch`, `parser.py` L137-163). The web app uploads
   nothing for a match, and neither does photos-mcp.
2. **Start a resumable upload:** `POST https://photos.google.com/_/upload/uploadmedia/interactive?authuser=0`,
   no `at` token, with the app's XHR headers (the `rpc` template without `x-same-domain`) and

   | header | value |
   |---|---|
   | `x-goog-hash` | `sha1=<sha1_base64>` |
   | `x-goog-upload-protocol` | `resumable` |
   | `x-goog-upload-file-name` | the file name (photos-mcp sends `_` for characters outside printable ASCII) |
   | `x-goog-upload-header-content-length` | the file's size in bytes |
   | `x-goog-upload-command` | `start` |
   | `content-type` | `application/x-www-form-urlencoded;charset=UTF-8` (whatever the file is) |

   The body is a 13-byte protobuf for the recorded 468-byte file: `08 02 10 01 18 01 20 01 38 d4 03
   40 01`, i.e. fields 1=2, 2=1, 3=1, 4=1, 8=1 (meaning unknown) and field 7 = the size as a varint.
   The answer is `200` with `x-goog-upload-status: active`, `x-goog-upload-url` (the same path with
   `upload_id` and `upload_protocol=resumable` added) and `x-goog-upload-chunk-granularity: 262144`.
3. **Send the bytes** to that address: `POST` with `x-goog-hash` and `x-goog-upload-file-name` as
   above, `x-goog-upload-offset: <bytes sent so far>`, `x-goog-upload-command: upload` for a chunk
   and `upload, finalize` for the last (the web app sent the small file in one), `content-type:
   application/x-www-form-urlencoded;charset=utf-8`, the chunk as the body. photos-mcp sends 16 MiB
   chunks (a multiple of the granularity). Each chunk answers `x-goog-upload-status: active`; the
   last answers `final` with the **upload token** as the body (base64 text, ~490 characters for
   the recorded file). Nothing is in the library yet.
4. **Make the item:** `mdpdU` (batchexecute) with `[[[upload_token, file_name, modified_ms, 2, 2]]]`,
   where `modified_ms` is the file's last-modified time (the app sends `File.lastModified`; Google
   uses the file's own EXIF date when it has one) and the two trailing `2`s are the app's (meaning
   unknown). The answer is `[[[upload_token, record, 0]]]` with `record` in the `swbisb` shape:
   `record[0]` is the new media key. This is the change: it goes through `GoogleTransport.write`
   and is never retried after it may have reached Google.

Album placement is a separate change (`E1Cajb` / `laUYf`, as for `photos_album_add`). After an
upload the app also refreshed its views (`rJ0tlb`, `dIx4uf`, `Z5xsfc`, `EzwWhf`, …); photos-mcp
only drops its cached pages.

## Deleting

* **Trash:** `XwAOJf` with `[null, 1, [dedup_key, …], 3]` moves items to the trash and
  `[null, 3, [dedup_key, …], 2]` brings them back (gpwc `payloads.MoveToTrash` /
  `RestoreFromTrash`, `payloads.py` L163-180; GPTK `moveItemsToTrash` / `restoreFromTrash`,
  `api.ts` L235-257). They take **dedup keys**, not media keys: photos-mcp reads them with
  `VrseUb` (`data[0][3]`) for the preview, and from the trash listing for a restore. The trash
  is `zy0IHe` with `[page_id]`, answering `[items, next_page_id]` with items in the library-page
  layout (gpwc `GetTrashPage`, `payloads.py` L310-325, `parser.TrashPage`; GPTK
  `getTrashItems`, `api.ts` L203-217), most recently deleted first. Google keeps trashed items
  60 days; emptying the trash or deleting from it is not built.
* **Album:** `nV6Qv` with `[null, null, [[album_key, null, 1]]]` answers `[]` and deletes the
  album; its photos stay in the library. Neither source has it: recorded from photos.google.com's
  "Delete album" (the album's "More options" menu, then "Delete"; 2026-10). The app then reloaded
  the albums list (`Z5xsfc`). Afterwards `snAcKc` for that album answers without data.

## Not available from the web API (as far as these sources go)

* **Cover media key of an album.** `Z5xsfc` and `snAcKc` expose only the cover's
  thumbnail URL (`[1][0]`), never a media key. `list_albums` returns
  `cover_thumbnail_url` instead of `cover_media_key`.
* **Structured EXIF** (aperture, exposure, ISO, focal length). `fDcn4b` `[0][23]` is
  the only camera field either project reads, and its layout is undocumented; it is
  passed through raw.
* **Original bytes via the thumbnail URL** (`=d`): not used by either project; not used here.
