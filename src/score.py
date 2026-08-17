"""Rank candidates and pick tonight's story.

Four signals, weighted in config: how fresh it is, how much traction it has,
how well it fits the lane, and whether we've covered the same ground recently.
Anything the ledger or blocklist rejects scores zero and is gone.
"""
from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

from . import config as cfg
from .harvest import Item
from .ledger import Ledger

log = logging.getLogger(__name__)

_WORD = re.compile(r"[a-z0-9+#.]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


def recency(item: Item) -> float:
    age_h = (datetime.now(timezone.utc) - item.published).total_seconds() / 3600
    return max(0.0, 1.0 - age_h / cfg.RECENCY_HALFLIFE_H)


def engagement(item: Item) -> float:
    if item.engagement is None:
        return cfg.ENGAGEMENT_DEFAULT
    scale = cfg.ENGAGEMENT_SCALE.get(item.source, 100.0)
    # log-normalised so a 4000-star repo doesn't drown a 300-point HN story
    return min(1.0, math.log1p(item.engagement) / math.log1p(scale))


def niche_fit(item: Item) -> float:
    haystack = f"{item.title} {item.summary}".lower()
    positive = sum(w for term, w in cfg.NICHE_TERMS.items() if term in haystack)
    negative = sum(w for term, w in cfg.NICHE_NEGATIVE.items() if term in haystack)
    return max(0.0, min(1.0, positive - negative))


def novelty(item: Item, recent_titles: list[str]) -> float:
    """1.0 = nothing like it recently, 0.0 = we just posted this."""
    mine = _tokens(item.title)
    if not mine:
        return 0.0
    worst_overlap = 0.0
    for title in recent_titles:
        theirs = _tokens(title)
        if not theirs:
            continue
        overlap = len(mine & theirs) / len(mine | theirs)   # Jaccard
        worst_overlap = max(worst_overlap, overlap)
    return 1.0 - worst_overlap


def score_item(item: Item, ledger: Ledger, recent_titles: list[str]) -> float:
    if ledger.contains(item.key()):
        return 0.0
    if ledger.blocked(item.title):
        log.info("blocklist rejected: %s", item.title[:70])
        return 0.0

    parts = {
        "recency": recency(item),
        "engagement": engagement(item),
        "fit": niche_fit(item),
        "novelty": novelty(item, recent_titles),
    }
    item.score_parts = parts
    item.score = sum(cfg.WEIGHTS[k] * v for k, v in parts.items())
    return item.score


def rank(items: list[Item], ledger: Ledger) -> list[Item]:
    recent_titles = ledger.recent_titles(cfg.NOVELTY_LOOKBACK_DAYS)
    for it in items:
        score_item(it, ledger, recent_titles)
    return sorted((i for i in items if i.score > 0), key=lambda i: i.score, reverse=True)


def select(items: list[Item], ledger: Ledger) -> Item:
    """Pick tonight's story, or refuse.

    Publishing nothing is a valid outcome. An unattended pipeline that posts
    whatever scraped through on a thin night does more damage than a gap.
    """
    ranked = rank(items, ledger)
    if not ranked:
        raise LookupError("no eligible candidates after ledger and blocklist filtering")

    for i, it in enumerate(ranked[:5], 1):
        p = it.score_parts
        log.info(
            "%d. [%.3f] r=%.2f e=%.2f f=%.2f n=%.2f  %s",
            i, it.score, p["recency"], p["engagement"], p["fit"], p["novelty"], it.title[:64],
        )

    winner = ranked[0]
    if winner.score < cfg.MIN_SCORE:
        raise LookupError(
            f"best candidate scored {winner.score:.3f}, below floor {cfg.MIN_SCORE} "
            f"-- skipping tonight rather than posting filler"
        )
    return winner
