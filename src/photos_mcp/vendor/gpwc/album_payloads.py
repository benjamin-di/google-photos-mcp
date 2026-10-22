"""Album-change RPC payload builders (gpwc ``payloads.py`` at 651aa268 and Google-Photos-Toolkit ``api.ts``; see
README.md). Only these changes exist in photos-mcp: create an album, add items to an album or a shared album, remove
items from an album or a shared album. Nothing deletes, trashes, archives or edits media, and nothing shares.
"""

from .payloads import Payload


class CreateAlbum(Payload):
    def __init__(self, name: str):
        super().__init__()
        self.rpcid = "OXvT9d"
        self.data = [name, None, 2]


class AddItemsToExistingAlbum(Payload):
    def __init__(self, item_media_keys: list[str], album_media_key: str):
        super().__init__()
        self.rpcid = "E1Cajb"
        self.data = [item_media_keys, album_media_key]


class AddItemsToExistingSharedAlbum(Payload):
    def __init__(self, item_media_keys: list[str], album_media_key: str):
        super().__init__()
        self.rpcid = "laUYf"
        self.data = [album_media_key, [2, None, [[[key]] for key in item_media_keys], None, None, None, [1]]]


class RemoveItemsFromAlbum(Payload):
    """``album_item_media_keys`` are the keys the items have in that album's own listing: the RPC names no album."""

    def __init__(self, album_item_media_keys: list[str]):
        super().__init__()
        self.rpcid = "ycV3Nd"
        self.data = [album_item_media_keys]


class RemoveItemsFromSharedAlbum(Payload):
    def __init__(self, album_media_key: str, item_media_keys: list[str]):
        super().__init__()
        self.rpcid = "LjmOue"
        self.data = [
            [album_media_key],
            [item_media_keys],
            [[None, None, None, [None, [], []], None, None, None, None, None, None, None, None, None, []]],
        ]
