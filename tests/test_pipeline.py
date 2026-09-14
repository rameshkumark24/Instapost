"""Regression tests for the parts that fail expensively.

Nobody watches this pipeline at 19:45, so a silent regression posts publicly
before anyone notices. These tests cover the failures that would actually
reach an audience: the safety gates, the scoring bug that filled the account
with famous repos, the approval queue, escaping of LLM-written text, and every
finding from the code review of the enrichment, branding, workflow and token
health changes.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import io
import os
import socket
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

import yaml

from src import compose, score, token_health
from src import config as cfg
from src.enrich import (
    Enriched,
    _decode,
    _extract,
    _fetch_head,
    _is_public_ip,
    _parse_meta,
    _safe_url,
    _site_name,
    fetch,
)
from src.flirt import Rejected, validate
from src.harvest import Item
from src.render import _highlight

ROOT = Path(__file__).resolve().parent.parent


def item(**kw) -> Item:
    base = dict(
        title="A thing happened",
        url="https://example.com/a",
        source="hn",
        publication="Hacker News",
        published=datetime.now(timezone.utc) - timedelta(hours=2),
        engagement=200.0,
        summary="",
    )
    base.update(kw)
    return Item(**base)


def meta(content: str, key: str = "og:description", quote: str = '"') -> str:
    return f"<meta property={quote}{key}{quote} content={quote}{content}{quote}>"


def resolve_literals(host: str) -> list[str]:
    """Fake DNS: IP literals resolve to themselves, names to a public address."""
    return [host] if host.replace(".", "").isdigit() else ["93.184.216.34"]


class SafetyGates(unittest.TestCase):
    """The gates are the last thing between an LLM and a public feed."""

    concept = {"term": "FOREIGN KEY", "meaning": "x", "id": "fk", "domain": "sql"}

    def test_accepts_a_good_line(self):
        text = "She was my PRIMARY KEY. Then she became a FOREIGN KEY in someone else's table."
        self.assertEqual(validate(text, self.concept), text)

    def test_each_gate_fires_for_its_own_reason(self):
        # All >= 60 chars so the length check cannot mask the gate under test.
        cases = [
            ("She was my FOREIGN KEY and honestly what an absolute bitch she turned out to be.", "banned"),
            ("Women are all the same, every one of them just a FOREIGN KEY pointing elsewhere.", "banned"),
            ("She took me to bed and it was sexy, but she was still a FOREIGN KEY to someone.", "banned"),
            ("He said he would rather die than admit he was just a FOREIGN KEY in her table.", "banned"),
            ("A FOREIGN KEY is a column that references the primary key of another table here.", "metaphor"),
            ("She was my everything and then one day she simply left and it hurt for months.", "does not use"),
        ]
        for text, expected in cases:
            with self.subTest(text=text[:40]):
                with self.assertRaises(Rejected) as ctx:
                    validate(text, self.concept)
                self.assertIn(expected, str(ctx.exception))

    def test_rejects_emoji_and_hashtags(self):
        for bad in [
            "She was my FOREIGN KEY, pointing somewhere else entirely and I hated it \U0001F494",
            "She was my FOREIGN KEY pointing off somewhere else entirely and I hated #sql",
        ]:
            with self.subTest(bad=bad[-12:]):
                with self.assertRaises(Rejected):
                    validate(bad, self.concept)


class GitHubRecency(unittest.TestCase):
    """Regression: 13 of the first 17 picks were famous evergreen repos."""

    def test_old_repo_scores_zero_recency(self):
        ancient = item(source="github", published=datetime.now(timezone.utc) - timedelta(days=3650))
        self.assertEqual(score.recency(ancient), 0.0)

    def test_new_repo_scores_well(self):
        fresh = item(source="github", published=datetime.now(timezone.utc) - timedelta(days=3))
        self.assertGreater(score.recency(fresh), 0.85)

    def test_github_window_is_wider_than_news(self):
        age = datetime.now(timezone.utc) - timedelta(days=20)
        self.assertGreater(score.recency(item(source="github", published=age)), 0.0)
        self.assertEqual(score.recency(item(source="hn", published=age)), 0.0)


class Diversity(unittest.TestCase):
    """No single source may quietly become the whole account."""

    def test_dominant_source_is_penalised(self):
        self.assertLess(score.diversity(item(source="github"), ["github"] * 9 + ["hn"]), 0.75)

    def test_minority_source_is_untouched(self):
        self.assertEqual(score.diversity(item(source="hn"), ["github"] * 9 + ["hn"]), 1.0)

    def test_empty_history_is_neutral(self):
        self.assertEqual(score.diversity(item(source="github"), []), 1.0)

    def test_penalty_never_zeroes_a_source(self):
        self.assertGreater(score.diversity(item(source="github"), ["github"] * 10), 0.6)


class ApprovalQueue(unittest.TestCase):
    """The human gate must not be bypassable, in either direction."""

    def setUp(self):
        from src import queue
        self.queue = queue

    def test_only_approved_entries_are_postable(self):
        entries = [
            {"concept": "a", "status": "pending", "text": "x"},
            {"concept": "b", "status": "approved", "text": "y"},
        ]
        self.assertEqual(self.queue.next_approved(entries)["concept"], "b")

    def test_nothing_postable_when_all_pending(self):
        self.assertIsNone(self.queue.next_approved([{"concept": "a", "status": "pending", "text": "x"}]))

    def test_issue_body_lists_only_pending(self):
        body = self.queue.render_issue_body([
            {"concept": "a", "status": "pending", "text": "line a"},
            {"concept": "b", "status": "posted", "text": "line b"},
        ])
        self.assertIn("`a`", body)
        self.assertNotIn("`b`", body)

    def test_posted_entries_drain_in_order(self):
        entries = [
            {"concept": "a", "status": "approved", "text": "x"},
            {"concept": "b", "status": "approved", "text": "y"},
        ]
        self.queue.mark_posted(entries, "a")
        self.assertEqual(entries[0]["status"], "posted")
        self.assertEqual(self.queue.next_approved(entries)["concept"], "b")


class Escaping(unittest.TestCase):
    """Quote text is LLM-written and goes into HTML. It is never trusted."""

    def test_injected_markup_is_neutralised(self):
        out = _highlight("A <script>alert(1)</script> FOREIGN KEY here.", ["FOREIGN KEY"])
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)

    def test_terms_are_still_bolded(self):
        self.assertIn("<b>PRIMARY KEY</b>", _highlight("She was my PRIMARY KEY today.", ["PRIMARY KEY"]))


class EnrichmentParsing(unittest.TestCase):
    """Reading the publisher's description exactly as they wrote it."""

    def test_apostrophe_inside_double_quotes_survives(self):  # review 1
        text = "ChatGPT, Claude, and Grok all suffered outages at nearly the same time, and nobody's saying why yet."
        self.assertEqual(_extract(meta(text)), text)

    def test_double_quote_inside_single_quotes_survives(self):
        text = 'The team calls it a "durable by default" engine that needs no write-ahead log at all.'
        self.assertEqual(_extract(meta(text, quote="'")), text)

    def test_entity_encoded_apostrophe_is_decoded(self):
        self.assertEqual(
            _extract(meta("It&#39;s the first database engine to ship durable writes without a log.")),
            "It's the first database engine to ship durable writes without a log.",
        )

    def test_angle_bracket_inside_a_value_does_not_end_the_tag(self):
        text = "Benchmarks show latency > 40% lower than the previous release across every workload."
        self.assertEqual(_extract(meta(text)), text)

    def test_long_description_is_returned_whole_not_cut_mid_word(self):  # review 2
        text = ("The company said it would roll out the update gradually over the coming weeks. " * 5).strip()
        self.assertEqual(_extract(meta(text)), text)

    def test_prefers_og_description(self):
        html = (
            '<meta name="description" content="Generic site tagline that is long enough to pass.">'
            + meta("The specific summary of this particular article, written by its publisher.")
        )
        self.assertEqual(_extract(html), "The specific summary of this particular article, written by its publisher.")

    def test_site_name_prefers_og_site_name(self):
        found = _parse_meta('<meta property="og:site_name" content="WIRED">')
        self.assertEqual(_site_name(found, "https://www.wired.com/story/x"), "WIRED")

    def test_site_name_falls_back_to_host(self):
        self.assertEqual(_site_name({}, "https://www.wired.com/story/x"), "wired.com")


class EnrichmentFilters(unittest.TestCase):
    def test_real_summary_mentioning_cookies_is_kept(self):  # review 15
        text = "Google is finally phasing out third-party cookies in Chrome for all users this year."
        self.assertEqual(_extract(meta(text)), text)

    def test_real_summary_mentioning_a_newsletter_is_kept(self):
        text = "The newsletter platform raised prices for every creator with more than a thousand readers."
        self.assertEqual(_extract(meta(text)), text)

    def test_boilerplate_is_rejected(self):
        for junk in [
            "We use cookies to improve your experience on our website and for analytics.",
            "Sign up for our newsletter to get the latest stories delivered to your inbox.",
            "Please enable JavaScript to continue",
            "Browse all models available on Cerebras public endpoints.",
        ]:
            with self.subTest(junk=junk[:30]):
                self.assertIsNone(_extract(meta(junk)))

    def test_github_boilerplate_is_stripped(self):  # review 4, via an HN link to a repo
        text = "A tiny, dependency-free JSON parser written in Rust. Contribute to o/r development by creating an account on GitHub."
        self.assertEqual(_extract(meta(text)), "A tiny, dependency-free JSON parser written in Rust.")

    def test_github_boilerplate_alone_is_rejected(self):
        self.assertIsNone(_extract(meta("Fast JSON parser. Contribute to o/r development by creating an account on GitHub.")))


class EnrichmentDecoding(unittest.TestCase):
    """Bytes are decoded once, with the charset the page declares."""

    def test_utf8_page_with_bare_text_html_header_is_not_read_as_latin1(self):  # review 3
        body = meta("It’s here — and it’s fast enough to replace the old engine outright.").encode("utf-8")
        self.assertIn("It’s here — and", _decode(body, "text/html"))

    def test_meta_charset_is_honoured(self):
        body = '<meta charset="windows-1252"><title>café</title>'.encode("cp1252")
        self.assertIn("café", _decode(body, "text/html"))

    def test_header_charset_takes_precedence(self):
        self.assertEqual(_decode("café".encode("latin-1"), "text/html; charset=ISO-8859-1"), "café")

    def test_unknown_charset_falls_back_to_utf8(self):
        self.assertEqual(_decode("ok".encode(), "text/html; charset=bogus-9"), "ok")


class EnrichmentAddressSafety(unittest.TestCase):
    """Harvested URLs come from a site anyone can post to. Treat them as hostile."""

    def test_non_public_addresses_are_refused(self):  # review 7
        for address in [
            "127.0.0.1", "0.0.0.0", "10.0.0.1", "172.16.0.1", "192.168.1.5",
            "169.254.169.254", "100.64.0.1", "::1", "::ffff:127.0.0.1",
            "0:0:0:0:0:0:0:1", "fe80::1", "fc00::1", "224.0.0.1",
        ]:
            with self.subTest(address=address):
                self.assertFalse(_is_public_ip(address))

    def test_public_addresses_are_allowed(self):
        for address in ["93.184.216.34", "2606:4700:4700::1111"]:
            with self.subTest(address=address):
                self.assertTrue(_is_public_ip(address))

    def test_name_resolving_inward_is_refused(self):
        self.assertFalse(_safe_url("https://innocent.example/x", resolve=lambda h: ["127.0.0.1"]))

    def test_one_inward_address_refuses_the_whole_host(self):
        self.assertFalse(_safe_url("https://mixed.example/", resolve=lambda h: ["93.184.216.34", "10.0.0.1"]))

    def test_unresolvable_host_is_refused(self):
        def fail(host):
            raise socket.gaierror("no such host")
        self.assertFalse(_safe_url("https://nope.example/", resolve=fail))

    def test_credentials_and_other_schemes_are_refused(self):
        for url in ["https://user:pw@example.com/", "file:///etc/passwd", "ftp://example.com/a", "javascript:alert(1)"]:
            with self.subTest(url=url):
                self.assertFalse(_safe_url(url, resolve=resolve_literals))

    def test_public_https_is_allowed(self):
        self.assertTrue(_safe_url("https://arstechnica.com/story", resolve=resolve_literals))


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(b"",)):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = chunks

    @property
    def is_redirect(self):
        return "location" in self.headers and self.status_code in (301, 302, 303, 307, 308)

    def iter_content(self, chunk_size=1):
        yield from self._chunks

    def close(self):
        pass


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requested: list[str] = []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.responses.pop(0)


class EnrichmentFetching(unittest.TestCase):
    def test_redirect_to_an_internal_address_is_never_requested(self):  # review 7
        session = FakeSession([FakeResponse(302, {"location": "http://169.254.169.254/latest/meta-data/"})])
        self.assertIsNone(_fetch_head("https://attacker.example/x", session, resolve=resolve_literals))
        self.assertEqual(session.requested, ["https://attacker.example/x"])

    def test_redirect_chains_are_capped(self):
        session = FakeSession([FakeResponse(302, {"location": f"https://hop{i}.example/"}) for i in range(10)])
        self.assertIsNone(_fetch_head("https://start.example/", session, resolve=resolve_literals))
        self.assertLessEqual(len(session.requested), 4)

    def test_reading_stops_at_end_of_head_even_across_chunks(self):  # review 14
        chunks = [b"<html><head><title>x</title>", b"</he", b"ad><body>", b"x" * 1_000_000]
        session = FakeSession([FakeResponse(200, {"content-type": "text/html"}, chunks)])
        body, _, _ = _fetch_head("https://ok.example/", session, resolve=resolve_literals)
        self.assertLess(len(body), 1_000)

    def test_non_html_is_ignored(self):
        session = FakeSession([FakeResponse(200, {"content-type": "application/pdf"}, [b"%PDF"])])
        self.assertIsNone(_fetch_head("https://ok.example/a.pdf", session, resolve=resolve_literals))

    def test_a_slow_server_cannot_hold_the_build(self):  # review 8
        def drip(url, session):
            time.sleep(3)
        started = time.monotonic()
        self.assertIsNone(fetch("https://slow.example/", deadline=0.2, _fetch=drip))
        self.assertLess(time.monotonic() - started, 1.5)

    def test_fetch_returns_the_description_and_its_publisher(self):
        page = (
            b'<head><meta property="og:site_name" content="WIRED">'
            b'<meta property="og:description" content="Three AI assistants went down within minutes of each other and no one has explained why.">'
        )
        got = fetch("https://www.wired.com/x", _fetch=lambda url, s: (page, "text/html; charset=utf-8", url))
        self.assertEqual(got.site_name, "WIRED")
        self.assertIn("no one has explained why", got.description)


ENRICHED = Enriched(
    description=(
        "Three AI assistants went down within minutes of each other, and none of the "
        "companies has said why. Engineers suspect a shared upstream dependency."
    ),
    site_name="WIRED",
)
NO_LLM = {"GEMINI_API_KEY": "", "GROQ_API_KEY": ""}
HEADLINE = "Nobody is saying why the AI assistants went down"


class ComposeEnrichment(unittest.TestCase):
    def test_github_items_are_never_enriched(self):  # review 4
        with mock.patch.object(compose, "fetch_enrichment") as fetch_mock, mock.patch.dict(os.environ, NO_LLM):
            compose.compose(item(
                source="github", publication="GitHub", url="https://github.com/o/fastjson",
                title="fastjson: Fast JSON parser", summary="Fast JSON parser",
            ))
        fetch_mock.assert_not_called()

    def test_items_with_their_own_summary_are_not_enriched(self):
        with mock.patch.object(compose, "fetch_enrichment") as fetch_mock, mock.patch.dict(os.environ, NO_LLM):
            compose.compose(item(summary="A summary supplied by the source itself, long enough to need nothing more."))
        fetch_mock.assert_not_called()

    def test_publisher_words_are_quoted_and_credited_to_the_publisher(self):  # review 10
        with mock.patch.object(compose, "fetch_enrichment", return_value=ENRICHED), mock.patch.dict(os.environ, NO_LLM):
            post = compose.compose(item(title=HEADLINE))
        self.assertTrue(post["body"].startswith("“") and post["body"].endswith("”"))
        self.assertEqual(post["publication"], "WIRED")
        self.assertIn("Source: WIRED (found via Hacker News)", post["caption"])

    def test_a_long_quote_is_trimmed_at_a_sentence(self):  # review 2
        long = Enriched(description=("The rollout reaches every region over the next month. " * 8).strip(), site_name="The Verge")
        with mock.patch.object(compose, "fetch_enrichment", return_value=long), mock.patch.dict(os.environ, NO_LLM):
            post = compose.compose(item(title=HEADLINE))
        self.assertLessEqual(len(post["body"]), cfg.BODY_MAX_CHARS)
        self.assertTrue(post["body"].endswith(".”"))

    def test_quotation_marks_inside_a_quote_are_nested(self):  # seen in the first live run
        said = Enriched(
            description=(
                "Dario has written that we need to “pace the frontier,” and Sam has agreed. "
                "People may be surprised by my response: go ahead."
            ),
            site_name="a post on X",
        )
        with mock.patch.object(compose, "fetch_enrichment", return_value=said), mock.patch.dict(os.environ, NO_LLM):
            post = compose.compose(item(title="David Sacks: the labs do not need regulation"))
        self.assertEqual(post["body"].count("“"), 1)
        self.assertEqual(post["body"].count("”"), 1)
        self.assertIn("‘pace the frontier,’", post["body"])

    def test_straight_double_quotes_are_nested_in_pairs(self):
        self.assertEqual(
            compose._nest_quotes('He called it "fast" and "cheap".'),
            "He called it ‘fast’ and ‘cheap’.",
        )

    def test_social_posts_are_credited_as_posts_not_to_the_platform(self):  # seen in the first live run
        cases = [
            ({"og:site_name": "X (formerly Twitter)"}, "https://x.com/someone/status/1", "a post on X"),
            ({}, "https://mobile.twitter.com/someone/status/1", "a post on X"),
            ({"og:site_name": "YouTube"}, "https://youtu.be/abc", "a video on YouTube"),
            ({"og:site_name": "WIRED"}, "https://www.wired.com/story/x", "WIRED"),
        ]
        for found, url, expected in cases:
            with self.subTest(url=url):
                self.assertEqual(_site_name(found, url), expected)

    def test_llm_is_given_the_enriched_text(self):  # review 9
        prompts: list[str] = []
        with mock.patch.object(compose, "fetch_enrichment", return_value=ENRICHED), \
                mock.patch.object(compose, "_call_llm", side_effect=lambda p: prompts.append(p)), \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "test", "GROQ_API_KEY": ""}):
            compose.compose(item(title=HEADLINE))
        self.assertEqual(len(prompts), 1)
        self.assertIn("none of the companies has said why", prompts[0])
        self.assertIn("publication: WIRED", prompts[0])

    def test_llm_body_copying_the_source_is_rejected(self):  # review 10
        source = item(summary=ENRICHED.description)
        copied = {
            "headline": "AI assistants went down together",
            "body": "Reports say none of the companies has said why. Engineers suspect a shared upstream dependency.",
        }
        self.assertFalse(compose._validate(copied, source))

    def test_llm_body_in_its_own_words_is_accepted(self):
        source = item(summary=ENRICHED.description)
        own = {
            "headline": "AI assistants failed at once",
            "body": "Several chatbots failed together and their makers have stayed quiet about the cause.",
        }
        self.assertTrue(compose._validate(own, source))


class BrandingGuard(unittest.TestCase):
    """A card carrying a placeholder handle must never reach a live account."""

    def setUp(self):
        self._dry = cfg.DRY_RUN

    def tearDown(self):
        cfg.DRY_RUN = self._dry

    def test_every_shipped_placeholder_is_blocked_live(self):
        cfg.DRY_RUN = False
        for name, channel in cfg.CHANNELS.items():
            with self.subTest(channel=name):
                with self.assertRaises(RuntimeError):
                    cfg.assert_branding_ready(channel)

    def test_placeholders_are_not_substrings_of_each_other(self):  # review 6
        handles = [c["handle"] for c in cfg.CHANNELS.values()]
        for a in handles:
            for b in handles:
                if a != b:
                    self.assertNotIn(a, b)

    def test_guard_does_not_repeat_the_placeholder_text(self):  # review 6
        # A find-and-replace on a placeholder must not also rewrite the check.
        source = (ROOT / "src" / "config.py").read_text(encoding="utf-8")
        guard = source[source.index("_HANDLE = re.compile"): source.index("# --- caption")]
        for channel in cfg.CHANNELS.values():
            self.assertNotIn(channel["handle"], guard)

    def test_malformed_handles_are_blocked_live(self):
        cfg.DRY_RUN = False
        for bad in ["", "technews", "@", "@has space", "@" + "a" * 31, "@emoji\U0001F642"]:
            with self.subTest(handle=bad):
                with self.assertRaises(RuntimeError):
                    cfg.assert_branding_ready({"handle": bad})

    def test_real_handle_passes_live(self):
        cfg.DRY_RUN = False
        cfg.assert_branding_ready({"handle": "@some_real.account"})

    def test_shadow_build_allows_placeholder(self):
        cfg.DRY_RUN = True
        cfg.assert_branding_ready(cfg.CHANNELS["news"])


class BuildWorkflow(unittest.TestCase):
    """Regression: one channel failing threw away the other channel's card."""

    def setUp(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"))
        self.steps = {s["name"]: s for s in workflow["jobs"]["build"]["steps"] if s.get("name")}

    def test_commit_runs_even_if_a_build_step_failed(self):  # review 5
        self.assertIn("!cancelled()", str(self.steps["Commit card and ledger"].get("if", "")))

    def test_flirt_build_runs_even_if_news_failed(self):
        self.assertIn("!cancelled()", str(self.steps["Build tech-metaphor card"].get("if", "")))


class TokenHealth(unittest.TestCase):
    """Broken and inconclusive must never be confused."""

    def test_expired_token_is_broken(self):
        body = '{"error": {"code": 190, "message": "Session has expired"}}'
        self.assertEqual(token_health.classify(400, body)[0], token_health.BROKEN)

    def test_missing_permission_is_broken(self):
        body = '{"error": {"code": 10, "message": "Permission denied"}}'
        self.assertEqual(token_health.classify(403, body)[0], token_health.BROKEN)

    def test_network_failure_is_inconclusive_not_broken(self):  # review 12
        self.assertEqual(token_health.classify(None, None)[0], token_health.INCONCLUSIVE)

    def test_non_json_response_is_inconclusive(self):
        self.assertEqual(token_health.classify(502, "<html>Bad gateway</html>")[0], token_health.INCONCLUSIVE)

    def test_rate_limit_is_inconclusive(self):
        body = '{"error": {"code": 4, "message": "Application request limit reached"}}'
        self.assertEqual(token_health.classify(400, body)[0], token_health.INCONCLUSIVE)

    def test_server_error_is_inconclusive(self):
        self.assertEqual(token_health.classify(500, "{}")[0], token_health.INCONCLUSIVE)

    def test_success_is_ok(self):
        self.assertEqual(token_health.classify(200, '{"id": "1"}')[0], token_health.OK)

    def test_data_access_expiry_is_not_ignored(self):  # review 13
        now = 1_000_000
        data = {"expires_at": 0, "data_access_expires_at": now + 10 * 86400}
        self.assertAlmostEqual(token_health.expiry_days(data, now), 10)

    def test_earlier_of_two_running_clocks_wins(self):
        now = 1_000_000
        data = {"expires_at": now + 30 * 86400, "data_access_expires_at": now + 5 * 86400}
        self.assertAlmostEqual(token_health.expiry_days(data, now), 5)

    def test_no_running_clock_means_no_expiry(self):
        self.assertIsNone(token_health.expiry_days({"expires_at": 0, "data_access_expires_at": 0}, 1_000_000))

    def test_alert_is_sent_even_when_the_console_cannot_print_it(self):
        # Regression: printing before sending let a cp1252 console crash the
        # run on the emoji and swallow the alert.
        narrow_console = io.TextIOWrapper(io.BytesIO(), encoding="cp1252")
        with mock.patch.object(token_health.urllib.request, "urlopen") as urlopen, \
                mock.patch.object(sys, "stdout", narrow_console), \
                mock.patch.dict(os.environ, {"TG_TOKEN": "t", "TG_CHAT": "c"}):
            status = token_health._report("🔑 Instagram token is BROKEN — test", 1)
        self.assertEqual(status, 1)
        urlopen.assert_called_once()

    def test_token_echoed_in_a_graph_error_is_redacted_before_truncation(self):  # found by the live smoke test
        token = "EAAG" + "x7Kq2" * 40          # real tokens run to ~200 characters
        body = token_health.json.dumps({"error": {"code": 190, "message": f"Malformed access token {token}"}})
        outcome, payload = token_health.classify(400, body)
        self.assertEqual(outcome, token_health.BROKEN)
        self.assertNotIn("x7Kq2x7Kq2", payload["reason"])      # not even a truncated fragment

    def test_alert_never_carries_a_secret(self):
        token, app_secret = "EAAbogus_token_for_test_1234567890", "s3cr3t-app-secret-value"
        sent: list[str] = []

        def capture(url, data=None, timeout=None):
            sent.append(token_health.urllib.parse.unquote_plus(data.decode()))
            return mock.MagicMock()

        env = {"TG_TOKEN": "t", "TG_CHAT": "c", "IG_TOKEN": token, "META_APP_SECRET": app_secret}
        with mock.patch.object(token_health.urllib.request, "urlopen", side_effect=capture), \
                mock.patch.object(sys, "stdout", io.StringIO()), \
                mock.patch.dict(os.environ, env):
            token_health._report(f"reason mentions {token} and {app_secret}", 1)
        self.assertEqual(len(sent), 1)
        self.assertNotIn(token, sent[0])
        self.assertNotIn(app_secret, sent[0])


GEMINI_OK = {"candidates": [{"content": {"parts": [{"text": "hello"}]}}]}


def http(status: int, payload: dict | None = None, text: str = ""):
    response = mock.MagicMock()
    response.status_code = status
    response.text = text
    response.json.return_value = payload
    return response


class LanguageModel(unittest.TestCase):
    """Regression: every LLM call targeted gemini-2.0-flash after its shutdown."""

    def setUp(self):
        from src import llm
        self.llm = llm

    def run_with(self, env: dict, **post):
        with mock.patch.object(self.llm.requests, "post", **post) as fake, mock.patch.dict(os.environ, env):
            return self.llm.complete("p", temperature=0.4), fake

    def test_shut_down_model_is_not_configured(self):
        self.assertNotIn("gemini-2.0-flash", cfg.GEMINI_MODELS)

    def test_retired_model_falls_through_to_the_next(self):
        replies = iter([http(404, text="models/x is not found"), http(200, GEMINI_OK)])
        reply, fake = self.run_with({"GEMINI_API_KEY": "k", "GROQ_API_KEY": ""}, side_effect=lambda *a, **k: next(replies))
        self.assertEqual(reply.text, "hello")
        self.assertEqual(fake.call_count, 2)
        self.assertIn(cfg.GEMINI_MODELS[1], fake.call_args_list[1].args[0])

    def test_rejected_key_stops_the_chain(self):
        reply, fake = self.run_with(
            {"GEMINI_API_KEY": "bad", "GROQ_API_KEY": ""},
            return_value=http(400, text="API key not valid. Please pass a valid API key."),
        )
        self.assertIsNone(reply.text)
        self.assertEqual(fake.call_count, 1)
        self.assertIn("key rejected", reply.error)

    def test_failure_reason_is_reported_not_swallowed(self):
        reply, _ = self.run_with({"GEMINI_API_KEY": "k", "GROQ_API_KEY": ""}, return_value=http(404, text="not found"))
        self.assertIsNone(reply.text)
        self.assertIn("model not found", reply.error)

    def test_no_key_makes_no_request(self):
        reply, fake = self.run_with({"GEMINI_API_KEY": "", "GROQ_API_KEY": ""})
        fake.assert_not_called()
        self.assertEqual(reply.error, "no LLM key configured")

    def test_api_key_travels_in_a_header_never_the_url(self):
        _, fake = self.run_with({"GEMINI_API_KEY": "AIzaSecretKey123", "GROQ_API_KEY": ""}, return_value=http(200, GEMINI_OK))
        self.assertNotIn("AIzaSecretKey123", fake.call_args.args[0])
        self.assertNotIn("params", fake.call_args.kwargs)
        self.assertEqual(fake.call_args.kwargs["headers"]["x-goog-api-key"], "AIzaSecretKey123")

    def test_gemini_output_budget_leaves_room_for_thinking(self):
        _, fake = self.run_with({"GEMINI_API_KEY": "k", "GROQ_API_KEY": ""}, return_value=http(200, GEMINI_OK))
        budget = fake.call_args.kwargs["json"]["generationConfig"]["maxOutputTokens"]
        self.assertGreaterEqual(budget, 2048)


class FlirtDrafting(unittest.TestCase):
    def test_batch_stops_at_the_first_unreachable_model(self):
        from src import flirt
        dead = flirt.llm.Reply(None, "gemini-3.8-flash: model not found (HTTP 404)")
        with mock.patch.object(flirt.llm, "complete", return_value=dead) as complete:
            drafts, rejects = flirt.generate_batch(set(), size=12)
        self.assertEqual(drafts, [])
        self.assertEqual(complete.call_count, 1)        # not once per concept in the bank
        self.assertIn("llm unavailable", rejects[0])

    def test_a_refill_that_drafts_nothing_says_why(self):
        from src import pipeline_flirt
        with mock.patch.object(pipeline_flirt, "generate_batch",
                               return_value=([], ["fk: llm unavailable: no LLM key configured"])), \
                mock.patch.object(pipeline_flirt.notify, "skipped") as skipped:
            pipeline_flirt._refill_if_low([])
        skipped.assert_called_once()
        self.assertIn("drafted nothing", skipped.call_args.args[0])
        self.assertIn("no LLM key configured", skipped.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
