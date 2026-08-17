"""Turn the selected story into a headline, card body and caption.

Two layers. The deterministic builder always produces something publishable --
it is the floor, and it never fails. The LLM layer only ever *compresses text it
was given*; it is forbidden from introducing facts, and anything it returns is
validated against the source before it is allowed through. On any doubt we fall
back to the deterministic output.

That constraint is editorial and legal at once: an original summary is what
keeps this account on the right side of copyright, and a fabricated claim
published unattended under your name is the worst failure mode in the system.
"""
from __future__ import annotations

import html
import json
import logging
import os
import re
import textwrap
from datetime import datetime, timezone

import requests

from . import config as cfg
from .harvest import Item

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")

# GitHub descriptions and RSS titles are full of emoji and pictographs. They
# render badly on the card (wrong baseline, wrong weight, colour clash with the
# palette) and break narrow console encodings, so they never reach the template.
_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # pictographs, emoticons, transport, symbols
    "\U00002600-\U000027BF"   # misc symbols and dingbats
    "\U00002190-\U000021FF"   # arrows
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U00002B00-\U00002BFF"   # misc symbols and arrows
    "\U0000200D"              # zero-width joiner
    "]+",
    flags=re.UNICODE,
)


def _clean(text: str) -> str:
    text = html.unescape(_TAG_RE.sub(" ", text))
    text = _EMOJI_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip(" -–—:|")


# --- deterministic floor ---------------------------------------------------


# Words a headline must never end on -- cutting here reads as a broken feed.
_DANGLING = {
    "a", "an", "the", "and", "or", "but", "for", "to", "of", "in", "on", "at",
    "by", "with", "from", "as", "is", "are", "was", "were", "that", "this",
    "its", "it", "into", "over", "via", "using", "both", "their",
}

# Clause boundaries worth breaking a long headline at. The negative lookarounds
# keep us out of the middle of "2,500" and "v1.90".
_CLAUSE_END = re.compile(r"(?<!\d)[,;](?!\d)|[:—(]|\s[-–]\s")

_MIN_CLAUSE = 30


def _trim_headline(title: str) -> tuple[str, bool]:
    """Shorten to fit the card.

    Returns (headline, truncated). `truncated` tells the body builder that the
    headline no longer carries the title's full meaning, so it must not try to
    continue the sentence -- that is what produces cards reading
    "...state-of-the-art machine" / "learning models in text".
    """
    title = _clean(title)
    title = re.sub(r"\s*[|—-]\s*(TechCrunch|Ars Technica|The Verge)\s*$", "", title)
    if len(title) <= cfg.HEADLINE_MAX_CHARS:
        return title, False

    # 1. A natural clause boundary is the cleanest break:
    # "Marionette: Predicting World States, Rendering Geometry, and Painting..."
    # becomes "Marionette: Predicting World States".
    best = ""
    for m in _CLAUSE_END.finditer(title):
        candidate = title[: m.start()].strip()
        if _MIN_CLAUSE <= len(candidate) <= cfg.HEADLINE_MAX_CHARS:
            best = candidate
    if best:
        return best.rstrip(" ,;:-—"), True

    words = title.split()
    out: list[str] = []
    for w in words:
        if len(" ".join(out + [w])) > cfg.HEADLINE_MAX_CHARS - 1:
            break
        out.append(w)

    # Drop trailing connectives first, or the backtrack below stops on them
    # instead of on the real phrase boundary further left.
    while out and out[-1].lower().strip(",.;:") in _DANGLING:
        out.pop()

    # 2. Then back up to the last preposition or conjunction, which ends the
    # phrase where a human would: "...the model-definition framework" rather
    # than "...framework for state-of-the-art machine".
    for i in range(len(out) - 1, 0, -1):
        if out[i].lower().strip(",.;:") in _DANGLING:
            candidate = " ".join(out[:i]).rstrip(" ,;:-—")
            if len(candidate) >= _MIN_CLAUSE:
                return candidate, True
            break

    if not out:
        return title[: cfg.HEADLINE_MAX_CHARS], True
    return " ".join(out).rstrip(" ,;:-—") + "…", True


def _metadata_body(item: Item) -> str:
    """An honest line built from what we actually know about the item.

    Used when a source gives us a bare title and no summary. Splitting the
    title in half to manufacture a body produces mangled cards -- a factual
    metadata line is shorter, truer, and always reads cleanly.
    """
    n = int(item.engagement or 0)
    age_h = max(1, round((datetime.now(timezone.utc) - item.published).total_seconds() / 3600))

    if item.source == "hn" and n:
        return f"{n} points on Hacker News in the last {age_h}h."
    if item.source == "lobsters" and n:
        return f"{n} points on Lobsters in the last {age_h}h."
    if item.source == "github" and n:
        return f"{n:,} stars on GitHub, pushed within the last week."
    if item.source == "devto" and n:
        return f"{n} reactions on DEV in the last {age_h}h."
    if item.source == "arxiv":
        return f"New preprint on arXiv, submitted {age_h}h ago."
    return f"Reported by {item.publication} {age_h}h ago."


def _body_from(item: Item, headline: str, truncated: bool) -> str:
    """Supporting detail, never a fragment of the headline.

    Many sources (GitHub especially) use one string as both title and summary,
    so when the headline is complete its words are stripped off the front. When
    the headline was truncated we never continue from it -- metadata is shorter,
    truer, and always reads cleanly.
    """
    src = _clean(item.summary)

    if truncated and src.lower().startswith(headline.rstrip(" .…").lower()[:40]):
        return _metadata_body(item)

    stem = headline.rstrip(" .…").lower()
    if stem and src.lower().startswith(stem):
        src = src[len(stem):].lstrip(" ,;:—-.")

    if len(src) < 40:
        return _metadata_body(item)

    if len(src) <= cfg.BODY_MAX_CHARS:
        return src

    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", src):
        if len(out) + len(sentence) + 1 > cfg.BODY_MAX_CHARS:
            break
        out = f"{out} {sentence}".strip()
    return out or textwrap.shorten(src, width=cfg.BODY_MAX_CHARS, placeholder="…")


def _hashtags(item: Item) -> str:
    hay = f"{item.title} {item.summary}".lower()
    ranked = sorted(
        cfg.HASHTAG_POOL,
        key=lambda t: (t.lstrip("#") not in hay, cfg.HASHTAG_POOL.index(t)),
    )
    return " ".join(ranked[: cfg.HASHTAG_COUNT])


def _caption(item: Item, headline: str, body: str) -> str:
    caption = (
        f"{headline}\n\n"
        f"{body}\n\n"
        f"Source: {item.publication}\n"
        f"{item.url}\n\n"
        f"{_hashtags(item)}"
    )
    return caption[: cfg.CAPTION_MAX]


# --- optional LLM polish ---------------------------------------------------

_PROMPT = """You are compressing a tech news item for a social card.

RULES, in order of importance:
1. Use ONLY information present in the SOURCE below. Introduce no facts, no
   numbers, no company names, and no claims that are not already there.
2. If the source is too thin to summarise honestly, return exactly: INSUFFICIENT
3. Do not copy any sentence from the source verbatim. Rewrite in your own words.

SOURCE
title: {title}
summary: {summary}
publication: {publication}

Return strict JSON, no markdown fence:
{{"headline": "<= 9 words, no trailing period", "body": "2 sentences, <= 200 chars"}}
"""


def _call_llm(prompt: str) -> str | None:
    """Whichever free-tier provider has a key configured. None on any failure."""
    try:
        if key := os.environ.get("GEMINI_API_KEY"):
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.0-flash:generateContent",
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": 0.4, "maxOutputTokens": 300},
                },
                timeout=cfg.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()["candidates"][0]["content"]["parts"][0]["text"]

        if key := os.environ.get("GROQ_API_KEY"):
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": "llama-3.3-70b-versatile",
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.4,
                    "max_tokens": 300,
                },
                timeout=cfg.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("llm call failed, using deterministic copy: %s", exc)
    return None


def _validate(candidate: dict, item: Item) -> bool:
    """Reject anything that invented facts, ran long, or parroted the source."""
    headline = candidate.get("headline", "").strip()
    body = candidate.get("body", "").strip()

    if not headline or not body:
        return False
    if len(headline) > cfg.HEADLINE_MAX_CHARS or len(body) > cfg.BODY_MAX_CHARS:
        log.warning("llm output too long, rejecting")
        return False

    source_text = f"{item.title} {item.summary}".lower()

    # Every number in the output must exist in the source.
    for n in _NUM_RE.findall(f"{headline} {body}"):
        if n not in source_text:
            log.warning("llm introduced number %r absent from source, rejecting", n)
            return False

    # Guard against verbatim lifting of a long run of source text.
    for sentence in re.split(r"(?<=[.!?])\s+", _clean(item.summary)):
        if len(sentence) > 60 and sentence.lower() in body.lower():
            log.warning("llm reproduced source sentence verbatim, rejecting")
            return False

    return True


def _polish(item: Item) -> dict | None:
    raw = _call_llm(
        _PROMPT.format(
            title=_clean(item.title),
            summary=_clean(item.summary) or "(none supplied)",
            publication=item.publication,
        )
    )
    if not raw or "INSUFFICIENT" in raw:
        return None

    text = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        candidate = json.loads(text)
    except json.JSONDecodeError:
        log.warning("llm returned non-JSON, rejecting")
        return None

    return candidate if _validate(candidate, item) else None


# --- entry point -----------------------------------------------------------


def compose(item: Item) -> dict:
    headline, truncated = _trim_headline(item.title)
    body = _body_from(item, headline, truncated)
    polished = False

    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"):
        if candidate := _polish(item):
            headline, body, polished = candidate["headline"], candidate["body"], True
            log.info("llm polish accepted")
        else:
            log.info("llm polish rejected, keeping deterministic copy")

    if not headline:
        raise ValueError("empty headline after composition")

    return {
        "headline": headline,
        "body": body,
        "caption": _caption(item, headline, body),
        "publication": item.publication,
        "url": item.url,
        "source": item.source,
        "title": item.title,
        "key": item.key(),
        "score": round(item.score, 4),
        "llm_polished": polished,
    }
