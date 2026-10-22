"""Album changes: create, add and remove send exactly the album RPCs, read the outcome back from the album, count
against the daily write budget and the audit log; a removal is previewed first and needs its single-use token; the
transport never repeats a change that may have reached Google."""

from __future__ import annotations

import json

import pytest
from conftest import BATCH, PAGE, FakeTransport, all_fixtures, app_page, make_transport, status

from photos_mcp.cache import ThumbCache
from photos_mcp.client import CONFIRM_TTL, PhotosService
from photos_mcp.cursors import CursorStore
from photos_mcp.errors import PhotosError
from photos_mcp.vendor.gpwc import album_payloads, payloads

ALBUM, SHARED = "AF1QipALBUM" + "0" * 30, "AF1QipSHARED" + "0" * 29


def key(n: int) -> str:
    return f"AF1QipITEM{n:04d}" + "0" * 30


def record(k: str) -> list:
    return [k, ["https://photos.fife.usercontent.google.com/pw/FAKE", 100, 100], 1_700_000_000_000, "d", 0, 0]


class Albums(FakeTransport):
    """Two albums Google keeps: a private one and a shared one, pages of 3 items."""

    def __init__(self, state):
        super().__init__(state, all_fixtures())
        self.albums: dict[str, list[str]] = {ALBUM: [key(n) for n in range(1, 6)], SHARED: [key(9)]}
        self.titles = {ALBUM: "Trip", SHARED: "Family"}
        self.writes: list[list[tuple[str, list]]] = []
        self.fail_write: PhotosError | None = None
        self.album_lists = 0

    def rpc(self, payloads_):
        from photos_mcp.transport import RpcResult

        out = []
        for payload in payloads_:
            if payload.rpcid == "Z5xsfc":
                self.album_lists += 1
                payload.payload_id = "generic"
                records = [
                    [k, ["https://lh3.googleusercontent.com/FAKE"], {"72930366": [None, t, [], 1, k == SHARED, "AUTH"]}]
                    for k, t in self.titles.items()
                ]
                return [RpcResult("Z5xsfc", "generic", [records, None])]
            if payload.rpcid != "snAcKc":
                return super().rpc(payloads_)
            album, page_id = payload.data[0], payload.data[1]
            items = self.albums[album]
            start = int(page_id or 0)
            meta = [None] * 22
            meta[0], meta[1], meta[21] = album, self.titles[album], len(items)
            # Live, every album page has members and an auth key, shared or not: neither tells.
            meta[9], meta[19] = [["owner"]], "AUTHKEY"
            nxt = str(start + 3) if start + 3 < len(items) else None
            payload.payload_id = "generic"
            out.append(RpcResult("snAcKc", "generic", [None, [record(k) for k in items[start : start + 3]], nxt, meta]))
        return out

    def write(self, payloads_):
        from photos_mcp.transport import RpcResult

        if self.fail_write:
            raise self.fail_write
        self.writes.append([(p.rpcid, p.data) for p in payloads_])
        results = []
        for index, p in enumerate(payloads_, start=1):
            p.payload_id = "generic" if len(payloads_) == 1 else str(index)
            if p.rpcid == "OXvT9d":
                new = "AF1QipNEW" + "0" * 32
                self.albums[new], self.titles[new] = [], p.data[0]
                results.append(RpcResult(p.rpcid, p.payload_id, [[new]]))
            elif p.rpcid == "E1Cajb":
                keys, album = p.data
                self.albums[album] += [k for k in keys if k not in self.albums[album]]
                results.append(RpcResult(p.rpcid, p.payload_id, []))
            elif p.rpcid == "laUYf":
                album, keys = p.data[0], [k[0][0] for k in p.data[1][2]]
                self.albums[album] += [k for k in keys if k not in self.albums[album]]
                results.append(RpcResult(p.rpcid, p.payload_id, []))
            elif p.rpcid == "ycV3Nd":
                for album in self.albums:
                    self.albums[album] = [k for k in self.albums[album] if k not in p.data[0]]
                results.append(RpcResult(p.rpcid, p.payload_id, []))
            elif p.rpcid == "LjmOue":
                album, keys = p.data[0][0], p.data[1][0]
                self.albums[album] = [k for k in self.albums[album] if k not in keys]
                results.append(RpcResult(p.rpcid, p.payload_id, []))
        return results


@pytest.fixture
def google(state) -> Albums:
    return Albums(state)


@pytest.fixture
def svc(google, tmp_path, clock) -> PhotosService:
    return PhotosService(google, ThumbCache(tmp_path / "cache", 1024 * 1024), CursorStore(), clock=clock)


def audit(svc) -> list[dict]:
    path = svc.state.path.parent / "audit.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_create_an_album_with_items(svc, google):
    result = svc.album_create("  Picks  ", [key(1), key(2)])
    new = result["album"]["id"]
    assert google.writes[0] == [("OXvT9d", ["Picks", None, 2])]
    assert google.writes[1] == [("E1Cajb", [[key(1), key(2)], new])]
    assert result["album"] == {"id": new, "url": f"https://photos.google.com/album/{new}", "title": "Picks",
                               "count": 2, "shared": False}  # fmt: skip
    assert google.album_lists == 0  # a new album is this account's own: no lookup
    assert result["created"] and result["added"] == 2
    assert [a["action"] for a in audit(svc)] == ["create", "created", "add"]
    assert svc.status()["budgets"]["write_left"] == 500 - 3
    for bad in ("", " ", "x" * 201):
        with pytest.raises(PhotosError):
            svc.album_create(bad)


def test_add_reads_the_outcome_back_and_uses_the_shared_rpc_for_shared_albums(svc, google):
    result = svc.album_add(ALBUM, [key(1), key(7)])  # key(1) is already in it
    # Whether the album is shared came from the albums list (its page has members and an auth key either way).
    assert google.writes[-1] == [("E1Cajb", [[key(1), key(7)], ALBUM])] and google.album_lists == 1
    assert result["requested"] == 2 and result["added"] == 1 and result["album"]["count"] == 6
    shared = svc.album_add(SHARED, [key(8)])
    assert google.writes[-1] == [("laUYf", [SHARED, [2, None, [[[key(8)]]], None, None, None, [1]]])]
    assert shared["album"]["shared"] is True and shared["added"] == 1


def test_remove_is_previewed_then_confirmed_once(svc, google):
    preview = svc.album_remove(ALBUM, [key(2), key(5), key(8)])
    assert preview["preview"] is True and google.writes == []
    assert [i["id"] for i in preview["remove"]] == [key(2), key(5)]  # key(5) is on the album's second page
    assert preview["not_in_album"] == [key(8)]
    token = preview["confirm_token"]
    # The token is for exactly this album and these ids.
    for album, ids in ((SHARED, [key(2), key(5), key(8)]), (ALBUM, [key(2), key(5)])):
        with pytest.raises(PhotosError) as caught:
            svc.album_remove(album, ids, confirm=token)
        assert caught.value.code == "confirm_invalid"
    preview = svc.album_remove(ALBUM, [key(2), key(5), key(8)])  # a wrong use spent the old token
    done = svc.album_remove(ALBUM, [key(8), key(5), key(2)], confirm=preview["confirm_token"])
    assert google.writes == [[("ycV3Nd", [[key(2), key(5)]])]]  # only what the preview found in this album
    assert done["removed"] == 2 and done["album"]["count"] == 3
    with pytest.raises(PhotosError) as caught:
        svc.album_remove(ALBUM, [key(2), key(5), key(8)], confirm=preview["confirm_token"])
    assert caught.value.code == "confirm_invalid"  # single use


def test_a_token_expires(svc, clock):
    token = svc.album_remove(ALBUM, [key(1)])["confirm_token"]
    clock.now += CONFIRM_TTL
    with pytest.raises(PhotosError) as caught:
        svc.album_remove(ALBUM, [key(1)], confirm=token)
    assert caught.value.code == "confirm_invalid"


def test_removing_from_a_shared_album_names_the_album(svc, google):
    token = svc.album_remove(SHARED, [key(9)])["confirm_token"]
    svc.album_remove(SHARED, [key(9)], confirm=token)
    assert google.writes[-1][0][0] == "LjmOue" and google.writes[-1][0][1][:2] == [[SHARED], [[key(9)]]]


def test_ids_not_in_the_album_are_never_removed(svc, google):
    with pytest.raises(PhotosError) as caught:
        svc.album_remove(ALBUM, [key(8)])
    assert caught.value.code == "invalid_argument" and google.writes == []


def test_the_write_budget_stops_changes_before_they_are_sent(svc, google):
    svc.state.set_budgets(rpc_budget=3000, thumb_budget=2000)
    svc.state.write_budget = 1
    with pytest.raises(PhotosError) as caught:
        svc.album_add(ALBUM, [key(7), key(8)])
    assert caught.value.code == "budget_exhausted" and google.writes == []


def test_an_unknown_outcome_is_logged_and_never_repeated(svc, google):
    google.fail_write = PhotosError("outcome_unknown")
    with pytest.raises(PhotosError) as caught:
        svc.album_create("Picks")
    assert caught.value.code == "outcome_unknown"
    assert audit(svc)[-1] == {"at": audit(svc)[-1]["at"], "action": "create", "album": None, "items": 0,
                              "outcome": "outcome_unknown"}  # fmt: skip


# -- the transport ----------------------------------------------------------------------------------------------------


def test_the_transport_keeps_reads_and_changes_apart(tmp_path, clock):
    transport, _ = make_transport(tmp_path, {PAGE: [status(200)]}, clock)
    with pytest.raises(RuntimeError):
        transport.write([payloads.GetSearchPage("x")])
    with pytest.raises(RuntimeError):
        transport.rpc([album_payloads.CreateAlbum("x")])


def test_a_change_is_never_retried_after_a_server_error(tmp_path, clock):
    from conftest import ok

    transport, sender = make_transport(
        tmp_path, {PAGE: ok(app_page(), "text/html"), BATCH: [status(500), status(500), status(500)]}, clock
    )
    with pytest.raises(PhotosError) as caught:
        transport.write([album_payloads.CreateAlbum("Picks")])
    assert caught.value.code == "outcome_unknown"
    assert sender.hosts().count("POST photos.google.com/_/PhotosUi/data/batchexecute") == 1
