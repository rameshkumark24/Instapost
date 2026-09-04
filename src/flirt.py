"""Generate tech-metaphor cards: a real concept taught through a relatable line.

The format is a wordplay card -- "she was my PRIMARY KEY... then she became a
FOREIGN KEY in someone else's table" -- where the joke only lands if the
technical meaning is actually correct. That is the whole editorial bet: the
teaching is what makes it worth following, and the metaphor is what makes it
worth sharing.

Humour fails differently from news. It does not fail by being wrong; it fails
by being crude, mean, or cringe, and one bad line published under your name
undoes a month of good ones. So nothing generated here reaches Instagram
directly: every candidate goes into a queue that a human approves in batches.
The gates below are the second line of defence, not the first.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
from pathlib import Path

import requests

from . import config as cfg

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
CONCEPTS = ROOT / "state" / "concepts.json"

# Hard vetoes. Anything matching is discarded without appeal -- these are the
# failure modes that would embarrass the account, and no amount of cleverness
# elsewhere in the line earns an exception.
BANNED = re.compile(
    r"\b("
    r"sex|sexy|nude|naked|horny|hookup|thirst|smash|bed|kink|"          # explicit
    r"slut|whore|bitch|hoe|simp|incel|friendzone|"                      # demeaning
    r"woman|women|girls|females|men are|all guys|"                      # generalising
    r"kill|die|suicide|depress|worthless|pathetic|revenge|toxic|creep"  # dark
    r")\b",
    re.IGNORECASE,
)

# The line must read as a metaphor about a person, not as documentation.
PERSONAL = re.compile(r"\b(she|he|they|her|him|them|you|your|my|me|i|we|us)\b", re.IGNORECASE)

MIN_CHARS, MAX_CHARS = 60, 190
MAX_LINES = 4


class Rejected(ValueError):
    """A candidate that failed a gate. Never a crash -- just a discarded draft."""


def load_concepts() -> list[dict]:
    return json.loads(CONCEPTS.read_text(encoding="utf-8"))


def unused_concepts(used_ids: set[str]) -> list[dict]:
    return [c for c in load_concepts() if c["id"] not in used_ids]


_PROMPT = """Write a short, clever card for a tech-humour Instagram account.

It uses one real programming concept as a metaphor for an ordinary human
situation -- a relationship, a friendship, a job, growing up. The joke only
works if the technical meaning is accurate, so the metaphor must genuinely
match how the concept behaves.

CONCEPT: {term}
WHAT IT ACTUALLY MEANS: {meaning}

RULES:
- 1 to 3 short sentences. Under 180 characters total.
- Use the exact term "{term}" once, in capitals.
- The metaphor must be true to the technical meaning. That is the point.
- Wry and knowing, never crude, never bitter, never insulting anyone.
- Never generalise about a gender or group.
- No emoji, no hashtags, no quotation marks around the whole line.

GOOD EXAMPLE (for FOREIGN KEY):
She was my PRIMARY KEY. Unique, irreplaceable. Then she became a FOREIGN KEY in someone else's table.

Return strict JSON, no markdown fence:
{{"text": "<the line>", "terms": ["{term}"]}}
"""


def _call_llm(prompt: str, temperature: float = 0.9) -> str | None:
    """Whichever free-tier provider has a key set. None on any failure."""
    try:
        if key := os.environ.get("GEMINI_API_KEY"):
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/"
                "gemini-2.0-flash:generateContent",
                params={"key": key},
                json={
                    "contents": [{"parts": [{"text": prompt}]}],
                    "generationConfig": {"temperature": temperature, "maxOutputTokens": 300},
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
                    "temperature": temperature,
                    "max_tokens": 300,
                },
                timeout=cfg.HTTP_TIMEOUT,
            )
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
    except Exception as exc:
        log.warning("llm call failed: %s", exc)
    return None


def validate(text: str, concept: dict) -> str:
    """Every gate a candidate must clear. Raises Rejected with the reason."""
    text = re.sub(r"\s+", " ", text or "").strip().strip('"').strip()

    if not text:
        raise Rejected("empty")
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        raise Rejected(f"length {len(text)} outside {MIN_CHARS}-{MAX_CHARS}")
    if hit := BANNED.search(text):
        raise Rejected(f"banned term {hit.group(0)!r}")
    if concept["term"].lower() not in text.lower():
        raise Rejected(f"does not use the term {concept['term']!r}")
    if not PERSONAL.search(text):
        raise Rejected("reads as documentation, not as a metaphor about a person")
    if text.count(".") + text.count("!") + text.count("?") > MAX_LINES:
        raise Rejected("too many sentences for the card")
    if re.search(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]", text):
        raise Rejected("contains emoji")
    if "#" in text:
        raise Rejected("contains a hashtag")
    return text


def generate(concept: dict, attempts: int = 3) -> dict:
    """One validated candidate for a concept, or raise Rejected."""
    prompt = _PROMPT.format(term=concept["term"], meaning=concept["meaning"])
    reasons = []

    for i in range(attempts):
        raw = _call_llm(prompt, temperature=0.85 + 0.05 * i)
        if not raw:
            reasons.append("llm unavailable")
            break
        body = raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            reasons.append("non-JSON reply")
            continue
        try:
            text = validate(parsed.get("text", ""), concept)
        except Rejected as exc:
            reasons.append(str(exc))
            continue

        terms = [t for t in parsed.get("terms", []) if t and t.lower() in text.lower()]
        return {
            "concept": concept["id"],
            "term": concept["term"],
            "domain": concept["domain"],
            "text": text,
            "terms": terms or [concept["term"]],
        }

    raise Rejected(f"{concept['id']}: {'; '.join(reasons) or 'no candidate'}")


def generate_batch(used_ids: set[str], size: int) -> tuple[list[dict], list[str]]:
    """Draft `size` candidates from concepts not yet used. Returns (ok, rejects)."""
    pool = unused_concepts(used_ids)
    if not pool:
        raise Rejected("concept bank exhausted -- add more to state/concepts.json")

    random.shuffle(pool)
    out, rejects = [], []
    for concept in pool:
        if len(out) >= size:
            break
        try:
            out.append(generate(concept))
        except Rejected as exc:
            rejects.append(str(exc))

    log.info("drafted %d candidates, %d rejected by gates", len(out), len(rejects))
    return out, rejects


def caption(entry: dict) -> str:
    """Caption carries the actual teaching. The card is the hook; this is why to follow."""
    concept = next((c for c in load_concepts() if c["id"] == entry["concept"]), None)
    meaning = concept["meaning"] if concept else ""
    tags = " ".join(cfg.FLIRT_HASHTAGS[: cfg.HASHTAG_COUNT])
    return (
        f"{entry['text']}\n\n"
        f"—\n"
        f"{entry['term']}: {meaning}.\n\n"
        f"{tags}"
    )[: cfg.CAPTION_MAX]
