"""Trash RPC payload builders (gpwc ``payloads.py`` at 651aa268, ``MoveToTrash`` and ``RestoreFromTrash``; GPTK
``moveItemsToTrash`` / ``restoreFromTrash`` send the same). Both take the items' dedup keys, not their media keys.
Google keeps trashed items 60 days; nothing here empties the trash or deletes for good.
"""

from .payloads import Payload


class MoveToTrash(Payload):
    def __init__(self, dedup_keys: list[str]):
        super().__init__()
        self.rpcid = "XwAOJf"
        self.data = [None, 1, dedup_keys, 3]


class RestoreFromTrash(Payload):
    def __init__(self, dedup_keys: list[str]):
        super().__init__()
        self.rpcid = "XwAOJf"
        self.data = [None, 3, dedup_keys, 2]
