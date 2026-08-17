"""Pull candidate stories from every source into one normalised shape.

Every source here is an officially published endpoint, keyless or free-keyed.
Nothing scrapes HTML, which keeps us clear of robots.txt and of selectors that
rot without warning. A source that fails is logged and skipped -- one dead feed
must never take the night's run down with it.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

import feedparser
import requests

from . import config as cfg

log = logging.getLogger(__name__)

_session = requests.Session()
_session.headers["User-Agent"] = cfg.USER_AGENT


@dataclass
class Item:
    """One candidate story, normalised across all sources."""

    title: str
    url: str
    source: str                  # which harvester produced it
    publication: str             # human-readable attribution for the card
    published: datetime          # tz-aware UTC
    engagement: float | None = None   # raw source-native count, or None
    summary: str = ""
    score: float = 0.0
    score_parts: dict[str, float] = field(default_factory=dict)

    def key(self) -> str:
        """Stable identity for the dedup ledger."""
        u = self.url.split("?")[0].rstrip("/")
        return u.replace("https://", "").replace("http://", "").replace("www.", "")


def _get(url: str, **kw: Any) -> requests.Response:
    r = _session.get(url, timeout=cfg.HTTP_TIMEOUT, **kw)
    r.raise_for_status()
    return r


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def _since(hours: float | None = None) -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=hours or cfg.HARVEST_WINDOW_H)


# --- individual sources ----------------------------------------------------


def hacker_news() -> list[Item]:
    since = int(_since().timestamp())
    url = (
        "https://hn.algolia.com/api/v1/search"
        f"?tags=story&hitsPerPage=60"
        f"&numericFilters=points>{cfg.HN_MIN_POINTS},created_at_i>{since}"
    )
    hits = _get(url).json().get("hits", [])
    out = []
    for h in hits:
        link = h.get("url")
        if not link:
            continue  # self-posts have no article to summarise
        out.append(
            Item(
                title=h["title"],
                url=link,
                source="hn",
                publication="Hacker News",
                published=_utc(h["created_at_i"]),
                engagement=float(h.get("points") or 0),
            )
        )
    return out


def lobsters() -> list[Item]:
    since = _since()
    out = []
    for s in _get("https://lobste.rs/hottest.json").json():
        published = datetime.fromisoformat(s["created_at"])
        if published.tzinfo is None:
            published = published.replace(tzinfo=timezone.utc)
        if published < since or not s.get("url"):
            continue
        out.append(
            Item(
                title=s["title"],
                url=s["url"],
                source="lobsters",
                publication="Lobsters",
                published=published,
                engagement=float(s.get("score") or 0),
            )
        )
    return out


def devto() -> list[Item]:
    since = _since()
    out = []
    for a in _get("https://dev.to/api/articles?top=1&per_page=30").json():
        published = datetime.fromisoformat(a["published_timestamp"].replace("Z", "+00:00"))
        if published < since:
            continue
        out.append(
            Item(
                title=a["title"],
                url=a["url"],
                source="devto",
                publication="DEV",
                published=published,
                engagement=float(a.get("public_reactions_count") or 0),
                summary=(a.get("description") or "").strip(),
            )
        )
    return out


def _repo_title(repo: dict) -> str:
    """Avoid the redundant 'transformers: Transformers is a ...' construction."""
    name = repo["name"]
    desc = (repo.get("description") or "").strip()
    if not desc:
        return f"{name} — {repo.get('language') or 'open source'} project"
    if desc.lower().lstrip("🤗🙃 ").startswith(name.lower()):
        return desc
    return f"{name}: {desc}"


def github_trending() -> list[Item]:
    """Repos pushed recently that already have real traction."""
    date = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
    url = (
        "https://api.github.com/search/repositories"
        f"?q=pushed:>{date}+stars:>{cfg.GITHUB_MIN_STARS}"
        "&sort=stars&order=desc&per_page=30"
    )
    headers = {"Accept": "application/vnd.github+json"}
    if token := os.environ.get("GH_TOKEN"):
        headers["Authorization"] = f"Bearer {token}"

    out = []
    for r in _get(url, headers=headers).json().get("items", []):
        pushed = datetime.fromisoformat(r["pushed_at"].replace("Z", "+00:00"))
        out.append(
            Item(
                title=_repo_title(r),
                url=r["html_url"],
                source="github",
                publication="GitHub",
                published=pushed,
                engagement=float(r.get("stargazers_count") or 0),
                summary=(r.get("description") or "").strip(),
            )
        )
    return out


def arxiv() -> list[Item]:
    since = _since(cfg.ARXIV_WINDOW_H)
    query = "+OR+".join(f"cat:{c}" for c in cfg.ARXIV_CATEGORIES)
    url = (
        "http://export.arxiv.org/api/query"
        f"?search_query={query}&sortBy=submittedDate&sortOrder=descending&max_results=30"
    )
    feed = feedparser.parse(_get(url).text)
    out = []
    for e in feed.entries:
        published = datetime(*e.published_parsed[:6], tzinfo=timezone.utc)
        if published < since:
            continue
        out.append(
            Item(
                title=e.title.replace("\n", " ").strip(),
                url=e.link,
                source="arxiv",
                publication="arXiv",
                published=published,
                summary=e.summary.replace("\n", " ").strip()[:400],
            )
        )
    return out


def rss() -> list[Item]:
    since = _since()
    out = []
    for name, feed_url in cfg.RSS_FEEDS.items():
        try:
            feed = feedparser.parse(_get(feed_url).text)
        except Exception as exc:
            log.warning("rss %s failed: %s", name, exc)
            continue
        for e in feed.entries:
            parsed = getattr(e, "published_parsed", None) or getattr(e, "updated_parsed", None)
            if not parsed:
                continue
            published = datetime(*parsed[:6], tzinfo=timezone.utc)
            if published < since:
                continue
            out.append(
                Item(
                    title=e.title.strip(),
                    url=e.link,
                    source="rss",
                    publication=name,
                    published=published,
                    summary=getattr(e, "summary", "")[:400],
                )
            )
    return out


HARVESTERS: dict[str, Callable[[], list[Item]]] = {
    "hacker_news": hacker_news,
    "lobsters": lobsters,
    "devto": devto,
    "github_trending": github_trending,
    "arxiv": arxiv,
    "rss": rss,
}


def harvest_all() -> list[Item]:
    """Run every harvester. Failures are isolated, never fatal."""
    items: list[Item] = []
    failures: list[str] = []

    for name, fn in HARVESTERS.items():
        for attempt in (1, 2):
            try:
                got = fn()
                log.info("%-16s %3d items", name, len(got))
                items.extend(got)
                break
            except Exception as exc:
                if attempt == 2:
                    log.warning("%-16s FAILED: %s", name, exc)
                    failures.append(name)
                else:
                    time.sleep(2)

    if len(failures) >= len(HARVESTERS) - 1:
        raise RuntimeError(f"harvest collapsed, only {failures} responded")

    return _dedup_by_url(items)


def _dedup_by_url(items: Iterable[Item]) -> list[Item]:
    """The same story often surfaces on HN and Lobsters within an hour."""
    best: dict[str, Item] = {}
    for it in items:
        k = it.key()
        prior = best.get(k)
        if prior is None or (it.engagement or 0) > (prior.engagement or 0):
            best[k] = it
    return list(best.values())
