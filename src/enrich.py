"""Fetch a publisher's own share summary for a story that arrived without one.

Hacker News and Lobsters hand over a title and a link and nothing else. Without
this, the card body falls back to a metadata line -- "616 points on Hacker News
in the last 4h." -- which is honest but says nothing about the story.

`og:description` is the summary a publisher writes to be shown wherever their
link is shared. It is still the publisher's words, so compose.py either
rewrites it (LLM path, validated against it) or prints it as an attributed
quotation. It is never passed off as ours.

This is the only code that requests an arbitrary third-party URL, and those
URLs come from aggregators anyone can post to, so every request is treated as
hostile:

  * each hop of a redirect chain is checked *before* it is requested;
  * the host is resolved and every address it resolves to must be globally
    routable, so loopback, private, link-local and cloud-metadata addresses
    are refused however they are spelled -- 0.0.0.0, ::ffff:127.0.0.1,
    2130706433, or an ordinary DNS name pointing inward;
  * the whole fetch runs against a wall-clock deadline, so a server that drips
    one byte every few seconds cannot hold the nightly build hostage;
  * reading stops at </head> or MAX_BYTES, and the bytes are decoded once with
    the charset the page declares rather than the ISO-8859-1 guess requests
    falls back to for a bare text/html header.

Residual risk, stated plainly: requests resolves the name again when it
connects, so a DNS answer that changes between our check and that connection
(rebinding) is not covered. Closing it means connecting to the pre-resolved
address, which is out of proportion for a fetch that reads one meta tag from
public pages.

Any failure returns None, and the caller falls back to the metadata line.
"""
from __future__ import annotations

import codecs
import ipaddress
import logging
import re
import socket
import threading
from dataclasses import dataclass
from html import unescape
from typing import Callable
from urllib.parse import urljoin, urlparse

import requests

from . import config as cfg

log = logging.getLogger(__name__)

MAX_BYTES = 400_000          # the tags live in <head>; nothing past this is read
DEADLINE_S = 12.0            # wall clock for the whole fetch, redirects included
CONNECT_TIMEOUT = 4
READ_TIMEOUT = 4
MAX_REDIRECTS = 3
MAX_DESCRIPTION = 1_000      # compose.py trims to the card, at a sentence boundary

_HEADERS = {
    "User-Agent": cfg.USER_AGENT,
    "Accept": "text/html,application/xhtml+xml",
    # A compressed body can expand far past MAX_BYTES inside a single read.
    "Accept-Encoding": "identity",
}


@dataclass(frozen=True)
class Enriched:
    description: str
    site_name: str        # "WIRED", or the bare hostname when the page names no one


# --- where we are allowed to connect ---------------------------------------


def _resolve(host: str) -> list[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)]


def _is_public_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])    # drop any IPv6 zone id
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global already excludes private, loopback, link-local, reserved,
    # unspecified and shared (100.64/10) space.
    return ip.is_global and not ip.is_multicast


def _safe_url(url: str, resolve: Callable[[str], list[str]] = _resolve) -> bool:
    """Plain http(s) to a host whose every address is public."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        parsed.port                  # raises on a malformed port
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or not host:
        return False
    if parsed.username or parsed.password:
        return False
    try:
        addresses = resolve(host)
    except (OSError, UnicodeError):
        return False
    return bool(addresses) and all(_is_public_ip(a) for a in addresses)


def _host(url: str) -> str:
    try:
        return urlparse(url).hostname or "?"
    except ValueError:
        return "?"


# --- reading the page -------------------------------------------------------


def _fetch_head(
    url: str, session, resolve: Callable[[str], list[str]] = _resolve
) -> tuple[bytes, str, str] | None:
    """(head bytes, content-type, final url), following redirects by hand.

    Redirects are never followed automatically: requests would send the next
    request before we saw where it was going.
    """
    current = url
    for _ in range(MAX_REDIRECTS + 1):
        if not _safe_url(current, resolve):
            log.info("enrich refused %s", _host(current))
            return None

        r = session.get(
            current,
            headers=_HEADERS,
            stream=True,
            allow_redirects=False,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        )
        try:
            if r.is_redirect:
                location = r.headers.get("location")
                if not location:
                    return None
                current = urljoin(current, location)
                continue
            if r.status_code != 200:
                return None
            content_type = r.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return None

            body = bytearray()
            for chunk in r.iter_content(chunk_size=8_192):
                # Scan only the new bytes plus enough overlap to catch a tag
                # split across two chunks; rescanning the whole buffer every
                # time is quadratic.
                scan_from = max(0, len(body) - 8)
                body += chunk
                if b"</head" in bytes(body[scan_from:]).lower() or len(body) >= MAX_BYTES:
                    break
            return bytes(body[:MAX_BYTES]), content_type, current
        finally:
            r.close()

    log.info("enrich gave up after %d redirects from %s", MAX_REDIRECTS, _host(url))
    return None


_CHARSET_HEADER = re.compile(r"""charset\s*=\s*["']?([\w.:-]+)""", re.IGNORECASE)
_CHARSET_META = re.compile(rb"""<meta[^>]+charset\s*=\s*["']?\s*([\w.:-]+)""", re.IGNORECASE)


def _charset(content_type: str, body: bytes) -> str:
    """Header charset, then <meta charset>, then UTF-8. Never requests' Latin-1 guess."""
    candidates = []
    if m := _CHARSET_HEADER.search(content_type or ""):
        candidates.append(m.group(1))
    if m := _CHARSET_META.search(body[:8_192]):
        candidates.append(m.group(1).decode("ascii", "ignore"))
    for name in candidates:
        try:
            return codecs.lookup(name).name
        except LookupError:
            continue
    return "utf-8"


def _decode(body: bytes, content_type: str) -> str:
    return body.decode(_charset(content_type, body), errors="replace")


# --- finding the description -------------------------------------------------

# A whole <meta> tag, allowing ">" inside quoted attribute values.
_META_TAG = re.compile(r"""<meta\b(?:[^>"']|"[^"]{0,4000}"|'[^']{0,4000}')*>""", re.IGNORECASE)

# One attribute. The value ends at the quote character that opened it, so an
# apostrophe inside a double-quoted value ("nobody's saying why") is kept.
_ATTR = re.compile(r"""([^\s"'=<>/]+)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'=<>`]+))""")

# Text that describes the page chrome rather than the story. Anchored to the
# whole string, so an article that merely mentions cookies or a newsletter is
# not thrown away.
_BOILERPLATE = re.compile(
    r"^(?:read more|subscribe(?: now)?|sign in|log in|comments?|discussion|home|"
    r"(?:please )?enable javascript(?: to continue)?|"
    r"(?:this (?:site|website) uses|we use) cookies\b.*|"
    r"(?:subscribe|sign up) (?:to|for) (?:our|the) newsletter\b.*)[.!]?$",
    re.IGNORECASE,
)

# Site-level taglines: served on every URL, so they pass every length check
# while describing the site, not the story. An imperative opener is the tell.
_TAGLINE = re.compile(
    r"^(?:browse|discover|explore|welcome to|the official|learn (?:more|how)|"
    r"get started|sign up|find (?:the )?(?:best|out)|shop |join )",
    re.IGNORECASE,
)

# GitHub appends this to every repository's description. HN stories link to
# repos constantly, so it is stripped wherever it appears.
_GITHUB_SUFFIX = re.compile(
    r"\s*Contribute to \S+ development by creating an account on GitHub\.?\s*$",
    re.IGNORECASE,
)


def _parse_meta(text: str) -> dict[str, str]:
    """First value of each property/name, entity-decoded and whitespace-collapsed."""
    found: dict[str, str] = {}
    for tag in _META_TAG.finditer(text):
        attrs: dict[str, str] = {}
        for m in _ATTR.finditer(tag.group(0)[len("<meta"):]):
            value = next(v for v in m.group(2, 3, 4) if v is not None)
            attrs[m.group(1).lower()] = value
        key = (attrs.get("property") or attrs.get("name") or "").lower()
        content = attrs.get("content")
        if key and content and key not in found:
            found[key] = re.sub(r"\s+", " ", unescape(content)).strip()
    return found


def _pick_description(found: dict[str, str]) -> str | None:
    for key in ("og:description", "twitter:description", "description"):
        text = _GITHUB_SUFFIX.sub("", found.get(key, "")).strip()
        if len(text) < 40 or _BOILERPLATE.match(text) or _TAGLINE.match(text):
            continue
        # Returned whole. Trimming belongs to compose.py, which cuts at a
        # sentence; cutting here would hand it a fragment already "short enough".
        return text[:MAX_DESCRIPTION]
    return None


def _extract(text: str) -> str | None:
    return _pick_description(_parse_meta(text))


# On these hosts og:site_name names the platform, not whoever wrote the words.
# Crediting it would present "X (formerly Twitter)" as the author of someone's
# post -- which is exactly what the first live run printed.
_SOCIAL_HOSTS = {
    "x.com": "a post on X",
    "twitter.com": "a post on X",
    "bsky.app": "a post on Bluesky",
    "threads.net": "a post on Threads",
    "linkedin.com": "a post on LinkedIn",
    "facebook.com": "a post on Facebook",
    "instagram.com": "a post on Instagram",
    "reddit.com": "a thread on Reddit",
    "youtube.com": "a video on YouTube",
    "youtu.be": "a video on YouTube",
}


def _site_name(found: dict[str, str], final_url: str) -> str:
    host = _host(final_url).lower()
    for prefix in ("www.", "mobile.", "m.", "old."):
        host = host.removeprefix(prefix)
    if platform := _SOCIAL_HOSTS.get(host):
        return platform

    name = found.get("og:site_name", "").strip()
    if 0 < len(name) <= 60:
        return name
    return host


# --- entry point ------------------------------------------------------------


def fetch(
    url: str,
    *,
    deadline: float = DEADLINE_S,
    _fetch: Callable[..., tuple[bytes, str, str] | None] = _fetch_head,
) -> Enriched | None:
    """The publisher's description and name, or None.

    Never raises, and never holds the caller past `deadline`. Socket timeouts
    bound each read, not the download: a server dripping a byte every few
    seconds satisfies every one of them indefinitely. So the fetch runs on a
    daemon thread and is abandoned when the deadline passes; the thread dies
    with the process at the end of the build.
    """
    if not cfg.ENRICH_FROM_SOURCE:
        return None

    result: list = []
    session = requests.Session()

    def work() -> None:
        try:
            result.append(_fetch(url, session))
        except Exception as exc:
            log.info("enrich failed for %s: %s", _host(url), type(exc).__name__)

    worker = threading.Thread(target=work, name="enrich", daemon=True)
    worker.start()
    worker.join(deadline)
    if worker.is_alive():
        log.info("enrich abandoned %s after %.0fs", _host(url), deadline)
        session.close()
        return None
    session.close()

    fetched = result[0] if result else None
    if not fetched:
        return None

    body, content_type, final_url = fetched
    found = _parse_meta(_decode(body, content_type))
    description = _pick_description(found)
    if not description:
        return None
    return Enriched(description=description, site_name=_site_name(found, final_url))
