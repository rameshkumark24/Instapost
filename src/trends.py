"""What people are talking about today, for the meme ideas.

Three inputs, all official and keyless:

  * Google Trends' daily RSS, per country: the searches that spiked today;
  * Imgflip's /get_memes: the templates captioned most over the last 30 days,
    which is as close to "the formats people recognise right now" as any
    public source gets;
  * today's top tech stories, which the news build has already harvested and
    scored, and leaves in dist/news/trending.json.

A source that fails is logged and left out. Ideas from two sources beat none.
"""
from __future__ import annotations

import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from . import config as cfg

log = logging.getLogger(__name__)

TRENDS_URL = "https://trends.google.com/trending/rss?geo={geo}"
TEMPLATES_URL = "https://api.imgflip.com/get_memes"
_HT = "{https://trends.google.com/trending/rss}"

ROOT = Path(__file__).resolve().parent.parent
TECH_FILE = ROOT / "dist" / "news" / "trending.json"


@dataclass(frozen=True)
class Trend:
    title: str
    where: str              # "IN", "US", or "tech"
    context: str = ""       # a headline saying why it is trending
    traffic: str = ""       # Google's rough search count, such as "200K+"


@dataclass(frozen=True)
class Template:
    id: str
    name: str
    boxes: int

    @property
    def maker(self) -> str:
        """Imgflip's editor with this template loaded: type the boxes, download."""
        return f"https://imgflip.com/memegenerator/{self.id}"


def _get(url: str) -> requests.Response:
    r = requests.get(url, timeout=cfg.HTTP_TIMEOUT, headers={"User-Agent": cfg.USER_AGENT})
    r.raise_for_status()
    return r


def parse_trends(xml: bytes, geo: str) -> list[Trend]:
    # A feed never needs a DTD. Refusing one rules out entity-expansion tricks
    # outright, rather than trusting the parser to survive them.
    if b"<!DOCTYPE" in xml or b"<!ENTITY" in xml:
        raise ValueError("feed declares a DTD")
    out = []
    for item in ET.fromstring(xml).iter("item"):
        title = (item.findtext("title") or "").strip()
        if not title:
            continue
        # Only the first headline: feeds list the English one first, and one is
        # enough for the model to know what the search is about.
        news = item.find(f"{_HT}news_item")
        context = (news.findtext(f"{_HT}news_item_title") or "").strip() if news is not None else ""
        traffic = (item.findtext(f"{_HT}approx_traffic") or "").strip()
        out.append(Trend(title=title, where=geo, context=context, traffic=traffic))
    return out[: cfg.MEME_TRENDS_PER_GEO]


def parse_templates(payload: dict) -> list[Template]:
    if not payload.get("success"):
        raise ValueError("imgflip answered without success")
    out = []
    for m in payload["data"]["memes"]:
        boxes = int(m.get("box_count", 0))
        if 2 <= boxes <= cfg.MEME_MAX_BOXES:
            out.append(Template(id=str(m["id"]), name=str(m["name"]).strip(), boxes=boxes))
    return out[: cfg.MEME_TEMPLATE_POOL]


def tech_stories(today: date) -> list[Trend]:
    """Today's top tech stories, if the news build ran today and left them."""
    try:
        data = json.loads(TECH_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if data.get("date") != today.isoformat():
        return []           # yesterday's stories are not today's talk
    return [
        Trend(title=s["title"], where="tech", context=s.get("publication", ""))
        for s in data.get("stories", [])[: cfg.MEME_TECH_STORIES]
        if s.get("title")
    ]


def gather(today: date) -> tuple[list[Trend], list[Template], list[str]]:
    """Every trend and template that could be fetched, and what could not."""
    trends: list[Trend] = []
    problems: list[str] = []
    for geo in cfg.MEME_TREND_GEOS:
        try:
            got = parse_trends(_get(TRENDS_URL.format(geo=geo)).content, geo)
            log.info("google trends %s: %d", geo, len(got))
            trends += got
        except Exception as exc:
            log.warning("google trends %s failed: %s", geo, exc)
            problems.append(f"Google Trends {geo} unavailable ({type(exc).__name__})")

    tech = tech_stories(today)
    log.info("tech stories from today's news build: %d", len(tech))
    trends += tech

    templates: list[Template] = []
    try:
        templates = parse_templates(_get(TEMPLATES_URL).json())
        log.info("imgflip templates: %d", len(templates))
    except Exception as exc:
        log.warning("imgflip failed: %s", exc)
        problems.append(f"Imgflip templates unavailable ({type(exc).__name__})")
    return trends, templates, problems
