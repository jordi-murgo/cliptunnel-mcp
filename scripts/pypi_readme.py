"""Rewrite README.md for PyPI: absolute links and rendered Mermaid diagrams.

PyPI renders the long description as HTML but has no filesystem context, so
relative links like ``LICENSE`` or ``docs/ARCHITECTURE.md`` resolve to broken
``pypi.org/project/cliptunnel-mcp/...`` URLs.  This script rewrites in-place
every relative markdown link or image to an absolute GitHub URL so the same
README works on both GitHub and PyPI.

PyPI also cannot render ``mermaid`` fenced code blocks.  Each block is
replaced by an inline PNG image via `mermaid.ink <https://mermaid.ink>`_,
which encodes the diagram source as a URL-safe base64 path segment.  No
external files are generated and no build-time renderer (mmdc, Node) is
needed; the image is served on demand by mermaid.ink.

Run it in CI immediately before ``python -m build``; never commit the
rewritten file.
"""

from __future__ import annotations

import base64
import re
import sys
from pathlib import Path

REPO_URL = "https://github.com/jordi-murgo/cliptunnel-mcp"
BLOB_BASE = f"{REPO_URL}/blob/main"
RAW_BASE = f"{REPO_URL}/raw/main"
MERMAID_INK = "https://mermaid.ink/img"


# Matches [text](url) and ![alt](url)
_LINK_RE = re.compile(r"(?P<bang>!)?\[(?P<text>[^\]]*)\]\((?P<url>[^)]+)\)")

# Matches ````mermaid ... ```` fenced blocks (tolerates 3+ backticks).
_MERMAID_RE = re.compile(
    r"(?P<fence>```+mermaid\n)(?P<src>.*?)(?P<close>^\s*```+\s*$)",
    re.DOTALL | re.MULTILINE,
)


def _is_absolute(url: str) -> bool:
    return url.startswith(("http://", "https://", "#", "mailto:", "data:"))


def _rewrite_link(url: str, is_image: bool) -> str:
    if _is_absolute(url):
        return url
    # Strip optional anchor/query, preserve for reattachment
    anchor = ""
    if "#" in url:
        url, anchor = url.split("#", 1)
        anchor = f"#{anchor}"
    base = RAW_BASE if is_image else BLOB_BASE
    return f"{base}/{url}{anchor}"


def _encode_mermaid(src: str) -> str:
    """Encode Mermaid source as mermaid.ink URL-safe base64 (no padding)."""
    return base64.urlsafe_b64encode(src.encode("utf-8")).decode("ascii").rstrip("=")


def _replace_mermaid(m: re.Match[str]) -> str:
    src = m.group("src")
    encoded = _encode_mermaid(src)
    return f"![Mermaid diagram]({MERMAID_INK}/{encoded})\n\n"


def transform(text: str) -> str:
    # 1. Replace Mermaid fenced blocks with inline PNG images.
    #    Must run before link rewriting — Mermaid syntax contains
    #    [text](url) patterns that _LINK_RE would corrupt.
    text = _MERMAID_RE.sub(_replace_mermaid, text)

    # 2. Rewrite relative links to absolute GitHub URLs.
    def _sub_link(m: re.Match[str]) -> str:
        bang = m.group("bang") or ""
        text_val = m.group("text")
        url = m.group("url").strip()
        is_image = bool(bang)
        return f"{bang}[{text_val}]({_rewrite_link(url, is_image)})"

    text = _LINK_RE.sub(_sub_link, text)

    return text


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: pypi_readme.py <README.md>", file=sys.stderr)
        return 2
    path = Path(argv[1])
    if not path.is_file():
        print(f"not a file: {path}", file=sys.stderr)
        return 1
    original = path.read_text(encoding="utf-8")
    rewritten = transform(original)
    if rewritten == original:
        print("no relative links or mermaid blocks found; README unchanged", file=sys.stderr)
        return 0
    path.write_text(rewritten, encoding="utf-8")
    print(f"rewrote README for PyPI in {path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))