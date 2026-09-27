"""Meme ideas from today's trends: the third daily segment.

The model gets today's trends and Imgflip's most-used templates, and suggests a
few memes a programmer would share. It writes the joke. Everything it could get
wrong in a way that matters is checked here rather than trusted:

  * the trend must be one it was given, word for word, so it cannot invent news;
  * the template must be one it was given, filled with exactly its number of
    text boxes, so the link it comes with opens the right picture;
  * nothing about deaths, disasters, crime, politics or religion, however it
    is framed -- checked on the trend, its headline, and every word written;
  * no hashtags or emoji in the picture, and every box short enough to read.

Ideas, not finished images: you make the one you like and post it yourself.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, timedelta
from pathlib import Path

from . import config as cfg
from . import llm
from .trends import Template, Trend

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "state" / "meme_log.json"

_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]")
_URL = re.compile(r"https?://|www\.", re.I)


class Rejected(ValueError):
    """One idea failed a check. The others may still be good."""


class Unavailable(RuntimeError):
    """No model answered at all."""


_PROMPT = """You write memes for @genphile.meme, an Instagram meme page for programmers and tech people.

Today's trends. Pick from these only, and copy the trend text exactly:
{trends}

Meme templates. Pick from these only, give the id, and fill exactly the number of boxes shown:
{templates}
{avoid}
Write {n} meme ideas. Each uses a different trend and ties it to programming or tech life: code,
bugs, deadlines, standups, interviews, AI tools, production outages, the job itself. The joke must
work for someone who writes code but missed the trend, and land harder for someone who saw it.

Rules:
- A real person may appear only in a neutral or admiring comparison, never as the butt of the joke.
- Skip any trend about death, injury, disaster, crime, politics or religion.
- Box text is short and plain: no hashtags, no emoji, at most {box_max} characters per box.
- caption: one or two sentences for the Instagram post. No hashtags.
- why: one line saying where the joke is.

Reply with JSON only, no prose:
{{"ideas": [{{"trend": "...", "template_id": "...", "boxes": ["...", "..."], "caption": "...", "why": "..."}}]}}"""


def _trend_line(t: Trend) -> str:
    where = {"tech": "tech news"}.get(t.where, f"trending in {t.where}")
    extra = f" -- {t.context}" if t.context else ""
    return f'- "{t.title}" ({where}){extra}'


def prompt(trends: list[Trend], templates: list[Template], avoid: set[str], n: int) -> str:
    avoid_names = [t.name for t in templates if t.id in avoid]
    return _PROMPT.format(
        trends="\n".join(_trend_line(t) for t in trends),
        templates="\n".join(f"- id {t.id}: {t.name} ({t.boxes} boxes)" for t in templates),
        avoid=f"\nUsed in the last few days, so pick others: {', '.join(avoid_names)}\n" if avoid_names else "",
        n=n,
        box_max=cfg.MEME_BOX_MAX,
    )


def sensitive(trend: Trend) -> bool:
    return bool(cfg.MEME_SENSITIVE.search(f"{trend.title} {trend.context}"))


def usable(trends: list[Trend], recent: set[str]) -> list[Trend]:
    """Trends worth offering: not sensitive, not memed this week, not repeated."""
    out, seen = [], set()
    for t in trends:
        key = t.title.casefold()
        if key in seen or key in recent:
            continue
        if sensitive(t):
            log.info("left out a sensitive trend: %s", t.title[:60])
            continue
        seen.add(key)
        out.append(t)
    return out


def validate(idea: dict, trends: list[Trend], templates: list[Template]) -> dict:
    """The idea in its final shape, or Rejected with the reason."""
    if not isinstance(idea, dict):
        raise Rejected("not an object")
    by_title = {t.title.casefold(): t for t in trends}
    trend = by_title.get(str(idea.get("trend", "")).strip().casefold())
    if trend is None:
        raise Rejected(f"trend not in today's list: {str(idea.get('trend'))[:60]!r}")

    template = next((t for t in templates if t.id == str(idea.get("template_id", "")).strip()), None)
    if template is None:
        raise Rejected(f"unknown template id {idea.get('template_id')!r}")

    boxes = idea.get("boxes")
    if not isinstance(boxes, list) or not all(isinstance(b, str) for b in boxes):
        raise Rejected("boxes is not a list of text")
    boxes = [b.strip() for b in boxes]
    if len(boxes) != template.boxes or not all(boxes):
        raise Rejected(f"{template.name} takes {template.boxes} boxes, got {len(boxes)}")
    for b in boxes:
        if len(b) > cfg.MEME_BOX_MAX:
            raise Rejected(f"a box runs to {len(b)} characters")
        if "#" in b or _EMOJI.search(b) or _URL.search(b):
            raise Rejected("a box carries a hashtag, emoji or link")

    text = str(idea.get("caption", "")).strip()
    if not text or len(text) > cfg.MEME_CAPTION_MAX:
        raise Rejected("caption missing or too long")
    if "#" in text or _URL.search(text):
        raise Rejected("caption carries a hashtag or link")
    why = str(idea.get("why", "")).strip()[:200]

    if cfg.MEME_SENSITIVE.search(" ".join([*boxes, text, why])):
        raise Rejected("touches a subject the page never jokes about")

    return {
        "trend": trend.title,
        "where": trend.where,
        "context": trend.context,
        "template_id": template.id,
        "template": template.name,
        "maker": template.maker,
        "boxes": boxes,
        "caption": caption(text),
        "why": why,
    }


def caption(text: str) -> str:
    tags = " ".join(cfg.MEME_HASHTAGS[: cfg.HASHTAG_COUNT])
    return f"{text}\n\n{tags}"[: cfg.CAPTION_MAX]


def _parse(raw: str) -> list:
    body = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    data = json.loads(body)
    ideas = data.get("ideas") if isinstance(data, dict) else data
    if not isinstance(ideas, list):
        raise ValueError("no ideas list")
    return ideas


def draft(trends: list[Trend], templates: list[Template], avoid: set[str]) -> tuple[list[dict], list[str]]:
    """Up to MEME_IDEAS checked ideas, and why the rest were thrown away.

    Raises Unavailable when no model answers, so the day can say so instead
    of passing off an outage as a quiet day.
    """
    ideas: list[dict] = []
    rejects: list[str] = []
    for attempt in range(cfg.MEME_ATTEMPTS):
        wanted = cfg.MEME_IDEAS - len(ideas)
        used = {i["trend"].casefold() for i in ideas}
        left = [t for t in trends if t.title.casefold() not in used]
        if wanted <= 0 or not left:
            break
        reply = llm.complete(prompt(left, templates, avoid, wanted), temperature=0.9 + 0.05 * attempt, max_tokens=1200)
        if not reply.text:
            raise Unavailable(reply.error or "no reply")
        try:
            candidates = _parse(reply.text)
        except (ValueError, AttributeError) as exc:
            rejects.append(f"reply was not the JSON asked for ({type(exc).__name__})")
            continue
        for c in candidates:
            try:
                idea = validate(c, left, templates)
            except Rejected as exc:
                rejects.append(str(exc))
                continue
            if idea["trend"].casefold() in used:
                rejects.append("second idea on the same trend")
                continue
            used.add(idea["trend"].casefold())
            ideas.append(idea)
            if len(ideas) >= cfg.MEME_IDEAS:
                break
    return ideas, rejects


# --- the log: what has been suggested, so the week does not repeat itself ------

def load_log() -> list[dict]:
    try:
        entries = json.loads(LOG_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def recent(entries: list[dict], field: str, days: int, today: date) -> set[str]:
    since = (today - timedelta(days=days)).isoformat()
    return {str(e[field]).casefold() for e in entries if e.get("date", "") > since and e.get(field)}


def record(entries: list[dict], ideas: list[dict], today: date) -> list[dict]:
    """The log with today's ideas added and anything past MEME_LOG_DAYS dropped."""
    since = (today - timedelta(days=cfg.MEME_LOG_DAYS)).isoformat()
    kept = [e for e in entries if e.get("date", "") > since]
    kept += [{"date": today.isoformat(), "trend": i["trend"], "template_id": i["template_id"]} for i in ideas]
    return kept


def save_log(entries: list[dict]) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.write_text(json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
