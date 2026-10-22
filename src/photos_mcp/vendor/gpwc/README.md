# Vendored gpwc

Source: [xob0t/google_photos_web_client](https://github.com/xob0t/google_photos_web_client)
at commit `651aa268d50529cb6128c8a42c9cd4ce668f2189`, `src/gpwc/`. MIT licence, see
`LICENCE` (copyright 2024 xob0t).

What is kept: the `batchexecute` request framing (`Client.prepare_payload`, the
`f.req` / `at` body and the query string), `parse_main_page` and the read-only payload
builders photos-mcp uses.

Modifications for photos-mcp (2026-10-14):

- **Multi-envelope fix.** `parse_api_response` reads every `wrb.fr` entry of every chunk
  line. Upstream read only `response[0]`, so a chunk carrying two envelopes lost the
  second one.
- **No network I/O here.** `Client` builds requests and parses responses; sending goes
  through `photos_mcp.transport`, which uses `curl_cffi` (Chrome impersonation) with
  pacing, budgets, host and RPC allowlists and manual redirects. `requests` and `lxml`
  are no longer dependencies: `_gd` is read with the standard library's `HTMLParser`.
- **Read-only subset of payloads.** Only `GetLibraryPageByTakenDate` (`lcxiM`),
  `GetSearchPage` (`EzkLib`), `GetBatchMediaInfo` (`EWgK9e`), `GetAlbumsPage` (`Z5xsfc`),
  `GetAlbumPage` (`snAcKc`), `GetItemInfo` (`VrseUb`), `GetItemInfoExt` (`fDcn4b`) and
  `GetStorageQuota` (`EzwWhf`) remain, plus (photos-mcp 0.5, from the same gpwc commit)
  `GetLibraryPageByUploadedDate` and `GetFavoritesPage`, two filters of the search RPC `EzkLib`. Every write builder (trash, albums, favourites,
  sharing, archive, uploads, ...) was removed, as were `parser.py` and the
  `parse_response` / `execute` helpers; photos-mcp parses responses in `photos_mcp.rpc`.
- **Payload ids** follow the web app: `"generic"` for a single RPC, `"1"`, `"2"`, ... in
  a batch (upstream used random hex ids).

Album changes (photos-mcp 0.6): `album_payloads.py` holds the only write builders, from the same gpwc commit
(`CreateAlbum` `OXvT9d`, `AddItemsToExistingAlbum` `E1Cajb`, `AddItemsToExistingSharedAlbum` `laUYf`,
`RemoveItemsFromAlbum` `ycV3Nd`) and Google-Photos-Toolkit's `removeItemsFromSharedAlbum` (`LjmOue`). The transport
sends them only through `GoogleTransport.write`, whose allowlist is exactly these five RPC ids.
