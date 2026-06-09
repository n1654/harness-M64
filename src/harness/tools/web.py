"""Web tools — fetch content from a URL.

Single tool for now: `read_url`. Returns the raw response body up to a
configurable size cap; the model can decide how to interpret HTML / JSON / text.
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

import httpx

from harness.tools.registry import ToolContext, ToolEntry

log = logging.getLogger("harness.tools.web")

_DEFAULT_MAX_CHARS = 20_000
_HARD_MAX_CHARS   = 100_000
_TIMEOUT_SEC      = 20.0
_USER_AGENT       = "harness-m64/0.0 (+read_url)"


def _strip_html(html: str) -> str:
    """Crude but dependency-free HTML → text. Drops script/style blocks then tags."""
    s = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", html)
    s = re.sub(r"(?is)<style[^>]*>.*?</style>",   " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    # collapse whitespace
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s).strip()
    return s


async def _read_url(ctx: ToolContext, args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "⚠️ url must start with http:// or https://"

    try:
        max_chars = int(args.get("max_chars") or _DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        max_chars = _DEFAULT_MAX_CHARS
    max_chars = min(max(max_chars, 200), _HARD_MAX_CHARS)

    strip = bool(args.get("strip_html") if "strip_html" in args else True)

    try:
        async with httpx.AsyncClient(
            timeout=_TIMEOUT_SEC,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
        ) as client:
            resp = await client.get(url)
    except httpx.HTTPError as e:
        return f"⚠️ fetch failed: {type(e).__name__}: {e}"

    ctype = resp.headers.get("Content-Type", "").lower()
    text = resp.text or ""

    if strip and ("html" in ctype or text.lstrip().startswith("<")):
        text = _strip_html(text)

    truncated = ""
    if len(text) > max_chars:
        truncated = f"\n\n[...truncated {len(text) - max_chars} chars]"
        text = text[:max_chars]

    header = (
        f"GET {url}\n"
        f"HTTP {resp.status_code}  Content-Type: {ctype or '(none)'}\n"
        f"length: {len(text)} chars\n"
        f"---\n"
    )
    return header + text + truncated


def get_tools() -> List[ToolEntry]:
    return [
        ToolEntry(
            name="read_url",
            schema={
                "name": "read_url",
                "description": (
                    "Fetch the contents of an http(s) URL and return the body text. "
                    "Follows redirects. By default strips HTML tags for readable output. "
                    "Use for retrieving documentation, articles, JSON APIs, etc."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "http(s) URL"},
                        "max_chars": {
                            "type": "integer",
                            "description": (
                                f"max characters to return (default {_DEFAULT_MAX_CHARS}, "
                                f"hard cap {_HARD_MAX_CHARS})"
                            ),
                        },
                        "strip_html": {
                            "type": "boolean",
                            "description": "strip HTML tags from the response (default: true)",
                        },
                    },
                    "required": ["url"],
                },
            },
            handler=_read_url,
            timeout_sec=30,
        ),
    ]
