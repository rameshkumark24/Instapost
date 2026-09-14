/**
 * Instapost publisher.
 *
 * Fires at 14:15 UTC (19:45 IST) from a Cloudflare cron trigger, because
 * GitHub Actions' `schedule` event is best-effort and drifted by hours on this
 * repo. Publishing is a handful of HTTPS calls, so the whole job fits in a
 * Worker.
 *
 * Each account is guarded on its own, and left unpublished -- with a Telegram
 * message tagged with its name -- when its card is missing, from another day,
 * held, or still in shadow mode. A deliberate skip (no story good enough, no
 * approved card) was already reported by the morning build, so it stays silent
 * here instead of arriving a second time dressed as a failure.
 *
 * Two ways to hold tonight's posts, both read at 19:45:
 *   - send the bot "hold" (both accounts), "hold news" or "hold tech" on the
 *     day, and "resume" to undo. The latest command wins.
 *   - commit state/hold.flag. It works while Telegram is down, and holds every
 *     night until the file is deleted.
 *
 * The /run address is a test, and only ever a test. Publishing happens on the
 * schedule and nowhere else.
 */

const GRAPH = "https://graph.facebook.com";
const ALIASES = { news: ["news"], flirt: ["flirt"], tech: ["flirt"], metaphor: ["flirt"], all: ["news", "flirt"] };

/** Swapped out in tests so retries and polling do not really wait. */
export const timing = { sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)) };

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env));
  },

  // Test without posting: curl -H "x-key: $MANUAL_KEY" https://<worker>/run
  async fetch(request, rawEnv) {
    const env = cleanEnv(rawEnv);
    const url = new URL(request.url);
    if (url.pathname !== "/run") return new Response("instapost", { status: 200 });
    const key = (request.headers.get("x-key") || "").trim();
    if (!env.MANUAL_KEY || key !== env.MANUAL_KEY) {
      return new Response("forbidden", { status: 403 });
    }
    const report = await test(env);
    return new Response(redact(JSON.stringify(report, null, 2), env), {
      headers: { "content-type": "application/json; charset=utf-8" },
    });
  },
};

/**
 * Publishes every configured account. Each has its own IG_USER_ID, so they
 * never share a publishing quota, and one failing never stops the other.
 */
export async function run(rawEnv) {
  const env = cleanEnv(rawEnv);
  const holds = await readHolds(env);
  for (const problem of holds.problems) await alert(env, `⚠️ ${problem}`);

  const results = {};
  for (const name of channelNames(env)) {
    const idName = `IG_USER_ID_${name.toUpperCase()}`;
    const userId = env[idName];
    if (!userId) {
      await alert(env, `⚠️ [${name}] ${idName} is not set on the publisher, so this account cannot post.`);
      results[name] = { skipped: `${idName} not set` };
      continue;
    }
    results[name] = await runChannel(env, name, userId, holds.reason(name));
  }
  return results;
}

export async function runChannel(env, channel, userId, hold = null) {
  const tag = `[${channel}]`;
  try {
    const { post, status } = await loadPost(env, channel);
    const decision = verdict(post, status, istDate(), hold);
    if (!decision.publish) {
      if (decision.alert) await alert(env, `${decision.icon} ${tag} ${decision.alert}`);
      return { skipped: decision.skipped };
    }

    // --- publish -----------------------------------------------------------

    const ig = `${GRAPH}/${env.GRAPH_VERSION}/${userId}`;
    const imageUrl = `${repoUrl(env, `dist/${channel}`)}/${post.image}`;

    // Meta fetches the image from its own network. A 429 here only means
    // GitHub is throttling Cloudflare's shared addresses, which says nothing
    // about whether Meta can fetch it, so it does not stop the post.
    const head = await fetch(imageUrl, { method: "HEAD" });
    if (!head.ok && head.status !== 429) {
      throw new Error(`card image not reachable (HTTP ${head.status}) at ${imageUrl}`);
    }

    const container = await graphPost(`${ig}/media`, {
      image_url: imageUrl,
      caption: post.caption,
      access_token: env.IG_TOKEN,
    });

    await waitForContainer(env, container.id);

    const published = await graphPost(`${ig}/media_publish`, {
      creation_id: container.id,
      access_token: env.IG_TOKEN,
    });

    const permalink = await getPermalink(env, published.id);
    await alert(env, `✅ ${tag} Live: ${post.headline}\n${permalink || published.id}`);
    return { published: published.id, permalink };
  } catch (err) {
    await alert(env, `❌ ${tag} Publish failed:\n${err.message}`);
    return { error: redact(err.message, env) };
  }
}

/**
 * What tonight's run does with one account's card: {publish: true}, or a skip
 * with the message to send about it -- none for a skip the morning build has
 * already reported. `summary` is the same decision in a few words, for the test.
 */
export function verdict(post, status, today, hold = null) {
  if (!post) {
    const why = status === 404 ? "is missing" : typeof status === "number" ? `could not be read (HTTP ${status})` : `is ${status}`;
    return {
      skipped: "no post.json",
      icon: "⚠️",
      alert: `Today's card ${why}: the morning build failed or never ran. Nothing published.`,
      summary: `nothing, today's card ${why}`,
    };
  }
  if (post.date !== today) {
    return {
      skipped: "stale post.json",
      icon: "⚠️",
      alert: `The newest card is dated ${post.date}, today is ${today}: the morning build did not run. Nothing published.`,
      summary: `nothing yet, the newest card is from ${post.date}`,
    };
  }
  if (post.skip) {
    return { skipped: post.skip, summary: `skips (${post.skip})` };
  }
  if (hold || post.hold) {
    const by = hold ? ` ${hold}` : "";
    return { skipped: "hold", icon: "⏸", alert: `Held${by}. Nothing published tonight.`, summary: `held${by}` };
  }
  if (post.dry_run) {
    return {
      skipped: "dry_run",
      icon: "🌓",
      alert: `Shadow mode. Would have published:\n${post.headline}`,
      summary: `shadow mode, would publish "${post.headline}"`,
    };
  }
  return { publish: true, summary: `publishes "${post.headline}"` };
}

// --- the test address --------------------------------------------------------

/**
 * The /run address. Checks what tonight's run depends on -- each account,
 * today's cards, the holds, Telegram -- and posts nothing, whatever the cards
 * say. A test that could publish would post a live card during a deploy, and
 * the 19:45 run would then post the same card again.
 */
export async function test(rawEnv) {
  const env = cleanEnv(rawEnv);
  const holds = await readHolds(env);
  const channels = {};
  for (const name of channelNames(env)) {
    channels[name] = await testChannel(env, name, holds.reason(name));
  }

  const lines = Object.entries(channels).map(
    ([name, c]) => `[${name}] ${c.error ? `❌ ${c.error}` : `✅ ${c.account} reachable`}. Tonight: ${c.tonight}.`
  );
  const telegram = await alert(
    env,
    ["🧪 Publisher test. Nothing was posted.", ...lines, ...holds.problems.map((p) => `⚠️ ${p}`)].join("\n")
  );
  return { mode: "test", posted: false, channels, telegram, notes: holds.problems };
}

async function testChannel(env, channel, hold) {
  const out = {};
  const idName = `IG_USER_ID_${channel.toUpperCase()}`;
  if (!env.IG_TOKEN) {
    out.error = "IG_TOKEN is not set";
  } else if (!env[idName]) {
    out.error = `${idName} is not set`;
  } else {
    try {
      // Read-only: proves the token and the account ID work together.
      const account = await graphGet(env, env[idName], { fields: "username" });
      out.account = account.username ? `@${account.username}` : `account ${account.id}`;
    } catch (err) {
      out.error = `Meta refused the account check: ${err.message}`;
    }
  }
  try {
    const { post, status } = await loadPost(env, channel);
    out.tonight = verdict(post, status, istDate(), hold).summary;
  } catch (err) {
    out.tonight = `unknown, the card could not be read (${err.message})`;
  }
  return out;
}

// --- holds -------------------------------------------------------------------

/**
 * Tonight's holds, from your Telegram messages and from state/hold.flag.
 *
 * A source that cannot be read gives no hold -- holding on an outage would
 * cancel posts nobody asked to cancel -- but it is named in `problems` and
 * reported, so a hold you sent is never lost without a word.
 */
async function readHolds(env) {
  const [telegram, repo] = await Promise.all([telegramHolds(env), repoHold(env)]);
  return {
    problems: [telegram.problem, repo.problem].filter(Boolean),
    reason: (channel) => (telegram.held[channel] ? "by your message" : repo.held ? "by state/hold.flag" : null),
  };
}

async function telegramHolds(env) {
  if (!env.TG_TOKEN || !env.TG_CHAT) return { held: {} };
  try {
    // offset=-100: the latest hundred messages. No webhook is ever set on the
    // bot, so getUpdates can read them without taking them from anyone.
    const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/getUpdates?offset=-100`);
    const body = await r.json();
    if (!body.ok) {
      throw new Error(`Telegram answered ${body.error_code || r.status}: ${body.description || "no detail"}`);
    }
    return { held: holdState(body.result, env.TG_CHAT, istDate()) };
  } catch (err) {
    return {
      held: {},
      problem: `Could not read your messages to the bot (${err.message}), so a hold sent there today cannot be seen. To hold tonight's posts, commit state/hold.flag.`,
    };
  }
}

async function repoHold(env) {
  try {
    const { status } = await readRepoFile(env, "state/hold.flag");
    if (status === 200) return { held: true };
    if (status === 404) return { held: false };
    throw new Error(`HTTP ${status}`);
  } catch (err) {
    return {
      held: false,
      problem: `Could not check the repo for state/hold.flag (${err.message}), so a hold committed there cannot be seen tonight.`,
    };
  }
}

/**
 * {news: true, ...} for accounts held by today's messages from your chat.
 * Messages from any other chat, or from another day, are ignored.
 */
export function holdState(updates, chatId, today) {
  const messages = (updates || [])
    .map((u) => u.message || u.edited_message)
    .filter((m) => m && String(m.chat?.id) === String(chatId) && typeof m.text === "string")
    .filter((m) => istDate(new Date(m.date * 1000)) === today)
    .sort((a, b) => a.date - b.date);

  const held = {};
  for (const m of messages) {
    const match = m.text.trim().toLowerCase().match(/^\/?(hold|resume)(?:@\w+)?(?:\s+(news|flirt|tech|metaphor|all))?\s*$/);
    if (!match) continue;
    for (const channel of ALIASES[match[2] || "all"]) held[channel] = match[1] === "hold";
  }
  return held;
}

// --- helpers -----------------------------------------------------------------

/**
 * The settings with every text value trimmed, once, on the way in. A value
 * piped into `wrangler secret put` can carry a trailing newline; trimming here
 * means the key check, the redaction and every API call see the same value.
 */
export function cleanEnv(env) {
  return Object.fromEntries(
    Object.entries(env || {}).map(([name, value]) => [name, typeof value === "string" ? value.trim() : value])
  );
}

function channelNames(env) {
  return (env.CHANNELS || "news").split(",").map((s) => s.trim()).filter(Boolean);
}

/** Today's date in Asia/Kolkata, as YYYY-MM-DD. */
export function istDate(date = new Date()) {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(date);
}

/**
 * Removes secrets from anything headed for Telegram or a response. Meta echoes
 * a bad token inside its own error message, and nothing masks a chat message.
 */
export function redact(text, env) {
  let out = String(text);
  for (const value of [env.IG_TOKEN, env.TG_TOKEN, env.MANUAL_KEY]) {
    const secret = typeof value === "string" ? value.trim() : "";
    if (secret.length >= 8) out = out.split(secret).join("[redacted]");
  }
  return out.replace(/EAA[A-Za-z0-9_-]{8,}/g, "[redacted]");
}

function repoUrl(env, path) {
  return `https://raw.githubusercontent.com/${env.REPO}/main/${path}`;
}

/**
 * A file from the repo as it is now, past GitHub's and Cloudflare's caches.
 * Throttling and passing errors are retried; a 404 is an answer, not an error.
 */
async function readRepoFile(env, path) {
  for (let attempt = 1; ; attempt++) {
    const r = await fetch(`${repoUrl(env, path)}?t=${Date.now()}`, {
      cf: { cacheTtl: 0 },
      headers: { "cache-control": "no-cache" },
    });
    if (r.ok || r.status === 404 || attempt === 3) return r;
    await timing.sleep(attempt * 10_000);
  }
}

async function loadPost(env, channel) {
  const r = await readRepoFile(env, `dist/${channel}/post.json`);
  if (!r.ok) return { post: null, status: r.status };
  try {
    return { post: await r.json(), status: r.status };
  } catch {
    return { post: null, status: "not valid JSON" };
  }
}

async function graphPost(url, params) {
  return graphCall(url, () => ({
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params),
  }));
}

async function graphGet(env, path, params = {}) {
  const url = new URL(`${GRAPH}/${env.GRAPH_VERSION}/${path}`);
  for (const [name, value] of Object.entries(params)) url.searchParams.set(name, value);
  url.searchParams.set("access_token", env.IG_TOKEN);
  return graphCall(url, () => ({ method: "GET" }));
}

async function graphCall(url, init, attempt = 1) {
  const r = await fetch(url, init());

  let body;
  try {
    body = await r.json();
  } catch {
    body = { error: { type: "GraphError", code: r.status >= 500 ? 2 : 0, message: `HTTP ${r.status} with no JSON body` } };
  }

  if (body.error) {
    const retriable = [1, 2, 4, 17, 341].includes(body.error.code); // transient or throttled
    if (retriable && attempt < 3) {
      await timing.sleep(attempt * 5000);
      return graphCall(url, init, attempt + 1);
    }
    throw new Error(`${body.error.type || "GraphError"} ${body.error.code}: ${body.error.message}`);
  }
  return body;
}

/**
 * Containers are usually FINISHED immediately for images, but publishing an
 * IN_PROGRESS container fails, so confirm before spending the publish call.
 */
async function waitForContainer(env, creationId) {
  for (let i = 0; i < 6; i++) {
    const url = new URL(`${GRAPH}/${env.GRAPH_VERSION}/${creationId}`);
    url.searchParams.set("fields", "status_code,status");
    url.searchParams.set("access_token", env.IG_TOKEN);

    let body = {};
    try {
      body = await (await fetch(url)).json();
    } catch {
      /* treat an unreadable status like one that is not ready yet */
    }
    if (body.status_code === "FINISHED") return;
    if (body.status_code === "ERROR" || body.status_code === "EXPIRED") {
      throw new Error(`container ${body.status_code}: ${body.status || "no detail"}`);
    }
    await timing.sleep(3000);
  }
  throw new Error("container never reached FINISHED within 18s");
}

async function getPermalink(env, mediaId) {
  try {
    const url = new URL(`${GRAPH}/${env.GRAPH_VERSION}/${mediaId}`);
    url.searchParams.set("fields", "permalink");
    url.searchParams.set("access_token", env.IG_TOKEN);
    const body = await (await fetch(url)).json();
    return body.permalink || null;
  } catch {
    return null; // a missing permalink must not fail a successful publish
  }
}

/** Sends a Telegram message, and says what happened -- the test reports it. */
async function alert(env, text) {
  if (!env.TG_TOKEN || !env.TG_CHAT) return "not sent: TG_TOKEN or TG_CHAT is not set";
  try {
    const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: env.TG_CHAT,
        text: redact(text, env),
        disable_web_page_preview: true,
      }),
    });
    const body = await r.json().catch(() => ({}));
    return body.ok ? "sent" : `refused by Telegram (${body.error_code || r.status}: ${body.description || "no detail"})`;
  } catch (err) {
    // Never let a failed notification mask the result it was reporting.
    return `not sent: Telegram unreachable (${err.message})`;
  }
}
