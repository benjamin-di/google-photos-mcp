"""JPEG transfers: one-time capability links, account binding, expiry and bounded copies."""

from io import BytesIO
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from conftest import load, make_image
from PIL import Image
from starlette.testclient import TestClient

from photos_mcp import downloads
from photos_mcp.downloads import DownloadLinks
from photos_mcp.errors import PhotosError


@pytest.fixture
def photo(transport):
    data = load("VrseUb")
    data[0][-1].pop("76647426")
    transport.responses["VrseUb"] = data
    transport.image = make_image(2800, 2100)
    return data[0][0]


def link_path(result):
    return urlsplit(result["items"][0]["url"]).path.removeprefix("/photos/downloads")


def test_real_jpeg_copy_head_then_only_one_get(service, photo, clock):
    links = DownloadLinks("https://example.com/photos", lambda _: service, clock=clock)
    result = links.create(service, [photo], 2048)
    item = result["items"][0]
    assert item["original"] is False and (item["width"], item["height"]) == (2048, 1536)
    assert "usercontent" not in str(result)
    with TestClient(links.app()) as client:
        path = link_path(result)
        assert client.head(path).status_code == 200
        assert client.head(path).headers["content-length"] == str(item["size_bytes"])
        response = client.get(path)
        assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
        assert response.headers["content-type"] == "image/jpeg"
        with Image.open(BytesIO(response.content)) as image:
            assert image.size == (2048, 1536) and not image.getexif()
        assert client.get(path).status_code == 404


def test_expiry_queries_and_methods_do_not_consume_a_valid_link(service, photo, clock):
    links = DownloadLinks("https://example.com/photos", lambda _: service, clock=clock)
    path = link_path(links.create(service, [photo], 256))
    with TestClient(links.app()) as client:
        assert client.get(path + "?x=1").status_code == 404
        assert client.put(path).status_code == 405
        assert client.head(path).status_code == 200
        clock.now += downloads.TTL
        assert client.get(path).status_code == 404


def test_removed_or_replaced_account_revokes_download(service, photo, clock):
    service.account = SimpleNamespace(id="second", created_at=100, email="second@example.com")
    resolved = []

    def service_for(account):
        resolved.append(account)
        return service

    links = DownloadLinks("https://example.com/photos", service_for, clock=clock)
    path = link_path(links.create(service, [photo], 256))
    service.account = SimpleNamespace(id="second", created_at=101, email="other@example.com")
    with TestClient(links.app()) as client:
        assert client.get(path).status_code == 404
    assert resolved == ["second"]
    service.account = None
    path = link_path(links.create(service, [photo], 256))
    links.service_for = lambda _: (_ for _ in ()).throw(PhotosError("account_not_connected"))
    with TestClient(links.app()) as client:
        assert client.get(path).status_code == 404


def test_video_is_an_error_and_never_a_poster(service, clock):
    key = load("VrseUb")[0][0]
    links = DownloadLinks("https://example.com/photos", lambda _: service, clock=clock)
    item = links.create(service, [key], 2048)["items"][0]
    assert item["error"]["code"] == "invalid_argument" and not links.links


def test_copy_memory_cap_and_expired_slots(service, photo, clock, monkeypatch):
    links = DownloadLinks("https://example.com/photos", lambda _: service, clock=clock)
    monkeypatch.setattr(downloads, "MAX_LINKS", 1)
    assert "url" in links.create(service, [photo], 256)["items"][0]
    assert links.create(service, [photo], 256)["items"][0]["error"]["code"] == "busy"
    clock.now += downloads.TTL
    assert "url" in links.create(service, [photo], 256)["items"][0]
    monkeypatch.setattr(downloads, "MAX_BYTES", 1)
    assert links.create(service, [photo], 256)["items"][0]["error"]["code"] == "busy"


def test_download_tool_is_conditional_and_returns_transfer_url(service, photo, tmp_path):
    import asyncio

    from fastmcp import Client

    from photos_mcp.config import Settings
    from photos_mcp.server import Accounts, create_server

    registry = Accounts(service)
    registry.settings = Settings(expected_email="owner@example.com", public_url="https://example.com/photos")
    server = create_server(registry)

    async def call():
        async with Client(server) as client:
            return await client.call_tool("photos_download", {"ids": [photo]})

    result = asyncio.run(call())
    import json

    assert json.loads(result.content[0].text)["items"][0]["url"].startswith("https://example.com/photos/downloads/")
