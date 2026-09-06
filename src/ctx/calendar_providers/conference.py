from __future__ import annotations

import html
import re
from urllib.parse import urlparse

_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)


def _clean_url(url: str) -> str:
    return html.unescape(url).strip().rstrip(".,;:!?)\"]}'")


def _conference_kind(url: str) -> str | None:
    try:
        parsed = urlparse(url)
    except ValueError:
        return None

    host = (parsed.hostname or "").casefold()
    path = parsed.path.casefold()

    if host == "meet.google.com" or host.endswith(".meet.google.com"):
        return "google-meet"

    if host == "teams.microsoft.com" or host.endswith(".teams.microsoft.com"):
        if "/meet/" in path or "/l/meetup-join/" in path:
            return "teams"

    if host == "zoom.us" or host.endswith(".zoom.us"):
        if path.startswith("/j/") or path.startswith("/w/"):
            return "zoom"

    return None


def extract_conference_url(
    text: str | None = None,
    *,
    preferred_urls: list[str] | None = None,
) -> str | None:
    """Return the first recognised Meet, Teams, or Zoom conference URL."""
    candidates: list[str] = []

    for value in preferred_urls or []:
        if value:
            candidates.append(str(value))

    if text:
        candidates.extend(match.group(0) for match in _URL_RE.finditer(text))

    seen: set[str] = set()
    for raw in candidates:
        url = _clean_url(raw)
        if not url or url in seen:
            continue
        seen.add(url)
        if _conference_kind(url):
            return url

    return None
