"""Single source of truth for every tunable in the pipeline.

Everything that defines the account's editorial identity lives here. Changing
NICHE_TERMS is how you change lanes without touching any other module.
"""
from __future__ import annotations

import os
from zoneinfo import ZoneInfo

# --- clock -----------------------------------------------------------------

TZ = ZoneInfo("Asia/Kolkata")
PUBLISH_AT_LOCAL = "19:45"          # informational; the Worker cron is authoritative

# --- editorial lane --------------------------------------------------------
# Weighted terms. A story's niche-fit score is the sum of the weights it hits,
# capped at 1.0. Negative terms are subtracted. This is the single knob that
# decides what the account is about.

NICHE_TERMS: dict[str, float] = {
    # AI / ML shipping news
    "llm": 0.45, "gpt": 0.35, "claude": 0.40, "gemini": 0.35, "openai": 0.40,
    "anthropic": 0.40, "transformer": 0.35, "inference": 0.35, "fine-tun": 0.35,
    "diffusion": 0.30, "embedding": 0.30, "rag": 0.30, "agent": 0.30,
    "open-weight": 0.40, "open source model": 0.45, "benchmark": 0.25,
    # developer tooling
    "compiler": 0.35, "runtime": 0.30, "framework": 0.25, "typescript": 0.30,
    "rust": 0.35, "python": 0.30, "go 1.": 0.30, "postgres": 0.35, "sqlite": 0.35,
    "kubernetes": 0.30, "docker": 0.30, "wasm": 0.35, "webassembly": 0.35,
    "release": 0.20, "v2.0": 0.15, "ships": 0.20, "launches": 0.20,
    # systems / performance
    "latency": 0.30, "throughput": 0.30, "memory leak": 0.30,
    "database": 0.30, "distributed": 0.30,
}

NICHE_NEGATIVE: dict[str, float] = {
    "crypto": 0.60, "bitcoin": 0.60, "nft": 0.70, "web3": 0.60, "token price": 0.70,
    "hiring": 0.40, "layoff": 0.25, "stock": 0.40, "ipo": 0.40, "funding round": 0.30,
    "ask hn": 0.80, "show hn": 0.30, "tell hn": 0.80, "who is hiring": 0.90,
    "elon": 0.35, "lawsuit": 0.30, "politics": 0.60,
}

# --- scoring ---------------------------------------------------------------

WEIGHTS = {"recency": 0.30, "engagement": 0.30, "fit": 0.25, "novelty": 0.15}

RECENCY_HALFLIFE_H = 36.0           # score decays to zero over this many hours
MIN_SCORE = 0.30                    # below this we publish nothing rather than junk
NOVELTY_LOOKBACK_DAYS = 30          # how far back the dedup check reaches

# Per-source engagement normalisers. HN points and GitHub stars are not
# comparable numbers, so each source declares what "a lot" looks like.
ENGAGEMENT_SCALE = {
    "hn": 400.0, "lobsters": 40.0, "devto": 300.0,
    "github": 2000.0, "arxiv": 1.0, "rss": 1.0,
}

# Sources with no engagement signal get this flat baseline so they can still
# compete on recency and fit without dominating.
ENGAGEMENT_DEFAULT = 0.45

# --- sources ---------------------------------------------------------------

HN_MIN_POINTS = 120
GITHUB_MIN_STARS = 200
HARVEST_WINDOW_H = 48

# arXiv announces in weekday batches, so on a Monday the newest preprints are
# already three days old. A news-length window silently excludes the source
# entirely; preprints are not breaking news, so give them a wider one.
ARXIV_WINDOW_H = 120

RSS_FEEDS = {
    "TechCrunch":   "https://techcrunch.com/feed/",
    "Ars Technica": "https://feeds.arstechnica.com/arstechnica/technology-lab",
    "The Verge":    "https://www.theverge.com/rss/index.xml",
}

ARXIV_CATEGORIES = ["cs.AI", "cs.LG", "cs.SE"]

# --- card ------------------------------------------------------------------

CARD_W, CARD_H = 1080, 1350         # 4:5, the tallest ratio Instagram accepts
CARD_QUALITY = 90                   # JPEG. PNG containers fail on Meta's endpoint.

HEADLINE_MAX_CHARS = 78             # hard gate; longer headlines abort the run
BODY_MAX_CHARS = 240

BRAND = {
    "handle": "@yourhandle",        # <-- set this
    "label": "DAILY TECH BRIEF",
    "ink": "#0E1116",
    "paper": "#F4F1EB",
    "accent": "#FF6B35",
    "muted": "#8A93A0",
}

# --- caption ---------------------------------------------------------------

CAPTION_MAX = 2200                  # Instagram's hard limit
HASHTAG_COUNT = 7                   # 5-8 relevant beats 30 sprayed

HASHTAG_POOL = [
    "#programming", "#softwareengineering", "#devtools", "#opensource",
    "#machinelearning", "#ai", "#coding", "#backend", "#developer",
    "#technews", "#buildinpublic", "#computerscience",
]

# --- behaviour -------------------------------------------------------------

def _flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes"}


# Shadow mode: run the whole pipeline, publish nothing. Keep this on for the
# first few nights. The Worker refuses to publish while post.json says dry_run.
DRY_RUN = _flag("DRY_RUN", True)

# Send a Telegram receipt on success as well as failure. With nobody watching
# the pipeline, silence on success is indistinguishable from a dead pipeline.
NOTIFY_ON_SUCCESS = _flag("NOTIFY_ON_SUCCESS", True)

USER_AGENT = "instapost-nightly/1.0 (+https://github.com/)"
HTTP_TIMEOUT = 20
