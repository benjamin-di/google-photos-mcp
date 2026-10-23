"""RPC payload builders for reads (gpwc ``payloads.py`` at 651aa268, trimmed; see README.md).

Each payload carries its RPC id and the ``data`` list exactly as the Google Photos web app
sends it. ``payload_id`` is assigned when a batch is framed (``Client.build_request``).
"""

from typing import Any, Literal


class Payload:
    rpcid: str
    data: list[Any]

    def __init__(self) -> None:
        self.payload_id: str = "generic"


class GetLibraryPageByTakenDate(Payload):
    def __init__(
        self,
        timestamp: int | None = None,
        page_id: str | None = None,
        source: Literal["library", "archive", "both"] = "both",
        page_size: int = 500,
    ):
        super().__init__()
        source_map = {"library": 1, "archive": 2, "both": 3}
        self.rpcid = "lcxiM"
        self.data = [page_id, timestamp, page_size, None, 1, source_map[source]]


class GetSearchPage(Payload):
    def __init__(self, query: str, page_id: str | None = None):
        super().__init__()
        self.rpcid = "EzkLib"
        self.data = [query, None, page_id]


class GetLibraryPageByUploadedDate(Payload):
    """Newest upload first: ``EzkLib`` with the web app's "recently added" filter."""

    def __init__(self, page_id: str | None = None):
        super().__init__()
        self.rpcid = "EzkLib"
        self.data = ["", [[4, "ra", 0, 0]], page_id]


class GetFavoritesPage(Payload):
    """The favourites: ``EzkLib`` with the web app's favourites filter."""

    def __init__(self, page_id: str | None = None):
        super().__init__()
        self.rpcid = "EzkLib"
        self.data = ["Favorites", [[5, "8", 0, 9]], page_id]


class GetRemoteMatchesByHash(Payload):
    """Which of these SHA-1 hashes (base64) the library already holds; an upload checks first, as the web app does."""

    def __init__(self, hashes: list[str]):
        super().__init__()
        self.rpcid = "swbisb"
        self.data = [hashes, None, 3, 0]


class GetTrashPage(Payload):
    """The trash, most recently deleted first: ``[items, next_page_id]`` with items in the library-page layout."""

    def __init__(self, page_id: str | None = None):
        super().__init__()
        self.rpcid = "zy0IHe"
        self.data = [page_id]


class GetItemInfo(Payload):
    def __init__(self, media_key: str, album_media_key: str | None = None, auth_key: str | None = None):
        super().__init__()
        self.rpcid = "VrseUb"
        self.data = [media_key, None, auth_key, None, album_media_key]


class GetItemInfoExt(Payload):
    def __init__(self, media_key: str, auth_key: str | None = None):
        super().__init__()
        self.rpcid = "fDcn4b"
        self.data = [media_key, 1, auth_key, None, 1]


class GetBatchMediaInfo(Payload):
    def __init__(self, media_keys: list[str]):
        super().__init__()
        keys = [[key] for key in media_keys]
        self.rpcid = "EWgK9e"
        self.data = [
            [[keys], [[*([None] * 24), [], *([None] * 10), []]]],
        ]


class GetAlbumsPage(Payload):
    def __init__(self, page_id: str | None = None, page_size: int | None = 100):
        super().__init__()
        self.rpcid = "Z5xsfc"
        self.data = [page_id, None, None, None, 1, None, None, page_size, [2], 5]


class GetAlbumPage(Payload):
    def __init__(self, media_key: str, page_id: str | None = None, auth_key: str | None = None):
        super().__init__()
        self.rpcid = "snAcKc"
        self.data = [media_key, page_id, None, auth_key]


class GetStorageQuota(Payload):
    def __init__(self) -> None:
        super().__init__()
        self.rpcid = "EzwWhf"
        self.data = []
