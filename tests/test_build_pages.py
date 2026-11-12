"""Owned pages display the loaded artifact while retaining their existing controls."""

import asyncio
import html
import json
import re
import shutil
import subprocess
from types import SimpleNamespace

import pytest
from signin_window.oidc import BrowserAuth
from starlette.responses import RedirectResponse

from photos_mcp import __version__, build_pages, ui, uploads
from photos_mcp.build_identity import BuildIdentity
from photos_mcp.connections_auth import BrowserLogin

REVISION = "a123456789" * 4
IDENTITY = BuildIdentity("photos-mcp", __version__, REVISION)


def test_app_page_and_protocol_version_come_from_the_same_artifact():
    page = build_pages.view_html(IDENTITY)
    assert f"Google Photos v{__version__} · {REVISION[:12]}</summary>" in page
    assert f"Source revision <code>{REVISION}</code>" in page
    assert f'title="{REVISION}"' in page and "user-select:text" in page
    assert f'appInfo: {{ name: "photos-view", version: "{__version__}" }}' in page
    assert "BUILD_IDENTITY" not in page and "__BUILD_VERSION_JSON__" not in page
    assert page.index('<footer class="build-identity"') < page.index("<script>")


def test_footer_and_inline_protocol_version_are_escaped_without_refilling_values():
    version = '</script><img src=x onerror="bad">&__BUILD_VERSION_JSON__'
    revision = 'a"<>&' * 8
    identity = BuildIdentity("photos-mcp", version, revision)
    page = build_pages.view_html(identity)
    assert f"Google Photos v{html.escape(version)} · {html.escape(revision[:12])}</summary>" in page
    assert f"Source revision <code>{html.escape(revision)}</code>" in page
    assert f'title="{html.escape(revision, quote=True)}"' in page
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    literal = re.search(r'appInfo: \{ name: "photos-view", version: (.*?) \},', script).group(1)
    assert json.loads(literal) == version
    assert "<img" not in script


@pytest.mark.parametrize("expired", [False, True])
def test_upload_and_expired_pages_retain_safe_forms_and_artifact_footer(monkeypatch, expired):
    monkeypatch.setattr(build_pages, "BUILD", IDENTITY)
    link = uploads.Link(
        "fake-opaque-id", "fake-account", None, "fake-album", "<album> {{FOOTER}}", "m•••@example.com", 0
    )
    page = uploads._page(None if expired else link, 1024**2)
    assert page.count('<footer class="build-identity"') == 1
    assert f'title="{REVISION}"' in page
    assert f"Source revision <code>{REVISION}</code>" in page
    assert "fake-opaque-id" not in page and "fake-account" not in page
    if expired:
        assert "This link has expired" in page and "<script>" not in page
    else:
        assert "<input type=file" in page and 'xhr.open("PUT", target)' in page
        assert "&lt;album&gt; {{FOOTER}}" in page
        assert "const target = location.pathname" in page


def test_unmapped_account_denial_clears_session_and_keeps_safe_return_link(monkeypatch):
    monkeypatch.setattr(build_pages, "BUILD", IDENTITY)
    monkeypatch.setattr(ui, "BUILD", IDENTITY)

    async def accepted(self, request):
        return RedirectResponse("/connections/", status_code=303)

    monkeypatch.setattr(BrowserAuth, "callback", accepted)
    login = BrowserLogin.__new__(BrowserLogin)
    login.home = '/connections/?fake="<tag>'
    login.mapped = lambda _: None
    request = SimpleNamespace(session={"sub": "fake-private-subject", "token": "fake-private-token"})
    response = asyncio.run(login.callback(request))
    page = response.body.decode()
    assert response.status_code == 403 and request.session == {}
    assert "This sign-in cannot manage a Google Photos connection" in page
    assert f'href="{html.escape(login.home, quote=True)}"' in page
    assert page.count("<footer ") == 1
    assert f'title="{REVISION}"' in page or f'data-source-revision="{REVISION}"' in page
    assert "fake-private-subject" not in page and "fake-private-token" not in page


@pytest.mark.skipif(shutil.which("node") is None, reason="Node executes the App's page script without network")
def test_loaded_app_shell_identity_survives_status_result_error_and_cancel_rerenders():
    page = build_pages.view_html(IDENTITY)
    script = page.split("<script>", 1)[1].split("</script>", 1)[0]
    # A small DOM stand-in executes the actual App script; no provider or browser network is involved.
    harness = r"""
const vm = require('vm');
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; this.dataset = {}; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  addEventListener() {}
}
const root = new Element('div'), footer = new Element('footer'), sent = [], listeners = {};
footer.textContent = input.footer;
const body = new Element('body'); body.children = [root, footer];
const parent = {postMessage: message => sent.push(message)};
const document = {body, documentElement: new Element('html'),
  getElementById: id => id === 'root' ? root : null, createElement: tag => new Element(tag)};
document.documentElement.style = {setProperty() {}};
const window = {parent, addEventListener: (kind, fn) => listeners[kind] = fn,
  matchMedia: () => ({matches:false})};
vm.runInNewContext(input.script, {window, document, setTimeout: () => 0, URL});
function notify(method, params) {
  listeners.message({source:parent, data:{jsonrpc:'2.0', method, params}});
  if (body.children[1] !== footer || footer.textContent !== input.footer) throw Error('footer replaced');
  return root.children.map(child => child.textContent || child.tag);
}
const results = [
  notify('ui/notifications/tool-input', {}),
  notify('ui/notifications/tool-result', {structuredContent:{kind:'photos', items:[],
    build_identity:{version:'fake-new-version'} }}),
  notify('ui/notifications/tool-result', {isError:true}),
  notify('ui/notifications/tool-cancelled', {})
];
process.stdout.write(JSON.stringify({results, version:sent[0].params.appInfo.version}));
"""
    result = subprocess.run(
        ["node", "-e", harness],
        input=json.dumps({"script": script, "footer": build_pages.footer_html(IDENTITY)}),
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    result = json.loads(result.stdout)
    assert result["version"] == IDENTITY.version
    assert result["results"] == [["Loading photos…"], ["div"], ["These photos could not be shown."], ["Cancelled."]]
