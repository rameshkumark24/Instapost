// Tests for the publisher: the one component that talks to Instagram, and the
// one that had never run. Every network call is scripted; nothing leaves the
// machine. Run: node --test   (from the worker folder)

import { test } from "node:test";
import assert from "node:assert/strict";
import worker, { run, holdState, redact, istDate, timing, cleanEnv } from "../src/index.js";

timing.sleep = async () => {};

const TOKEN = "EAAsecretPublisherToken1234567890";
const TG = "123456:telegram-bot-token-abcdef";
const KEY = "test-key-123456";
const ENV = {
  REPO: "me/repo",
  CHANNELS: "news,flirt",
  GRAPH_VERSION: "v26.0",
  IG_USER_ID_NEWS: "111",
  IG_USER_ID_FLIRT: "222",
  IG_TOKEN: TOKEN,
  TG_TOKEN: TG,
  TG_CHAT: "42",
};

const now = () => Math.floor(Date.now() / 1000);

function card(channel, extra = {}) {
  return {
    channel,
    date: istDate(),
    headline: `${channel} headline`,
    caption: `${channel} caption`,
    image: "card.jpg",
    hold: false,
    dry_run: false,
    ...extra,
  };
}

function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function message(text, { chat = 42, date = now() } = {}) {
  return { update_id: date, message: { chat: { id: chat }, date, text } };
}

/**
 * Scripts every fetch the publisher makes and records it for assertions.
 * `inbox` and `delivery` replace Telegram's getUpdates and sendMessage replies;
 * `holdFlag` says whether state/hold.flag exists in the repo.
 */
function network({ posts = {}, updates = [], graph = [], head = 200, holdFlag = false, inbox = null, delivery = null } = {}) {
  const calls = [];
  const telegram = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = String(input);
    const method = init.method || "GET";
    calls.push({ url, method });

    if (url.includes("api.telegram.org") && url.includes("/sendMessage")) {
      telegram.push(JSON.parse(init.body).text);
      return delivery ? delivery() : json({ ok: true });
    }
    if (url.includes("api.telegram.org") && url.includes("/getUpdates")) {
      return inbox ? inbox() : json({ ok: true, result: updates });
    }

    if (url.includes("/state/hold.flag")) {
      return holdFlag ? new Response("", { status: 200 }) : new Response("not found", { status: 404 });
    }

    const postFor = url.match(/dist\/(\w+)\/post\.json/);
    if (postFor) {
      const scripted = posts[postFor[1]];
      if (typeof scripted === "function") return scripted();
      return scripted === undefined ? new Response("not found", { status: 404 }) : json(scripted);
    }

    if (method === "HEAD") return new Response(null, { status: head });

    if (url.includes("graph.facebook.com")) {
      for (const [matches, reply] of graph) {
        if (matches(url, method)) return reply(url, method);
      }
      if (url.includes("fields=username")) {
        const id = new URL(url).pathname.split("/").at(-1);
        return json({ id, username: `acct${id}` });
      }
      if (method === "POST" && url.endsWith("/media")) return json({ id: `container-${url.split("/").at(-2)}` });
      if (url.includes("/container-")) return json({ status_code: "FINISHED" });
      if (method === "POST" && url.endsWith("/media_publish")) return json({ id: `media-${url.split("/").at(-2)}` });
      if (url.includes("fields=permalink")) return json({ permalink: "https://www.instagram.com/p/abc/" });
    }
    throw new Error(`unscripted fetch: ${method} ${url}`);
  };
  const graphCalls = () => calls.filter((c) => c.url.includes("graph.facebook.com"));
  return { calls, telegram, graphCalls };
}

/** Calls the /run address the way the deploy assistant does. */
async function callTestAddress(env = ENV) {
  const request = new Request("https://publisher.example/run", { headers: { "x-key": KEY } });
  const response = await worker.fetch(request, { ...env, MANUAL_KEY: KEY });
  const text = await response.text();
  return { status: response.status, text, report: JSON.parse(text) };
}

test("publishes both accounts and tags each confirmation with its account", async () => {
  const net = network({ posts: { news: card("news"), flirt: card("flirt") } });
  const result = await run(ENV);
  assert.equal(result.news.published, "media-111");
  assert.equal(result.flirt.published, "media-222");
  assert.ok(net.telegram.some((t) => t.startsWith("✅ [news] Live")));
  assert.ok(net.telegram.some((t) => t.startsWith("✅ [flirt] Live")));
});

test("a deliberate skip stays silent and publishes nothing for that account", async () => {
  const net = network({
    posts: { news: { channel: "news", date: istDate(), skip: "no story cleared the bar" }, flirt: card("flirt") },
  });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "no story cleared the bar");
  assert.ok(!net.graphCalls().some((c) => c.url.includes("/111/")));
  assert.ok(!net.telegram.some((t) => t.includes("[news]")));
  assert.equal(result.flirt.published, "media-222");
});

test("a card from another day is refused and reported", async () => {
  const net = network({ posts: { news: card("news", { date: "2020-01-01" }), flirt: card("flirt") } });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "stale post.json");
  assert.ok(net.telegram.some((t) => t.includes("[news]") && t.includes("did not run")));
  assert.ok(!net.graphCalls().some((c) => c.url.includes("/111/")));
});

test("a missing card is reported as a build that failed or never ran", async () => {
  const net = network({ posts: { flirt: card("flirt") } });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "no post.json");
  assert.ok(net.telegram.some((t) => t.includes("[news]") && t.includes("failed or never ran")));
});

test("shadow mode never publishes", async () => {
  const net = network({ posts: { news: card("news", { dry_run: true }), flirt: card("flirt", { dry_run: true }) } });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "dry_run");
  assert.equal(net.graphCalls().length, 0);
  assert.ok(net.telegram.some((t) => t.startsWith("🌓 [news]")));
});

test("replying hold to the bot stops both accounts", async () => {
  const net = network({ posts: { news: card("news"), flirt: card("flirt") }, updates: [message("hold")] });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "hold");
  assert.equal(result.flirt.skipped, "hold");
  assert.equal(net.graphCalls().length, 0);
  assert.ok(net.telegram.some((t) => t.includes("Held by your message")));
});

test("hold news holds only the news account", async () => {
  network({ posts: { news: card("news"), flirt: card("flirt") }, updates: [message("hold news")] });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "hold");
  assert.equal(result.flirt.published, "media-222");
});

test("resume after a hold cancels it", async () => {
  network({
    posts: { news: card("news"), flirt: card("flirt") },
    updates: [message("hold", { date: now() - 600 }), message("resume", { date: now() - 60 })],
  });
  const result = await run(ENV);
  assert.equal(result.news.published, "media-111");
  assert.equal(result.flirt.published, "media-222");
});

test("holds from another chat or another day are ignored", () => {
  const today = istDate();
  assert.deepEqual(holdState([message("hold", { chat: 99 })], "42", today), {});
  assert.deepEqual(holdState([message("hold", { date: now() - 3 * 86400 })], "42", today), {});
  assert.deepEqual(holdState([message("please hold on")], "42", today), {});
});

test("tech and metaphor both mean the tech-metaphor account", () => {
  const today = istDate();
  assert.deepEqual(holdState([message("hold tech")], "42", today), { flirt: true });
  assert.deepEqual(holdState([message("/hold metaphor")], "42", today), { flirt: true });
});

test("a hold committed to the repo holds every account, even after the morning build", async () => {
  // The card says hold: false -- it was built before the flag was committed.
  const net = network({ posts: { news: card("news"), flirt: card("flirt") }, holdFlag: true });
  const result = await run(ENV);
  assert.equal(result.news.skipped, "hold");
  assert.equal(result.flirt.skipped, "hold");
  assert.equal(net.graphCalls().length, 0);
  assert.ok(net.telegram.some((t) => t.includes("Held by state/hold.flag")));
});

test("an inbox the publisher cannot read is reported, and the posts go ahead", async () => {
  const net = network({
    posts: { news: card("news"), flirt: card("flirt") },
    inbox: () => json({ ok: false, error_code: 409, description: "Conflict: can't use getUpdates method while webhook is active" }, 409),
  });
  const result = await run(ENV);
  assert.equal(result.news.published, "media-111");
  assert.equal(result.flirt.published, "media-222");
  assert.ok(net.telegram.some((t) => t.includes("Could not read your messages to the bot") && t.includes("state/hold.flag")));
});

test("GitHub throttling the image check does not stop the post", async () => {
  network({ posts: { news: card("news"), flirt: card("flirt") }, head: 429 });
  const result = await run(ENV);
  assert.equal(result.news.published, "media-111");
});

test("a throttled read of the card is retried", async () => {
  let reads = 0;
  const throttledThenFine = () => (++reads === 1 ? new Response("slow down", { status: 429 }) : json(card("news")));
  network({ posts: { news: throttledThenFine, flirt: card("flirt") } });
  const result = await run(ENV);
  assert.equal(reads, 2);
  assert.equal(result.news.published, "media-111");
});

test("the token never reaches Telegram, even when Meta echoes it back", async () => {
  const echoesToken = [
    (url, method) => method === "POST" && url.endsWith("/111/media"),
    () => json({ error: { type: "OAuthException", code: 190, message: `Malformed access token ${TOKEN}` } }, 400),
  ];
  const net = network({ posts: { news: card("news"), flirt: card("flirt") }, graph: [echoesToken] });
  const result = await run(ENV);
  assert.ok(net.telegram.some((t) => t.startsWith("❌ [news] Publish failed")));
  for (const text of net.telegram) assert.ok(!text.includes(TOKEN), `token leaked in: ${text}`);
  assert.ok(!result.news.error.includes(TOKEN));
  assert.equal(result.flirt.published, "media-222");
});

test("a passing Meta error is retried", async () => {
  let attempts = 0;
  const flaky = [
    (url, method) => method === "POST" && url.endsWith("/111/media"),
    (url) => (++attempts === 1 ? json({ error: { code: 4, message: "Application request limit reached" } }, 400) : json({ id: `container-${url.split("/").at(-2)}` })),
  ];
  network({ posts: { news: card("news"), flirt: card("flirt") }, graph: [flaky] });
  const result = await run(ENV);
  assert.equal(attempts, 2);
  assert.equal(result.news.published, "media-111");
});

test("a missing account ID is reported, not silently skipped", async () => {
  const net = network({ posts: { news: card("news"), flirt: card("flirt") } });
  const { IG_USER_ID_FLIRT, ...partial } = ENV;
  const result = await run(partial);
  assert.equal(result.flirt.skipped, "IG_USER_ID_FLIRT not set");
  assert.ok(net.telegram.some((t) => t.includes("[flirt]") && t.includes("IG_USER_ID_FLIRT")));
  assert.equal(result.news.published, "media-111");
});

test("a failed permalink lookup does not fail a successful post", async () => {
  const noPermalink = [(url) => url.includes("fields=permalink"), () => { throw new Error("network down"); }];
  const net = network({ posts: { news: card("news"), flirt: card("flirt") }, graph: [noPermalink] });
  const result = await run(ENV);
  assert.equal(result.news.published, "media-111");
  assert.ok(net.telegram.some((t) => t.startsWith("✅ [news] Live")));
});

// --- the test address --------------------------------------------------------

test("the test address checks every account and never publishes, even a live card", async () => {
  const net = network({ posts: { news: card("news"), flirt: card("flirt") } });
  const { status, report } = await callTestAddress();
  assert.equal(status, 200);
  assert.equal(report.mode, "test");
  assert.equal(report.posted, false);
  assert.equal(report.channels.news.account, "@acct111");
  assert.equal(report.channels.flirt.account, "@acct222");
  assert.match(report.channels.news.tonight, /^publishes "news headline"/);
  assert.ok(!net.calls.some((c) => c.method !== "GET"  && c.url.includes("graph.facebook.com")), "the test made a publishing call");
  assert.ok(!net.calls.some((c) => c.method === "HEAD"), "the test checked an image it had no reason to fetch");
  assert.equal(report.telegram, "sent");
  assert.ok(net.telegram.some((t) => t.startsWith("🧪 Publisher test. Nothing was posted.")));
});

test("the test address reports an account Meta refuses, without the token", async () => {
  const refuses = [
    (url) => url.includes("/222?") && url.includes("fields=username"),
    () => json({ error: { type: "OAuthException", code: 190, message: `Invalid OAuth access token ${TOKEN}` } }, 400),
  ];
  network({ posts: { news: card("news"), flirt: card("flirt") }, graph: [refuses] });
  const { report, text } = await callTestAddress();
  assert.match(report.channels.flirt.error, /190/);
  assert.equal(report.channels.news.account, "@acct111");
  assert.ok(!text.includes(TOKEN), "the token reached the test report");
});

test("the test address reports a missing account ID and a chat Telegram refuses", async () => {
  network({
    posts: { news: card("news", { dry_run: true }), flirt: card("flirt", { dry_run: true }) },
    delivery: () => json({ ok: false, error_code: 400, description: "Bad Request: chat not found" }, 400),
  });
  const { IG_USER_ID_FLIRT, ...partial } = ENV;
  const { report } = await callTestAddress(partial);
  assert.equal(report.channels.flirt.error, "IG_USER_ID_FLIRT is not set");
  assert.match(report.channels.news.tonight, /^shadow mode/);
  assert.match(report.telegram, /chat not found/);
});

test("the test address accepts a key stored with a trailing newline, and only the right key", async () => {
  network({ posts: { news: card("news", { dry_run: true }), flirt: card("flirt", { dry_run: true }) } });
  const env = { ...ENV, MANUAL_KEY: `${KEY}\n` };
  const ok = await worker.fetch(new Request("https://publisher.example/run", { headers: { "x-key": KEY } }), env);
  assert.equal(ok.status, 200);
  const denied = await worker.fetch(new Request("https://publisher.example/run", { headers: { "x-key": "wrong" } }), env);
  assert.equal(denied.status, 403);
  const noKeyConfigured = await worker.fetch(new Request("https://publisher.example/run", { headers: { "x-key": "" } }), ENV);
  assert.equal(noKeyConfigured.status, 403);
});

test("secrets stored with a trailing newline are used trimmed and still redacted", () => {
  const env = cleanEnv({ ...ENV, MANUAL_KEY: `${KEY}\r\n`, IG_TOKEN: `${TOKEN}\n` });
  assert.equal(env.MANUAL_KEY, KEY);
  assert.equal(env.IG_TOKEN, TOKEN);
  // Even given the untrimmed value, redaction matches the secret as it appears in text.
  assert.ok(!redact(`echoed ${KEY} back`, { MANUAL_KEY: `${KEY}\r\n` }).includes(KEY));
});

test("redact removes every known secret and any token-shaped string", () => {
  const text = `a ${TOKEN} b ${TG} c EAAotherTokenShape12345`;
  const out = redact(text, ENV);
  assert.ok(!out.includes(TOKEN));
  assert.ok(!out.includes(TG));
  assert.ok(!out.includes("EAAotherTokenShape12345"));
});
