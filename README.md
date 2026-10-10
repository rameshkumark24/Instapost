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

The third segment is ideas, not a card: three memes about developer life. Two
hang on today's tech news -- the Hacker News, Lobsters, DEV, TechCrunch, Ars
Technica and The Verge stories the news build has already scored -- and one on
a search trending in India or the US,
turned into a dev joke. Each is checked in code: a trend it was given, the
2-to-1 split, a real Imgflip template with exactly its number of text boxes,
and nothing about death, disaster, crime, politics or religion. Each arrives
with its text ready to copy and a link that opens the template in Imgflip.

With an Imgflip API key set, each idea is also drawn: Imgflip's caption API
puts the text on the template where its own editor would, and the finished
image arrives as a file with its caption, like the cards. Only templates
Instagram shows whole are offered, the image is fetched from Imgflip's image
host and nowhere else, and an idea that cannot be drawn still arrives as text.
Free Imgflip accounts carry a small "imgflip.com" mark.

**Publishing by API is built and tested but switched off.** Instagram only
allows it through Meta's developer stack, which cost an hour of broken screens,
so [`worker/`](worker/) and [`src/gate_a.py`](src/gate_a.py) sit ready for the
day that is worth doing. Nothing in this repo can post on its own.

**Lost your files, or on a new machine?** Nothing needs doing — the build runs on
GitHub. [RECOVER.md](RECOVER.md) has the clone-and-go steps, where each key comes
from, and a briefing block for a fresh assistant session.

## What it costs

Nothing, and the tests hold it there (`FreeToRun` in
[`tests/test_pipeline.py`](tests/test_pipeline.py)): every host the code
reaches is a listed free service, every model is one confirmed free, nothing
Imgflip bills for is ever requested, and every job runs on the standard runner.

| Service | Used for | Why it is free |
|---|---|---|
| GitHub Actions | The daily build and the tests | Standard runners on a public repository are not metered |
| Gemini API | Writing the summary, the cards' lines and the memes | Free tier; `gemini-3.8-flash`, `gemini-3.6-flash` and `gemini-3.5-flash-lite` are "Free of charge" there |
| Groq (optional) | A second model provider | Free plan, no card |
| Imgflip (optional) | The template list; drawing a meme | Both are free calls. The image carries Imgflip's small mark |
| Telegram | Delivery | The Bot API is free |
| Google Trends, Hacker News, Lobsters, DEV, arXiv, GitHub search, three RSS feeds | Research | Public feeds and APIs |
| Google Fonts | The cards' typefaces | Free |

Three things would start a bill, and each is something only you can do:

- **Turning on billing for the Google project behind the Gemini key.** The
  free tier ends where Cloud Billing begins. Google AI Studio's API keys page
  shows which plan a key is on; it should say free.
- **Adding a card, or Premium, on Imgflip or Groq.** With no card on file
  there is nothing for either to charge.
- **Making the repository private.** Actions minutes are then counted against
  a monthly allowance.

The Cloudflare publisher in `worker/` is not deployed, so it costs nothing either.

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
| [`src/trends.py`](src/trends.py) | Google Trends, Imgflip templates, today's tech stories |
| [`src/memes.py`](src/memes.py) | Meme ideas: the prompt, and every check on what comes back |
| [`src/imgflip.py`](src/imgflip.py) | Finished memes: asks Imgflip to draw an idea, and checks what comes back |
| [`src/pipeline_memes.py`](src/pipeline_memes.py) | Orchestrates the meme ideas |
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
python -m src.pipeline_memes    # meme ideas
```

Nothing here can publish to Instagram. The Cloudflare publisher is not
deployed, and it would refuse anyway until the repository variable `LIVE` is
set to exactly `true`.

Don't like a card? Don't post it. Nothing else has to happen.
