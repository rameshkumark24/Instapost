"""Fetch a publisher's own one-line summary for a story.

Hacker News and Lobsters pass along a title and a URL and nothing else, so
without this the card falls back to a metadata line -- "616 points on Hacker
News in the last 4h." That is honest but says nothing about the story, and it
is the single biggest weakness left in the news channel.

The fix is `og:description`: the summary a publisher writes specifically to be
shown when their link is shared. Using it is what the tag is for, and it is
the same string that appears when the article is posted anywhere else.

This is the only place in the pipeline that touches an arbitrary third-party
URL, so it is deliberately timid: short timeout, capped download, HTML only,
no redirects off to strange places, and any failure at all falls straight back
to the metadata line rather than holding up the build.
"""
from __future__ import annotations

import html
import logging
import re
from urllib.parse import urlparse

import requests

from . import config as cfg

log = logging.getLogger(__name__)

# Meta's own og:description is capped well under this; anything longer is a
# page that is not what we think it is.
MAX_BYTES = 400_000
TIMEOUT = 8

_META = re.compile(
    r"""<meta[^>]+?(?:property|name)\s*=\s*["'](?P<key>og:description|twitter:description|description)["'][^>]*?>""",
    re.IGNORECASE,
)
_CONTENT = re.compile(r"""content\s*=\s*["'](?P<value>[^"']*)["']""", re.IGNORECASE)

# Boilerplate that carries no information about the story itself.
_USELESS = re.compile(
    r"^(read more|subscribe|sign in|log in|comments?|discussion|home|"
    r"[\w\s]*newsletter[\w\s]*|enable javascript.*|.*cookies.*)$",
    re.IGNORECASE,
)

# Site-level taglines. Many pages serve the same description on every URL, so
# it passes every length and boilerplate check while describing the site rather
# than the story -- "Browse all models available on Cerebras public endpoints"
# on a page about one specific model release. An imperative opener is the
# reliable tell, and the metadata fallback is better than a wrong summary.
_TAGLINE = re.compile(
    r"^(browse|discover|explore|welcome to|the official|learn (more|how)|"
    r"get started|sign up|find (the )?(best|out)|shop |join )",
    re.IGNORECASE,
)


def _safe_url(url: str) -> bool:
    """Only plain public http(s). Never let a harvested URL reach anything else."""
    try:
        u = urlparse(url)
    except ValueError:
        return False
    if u.scheme not in {"http", "https"} or not u.hostname:
        return False
    host = u.hostname.lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        return False
    # Block the obvious private ranges; this pipeline has no business inside one.
    if re.match(r"^(10\.|127\.|169\.254\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", host):
        return False
    return True


def description(url: str) -> str | None:
    """The publisher's own summary, or None. Never raises."""
    if not cfg.ENRICH_FROM_SOURCE or not _safe_url(url):
        return None

    try:
        with requests.get(
            url,
            timeout=TIMEOUT,
            stream=True,
            allow_redirects=True,
            headers={
                "User-Agent": cfg.USER_AGENT,
                "Accept": "text/html,application/xhtml+xml",
            },
        ) as r:
            if r.status_code != 200:
                return None
            if "html" not in r.headers.get("content-type", "").lower():
                return None
            if not _safe_url(r.url):        # a redirect could have gone anywhere
                return None

            body = ""
            for chunk in r.iter_content(chunk_size=16_384, decode_unicode=True):
                if not isinstance(chunk, str):
                    chunk = chunk.decode(r.encoding or "utf-8", errors="ignore")
                body += chunk
                # The tags we want live in <head>; no need to read the article.
                if len(body) > MAX_BYTES or "</head>" in body.lower():
                    break
    except Exception as exc:
        log.info("enrich failed for %s: %s", urlparse(url).hostname, exc)
        return None

    return _extract(body)


def _extract(body: str) -> str | None:
    """First usable description tag, in order of how well publishers curate them."""
    found: dict[str, str] = {}
    for tag in _META.finditer(body):
        m = _CONTENT.search(tag.group(0))
        if not m:
            continue
        key = tag.group("key").lower()
        value = html.unescape(m.group("value")).strip()
        value = re.sub(r"\s+", " ", value)
        if value and key not in found:
            found[key] = value

    for key in ("og:description", "twitter:description", "description"):
        text = found.get(key)
        if not text:
            continue
        if len(text) < 40 or _USELESS.match(text) or _TAGLINE.match(text):
            continue
        return text[: cfg.BODY_MAX_CHARS].strip()
    return None
