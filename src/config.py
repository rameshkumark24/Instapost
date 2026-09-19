"""Single source of truth for every tunable in the pipeline.

Everything that defines the account's editorial identity lives here. Changing
NICHE_TERMS is how you change lanes without touching any other module.
"""
from __future__ import annotations

import os
import re
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

# "Fresh" means different things per source. A news story is stale in a day; a
# repo created three weeks ago is still a new project. Without this, filtering
# GitHub on created_at (rather than pushed_at) would zero out every repo, since
# almost nothing gains 150 stars within 36 hours of being created.
RECENCY_HALFLIFE_BY_SOURCE = {
    "github": 24 * 30.0,
    "arxiv": 24 * 5.0,
}

# No single source may quietly become the whole account. GitHub supplied 13 of
# the first 17 picks; this pulls a dominant source back without silencing it.
DIVERSITY_LOOKBACK = 10
DIVERSITY_FREE_SHARE = 0.34         # share below which there is no penalty
DIVERSITY_MAX_PENALTY = 0.35
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

# Repos must be genuinely new. Sorting all of GitHub by absolute stars just
# returns the same famous repos every night -- see github_trending().
GITHUB_MIN_STARS = 150
GITHUB_MAX_AGE_D = 45
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

# Shared palette only. Handle, label and accent belong to the channel -- two
# sources of truth for the same visible string is how a card ships with the
# wrong account name on it.
BRAND = {
    "ink": "#0E1116",
    "paper": "#F4F1EB",
    "accent": "#FF6B35",
    "muted": "#8A93A0",
}

# Instagram handles: letters, digits, periods and underscores, 30 at most.
_HANDLE = re.compile(r"@[A-Za-z0-9._]{1,30}")

# Placeholders are recognised by their shape, never by repeating their literal
# text here. A find-and-replace on a copied literal rewrites this check at the
# same time -- disabling it, or blocking the real handle that replaced it.
_PLACEHOLDER = re.compile(r"@__[A-Za-z0-9_]+__")


def assert_branding_ready(channel: dict) -> None:
    """Refuse to publish a card carrying a placeholder or malformed handle.

    The handle is printed on every card, and nothing downstream can tell that
    the string is wrong. Shadow builds pass so the whole path stays testable.
    Each channel is checked on its own: one account not being ready must never
    stop the other from posting (see the commit step in build.yml).
    """
    if DRY_RUN:
        return
    handle = (channel.get("handle") or "").strip()
    if _PLACEHOLDER.fullmatch(handle):
        raise RuntimeError(
            f"channel handle is still a placeholder ({handle}). "
            f"Set it in CHANNELS before going live."
        )
    if not _HANDLE.fullmatch(handle):
        raise RuntimeError(
            f"channel handle {handle!r} is not a valid Instagram handle; "
            f"expected '@' followed by letters, digits, '.' or '_'."
        )


# --- caption ---------------------------------------------------------------

CAPTION_MAX = 2200                  # Instagram's hard limit
HASHTAG_COUNT = 7                   # 5-8 relevant beats 30 sprayed

HASHTAG_POOL = [
    "#programming", "#softwareengineering", "#devtools", "#opensource",
    "#machinelearning", "#ai", "#coding", "#backend", "#developer",
    "#technews", "#buildinpublic", "#computerscience",
]

# --- channels --------------------------------------------------------------
# Two accounts, one repo, one Meta app. Each channel renders to its own folder
# under dist/ and is published by its own Worker cron against its own
# IG_USER_ID. Publish limits are per-account, so they never contend.

CHANNELS = {
    "news": {
        "template": "card.html",
        "dist": "news",
        "handle": "@daily.techforyou",
        "label": "DAILY TECH BRIEF",
        "accent": "#FF6B35",
    },
    "flirt": {
        "template": "quote.html",
        "dist": "flirt",
        "handle": "@genphile.meme",         # an existing meme page: these cards join its own posts
        "label": "// TECH, BUT MAKE IT PERSONAL",
        "accent": "#F0508A",
    },
}

# Cards drafted per batch, and the stock level -- approved plus still awaiting
# review -- below which another batch is drafted. Unreviewed drafts count, or a
# queue nobody has looked at yet would be refilled every night.
FLIRT_BATCH_SIZE = 12
FLIRT_REFILL_BELOW = 10

# Wall-clock cap on drafting per run. The first real batch took 10m36s of a
# 15-minute job; whatever is drafted in time is kept and topped up next run.
FLIRT_DRAFT_BUDGET_S = 300

# Unreviewed drafts expire after this long and their concepts return to the
# pool. Otherwise a batch nobody ticks would block refills for good.
FLIRT_PENDING_EXPIRE_DAYS = 7

FLIRT_HASHTAGS = [
    "#programmerhumor", "#codinglife", "#devlife", "#sqljokes",
    "#programming", "#softwareengineer", "#codingmemes", "#techhumor",
]

# --- language model -----------------------------------------------------------
# Tried in order. A model that answers "not found" has been retired, so the
# next one is tried; a rejected key stops the chain, since no other model on
# that provider will take it. gemini-2.0-flash sat here until Google shut it
# down on 1 June 2026, and nothing noticed because every copy of the call
# swallowed the error. When a model is announced for retirement, drop it.
GEMINI_MODELS = ["gemini-3.8-flash", "gemini-3.6-flash", "gemini-2.5-flash"]
GROQ_MODELS = ["llama-3.3-70b-versatile"]

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

# Fetch the publisher's own og:description when a source gives us only a title.
# This is the one place the pipeline touches an arbitrary third-party URL; set
# it False to keep every outbound request inside the known source APIs.
ENRICH_FROM_SOURCE = _flag("ENRICH_FROM_SOURCE", True)

# Only sources that send no summary of their own. Anything else "enriches" into
# page boilerplate -- a GitHub repo with a terse description would become
# "Contribute to owner/repo development by creating an account on GitHub."
ENRICH_SOURCES = {"hn", "lobsters"}

USER_AGENT = "instapost-nightly/1.0 (+https://github.com/)"
HTTP_TIMEOUT = 20

# Language-model calls get their own, longer limit. Current Flash models think
# before answering, and a non-streaming reply arrives only once they finish, so
# a 20s limit abandons slow-but-working calls and retries them on the next
# model. The first real batch took 636s for 12 cards (~53s each) while no line
# failed a gate. The worst case -- every model timing out -- is checked against
# the build job's time limit in tests/test_pipeline.py.
LLM_CONNECT_TIMEOUT_S = 10
LLM_TIMEOUT_S = 45
