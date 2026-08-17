# Instapost Nightly

An unattended pipeline that researches a tech story, renders it onto a designed
card, and publishes it publicly to Instagram at **19:45 IST** every evening.

Runs entirely on free tiers, using only official APIs.

```
18:30  harvest    6 sources -> ~160 candidates          GitHub Actions
18:31  select     score, dedup, blocklist -> 1 story
18:32  compose    headline, body, caption, hashtags
18:33  render     1080x1350 JPEG via headless Chromium
18:34  stage      commit to repo, verify public URL
18:35  notify     Telegram receipt

19:45  publish    create container -> media_publish      Cloudflare Worker
19:45  confirm    Telegram receipt with permalink
```

The build runs 75 minutes early on purpose: the Actions scheduler is
best-effort and drifts 30–60 minutes on free tiers. The publish call lives on a
Cloudflare cron trigger instead, which is minute-accurate.

**Start with [SETUP.md](SETUP.md).** Phase 0 settles whether Meta will publish
for you at all, and takes two hours. Nothing else matters until it passes.

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

- weak candidate field → posts nothing that night, and says so
- headline too long, fonts missing, content overflowing → render aborts
- LLM invents a number not in the source → output rejected, deterministic copy used
- `post.json` stale or dated yesterday → Worker refuses to republish
- image URL not reachable → build fails before 19:45, while there's still time

**Silence is the alarm.** A pipeline that reports nothing on success is
indistinguishable from one that died three weeks ago, so it sends a receipt
either way.

## Controls

```bash
# skip tonight
touch state/hold.flag && git add -A && git commit -m hold && git push

# build now, publish nothing
DRY_RUN=true python -m src.pipeline
```

Nothing publishes until the repository variable `LIVE` is set to exactly
`true` (Settings → Secrets and variables → Actions → Variables). Until then
every run is a shadow run. Removing the variable pulls the plug instantly, with
no commit and no deploy.
