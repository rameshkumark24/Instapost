/**
 * Instapost publisher.
 *
 * Fires at 14:15 UTC (19:45 IST) from a Cloudflare cron trigger, because
 * GitHub Actions' `schedule` event is best-effort and routinely drifts 30-60
 * minutes on free-tier repos. Publishing is only two HTTPS calls, so the whole
 * job fits comfortably inside a Worker.
 *
 * Everything this Worker does is guarded: it will refuse to publish stale,
 * held, or shadow-mode content rather than post the wrong thing unattended.
 */

const GRAPH = "https://graph.facebook.com";

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(run(env));
  },

  // Manual trigger for testing: curl -H "x-key: $MANUAL_KEY" https://<worker>/run
  async fetch(request, env) {
    const url = new URL(request.url);
    if (url.pathname !== "/run") return new Response("instapost", { status: 200 });
    if (request.headers.get("x-key") !== env.MANUAL_KEY) {
      return new Response("forbidden", { status: 403 });
    }
    const result = await run(env);
    return new Response(JSON.stringify(result, null, 2), {
      headers: { "content-type": "application/json" },
    });
  },
};

async function run(env) {
  try {
    const base = `https://raw.githubusercontent.com/${env.REPO}/main/dist`;
    const post = await loadPost(base);

    // --- guards ------------------------------------------------------------

    if (!post) {
      await alert(env, "⚠️ No post.json — tonight's build failed or skipped.");
      return { skipped: "no post.json" };
    }

    const today = istDate();
    if (post.date !== today) {
      await alert(
        env,
        `⚠️ post.json is dated ${post.date}, today is ${today}. ` +
          `Build did not run. Nothing published.`
      );
      return { skipped: "stale post.json" };
    }

    if (post.hold) {
      await alert(env, "⏸ hold.flag was set — nothing published tonight.");
      return { skipped: "hold" };
    }

    if (post.dry_run) {
      await alert(env, `🌓 Shadow mode. Would have published:\n${post.headline}`);
      return { skipped: "dry_run" };
    }

    // --- publish -----------------------------------------------------------

    const ig = `${GRAPH}/${env.GRAPH_VERSION}/${env.IG_USER_ID}`;
    const imageUrl = `${base}/${post.image}`;

    const head = await fetch(imageUrl, { method: "HEAD" });
    if (!head.ok) {
      throw new Error(`image not reachable (${head.status}) at ${imageUrl}`);
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
    await alert(env, `✅ Live: ${post.headline}\n${permalink || published.id}`);
    return { published: published.id, permalink };
  } catch (err) {
    await alert(env, `❌ Publish failed:\n${err.message}`);
    return { error: err.message };
  }
}

// --- helpers ---------------------------------------------------------------

async function loadPost(base) {
  const r = await fetch(`${base}/post.json?t=${Date.now()}`, {
    cf: { cacheTtl: 0 },
    headers: { "cache-control": "no-cache" },
  });
  return r.ok ? r.json() : null;
}

/** Today's date in Asia/Kolkata, as YYYY-MM-DD. */
function istDate() {
  return new Intl.DateTimeFormat("en-CA", {
    timeZone: "Asia/Kolkata",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(new Date());
}

async function graphPost(url, params, attempt = 1) {
  const r = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params),
  });
  const body = await r.json();

  if (body.error) {
    const retriable = [1, 2, 4, 17, 341].includes(body.error.code); // transient / throttled
    if (retriable && attempt < 3) {
      await sleep(attempt * 5000);
      return graphPost(url, params, attempt + 1);
    }
    throw new Error(
      `${body.error.type || "GraphError"} ${body.error.code}: ${body.error.message}`
    );
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

    const body = await (await fetch(url)).json();
    if (body.status_code === "FINISHED") return;
    if (body.status_code === "ERROR" || body.status_code === "EXPIRED") {
      throw new Error(`container ${body.status_code}: ${body.status || "no detail"}`);
    }
    await sleep(3000);
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
        text,
        disable_web_page_preview: true,
      }),
    });
  } catch {
    /* never let a failed notification mask the result it was reporting */
  }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
