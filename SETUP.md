# Setup

The cards are built for you and delivered to Telegram. You post them by hand,
so there is no Meta app, no token and no deploy to get working.

Times are Asia/Kolkata.

If the local files are gone, or you are on a different machine, go to
[RECOVER.md](RECOVER.md) instead — the build keeps running without this
laptop, and that file is the clone-and-go guide.

## The daily loop

| When | What |
|---|---|
| Late afternoon (the free scheduler runs hours late) | GitHub Actions builds both cards and Telegram sends each one: the image as a file, then the caption as its own message |
| Whenever suits you | Save the image, hold the caption to copy it, post on Instagram |
| Right after | Up to three finished memes about developer life, the editor's best: usually two on today's tech news and one on a trending search, though a stronger joke on either side takes a weaker one's place. Each arrives as an image with the editor's mark out of 10, then its caption |
| When one makes you laugh | Save the image and post it with the caption |
| When fewer than ten wait | A batch of tech-metaphor cards is drafted and scored by the editor; you see each one on the day it goes out |

Nothing in the repo can post to Instagram on its own.

## Helpers

| Step | Helper |
|---|---|
| Rebuild the profile kit (avatars, pinned cards) | `.venv/Scripts/python -m src.brand` |
| Build a card now and send it | `.venv/Scripts/python -m src.pipeline` or `-m src.pipeline_flirt` |
| Get today's meme ideas now | `.venv/Scripts/python -m src.pipeline_memes` |

---

## Phase 1 — configure the repo (30 min)

1. Create a **public** GitHub repo and push this code. Public buys unmetered
   Actions minutes, better scheduler priority, and free image hosting at
   `raw.githubusercontent.com`. Secrets stay encrypted and unreadable.
2. Edit [`src/config.py`](src/config.py):
   - `HANDLE` — the one Instagram account everything goes on. **It prints on
     every card and meme** and is named in the prompts that write them, and a
     live build refuses to run while it is still a placeholder.
   - `CHANNELS[...]["label"]` and `["accent"]`, plus `BRAND` colours, to taste.
   - `NICHE_TERMS` / `NICHE_NEGATIVE` — the editorial lane. This is the one
     knob that decides what the news card is about.
3. Only if you ever switch on the parked publisher: edit
   [`worker/wrangler.toml`](worker/wrangler.toml), set `REPO` to
   `youruser/yourrepo`, and pin `GRAPH_VERSION` to the current Graph API
   version. Posting by hand needs none of it.

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

**Get another three memes now** — Actions → meme-ideas-now → Run workflow.
Only the meme step runs, so no news story or tech-metaphor card is spent.
Before the day's build has run, all three hang on search trends; after it,
the usual two on tech news and one on a trend. Changing
[`.github/run-memes`](.github/run-memes) in a commit and pushing starts the same run.

**See why a candidate was left out** —
[`dist/memes/ideas.json`](dist/memes/ideas.json) keeps the day's memes with the
editor's score for each, and every reason a candidate was rejected. The drawing
is deterministic: those boxes on that format always draw the same picture.

**Add a meme format** — measure where its boxes go on the blank template, then
add it to [`src/meme_formats.py`](src/meme_formats.py) with what the format means
and a worked example. Draw it once and look before trusting it: the tests check
it is complete, not that the text lands on the right person.

**Draft new tech-metaphor cards now** — Actions → cards-draft-now → Run
workflow, or change [`.github/run-drafts`](.github/run-drafts) in a commit. It
drafts only when fewer than ten cards are waiting, through the same editor.

**Re-allow a story the ledger has burned** — delete its entry from
`state/ledger.json`.

**Allow a trend or template again before its cooldown** — delete its line
from `state/meme_log.json`. A trend gets one meme a week and a template rests
for three days; both are set in [`src/config.py`](src/config.py).

**Change the split, the countries or the off-limits subjects** — `MEME_MIX`,
`MEME_TREND_GEOS` and `MEME_SENSITIVE` in [`src/config.py`](src/config.py).

**Send a tech-metaphor card again** — set its entry in `state/flirt_queue.json`
back to `"status": "pending"`.

---

## When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| No Telegram message at all | Build failed or the run was dropped | Check the Actions log; re-run via Run workflow |
| `Instapost skipped tonight` | No story cleared the bar, or the card queue is empty | Normal on a thin news day. If it repeats, tune the scoring. |
| `Drafting failed` | The AI model or key is not working | The message names the reason |
| `Meme ideas failed` | No model answered, or every idea failed a check | The message still lists today's trends, so you can make one yourself |
| `Missing today: could not draw idea …` in the meme message | A blank template could not be fetched, or the text was too long for its box | That meme still arrives as text, with a link to make it in Imgflip |
| `Meme ideas skipped` | Google Trends unreachable and no tech stories, or every trend was off-limits | Nothing; tomorrow is a new list |
| `webfonts did not load` | Google Fonts unreachable in CI | Re-run. If it recurs, vendor the fonts into `templates/` as base64 `@font-face`. |
| `headline is N chars, over the limit` | Compose produced over-long copy | Working as designed — it refused rather than ship a bad card. |

### Calendar

- **By spring 2027** — top up `state/concepts.json`. Of its 226 concepts, 153
  were still unused on 20 Nov 2026: about five months at one card a day, less
  the ones the editor keeps turning down.
- **Occasionally** — bump dependencies.

---

## Legal, in one paragraph

Publishing via the official Graph API is permitted; driving the app or website
with tools like `instagrapi` or Selenium is not, and risks the account. Keep
this system write-only to your own feed — no auto-follow, auto-like or
auto-comment. The composer either writes its own summary, checked against the
source for copied wording, or prints the source's own words as a short
quotation credited by name; it never passes anyone's sentences off as its own,
which is what keeps the account clear of copyright trouble. The attribution
line and the source URL in every caption are part of that. Never
put a publisher's photo on a card. Use only OFL/Apache-licensed fonts — fonts
licensed to you *through Canva* are licensed for use *inside Canva* and must
not be embedded in this renderer. Full analysis in the build plan.
