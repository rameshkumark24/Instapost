"""Turn the selected story into a headline, card body and caption.

Two layers. The deterministic builder always produces something publishable --
it is the floor, and it never fails. The LLM layer only ever *compresses text it
was given*; it is forbidden from introducing facts, and anything it returns is
validated against the source before it is allowed through. On any doubt we fall
back to the deterministic output.

Whose words reach the card, and who is credited for them, is decided here:

  * An LLM body is our own summary. It is checked against the source for
    invented numbers and for runs of copied wording before it is used.
  * A deterministic body built from a publisher's share text is *their* words,
    so it is printed as a quotation and credited to them by name -- the way a
    link preview presents it -- never passed off as ours.
  * A metadata line ("616 points on Hacker News") is a fact about the story.

Credit goes to whoever wrote the words the reader sees, with the aggregator
noted as where the story was found. A fabricated or misattributed line
published unattended under your name is the worst failure mode in the system.
"""
from __future__ import annotations

import dataclasses
import html
import json
import logging
import os
import re
import textwrap
from datetime import datetime, timezone

from . import config as cfg
from . import llm
from .enrich import Enriched
from .enrich import fetch as fetch_enrichment
from .harvest import Item

log = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")
_WORD_RE = re.compile(r"[a-z0-9']+")

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

# An LLM body sharing this many consecutive words with the source is copying,
# not summarising. Short enough to catch a lifted clause, long enough that
# ordinary phrasing ("one of the most popular") does not trip it.
_COPIED_RUN_WORDS = 8


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


def _trim_to(text: str, limit: int) -> str:
    """The longest run of whole sentences that fits, else a word-boundary cut.

    This is the only place body text is shortened. Anything upstream that cut
    to length first would hand this function text already under the limit, and
    a mid-word fragment would reach the card untouched.
    """
    if len(text) <= limit:
        return text
    out = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    return out or textwrap.shorten(text, width=limit, placeholder="…")


def _nest_quotes(text: str) -> str:
    """Turn double quotation marks inside a quotation into single ones.

    The card wraps a publisher's words in “…”, so a “…” already inside them
    prints as a quotation that appears to end halfway through. Same length in,
    same length out, so trimming is unaffected.
    """
    text = text.replace("“", "‘").replace("”", "’")
    out, opening = [], True
    for ch in text:
        if ch == '"':
            out.append("‘" if opening else "’")
            opening = not opening
        else:
            out.append(ch)
    return "".join(out)


def _body_from(item: Item, headline: str, truncated: bool, quoted: bool = False) -> str:
    """Supporting detail, never a fragment of the headline.

    Many sources (GitHub especially) use one string as both title and summary,
    so when the headline is complete its words are stripped off the front. When
    the headline was truncated we never continue from it -- metadata is shorter,
    truer, and always reads cleanly.

    `quoted` marks the summary as a publisher's own words. Those are never
    edited into a fragment: they are trimmed only at sentence boundaries and
    printed inside quotation marks.
    """
    src = _clean(item.summary)
    stem = headline.rstrip(" .…").lower()

    if quoted:
        # A quotation that only repeats the headline adds nothing, and cutting
        # the repeated words off the front would misquote the publisher.
        if len(src) < 40 or (stem and src.lower().startswith(stem)):
            return _metadata_body(item)
        return f"“{_nest_quotes(_trim_to(src, cfg.BODY_MAX_CHARS - 2))}”"

    if truncated and src.lower().startswith(stem[:40]):
        return _metadata_body(item)

    if stem and src.lower().startswith(stem):
        src = src[len(stem):].lstrip(" ,;:—-.")

    if len(src) < 40:
        return _metadata_body(item)

    return _trim_to(src, cfg.BODY_MAX_CHARS)


def _enrich(item: Item) -> tuple[Item, Enriched | None]:
    """Fetch the publisher's share text for sources that send no summary.

    Runs once, before either composition path, so the LLM is given the same
    source text the deterministic body would have used and is validated
    against it. Scoped by source rather than by summary length: a GitHub repo
    with a terse description would otherwise "enrich" into GitHub's own page
    boilerplate.
    """
    if item.source not in cfg.ENRICH_SOURCES or len(_clean(item.summary)) >= 40:
        return item, None
    got = fetch_enrichment(item.url)
    if got is None:
        return item, None
    log.info("enriched from %s", got.site_name)
    return dataclasses.replace(item, summary=got.description), got


def _hashtags(item: Item) -> str:
    hay = f"{item.title} {item.summary}".lower()
    ranked = sorted(
        cfg.HASHTAG_POOL,
        key=lambda t: (t.lstrip("#") not in hay, cfg.HASHTAG_POOL.index(t)),
    )
    return " ".join(ranked[: cfg.HASHTAG_COUNT])


def _caption(item: Item, headline: str, body: str, credit: str) -> str:
    caption = (
        f"{headline}\n\n"
        f"{body}\n\n"
        f"Source: {credit}\n"
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
    """The polish model's reply, or None. The deterministic copy is the floor."""
    reply = llm.complete(prompt, temperature=0.4)
    if reply.error:
        log.warning("llm unavailable, keeping deterministic copy: %s", reply.error)
    return reply.text


def _words(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower().replace("’", "'"))


def _copied_run(source: str, body: str, n: int = _COPIED_RUN_WORDS) -> bool:
    """True if `body` repeats any n consecutive words of `source`."""
    src = _words(source)
    haystack = f" {' '.join(_words(body))} "
    return any(
        f" {' '.join(src[i:i + n])} " in haystack
        for i in range(len(src) - n + 1)
    )


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

    # An accepted LLM body is published as our own words, so it must be.
    if _copied_run(_clean(item.summary), body):
        log.warning("llm reproduced a run of source wording, rejecting")
        return False

    return True


def _polish(item: Item, publication: str) -> dict | None:
    raw = _call_llm(
        _PROMPT.format(
            title=_clean(item.title),
            summary=_clean(item.summary) or "(none supplied)",
            publication=publication,
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
    found_via = item.publication
    item, enriched = _enrich(item)

    # When the words came from a publisher's page, the publisher is the source
    # and the aggregator is only where the story was found.
    publication = enriched.site_name if enriched else found_via
    credit = f"{publication} (found via {found_via})" if enriched else found_via

    headline, truncated = _trim_headline(item.title)
    body = _body_from(item, headline, truncated, quoted=enriched is not None)
    polished = False

    if os.environ.get("GEMINI_API_KEY") or os.environ.get("GROQ_API_KEY"):
        if candidate := _polish(item, publication):
            headline, body, polished = candidate["headline"], candidate["body"], True
            log.info("llm polish accepted")
        else:
            log.info("llm polish rejected, keeping deterministic copy")

    if not headline:
        raise ValueError("empty headline after composition")

    return {
        "headline": headline,
        "body": body,
        "caption": _caption(item, headline, body, credit),
        "publication": publication,
        "found_via": found_via,
        "url": item.url,
        "source": item.source,
        "title": item.title,
        "key": item.key(),
        "score": round(item.score, 4),
        "llm_polished": polished,
        "enriched": enriched is not None,
    }
