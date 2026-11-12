"""Render the installed application's identity into its own pages."""

import html
import json
import re
from importlib import resources

from .build_identity import BUILD, BuildIdentity


def footer_html(identity: BuildIdentity | None = None) -> str:
    identity = BUILD if identity is None else identity
    return (
        "<style>.build-identity{font-size:12px;padding:12px 16px;text-align:center;user-select:text;"
        "color:var(--color-text-secondary,var(--p-muted,#4b4b4b));"
        "background:var(--color-background-primary,var(--p-card,#f4f4f4));overflow-wrap:anywhere}"
        ".build-identity summary{cursor:pointer;padding:6px 0;min-height:32px}"
        ".build-identity summary:focus-visible{outline:3px solid currentColor;outline-offset:2px}"
        ".build-identity p{margin:6px 0}"
        ".build-identity code{display:block;white-space:normal;overflow-wrap:anywhere;user-select:all}</style>"
        '<footer class="build-identity" '
        f'title="{html.escape(identity.source_revision, quote=True)}">'
        f"<details><summary>Google Photos v{html.escape(identity.version)} · "
        f"{html.escape(identity.source_revision[:12])}</summary>"
        f"<p>Source revision <code>{html.escape(identity.source_revision)}</code></p></details></footer>"
    )


def view_html(identity: BuildIdentity | None = None) -> str:
    identity = BUILD if identity is None else identity
    version = json.dumps(identity.version).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    values = {"__BUILD_VERSION_JSON__": version, "<!-- BUILD_IDENTITY -->": footer_html(identity)}
    template = (resources.files("photos_mcp") / "view.html").read_text(encoding="utf-8")
    return re.sub(r"__BUILD_VERSION_JSON__|<!-- BUILD_IDENTITY -->", lambda match: values[match.group()], template)
