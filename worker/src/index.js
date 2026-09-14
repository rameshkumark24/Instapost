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
 * Hold from your phone: send the bot "hold" (both accounts), "hold news" or
 * "hold tech" on the day, and "resume" to undo. The latest command wins.
 */

const GRAPH = "https://graph.facebook.com";
const ALIASES = { news: ["news"], flirt: ["flirt"], tech: ["flirt"], metaphor: ["flirt"], all: ["news", "flirt"] };

/** Swapped out in tests so retries and polling do not really wait. */
export const timing = { sleep: (ms) => new Promise((resolve) => setTimeout(resolve, ms)) };

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env));
  },

  // Manual trigger for testing: curl -H "x-key: $MANUAL_KEY" https://<worker>/run
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/run") return new Response("instapost", { status: 200 });
    // Trimmed: a key piped into `wrangler secret put` can carry a trailing
    // newline, which would turn every test call into a silent "forbidden".
    const key = (env.MANUAL_KEY || "").trim();
    if (!key || (request.headers.get("x-key") || "").trim() !== key) {
      return new Response("forbidden", { status: 403 });
    }
    const result = await run(env);
    return new Response(redact(JSON.stringify(result, null, 2), env), {
      headers: { "content-type": "application/json" },
    });
  },
};

/**
 * Publishes every configured account. Each has its own IG_USER_ID, so they
 * never share a publishing quota, and one failing never stops the other.
 */
export async function run(env) {
  const names = (env.CHANNELS || "news").split(",").map((s) => s.trim()).filter(Boolean);
  const holds = await fetchHolds(env);
  const results = {};
  for (const name of names) {
    const idName = `IG_USER_ID_${name.toUpperCase()}`;
    const userId = env[idName];
    if (!userId) {
      await alert(env, `⚠️ [${name}] ${idName} is not set on the publisher, so this account cannot post.`);
      results[name] = { skipped: `${idName} not set` };
      continue;
    }
    results[name] = await runChannel(env, name, userId, Boolean(holds[name]));
  }
  return results;
}

export async function runChannel(env, channel, userId, heldByMessage = false) {
  const tag = `[${channel}]`;
  try {
    const base = `https://raw.githubusercontent.com/${env.REPO}/main/dist/${channel}`;
    const { post, status } = await loadPost(base);

    // --- guards ------------------------------------------------------------

    if (!post) {
      const why = status === 404 ? "is missing" : typeof status === "number" ? `could not be read (HTTP ${status})` : `is ${status}`;
      await alert(env, `⚠️ ${tag} Today's card ${why}: the morning build failed or never ran. Nothing published.`);
      return { skipped: "no post.json" };
    }

    const today = istDate();
    if (post.date !== today) {
      await alert(env, `⚠️ ${tag} The newest card is dated ${post.date}, today is ${today}: the morning build did not run. Nothing published.`);
      return { skipped: "stale post.json" };
    }

    if (post.skip) {
      return { skipped: post.skip };        // chosen and reported by the morning build
    }

    if (heldByMessage || post.hold) {
      await alert(env, `⏸ ${tag} Held${heldByMessage ? " by your message" : ""}. Nothing published tonight.`);
      return { skipped: "hold" };
    }

    if (post.dry_run) {
      await alert(env, `🌓 ${tag} Shadow mode. Would have published:\n${post.headline}`);
      return { skipped: "dry_run" };
    }

    // --- publish -----------------------------------------------------------

    const ig = `${GRAPH}/${env.GRAPH_VERSION}/${userId}`;
    const imageUrl = `${base}/${post.image}`;

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

// --- hold from Telegram ----------------------------------------------------

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

async function fetchHolds(env) {
  if (!env.TG_TOKEN || !env.TG_CHAT) return {};
  try {
    // offset=-100: the latest hundred messages. No webhook is ever set on the
    // bot, so getUpdates can read them without taking them from anyone.
    const r = await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/getUpdates?offset=-100`);
    const body = await r.json();
    return holdState(body.result, env.TG_CHAT, istDate());
  } catch {
    // Unreadable means no hold. Holding on a Telegram outage would cancel
    // posts nobody asked to cancel; a hold is rare and sent deliberately.
    return {};
  }
}

// --- helpers ---------------------------------------------------------------

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
  for (const secret of [env.IG_TOKEN, env.TG_TOKEN, env.MANUAL_KEY]) {
    if (secret && secret.length >= 8) out = out.split(secret).join("[redacted]");
  }
  return out.replace(/EAA[A-Za-z0-9_-]{8,}/g, "[redacted]");
}

async function loadPost(base) {
  let status = 0;
  for (let attempt = 1; attempt <= 3; attempt++) {
    const r = await fetch(`${base}/post.json?t=${Date.now()}`, {
      cf: { cacheTtl: 0 },
      headers: { "cache-control": "no-cache" },
    });
    status = r.status;
    if (r.ok) {
      try {
        return { post: await r.json(), status };
      } catch {
        return { post: null, status: "not valid JSON" };
      }
    }
    if (status === 404) break;                // genuinely missing: retrying will not help
    await timing.sleep(attempt * 10_000);     // throttled by GitHub, or a passing error
  }
  return { post: null, status };
}

async function graphPost(url, params, attempt = 1) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params),
  });

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
      return graphPost(url, params, attempt + 1);
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

async function alert(env, text) {
  if (!env.TG_TOKEN || !env.TG_CHAT) return;
  try {
    await fetch(`https://api.telegram.org/bot${env.TG_TOKEN}/sendMessage`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        chat_id: env.TG_CHAT,
        text: redact(text, env),
        disable_web_page_preview: true,
      }),
    });
  } catch {
    /* never let a failed notification mask the result it was reporting */
  }
}
