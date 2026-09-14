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
import random
import re
import time
from pathlib import Path

from . import config as cfg
from . import llm

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

# A sentence ends at terminal punctuation followed by a space or the end of the
# line. Counting full stops instead read "...", ".gitignore" and "v1.5" as
# several sentences and threw good lines away, each costing another model call.
_SENTENCE_END = re.compile(r"[.!?\u2026]+(?=\s|$)")


def term_pattern(term: str) -> re.Pattern:
    """The term as a model may space it: "TRY / CATCH" also matches "TRY/CATCH".

    Requiring the bank's exact spacing threw a good line away -- on every
    attempt, each a model call -- whenever the model wrote the usual spelling.
    Words stay apart; only the space around punctuation is optional.
    """
    pattern, previous_is_word = "", False
    for part in re.findall(r"\w+|[^\w\s]", term):
        is_word = bool(re.match(r"\w", part))
        if pattern:
            pattern += r"\s+" if is_word and previous_is_word else r"\s*"
        pattern += re.escape(part)
        previous_is_word = is_word
    return re.compile(pattern, re.IGNORECASE)


class Rejected(ValueError):
    """A candidate that failed a gate. Never a crash -- just a discarded draft."""


class LLMUnavailable(Rejected):
    """The model could not be reached at all, as opposed to a line failing a gate.

    Kept distinct so a batch stops at the first one. Retrying the same dead
    model once per concept costs a timeout each, and 64 of them outlast the
    build job's 15-minute limit -- cancelling the news card along with it.
    """


class BudgetSpent(Exception):
    """The drafting time budget ran out. Not a rejection: the concept was never tried."""


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


def validate(text: str, concept: dict) -> str:
    """Every gate a candidate must clear. Raises Rejected with the reason."""
    text = re.sub(r"\s+", " ", text or "").strip().strip('"').strip()

    if not text:
        raise Rejected("empty")
    if not (MIN_CHARS <= len(text) <= MAX_CHARS):
        raise Rejected(f"length {len(text)} outside {MIN_CHARS}-{MAX_CHARS}")
    if hit := BANNED.search(text):
        raise Rejected(f"banned term {hit.group(0)!r}")
    term = term_pattern(concept["term"])
    if not term.search(text):
        raise Rejected(f"does not use the term {concept['term']!r}")
    # The term's own words do not count: WORKS ON MY MACHINE holds "my", and would
    # otherwise wave through any dry definition that merely names it.
    if not PERSONAL.search(term.sub(" ", text)):
        raise Rejected("reads as documentation, not as a metaphor about a person")
    if len(_SENTENCE_END.findall(text)) > MAX_LINES:
        raise Rejected("too many sentences for the card")
    if re.search(r"[\U0001F000-\U0001FAFF\U00002600-\U000027BF]", text):
        raise Rejected("contains emoji")
    if "#" in text:
        raise Rejected("contains a hashtag")
    return text


def generate(concept: dict, attempts: int = 3, deadline: float | None = None) -> dict:
    """One validated candidate for a concept. Raises Rejected, or BudgetSpent past `deadline`."""
    prompt = _PROMPT.format(term=concept["term"], meaning=concept["meaning"])
    reasons = []

    for i in range(attempts):
        if deadline is not None and time.monotonic() >= deadline:
            raise BudgetSpent()
        reply = llm.complete(prompt, temperature=0.85 + 0.05 * i)
        if not reply.text:
            raise LLMUnavailable(f"{concept['id']}: llm unavailable: {reply.error}")
        raw = reply.text
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

        # The card bolds the term as this line spells it, which can differ from the bank.
        spelled = term_pattern(concept["term"]).search(text).group(0)
        terms = [t for t in parsed.get("terms", []) if t and t.lower() in text.lower()]
        return {
            "concept": concept["id"],
            "term": concept["term"],
            "domain": concept["domain"],
            "text": text,
            "terms": terms or [spelled],
        }

    raise Rejected(f"{concept['id']}: {'; '.join(reasons) or 'no candidate'}")


def generate_batch(
    used_ids: set[str], size: int, budget_s: float | None = None
) -> tuple[list[dict], list[str]]:
    """Draft up to `size` candidates from unused concepts. Returns (drafts, rejects).

    `budget_s` caps the wall-clock time spent. A thinking model can take many
    seconds per call and the gates discard a share of what comes back, so an
    unbounded batch took 10m36s in its first real run -- most of a 15-minute
    job. Whatever is drafted when the budget runs out is kept, and the queue
    tops itself up on the next run.
    """
    pool = unused_concepts(used_ids)
    if not pool:
        raise Rejected("concept bank exhausted -- add more to state/concepts.json")

    deadline = time.monotonic() + budget_s if budget_s is not None else None
    random.shuffle(pool)
    out, rejects = [], []
    for concept in pool:
        if len(out) >= size:
            break
        try:
            out.append(generate(concept, deadline=deadline))
        except BudgetSpent:
            log.info("drafting budget of %ss spent after %d drafts", budget_s, len(out))
            break
        except LLMUnavailable as exc:
            # The same model would fail every remaining concept the same way.
            rejects.append(str(exc))
            break
        except Rejected as exc:
            rejects.append(str(exc))

    log.info("drafted %d candidates, %d concepts gave no usable line", len(out), len(rejects))
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
