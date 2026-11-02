"""Private, deterministic inspection of captured mail.

Received content is untrusted data. This module never executes or interprets
instructions in it: it renders escaped text, keeps only links whose scheme,
host and path are explicitly approved, and writes everything to a private file.
Nothing sensitive is returned to the caller for printing.
"""
from __future__ import annotations

import re
import urllib.parse
from html.parser import HTMLParser
from typing import Any

from .core import CaptureError, _parsed_addresses

_SKIPPED = {"script", "style", "head", "title", "template", "noscript", "iframe", "object", "embed"}
_LINK = re.compile(r'https?://[^\s<>"\'\)\]]+')


class _TextExtractor(HTMLParser):
    """Collect visible text and anchor hrefs; never fetch or run anything."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hrefs: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIPPED:
            self._skip += 1
        if tag == "a":
            href = dict(attrs).get("href")
            if href:
                self.hrefs.append(href)
        if tag in {"br", "p", "div", "li", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def _link_allowed(url: str, hosts: set[str], path_prefixes: tuple[str, ...]) -> bool:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None:
        return False
    try:
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    if host not in hosts:
        return False
    return not path_prefixes or any(parsed.path.startswith(prefix) for prefix in path_prefixes)


def inspect_message(
    message: dict[str, Any],
    recipient: str,
    allowed_hosts: list[str],
    allowed_path_prefixes: list[str] | None = None,
) -> dict[str, Any]:
    """Return a private inspection record for one message of one allocation."""
    if not allowed_hosts:
        raise CaptureError("CONFIG_INVALID", "inspection requires at least one approved host")
    if recipient.lower() not in _parsed_addresses(message.get("envelope", {}).get("to")):
        raise CaptureError("AUTHORIZATION_DENIED", "message is outside allocation")
    content = message.get("content", {})
    text = content.get("text", "") or ""
    html = content.get("html", "") or ""
    extractor = _TextExtractor()
    extractor.feed(html)
    extractor.close()
    visible = text or re.sub(r"\n{3,}", "\n\n", "".join(extractor.parts)).strip()
    hosts = {host.lower() for host in allowed_hosts}
    prefixes = tuple(allowed_path_prefixes or ())
    candidates = sorted(set(_LINK.findall(text)) | {href for href in extractor.hrefs if href.startswith(("http://", "https://"))})
    links = [url for url in candidates if _link_allowed(url, hosts, prefixes)]
    codes = sorted(set(re.findall(r"(?<!\d)\d{4,8}(?!\d)", visible)))
    return {
        "subject": message.get("subject", ""),
        "text": visible,
        "links": links,
        "rejected_link_count": len(candidates) - len(links),
        "codes": codes,
        "remote_assets_rendered": False,
    }
