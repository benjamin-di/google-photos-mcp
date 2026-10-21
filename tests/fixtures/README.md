# Recorded RPC fixtures

Raw `batchexecute` results, one file per RPC id, used by the parser tests.

They are derived from the recorded fixtures in
[Google-Photos-Toolkit](https://github.com/xob0t/Google-Photos-Toolkit)
(`tests/fixtures/parser/*.json`, MIT licence) at commit `6db9ab35`, which were
already anonymised (`ANON_*` URLs, `file_N.ext` names, `TestUser`). On top of that:

* every media / album / actor key was replaced by a synthetic `AF1QipFAKE….` key,
  consistently across files (the same original key maps to the same fake key);
* dedup keys, page tokens and place ids were replaced by obvious fakes (one of GPTK's own
  public dedup keys remains, in `lcxiM.json`);
* lists were trimmed to a handful of records;
* `EWgK9e.json` is wrapped back into the RPC shape `[[null, rows]]` (GPTK stores only the
  inner row list);
* `EzwWhf.json` keeps only index 6 (the quota) and blanks the feature-flag tail.

No cookies, tokens or real identifiers are present. `parser-fixtures.sha256` pins these
files (`tests/test_fixture_privacy.py`), so any change to them is a deliberate, reviewed one.

## Session snapshot sample (`session/`)

`session/cookies.txt` and `session/meta.json` are a sample session snapshot (README
"Session snapshot"; schema 2: Chrome 149 with per-kind header templates);
`session/meta.schema1.json` is the same in schema 1 (what `photos-mcp-session import`
writes). Every value is fake. `tests/test_session.py` pins the field set, the template rules
and the host-only rules, and `tests/test_curl_sender.py` keeps its Chrome version within 4
majors of curl_cffi's newest impersonation profile.

## Recorded responses (`recorded/`)

`scripts/capture-fixtures` (`python -m photos_mcp.capture`) records real, anonymised
answers: every allowlisted RPC (library and archive pages, a search, albums, an album page,
filenames, details of a photo, a video and an archived item, the quota), the no-cookie
signed-out answer of `photos.google.com`, and a consent redirect if one occurs.
`tests/test_recorded.py` runs the parsers, the uniform item shape and the transport's
classification over every file here.

The recordings (`captured_at: "2026-10-14"`) carry no value of the account they came
from: every string is a placeholder, timestamps are synthetic minutes of January 2020 in
one order across all files, known time-zone offsets are +01:00, coordinates are a fixed
point, and sizes, dimensions, counts and quota are synthetic (7000001 and up; flags and
enums up to 64 are kept) (`capture.py`; `tests/test_fixture_privacy.py` checks every file). What they showed: search records carry no geo extension (so `place` is
null in search), library records end before the archived flag (the source decides it), and
an empty archive is `[]`. There is no archived-item recording (the library had none). The
video recordings (`VrseUb.video.json`, `fDcn4b.video.json`) come from a second capture with
`--query videos`, since the first library page held no video.

## Chrome wire order (`chrome_wire_order.json`)

Header names (no values) of Chrome 149's HTTP/2 HEADERS frames per request kind (`page`,
`rpc`, `image`), recorded with `--log-net-log` on 2026-10-14 (the app's dominant
batchexecute shape). `tests/test_session.py` checks that every request photos-mcp builds,
from schema 1 and 2, follows this order; `pragma`, `cache-control` (present only because
the recording disabled the cache), `x-client-data`, `x-goog-*` and `content-length` are in
the recording but never sent by photos-mcp.
