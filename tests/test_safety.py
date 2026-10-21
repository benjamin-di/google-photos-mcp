"""Static guard over the shipped source: nothing can write to the library, talk to another
host, or echo exception text. A new RPC, host or builder must be a deliberate change here."""

from __future__ import annotations

import ast
import re
from pathlib import Path

from photos_mcp import transport
from photos_mcp.vendor.gpwc import payloads

SRC = Path(__file__).resolve().parents[1] / "src" / "photos_mcp"
FILES = sorted(SRC.rglob("*.py"))
SOURCE = {path: path.read_text() for path in FILES}
ALL = "\n".join(SOURCE.values())

READ_RPCS = {"lcxiM", "EzkLib", "EWgK9e", "Z5xsfc", "snAcKc", "VrseUb", "fDcn4b", "EzwWhf"}
# Every other RPC id gpwc 651aa268 knows (trash, archive, favourites, albums, sharing,
# geo/description/timestamp edits, downloads, uploads, partner sharing, Drive import).
OTHER_GPWC_RPCS = {
    "AQNOFd", "DaSgWe", "dnv2s", "E1Cajb", "e9T5je", "Es7fke", "EtUHOe", "F2A0H", "Ftfh0", "laUYf",
    "OXvT9d", "pLFTfd", "SusGud", "swbisb", "TLvKMb", "V8RKJ", "w7TP3c", "XwAOJf", "yCLA7", "ycV3Nd",
    "zy0IHe",
}  # fmt: skip
WRITE_BUILDERS = {
    "MoveToTrash", "RestoreFromTrash", "CreateAlbum", "SetFavorite", "UnFavorite", "SetArchive", "UnArchive",
    "SetItemTimestamp", "SetItemDescription", "SetItemGeoData", "DeleteItemGeoData", "RemoveItemsFromAlbum",
    "AddItemsToNewAlbum", "AddItemsToExistingAlbum", "AddItemsToExistingSharedAlbum", "GetDownloadToken",
    "CheckDownloadToken", "SaveSharedMediaToLibrary", "SavePartnerSharedMedia", "GetUnsupportedVideoDownloadUrl",
    "ImportMediaFromDrive", "GetPartnerSharedMedia", "GetSharedLinksPage", "GetTrashPage",
}  # fmt: skip


def _strings(path: Path) -> list[str]:
    return [
        n.value for n in ast.walk(ast.parse(SOURCE[path])) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    ]


def test_transport_allowlist_is_exactly_the_read_rpcs():
    assert transport.RPC_ALLOWLIST == READ_RPCS


def test_every_payload_builder_uses_an_allowlisted_rpc():
    rpcids = set(re.findall(r'self\.rpcid = "([A-Za-z0-9]+)"', SOURCE[SRC / "vendor" / "gpwc" / "payloads.py"]))
    assert rpcids == READ_RPCS
    builders = {
        name
        for name, obj in vars(payloads).items()
        if isinstance(obj, type) and obj.__module__ == payloads.__name__ and name != "Payload"
    }
    assert len(builders) == len(READ_RPCS)
    for name in builders:
        assert getattr(payloads, name).__mro__[1] is payloads.Payload


def test_no_other_rpc_id_literal_anywhere():
    for path in FILES:
        found = set(_strings(path)) & OTHER_GPWC_RPCS
        found |= {r for r in OTHER_GPWC_RPCS if re.search(rf"\b{r}\b", SOURCE[path])}
        assert not found, (path, found)


def test_no_write_builders_exist_or_are_imported():
    for name in WRITE_BUILDERS:
        assert not hasattr(payloads, name), name
        assert not re.search(rf"\b{name}\b", ALL), name
    # Only the vendored subset is imported, never an installed gpwc with its write builders.
    for path in FILES:
        for node in ast.walk(ast.parse(SOURCE[path])):
            if isinstance(node, ast.ImportFrom) and node.level == 0:
                assert not (node.module or "").startswith("gpwc"), path
            elif isinstance(node, ast.Import):
                assert not any(alias.name.startswith("gpwc") for alias in node.names), path


def test_only_allowlisted_hosts_appear_in_url_literals():
    hosts = set()
    for path in FILES:
        for text in _strings(path):
            hosts |= set(re.findall(r"https?://([^/\"'\s?#]+)", text))
    assert hosts <= {"photos.google.com", "github.com"}, hosts  # github.com: provenance links only


def test_network_libraries_are_confined_to_the_transport():
    for path in FILES:
        tree = ast.parse(SOURCE[path])
        modules = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                modules |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                modules.add(node.module.split(".")[0])
        assert not modules & {"requests", "httpx", "urllib3", "socket", "http", "aiohttp"}, path
        if "curl_cffi" in modules:
            assert path.name == "transport.py", path


def test_only_get_and_post_and_post_only_to_batchexecute():
    text = SOURCE[SRC / "transport.py"]
    for verb in ("PUT", "PATCH", "DELETE"):
        assert not re.search(rf'"{verb}"', text), verb
    assert re.findall(r'self\._send\(ctx, "(\w+)"', text) == ["POST", "GET"]
    assert "/data/batchexecute" in SOURCE[SRC / "vendor" / "gpwc" / "client.py"]


def test_tool_errors_carry_only_the_typed_payload():
    server = SOURCE[SRC / "server.py"]
    raises = re.findall(r"raise ToolError\((.*)\)", server)
    assert raises and all(arg.startswith('_json({"error":') for arg in raises), raises
    for path in (SRC / "server.py", SRC / "client.py", SRC / "errors.py"):
        assert not re.search(r"str\(exc\)|\{exc\}|repr\(exc\)", SOURCE[path]), path


def test_no_cookie_or_token_writes():
    # Cookies are merged in memory only; nothing writes into the session directory.
    session = SOURCE[SRC / "session.py"]
    assert "write_text" not in session and "write_bytes" not in session and '"w"' not in session
