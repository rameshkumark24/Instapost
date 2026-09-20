# Instapost Nightly

A pipeline that researches a tech story, writes it up, renders it onto a
designed card, and sends the card and its caption to your phone. You post it.

Runs entirely on free tiers, using only official APIs.

```
daily  harvest    6 sources -> ~160 candidates          GitHub Actions
       select     score, dedup, blocklist -> 1 story
       compose    headline, body, caption, hashtags
       render     1080x1350 JPEG via headless Chromium
       stage      commit to repo
       hand off   Telegram: the card as a file, then the caption alone

you    post       save, copy, publish on Instagram      about 2 minutes
```

A second card is built the same way for the tech-metaphor account: lines are
drafted in batches from a bank of 226 programming concepts, and the oldest one
that has not gone out yet becomes the day's card.

**Publishing by API is built and tested but switched off.** Instagram only
allows it through Meta's developer stack, which cost an hour of broken screens,
so [`worker/`](worker/) and [`src/gate_a.py`](src/gate_a.py) sit ready for the
day that is worth doing. Nothing in this repo can post on its own.

**Lost your files, or on a new machine?** Nothing needs doing — the build runs on
GitHub. [RECOVER.md](RECOVER.md) has the clone-and-go steps, where each key comes
from, and a briefing block for a fresh assistant session.

## Layout

| Path | What it does |
|---|---|
| [`src/config.py`](src/config.py) | Every tunable. Editorial lane, scoring weights, brand, limits. |
| [`src/harvest.py`](src/harvest.py) | HN, Lobsters, DEV, GitHub, arXiv, RSS → one normalised shape |
| [`src/score.py`](src/score.py) | Recency × engagement × niche fit × novelty; refuses weak nights |
| [`src/compose.py`](src/compose.py) | Deterministic copy, optional LLM polish, validated against source |
| [`src/render.py`](src/render.py) | Jinja2 + Playwright → JPEG, with the safety gates |
| [`src/ledger.py`](src/ledger.py) | Posted-URL memory, committed to git |
| [`src/pipeline.py`](src/pipeline.py) | Orchestrates the build |
| [`templates/card.html`](templates/card.html) | The card design |
| [`worker/src/index.js`](worker/src/index.js) | The punctual publisher |

## Design notes

**Canva is the design tool, not the runtime.** Canva's Autofill API — the only
way to push text into a saved template programmatically — requires a Canva
Enterprise organisation. So you design the card in Canva once, then
`templates/card.html` reproduces it nightly with real CSS and webfonts. Same
result, no tier gate, no vendor in the hot path.

**It refuses rather than degrades.** With nobody watching at 19:45, every stage
fails loudly instead of shipping something broken:

- weak candidate field → builds nothing that day, and says so
- headline too long, fonts missing, content overflowing → render aborts
- LLM invents a number not in the source → output rejected, deterministic copy used
- a card Instagram would refuse (size, ratio, caption length) → build fails, not your post

**Silence is the alarm.** A pipeline that reports nothing on success is
indistinguishable from one that died three weeks ago, so it sends a receipt
either way.

## Controls

```bash
# build now and send it to Telegram
python -m src.pipeline          # news card
python -m src.pipeline_flirt    # tech-metaphor card
```

Nothing here can publish to Instagram. The Cloudflare publisher is not
deployed, and it would refuse anyway until the repository variable `LIVE` is
set to exactly `true`.

Don't like a card? Don't post it. Nothing else has to happen.
