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

# A pickup line can be thirty characters; a card that runs past about 140 has
# stopped being a line and started explaining. The brief asks for under 120.
MIN_CHARS, MAX_CHARS = 25, 150
MAX_LINES = 3

# The editor's mark, out of 10, below which a drafted card is dropped and its
# concept left for another day.
MIN_SCORE = 6

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


class Skipped(Rejected):
    """The model said the concept cannot carry a flirty line anyone would get.

    Taken at its word: the concept is set aside for good, not retried in
    every batch with the same result.
    """


def load_concepts() -> list[dict]:
    return json.loads(CONCEPTS.read_text(encoding="utf-8"))


def unused_concepts(used_ids: set[str]) -> list[dict]:
    return [c for c in load_concepts() if c["id"] not in used_ids]


# The first brief asked for "a metaphor for an ordinary human situation" and
# got careful little essays -- about therapy, house plants and DEEP COPY -- that
# only made sense once the caption had explained the tech. This one asks for a
# line about love that works without the tech, with the tech as a second layer.
_PROMPT = """Write one card for @genphile.meme's "tech, but make it personal" series: a flirty, cheeky
or heartbroken line about love, with a double meaning. Anyone reads it as a relationship line;
a programmer also gets a second, sharper meaning from the tech term.

CONCEPT: {term}
WHAT IT ACTUALLY MEANS: {meaning}

What makes it land:
- The relationship half works without knowing what {term} is. The tech meaning is the bonus
  layer, and it must be accurate.
- Stay in romance: crushes, dates, texting, situationships, exes, "what are we". Not friendship,
  therapy, work, chores, pets or plants.
- Spoken, like a pickup line, a confession, a 2am text or a breakup line. Say "you" and "me".
- Short: one or two sentences, under 120 characters. The term lands near the end, like a
  punchline.
- Cheeky, never crude: nothing sexual, no body talk, no insults, nothing about a gender or group.
- Use the exact term "{term}" once, in capitals. No emoji, no hashtags.

EXAMPLES
404: I've searched everywhere for someone like you. 404, not found.
RATE LIMIT: Five texts in thirty seconds? Babe, you've hit my RATE LIMIT.
MERGE CONFLICT: We both rewrote the same line of our story. Classic MERGE CONFLICT.
TIMEOUT: You took three days to reply, so we hit a TIMEOUT. I've moved on.
PRIMARY KEY: A million rows in this table, and you're still my PRIMARY KEY.

If {term} cannot carry a line like these that anyone would get, reply {{"skip": true}}.

Otherwise return strict JSON, no markdown fence:
{{"text": "<the line>", "terms": ["{term}"]}}
"""

_EDITOR = """You edit the "tech, but make it personal" cards of a meme page. Each is a flirty or
heartbroken relationship line with a tech double meaning. Score each from 1 to 10:

- 8 to 10: anyone gets the relationship joke at once, and a developer gets a sharp second meaning.
- 5 to 7: works, but needs a second read, or the double meaning is weak.
- 1 to 4: needs the tech explained to make sense; not about romance; reads like documentation;
  wordy; or the tech is wrong.

{items}

Reply with JSON only, one score for every card, in order:
{{"scores": [{{"n": 1, "score": 7, "note": "a few words"}}]}}"""


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
        if isinstance(parsed, dict) and parsed.get("skip"):
            raise Skipped(f"{concept['id']}: no line about love anyone would get")
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
    out, skipped, rejects = [], [], []
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
        except Skipped as exc:
            rejects.append(str(exc))
            skipped.append({"concept": concept["id"], "term": concept["term"], "domain": concept["domain"],
                            "text": "", "terms": [], "status": "skipped"})
        except Rejected as exc:
            rejects.append(str(exc))

    kept = review(out, rejects)
    log.info("drafted %d, kept %d after the editor, %d concepts set aside, %d gave no usable line",
             len(out), len(kept), len(skipped), len(rejects))
    # Set-aside concepts go in the queue too, marked, so no later batch spends a
    # call on them again. They never become cards.
    return kept + skipped, rejects


def review(drafts: list[dict], rejects: list[str]) -> list[dict]:
    """The drafts the editor would publish, scored, best first.

    One call scores the whole batch. A line that needs the tech explained, or
    is not about love, is dropped, and its concept stays in the bank for a
    later batch. Without a verdict the batch is kept as drafted: an editor
    outage must not cost a fortnight of cards.
    """
    if not drafts:
        return []
    items = "\n".join(f'Card {n}: "{d["text"]}"' for n, d in enumerate(drafts, 1))
    reply = llm.complete(_EDITOR.format(items=items), temperature=0.2, max_tokens=4096)
    try:
        if not reply.text:
            raise ValueError(reply.error or "no reply")
        body = reply.text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        rows = json.loads(body)["scores"]
        by_n = {int(r["n"]): (float(r["score"]), str(r.get("note", ""))[:120]) for r in rows}
        if set(by_n) != set(range(1, len(drafts) + 1)):
            raise ValueError(f"scored {len(by_n)} of {len(drafts)}")
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        log.warning("no editor's verdict on the cards (%s); keeping them as drafted", exc)
        return drafts

    kept = []
    for n, d in enumerate(drafts, 1):
        score, note = by_n[n]
        if score >= MIN_SCORE:
            kept.append({**d, "score": score})
        else:
            rejects.append(f"{d['concept']}: editor {score:g}/10 ({note}): {d['text'][:80]}")
    kept.sort(key=lambda d: -d["score"])        # the queue goes out best first
    return kept


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
