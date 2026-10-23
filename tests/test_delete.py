"""Deleting: items go to the trash (never emptied) and come back, an album is deleted (its photos stay); each sends
exactly the recorded RPCs in one call (the client asks the person first), is read back, and counts against the write
budget and the audit log."""

from __future__ import annotations

import pytest
from test_albums import ALBUM, SHARED, Albums, audit, key

from photos_mcp.cache import ThumbCache
from photos_mcp.client import PhotosService
from photos_mcp.cursors import CursorStore
from photos_mcp.errors import PhotosError
from photos_mcp.transport import RpcResult
from photos_mcp.vendor.gpwc import album_payloads, trash_payloads


def dedup(k: str) -> str:
    return f"fake-dedup-{k}"


def record(k: str) -> list:
    return [k, ["https://photos.fife.usercontent.google.com/pw/FAKE", 100, 100], 1_700_000_000_000, dedup(k), 0, 0]


class Library(Albums):
    """Albums, plus a library of items Google knows by key (VrseUb) and a trash it moves them to and back."""

    def __init__(self, state):
        super().__init__(state)
        self.library = {key(n) for n in range(1, 10)}
        self.binned: list[str] = []
        self.refuse = False

    def rpc(self, payloads_):
        if payloads_[0].rpcid == "VrseUb":
            out = []
            for index, payload in enumerate(payloads_, start=1):
                payload.payload_id = "generic" if len(payloads_) == 1 else str(index)
                k = payload.data[0]
                if k in self.library or k in self.binned:
                    out.append(RpcResult("VrseUb", payload.payload_id, [record(k)]))
            return out
        if payloads_[0].rpcid == "zy0IHe":
            payloads_[0].payload_id = "generic"
            return [RpcResult("zy0IHe", "generic", [[record(k) for k in reversed(self.binned)], None])]
        if payloads_[0].rpcid == "snAcKc" and payloads_[0].data[0] not in self.albums:
            payloads_[0].payload_id = "generic"
            return []  # a deleted album: Google answers without data
        return super().rpc(payloads_)

    def write(self, payloads_):
        rpcid = payloads_[0].rpcid
        if rpcid not in ("XwAOJf", "nV6Qv"):
            return super().write(payloads_)
        if self.fail_write:
            raise self.fail_write
        self.writes.append([(p.rpcid, p.data) for p in payloads_])
        payloads_[0].payload_id = "generic"
        if self.refuse:
            return []
        if rpcid == "nV6Qv":
            for album, _, _ in payloads_[0].data[2]:
                self.albums.pop(album, None)
                self.titles.pop(album, None)
            return [RpcResult(rpcid, "generic", [])]
        _, direction, dedups, _ = payloads_[0].data
        by_dedup = {dedup(k): k for k in self.library | set(self.binned)}
        for d in dedups:
            k = by_dedup[d]
            if direction == 1 and k in self.library:
                self.library.discard(k)
                self.binned.append(k)
            elif direction == 3 and k in self.binned:
                self.binned.remove(k)
                self.library.add(k)
        return [RpcResult(rpcid, "generic", [])]


@pytest.fixture
def google(state) -> Library:
    return Library(state)


@pytest.fixture
def svc(google, tmp_path, clock) -> PhotosService:
    return PhotosService(google, ThumbCache(tmp_path / "cache", 1024 * 1024), CursorStore(), clock=clock)


def test_the_builders_send_what_the_web_app_sends():
    assert trash_payloads.MoveToTrash(["d1"]).data == [None, 1, ["d1"], 3]
    assert trash_payloads.RestoreFromTrash(["d1"]).data == [None, 3, ["d1"], 2]
    assert album_payloads.DeleteAlbum(["AF1QipFAKEALBUM"]).data == [None, None, [["AF1QipFAKEALBUM", None, 1]]]
    assert album_payloads.DeleteAlbum([]).rpcid == "nV6Qv"


def test_deleting_moves_the_library_items_to_the_trash(svc, google):
    unknown = "AF1QipFAKEUNKNOWN" + "0" * 20
    done = svc.trash([key(1), unknown, key(2)])
    assert google.writes == [[("XwAOJf", [None, 1, [dedup(key(1)), dedup(key(2))], 3])]]
    assert done["trashed"] == 2 and done["not_found"] == [unknown] and all(i["in_trash"] for i in done["items"])
    assert done["items"][0]["url"] == f"https://photos.google.com/photo/{key(1)}"
    assert google.binned == [key(1), key(2)]
    assert [(a["action"], a["items"], a["outcome"]) for a in audit(svc)] == [("trash", 2, "sent")]
    assert svc.status()["budgets"]["write_left"] == 500 - 2


def test_ids_outside_the_library_are_refused_and_the_budget_stops_it(svc, google):
    with pytest.raises(PhotosError) as caught:
        svc.trash(["AF1QipFAKENOWHERE" + "0" * 20])
    assert caught.value.code == "invalid_argument" and google.writes == []
    svc.state.write_budget = 1
    with pytest.raises(PhotosError) as caught:
        svc.trash([key(3), key(4)])
    assert caught.value.code == "budget_exhausted" and google.writes == []


def test_the_trash_is_listed_and_items_come_back(svc, google):
    svc.trash([key(4), key(5)])
    listed = svc.list("trash")
    assert [i["id"] for i in listed["items"]] == [key(5), key(4)]  # most recently deleted first
    back = svc.restore([key(4), key(6)])
    assert google.writes[-1] == [("XwAOJf", [None, 3, [dedup(key(4))], 2])]
    assert back["restored"] == 1 and back["not_in_trash"] == [key(6)] and back["items"][0]["restored"] is True
    assert google.binned == [key(5)] and key(4) in google.library
    with pytest.raises(PhotosError) as caught:
        svc.restore([key(6)])
    assert caught.value.code == "invalid_argument"


def test_google_refusing_and_an_unknown_outcome_are_reported(svc, google):
    google.refuse = True
    with pytest.raises(PhotosError) as caught:
        svc.trash([key(7)])
    assert caught.value.code == "refused"
    google.refuse = False
    google.fail_write = PhotosError("outcome_unknown")
    with pytest.raises(PhotosError) as caught:
        svc.trash([key(7)])
    assert caught.value.code == "outcome_unknown"
    assert [a["outcome"] for a in audit(svc)] == ["refused", "outcome_unknown"]


def test_an_album_is_deleted_and_its_photos_stay(svc, google):
    done = svc.album_delete(ALBUM)
    assert google.writes == [[("nV6Qv", [None, None, [[ALBUM, None, 1]]])]]
    assert done["deleted"] is True and done["album"] == {"id": ALBUM, "title": "Trip", "count": 5}
    assert google.library >= {key(n) for n in range(1, 6)}  # the album's photos are still in the library
    assert [(a["action"], a["album"]) for a in audit(svc)] == [("delete_album", ALBUM)]
    with pytest.raises(PhotosError) as caught:
        svc.album_delete(ALBUM)  # gone
    assert caught.value.code == "item_unavailable"


def test_a_shared_album_is_left_to_the_app(svc, google):
    with pytest.raises(PhotosError) as caught:
        svc.album_delete(SHARED)
    assert caught.value.code == "invalid_argument" and "shared album" in caught.value.detail
    assert google.writes == []
