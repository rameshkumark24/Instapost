"""Meme ideas for developers: the third daily segment.

Every idea is a joke about developer life. Two hang on today's tech news --
the stories the news build has already scored -- and one on a search trending
outside tech, turned into a programming joke (MEME_MIX). If one pool is empty
on the day, the other makes up the three.

The model writes the joke. Everything it could get wrong in a way that matters
is checked here rather than trusted:

  * the trend must be one it was given, named by its id, so it cannot invent
    news -- and the split between tech and trending is enforced, not asked for;
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
from .trends import Template, Trend, latin

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
LOG_FILE = ROOT / "state" / "meme_log.json"

_EMOJI = re.compile(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]")
_URL = re.compile(r"https?://|www\.", re.I)

TECH, CURRENT = "tech", "current"


class Rejected(ValueError):
    """One idea failed a check. The others may still be good."""


class Unavailable(RuntimeError):
    """No model answered at all."""


def pool(trend: Trend) -> str:
    return TECH if trend.where == "tech" else CURRENT


def quotas(trends: list[Trend]) -> dict[str, int]:
    """Ideas each pool gives today: MEME_MIX, with a short pool's share moved to the other."""
    size = {TECH: sum(1 for t in trends if pool(t) == TECH)}
    size[CURRENT] = len(trends) - size[TECH]
    q = {p: min(cfg.MEME_MIX[p], size[p]) for p in (TECH, CURRENT)}
    short = cfg.MEME_IDEAS - sum(q.values())
    for p in (TECH, CURRENT):
        extra = min(short, size[p] - q[p])
        q[p] += extra
        short -= extra
    return q


def catalogue(trends: list[Trend]) -> dict[str, Trend]:
    """Short ids for the model to answer with. A tech headline is too long to
    expect back word for word, and a near-miss would throw a good idea away."""
    out: dict[str, Trend] = {}
    tech = [t for t in trends if pool(t) == TECH]
    current = [t for t in trends if pool(t) == CURRENT]
    out.update({f"T{i}": t for i, t in enumerate(tech, 1)})
    out.update({f"G{i}": t for i, t in enumerate(current, 1)})
    return out


_PROMPT = """You write memes for @genphile.meme, an Instagram meme page for programmers.
Every meme is about developer life: code, bugs, deadlines, standups, code review, interviews,
AI tools, production outages, the job itself.
{sections}
Meme templates. Give the id, and fill exactly the number of boxes shown:
{templates}
{avoid}
Rules:
- Each idea uses a different trend, named by its id in brackets.
- A real person may appear only in a neutral or admiring comparison, never as the butt of the joke.
- Skip any trend about death, injury, disaster, crime, politics or religion.
- Box text is short and plain: no hashtags, no emoji, at most {box_max} characters per box.
- caption: one or two sentences for the Instagram post. No hashtags.
- why: one line saying where the joke is.

Reply with JSON only, no prose:
{{"ideas": [{{"trend": "T1", "template_id": "...", "boxes": ["...", "..."], "caption": "...", "why": "..."}}]}}"""

_SECTION = {
    TECH: "\nTech news trending today. Write {n} idea(s), each on a different one of these:\n{lines}\n",
    CURRENT: "\nTrending searches today, outside tech. Write {n} idea(s), each on a different one of these,\n"
             "turned into a joke about developer life:\n{lines}\n",
}


def _line(tid: str, t: Trend) -> str:
    where = "" if t.where == "tech" else f" (trending in {t.where})"
    extra = f" -- {t.context}" if t.context else ""
    return f'[{tid}] "{t.title}"{where}{extra}'


def prompt(cat: dict[str, Trend], templates: list[Template], avoid: set[str], need: dict[str, int]) -> str:
    sections = "".join(
        _SECTION[p].format(n=need[p], lines="\n".join(_line(i, t) for i, t in cat.items() if pool(t) == p))
        for p in (TECH, CURRENT) if need.get(p)
    )
    avoid_names = [t.name for t in templates if t.id in avoid]
    return _PROMPT.format(
        sections=sections,
        templates="\n".join(f"- id {t.id}: {t.name} ({t.boxes} boxes)" for t in templates),
        avoid=f"\nUsed in the last few days, so pick others: {', '.join(avoid_names)}\n" if avoid_names else "",
        box_max=cfg.MEME_BOX_MAX,
    )


def sensitive(trend: Trend) -> bool:
    return bool(cfg.MEME_SENSITIVE.search(f"{trend.title} {trend.context}"))


def usable(trends: list[Trend], recent: set[str]) -> list[Trend]:
    """Trends worth offering: readable, not sensitive, not memed this week, not repeated."""
    out, seen = [], set()
    for t in trends:
        key = t.title.casefold()
        if key in seen or key in recent or not latin(t.title):
            continue
        if sensitive(t):
            log.info("left out a sensitive trend: %s", t.title[:60])
            continue
        seen.add(key)
        out.append(t)
    return out


def validate(idea: dict, cat: dict[str, Trend], templates: list[Template]) -> dict:
    """The idea in its final shape, or Rejected with the reason."""
    if not isinstance(idea, dict):
        raise Rejected("not an object")
    tid = str(idea.get("trend", "")).strip().strip("[]").upper()
    trend = cat.get(tid)
    if trend is None:
        raise Rejected(f"trend {str(idea.get('trend'))[:40]!r} is not one it was given")

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
        "pool": pool(trend),
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
    """Checked ideas in the MEME_MIX split -- tech first -- and why the rest were thrown away.

    Raises Unavailable when no model answers, so the day can say so instead
    of passing off an outage as a quiet day.
    """
    target = quotas(trends)
    ideas: list[dict] = []
    rejects: list[str] = []
    for attempt in range(cfg.MEME_ATTEMPTS):
        have = {p: sum(1 for i in ideas if i["pool"] == p) for p in target}
        need = {p: target[p] - have[p] for p in target}
        used = {i["trend"].casefold() for i in ideas}
        cat = catalogue([t for t in trends if t.title.casefold() not in used
                         and need[pool(t)] > 0])
        if not cat:
            break
        reply = llm.complete(prompt(cat, templates, avoid, need),
                             temperature=0.9 + 0.05 * attempt, max_tokens=1200)
        if not reply.text:
            raise Unavailable(reply.error or "no reply")
        try:
            candidates = _parse(reply.text)
        except (ValueError, AttributeError) as exc:
            rejects.append(f"reply was not the JSON asked for ({type(exc).__name__})")
            continue
        for c in candidates:
            try:
                idea = validate(c, cat, templates)
            except Rejected as exc:
                rejects.append(str(exc))
                continue
            if idea["trend"].casefold() in used:
                rejects.append("second idea on the same trend")
                continue
            if need[idea["pool"]] <= 0:
                rejects.append(f"one {idea['pool']} idea more than asked for")
                continue
            need[idea["pool"]] -= 1
            used.add(idea["trend"].casefold())
            ideas.append(idea)
    ideas.sort(key=lambda i: i["pool"] != TECH)      # tech first, stable within a pool
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
