# Setup

Work through these in order. Phase 0 exists to settle the riskiest unknown —
whether Meta will publish for you at all — before you invest in anything else.
**Do not skip it.**

Times are Asia/Kolkata. T-0 is 19:45 IST = 14:15 UTC.

---

## Phase 0 — prove publishing works (2–3 h)

Nothing in this repo matters until a post appears on your profile by API.

### 0.1 Accounts

1. Switch your Instagram account to **Professional** (Business or Creator),
   in Settings → Account type. It must also be **public** — API publishing is
   not available on private accounts.
2. Create a Facebook Page and link it to the Instagram account. The Page can
   stay completely empty; the Graph API path just requires it to exist.
3. At [developers.facebook.com](https://developers.facebook.com) create an app.
   Type: **Business**. Leave it in **Development** mode.
4. Add the *Instagram* product to the app.
5. Under **Roles → Instagram Testers**, add your Instagram account, then accept
   the invite from inside the Instagram app
   (Settings → Apps and websites → Tester invites).

> Publishing to your own account needs **no App Review**. Review only applies
> to apps acting on other people's accounts. Development mode is a legitimate
> permanent end state here.

### 0.2 Token

Two routes. Take the first if you can — it removes the single most common way
this kind of project dies.

**Route A — system-user token (recommended, no expiry).**
In [Meta Business Settings](https://business.facebook.com/settings) → Users →
System Users, create a system user, assign it your app and Page, then generate
a token with `instagram_basic`, `instagram_content_publish`,
`pages_show_list`, `pages_read_engagement`. Choose **Never** for expiry if the
option is offered. Verify the expiry in the
[Access Token Debugger](https://developers.facebook.com/tools/debug/accesstoken/)
before relying on it.

**Route B — long-lived user token (60 days, must be refreshed).**
Get a short-lived token from the Graph API Explorer, exchange it for a
long-lived one, and keep `.github/workflows/refresh-token.yml` enabled. This
token dies permanently if it ever goes 60 days without a refresh.

Get your Instagram user id:

```bash
curl -s "https://graph.facebook.com/v21.0/me/accounts?access_token=$TOKEN"
# then, with the page id from above:
curl -s "https://graph.facebook.com/v21.0/<PAGE_ID>?fields=instagram_business_account&access_token=$TOKEN"
```

### 0.3 The two calls that prove it

Host any JPEG at a public URL, then:

```bash
# 1. create the container
curl -s -X POST "https://graph.facebook.com/v21.0/$IG_USER_ID/media" \
  -d "image_url=https://example.com/test.jpg" \
  -d "caption=setup test" \
  -d "access_token=$TOKEN"
# -> {"id":"1789..."}

# 2. publish it
curl -s -X POST "https://graph.facebook.com/v21.0/$IG_USER_ID/media_publish" \
  -d "creation_id=1789..." \
  -d "access_token=$TOKEN"
```

**If a post appeared on your profile, the project is viable.** Delete the test
post and continue. If it did not, stop and fix this before going further —
every later phase assumes this works.

---

## Phase 1 — configure the repo (30 min)

1. Create a **public** GitHub repo and push this code. Public buys unmetered
   Actions minutes, better scheduler priority, and free image hosting at
   `raw.githubusercontent.com`. Secrets stay encrypted and unreadable.
2. Edit [`src/config.py`](src/config.py):
   - `BRAND["handle"]` — your Instagram handle. **This is on every card.**
   - `BRAND` colours and `label` to taste.
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

`DRY_RUN` defaults to true, so this builds `dist/card.jpg` and `dist/post.json`
and publishes nothing. Open the card. Iterate on
[`templates/card.html`](templates/card.html) until you like it.

---

## Phase 2 — secrets (20 min)

**GitHub → Settings → Secrets and variables → Actions:**

| Secret | Required | Notes |
|---|---|---|
| `TG_TOKEN` | yes | Telegram bot token from [@BotFather](https://t.me/botfather) |
| `TG_CHAT` | yes | Your chat id from [@userinfobot](https://t.me/userinfobot) |
| `GEMINI_API_KEY` | no | Free tier at [aistudio.google.com](https://aistudio.google.com) |
| `GROQ_API_KEY` | no | Alternative to Gemini |
| `IG_TOKEN` | only for route B | Used by the refresh workflow |

Without an LLM key the deterministic composer is used, which always works. It
is the floor, not a degraded mode.

**Cloudflare Worker:**

```bash
cd worker
npx wrangler login
npx wrangler secret put IG_USER_ID
npx wrangler secret put IG_TOKEN
npx wrangler secret put TG_TOKEN
npx wrangler secret put TG_CHAT
npx wrangler secret put MANUAL_KEY     # any random string
npx wrangler deploy
```

Test the Worker without waiting for its cron:

```bash
curl -H "x-key: $MANUAL_KEY" https://instapost-publisher.<subdomain>.workers.dev/run
```

While `post.json` still says `dry_run: true`, this reports what it *would*
have published and posts nothing.

---

## Phase 3 — shadow run (3 nights)

Let the scheduled build run for three nights with publishing still disabled.

Each morning, check:

- A Telegram receipt arrived. **No message is the alarm** — it means the build
  failed or the scheduler dropped the run.
- The card looks right for that story.
- The chosen story is one you'd have been happy to post.

Tune `WEIGHTS`, `NICHE_TERMS` and `state/blocklist.txt` based on what you see.
This is the only phase where you are training your own judgement into the
scorer, so don't rush it.

---

## Phase 4 — go live

Go to **Settings → Secrets and variables → Actions → Variables** and add:

```
LIVE = true
```

That is the only switch. Until it is set to exactly `true`, every run —
scheduled or manual — is a shadow run. Deleting the variable puts you straight
back into shadow mode with no commit and no deploy.

Watch it closely for the first week.

---

## Controls

**Stop tonight's post** — create `state/hold.flag`, commit, push. Any build
that sees it marks the post held and the Worker refuses to publish.

```bash
touch state/hold.flag && git add -A && git commit -m "hold" && git push
```

**Stop publishing entirely** — delete the `LIVE` repository variable, or set it
to anything other than `true`. Builds continue, nothing publishes.

**Stop everything** — disable the workflow in the Actions tab, or delete the
Worker's cron trigger.

**Force a rebuild now** — Actions → build-nightly-post → Run workflow. Leave
`dry_run` checked unless you mean it.

**Re-allow a story the ledger has burned** — delete its entry from
`state/ledger.json`.

---

## When it breaks

| Symptom | Cause | Fix |
|---|---|---|
| No Telegram message at all | Build failed or run dropped | Check the Actions log; re-run via workflow_dispatch |
| `⚠️ post.json is dated …` | Build didn't run today | The Worker correctly refused to repost yesterday. Re-run the build. |
| `OAuthException 190` | Token expired or revoked | Re-issue. A password change makes a token unrecoverable. |
| Error code `9` | Daily publish quota hit | You post once a day, so this means a retry loop ran away. |
| `container ERROR` / `EXPIRED` | Image unreachable, or container older than 24 h | Containers expire in 24 h — create and publish in the same run. |
| `webfonts did not load` | Google Fonts unreachable in CI | Re-run. If it recurs, vendor the fonts into `templates/` as base64 `@font-face`. |
| `headline is N chars, over the limit` | Compose produced over-long copy | Working as designed — it refused rather than ship a bad card. |

### Calendar

- **Monthly** — confirm the token refresh ran (route B only); bump dependencies.
- **Quarterly** — check Meta's changelog for deprecations on `/media` and
  `/media_publish`.
- **~18 months** — migrate `GRAPH_VERSION`. Don't leave this to the deadline.

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
