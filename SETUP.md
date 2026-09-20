# Setup

The cards are built for you and delivered to Telegram. You post them by hand,
so there is no Meta app, no token and no deploy to get working.

Times are Asia/Kolkata.

## The daily loop

| When | What |
|---|---|
| Early afternoon | GitHub Actions builds both cards and Telegram sends each one: the image as a file, then the caption as its own message |
| Whenever suits you | Save the image, hold the caption to copy it, post on Instagram |
| Every couple of weeks | A batch of tech-metaphor cards is drafted; you see each one on the day it goes out |

Nothing in the repo can post to Instagram on its own.

## Helpers

| Step | Helper |
|---|---|
| Rebuild the profile kit (avatars, pinned cards) | `.venv/Scripts/python -m src.brand` |
| Build a card now and send it | `.venv/Scripts/python -m src.pipeline` or `-m src.pipeline_flirt` |

---

## Phase 1 — configure the repo (30 min)

1. Create a **public** GitHub repo and push this code. Public buys unmetered
   Actions minutes, better scheduler priority, and free image hosting at
   `raw.githubusercontent.com`. Secrets stay encrypted and unreadable.
2. Edit [`src/config.py`](src/config.py):
   - `CHANNELS["news"]["handle"]` and `CHANNELS["flirt"]["handle"]` — the two
     Instagram handles. **These print on every card**, and a live build now
     refuses to run while either is still a placeholder.
   - `CHANNELS[...]["label"]` and `["accent"]`, plus `BRAND` colours, to taste.
   - `NICHE_TERMS` / `NICHE_NEGATIVE` — the editorial lane. This is the one
     knob that decides what the account is about.
3. Edit [`worker/wrangler.toml`](worker/wrangler.toml): set `REPO` to
   `youruser/yourrepo`, and pin `GRAPH_VERSION` to the current Graph API
   version. Note the date somewhere — versions are supported ~2 years.

### Local dry run

```bash
python -m venv .venv
.venv/Scripts/activate          # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements.txt
playwright install chromium
python -m src.pipeline
```

`DRY_RUN` defaults to true, so this builds `dist/news/card.jpg` and
`dist/news/post.json` and publishes nothing. Run `python -m src.pipeline_flirt`
for the second account. Open both cards and iterate on
[`templates/card.html`](templates/card.html) and
[`templates/quote.html`](templates/quote.html) until you like them.

Run the tests before you change scoring or the safety gates:

```bash
python -m unittest discover -s tests -v
```

---

## Phase 2 — secrets (10 min)

**GitHub → Settings → Secrets and variables → Actions:**

| Secret | Required | Notes |
|---|---|---|
| `TG_TOKEN` | yes | Telegram bot token from [@BotFather](https://t.me/botfather) |
| `TG_CHAT` | yes | Your chat id from [@userinfobot](https://t.me/userinfobot) |
| `GEMINI_API_KEY` | no | Free tier at [aistudio.google.com](https://aistudio.google.com) |
| `GROQ_API_KEY` | no | Alternative to Gemini |

Without an LLM key the deterministic composer is used, which always works. It
is the floor, not a degraded mode.

Nothing else is needed. `IG_TOKEN` and the Instagram account ids only matter if
you ever switch on the publisher in [`worker/`](worker/), which is documented in
its own files and in git history.

---

## Phase 3 — watch a few days

Let the build run for a few days and read each card as a stranger would. The
question is whether you would post it, not whether the code ran.

Tune `WEIGHTS`, `NICHE_TERMS` and `state/blocklist.txt` based on what you see.

---

## Controls

**Skip a day** — don't post the card. Nothing else happens.

**Stop the builds** — disable the workflow in the Actions tab.

**Force a build now** — Actions → build-nightly-post → Run workflow. The card
arrives on Telegram a few minutes later.

**Re-allow a story the ledger has burned** — delete its entry from
`state/ledger.json`.

**Send a tech-metaphor card again** — set its entry in `state/flirt_queue.json`
back to `"status": "pending"`.

---

## When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| No Telegram message at all | Build failed or the run was dropped | Check the Actions log; re-run via Run workflow |
| `Instapost skipped tonight` | No story cleared the bar, or the card queue is empty | Normal on a thin news day. If it repeats, tune the scoring. |
| `Drafting failed` | The AI model or key is not working | The message names the reason |
| `webfonts did not load` | Google Fonts unreachable in CI | Re-run. If it recurs, vendor the fonts into `templates/` as base64 `@font-face`. |
| `headline is N chars, over the limit` | Compose produced over-long copy | Working as designed — it refused rather than ship a bad card. |

### Calendar

- **Every seven months or so** — top up `state/concepts.json`; 226 concepts is
  about that much runway at one card a day.
- **Occasionally** — bump dependencies.

---

## Legal, in one paragraph

Publishing via the official Graph API is permitted; driving the app or website
with tools like `instagrapi` or Selenium is not, and risks the account. Keep
this system write-only to your own feed — no auto-follow, auto-like or
auto-comment. The composer writes original summaries and never copies source
sentences, which is what keeps the account clear of copyright trouble; the
attribution line and the source URL in every caption are part of that. Never
put a publisher's photo on a card. Use only OFL/Apache-licensed fonts — fonts
licensed to you *through Canva* are licensed for use *inside Canva* and must
not be embedded in this renderer. Full analysis in the build plan.
