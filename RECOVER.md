# Recover

Everything that matters runs on GitHub, not on your laptop. This file is the
answer to "my files are gone", "I'm on a different machine", or "the assistant
session ended and I need to pick up where we left off".

Read [SETUP.md](SETUP.md) for how the day normally runs. This file is only for
when something is missing.

---

## The short version

**Nothing on your laptop is needed for the cards to arrive.** The build runs on
GitHub's servers, on a schedule, from the code in this repository. You can close
your editor, reinstall Windows, or switch laptops, and tomorrow's cards still
land on Telegram.

The laptop is only for changing how the cards look or what they say.

---

## What lives where

| Thing | Where it lives | If it's lost |
|---|---|---|
| The code, the card designs, the concept bank | This GitHub repository | Nothing to do; `git clone` brings it all back |
| Today's cards and the running state | Also in the repo — the bot commits them daily under `dist/` and `state/` | Nothing to do |
| The daily build | GitHub Actions, workflow `build-nightly-post` | Re-enable it in the Actions tab |
| Three keys (Telegram bot, chat id, AI key) | GitHub repository secrets, encrypted and unreadable even by you | See [If a key is lost](#if-a-key-is-lost) |
| Delivery to your phone | Your Telegram bot chat | Re-issue the bot token |
| Posting | You, by hand, on `@genphile.meme` | — |
| API auto-posting (`worker/`) | Written and tested, **never deployed** | Irrelevant; nothing depends on it |

There is no server to pay for, no database, and no token that expires.

---

## Case 1 — the local files are gone, or you're on a different laptop

The builds keep running regardless. Do this only when you want to work on the
code again.

You need **Git** and **Python 3.12** (3.13 works locally; CI uses 3.12).

```bash
git clone https://github.com/rameshkumark24/Instapost.git
cd Instapost
python -m venv .venv
.venv/Scripts/activate            # Windows;  source .venv/bin/activate elsewhere
pip install -r requirements.txt
playwright install chromium       # ~120 MB, the browser that draws the cards
```

Check it works — this builds real cards into `dist/` and sends nothing:

```bash
.venv/Scripts/python -m src.pipeline          # news card
.venv/Scripts/python -m src.pipeline_flirt    # tech-metaphor card
```

Open `dist/news/card.jpg` and `dist/flirt/card.jpg`. If they look right, the
clone is complete and correct.

Run the tests before changing anything:
`.venv/Scripts/python -m unittest discover -s tests` (152 tests). The publisher's
own 24 tests need Node 22+: `cd worker` then `node --test`.

**A local run only messages Telegram if you give it the keys.** Without
`TG_TOKEN` and `TG_CHAT` in your environment it just writes the files, which is
what you want while experimenting. Never commit them.

> **Pull before you work.** The bot pushes a commit to `main` every day. A clone
> that sat idle is behind, and your first push will be rejected. Always
> `git pull` first.

---

## Case 2 — a new GitHub repository (account lost, or starting clean)

1. Create a **public** repo. Public is what makes this free: unmetered Actions
   minutes and better scheduler priority. Secrets stay encrypted in a public
   repo — that part is not public.
2. Push the code: `git remote set-url origin <new-url>`, then
   `git push -u origin main`.
3. Add the secrets (next section).
4. Actions tab, enable workflows if GitHub asks.
5. Actions, `build-nightly-post`, **Run workflow**. Two cards should reach
   Telegram within a few minutes.
6. Leave the repository variable `LIVE` **unset**. It is the lock that keeps the
   parked publisher from ever posting. Nothing in the hand-posting flow needs it.

---

## If a key is lost

GitHub, **Settings**, **Secrets and variables**, **Actions**, the **Repository
secrets** tab (not "Environment secrets"). Paste each value there and nowhere
else — never into a chat window, a screenshot, a file, or a commit.

| Secret | Needed? | Where to get it again |
|---|---|---|
| `TG_TOKEN` | Yes | Telegram, [@BotFather](https://t.me/botfather), `/mybots`, your bot, API Token |
| `TG_CHAT` | Yes | Telegram, [@userinfobot](https://t.me/userinfobot) — send it anything and it replies with your id |
| `GEMINI_API_KEY` | Optional | [aistudio.google.com](https://aistudio.google.com), Get API key (free tier) |
| `GROQ_API_KEY` | Optional | [console.groq.com](https://console.groq.com) — an alternative to Gemini |

Without any AI key the cards are still written, by the built-in composer. That
is the floor, not a broken state: the build never fails for want of a key.

If you re-issue the bot token, send your bot a message once afterwards so it can
reach you again.

---

## When something looks wrong

| What you see | What it means | What to do |
|---|---|---|
| No Telegram message at all, all day | The build never ran. **Silence is the only real alarm.** | Actions tab: is the workflow disabled? Any red run? Run it by hand. |
| `Instapost skipped tonight` | No story cleared the bar, or the card queue is empty | Nothing. Normal on a thin news day. |
| `Drafting failed` | The AI key or model isn't working | The message says why. The build still runs without it. |
| `Instapost failed` | A build step broke | Open the Actions log for that run and send it to me. |
| Cards arrive at an odd hour | GitHub's free scheduler drifts by hours | Expected. Post whenever suits you. |
| A red run, but no message | The failure happened before Telegram was reachable | Read the Actions log. |
| Actions disabled after 60 days idle | GitHub does this to dormant public repos | One click in the Actions tab re-enables it. |

Two runs are scheduled each day (09:30 and 14:30 IST nominal) precisely so a
dropped run isn't a missed day. Whichever fires first builds; the other sees
today is already done and exits.

---

## Picking up with an AI assistant in a new session

Paste the block below as your first message. It carries the facts a fresh
session cannot infer from the code alone.

```
Project: C:\Instapost — github.com/rameshkumark24/Instapost (public, main).
Read RECOVER.md, README.md and SETUP.md first.

Current mode: hand-posting. GitHub Actions builds two 1080x1350 cards a day
(news + tech-as-romance) and Telegram sends each as a file plus the caption as
a separate message. I post them myself on @genphile.meme, about 2 minutes a day.

Deliberate decisions, do not undo without asking me:
- No Instagram API publishing. Meta's setup wasted an hour and I abandoned it.
  worker/ is built and tested but never deployed, and the repo variable LIVE
  stays unset as a second lock.
- No approval step. Every built card is delivered; I just don't post the ones I
  don't like.
- Both content types go on the one account, @genphile.meme.
- Secrets only ever go in GitHub repository secrets. Never in chat, a
  screenshot, or a commit.

State: state/concepts.json holds 226 concepts (~7 months); state/flirt_queue.json
tracks which have gone out; state/ledger.json burns used stories. The bot pushes
dist/ and state/ to main daily, so pull before working.

Tests: 152 Python (python -m unittest discover -s tests) and 24 publisher
(cd worker, then node --test, Node 22+).

Windows notes: never pipe a commit message into git (PowerShell 5.1 adds a BOM)
— use git commit -m, or -F with a file. .gitattributes normalises to LF.
```

---

## The things to leave alone

- **`LIVE` stays unset.** It is the reason nothing can post without you.
- **Secrets never leave GitHub's settings page.** Not into chat, not into a
  file, not into a screenshot.
- **`state/ledger.json` and `state/flirt_queue.json` are memory, not clutter.**
  Deleting them makes the bot repeat stories and cards it has already used.
- **The repo stays public.** Private flips Actions onto a metered quota.
