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
                mock.patch.object(pipeline_flirt.notify, "notice") as notice:
            pipeline_flirt._refill_if_low([])
        notice.assert_called_once()
        title, text = notice.call_args.args
        self.assertEqual(title, "Drafting failed")
        self.assertIn("drafted nothing", text)
        self.assertIn("no LLM key configured", text)


class QueueStock(unittest.TestCase):
    """Regression: every run drafted twelve more while the first twelve sat unreviewed."""

    def setUp(self):
        from src import pipeline_flirt, queue
        self.pipeline, self.queue = pipeline_flirt, queue

    @staticmethod
    def rows(pending: int = 0, approved: int = 0) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = [{"concept": f"p{i}", "status": "pending", "text": "x", "drafted_at": now} for i in range(pending)]
        out += [{"concept": f"a{i}", "status": "approved", "text": "y", "drafted_at": now} for i in range(approved)]
        return out

    def test_unreviewed_drafts_count_toward_the_stock(self):
        with mock.patch.object(self.pipeline, "generate_batch") as batch:
            self.pipeline._refill_if_low(self.rows(pending=12))
        batch.assert_not_called()

    def test_refill_runs_when_approved_and_pending_are_low(self):
        with mock.patch.object(self.pipeline, "generate_batch", return_value=([], ["x: no usable line"])) as batch, \
                mock.patch.object(self.pipeline.notify, "notice"):
            self.pipeline._refill_if_low(self.rows(pending=3, approved=4))
        batch.assert_called_once()
        self.assertEqual(batch.call_args.kwargs["budget_s"], cfg.FLIRT_DRAFT_BUDGET_S)

    def test_stale_pending_drafts_expire_and_nothing_else_does(self):
        old = (datetime.now(timezone.utc) - timedelta(days=cfg.FLIRT_PENDING_EXPIRE_DAYS + 1)).isoformat()
        new = datetime.now(timezone.utc).isoformat()
        rows = [
            {"concept": "stale", "status": "pending", "drafted_at": old},
            {"concept": "fresh", "status": "pending", "drafted_at": new},
            {"concept": "kept", "status": "approved", "drafted_at": old},
        ]
        self.assertEqual(self.queue.expire_stale(rows, cfg.FLIRT_PENDING_EXPIRE_DAYS), 1)
        self.assertEqual([r["status"] for r in rows], ["expired", "pending", "approved"])

    def test_expired_concepts_return_to_the_pool(self):
        rows = [{"concept": "gone", "status": "expired"}, {"concept": "live", "status": "pending"}]
        self.assertEqual(self.queue.used_concept_ids(rows), {"live"})

    def test_issue_text_matches_what_the_code_does(self):
        body = self.queue.render_issue_body([{"concept": "a", "status": "pending", "text": "x"}])
        self.assertNotIn("dropped automatically", body)
        self.assertIn("expire", body)


class DraftingBudget(unittest.TestCase):
    """Regression: the first real batch took 10m36s of a 15-minute job."""

    concept = {"term": "TIMEOUT", "meaning": "x", "id": "t", "domain": "networking"}

    def test_no_model_calls_once_the_budget_is_spent(self):
        from src import flirt
        with mock.patch.object(flirt.llm, "complete") as complete:
            drafts, _ = flirt.generate_batch(set(), size=12, budget_s=0)
        complete.assert_not_called()
        self.assertEqual(drafts, [])

    def test_an_ellipsis_is_not_several_sentences(self):
        text = "I waited for your reply... and waited. Then my heart hit its TIMEOUT and stopped listening."
        self.assertEqual(validate(text, self.concept), text)

    def test_too_many_real_sentences_are_still_rejected(self):
        text = "I tried. You left. I called. You blocked. I waited for a TIMEOUT that never came."
        with self.assertRaises(Rejected):
            validate(text, self.concept)


class Notices(unittest.TestCase):
    """Regression: an informational "12 new cards drafted" arrived titled "skipped tonight"."""

    def test_new_drafts_are_announced_as_cards_to_review(self):
        from src import pipeline_flirt
        drafts = [{"concept": "fk", "term": "FOREIGN KEY", "domain": "sql", "text": "x", "terms": []}]
        with mock.patch.object(pipeline_flirt, "generate_batch", return_value=(drafts, [])), \
                mock.patch.object(pipeline_flirt.queue, "publish_issue", return_value=1), \
                mock.patch.object(pipeline_flirt.notify, "notice") as notice, \
                mock.patch.object(pipeline_flirt.notify, "skipped") as skipped:
            pipeline_flirt._refill_if_low([])
        skipped.assert_not_called()
        self.assertEqual(notice.call_args.args[0], "New cards to review")


class Timeouts(unittest.TestCase):
    """Regression: model calls shared the 20s limit meant for ordinary requests."""

    def test_model_calls_get_a_longer_limit_than_ordinary_requests(self):
        from src import llm
        with mock.patch.object(llm.requests, "post", return_value=http(200, GEMINI_OK)) as post, \
                mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k", "GROQ_API_KEY": ""}):
            llm.complete("p", temperature=0.4)
        _, read = post.call_args.kwargs["timeout"]
        self.assertGreaterEqual(read, 45)
        self.assertGreater(read, cfg.HTTP_TIMEOUT)

    def test_worst_case_run_fits_inside_the_job_limit(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"))
        job_s = workflow["jobs"]["build"]["timeout-minutes"] * 60
        chain = (cfg.LLM_CONNECT_TIMEOUT_S + cfg.LLM_TIMEOUT_S) * len(cfg.GEMINI_MODELS)
        # install + news build with one timed-out chain + drafting budget
        # overrun by one more chain + commit and verify
        worst = 180 + (120 + chain) + (cfg.FLIRT_DRAFT_BUDGET_S + chain) + 60
        self.assertLess(worst, job_s)


class GraphVersion(unittest.TestCase):
    """v21.0 stops working on 21 January 2027; nothing may still pin it."""

    def test_no_file_pins_an_expiring_graph_version(self):
        # Checks the pins themselves, not any mention: wrangler.toml records
        # in a comment that v21.0 was pinned before and when it expires.
        pins = {
            "src/token_health.py": 'DEFAULT_VERSION = "v21.0"',
            "worker/wrangler.toml": 'GRAPH_VERSION = "v21.0"',
            "SETUP.md": "graph.facebook.com/v21.0/",
        }
        for rel, pin in pins.items():
            with self.subTest(file=rel):
                self.assertNotIn(pin, (ROOT / rel).read_text(encoding="utf-8"))

    def test_publisher_and_token_check_use_the_same_version(self):
        wrangler = (ROOT / "worker" / "wrangler.toml").read_text(encoding="utf-8")
        self.assertIn(f'GRAPH_VERSION = "{token_health.DEFAULT_VERSION}"', wrangler)


class ProfileKit(unittest.TestCase):
    """Each account must be sign-up ready: limits Instagram enforces, no placeholders."""

    def setUp(self):
        from src import brand
        self.brand = brand

    def test_every_account_has_a_kit(self):
        self.assertEqual(set(self.brand.PROFILES), set(cfg.CHANNELS))

    def test_profiles_fit_instagram_limits(self):
        self.assertEqual(self.brand.check_profiles(), [])

    def test_the_two_accounts_do_not_share_an_identity(self):
        news, flirt = self.brand.PROFILES["news"], self.brand.PROFILES["flirt"]
        self.assertFalse(set(news["handle_ideas"]) & set(flirt["handle_ideas"]))
        self.assertFalse(set(news["display_names"]) & set(flirt["display_names"]))
        self.assertNotEqual(news["mark"], flirt["mark"])

    def test_a_placeholder_handle_is_never_printed_on_the_kit(self):
        for name in self.brand.PROFILES:
            with self.subTest(account=name):
                handle = self.brand._channel_for_kit(name)["handle"]
                self.assertFalse(cfg._PLACEHOLDER.fullmatch(handle or "_"))

    def test_the_news_pinned_post_fits_the_card_contract(self):
        intro = self.brand.PROFILES["news"]["intro"]
        self.assertLessEqual(len(intro["headline"]), cfg.HEADLINE_MAX_CHARS)
        self.assertLessEqual(len(intro["body"]), cfg.BODY_MAX_CHARS)


class Rendering(unittest.TestCase):
    """Regression: text was fitted before the webfont loaded, then overflowed the margin.

    Needs Chromium and network access for the webfonts; CI installs both.
    """

    @classmethod
    def setUpClass(cls):
        import tempfile
        cls.out = Path(tempfile.mkdtemp())

    def test_the_overflow_check_catches_text_past_the_margin(self):
        from playwright.sync_api import sync_playwright
        from src import render
        html = (
            '<div class="frame" style="width:1080px;padding:0 92px;box-sizing:border-box">'
            '<div id="quote" style="white-space:nowrap;font:600 80px monospace;display:inline-block">'
            "real programming concept, explained</div></div>"
        )
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1080, "height": 1350})
            page.set_content(html)
            with self.assertRaises(render.RenderError):
                render._assert_no_overflow(page, "#quote")
            browser.close()

    def test_a_long_unbreakable_term_is_shrunk_inside_the_margin(self):
        from src import render
        entry = {
            "text": "Every card here is a real programming concept, explained through a relationship. "
                    "If the joke lands, you just learned what the term means.",
            "terms": ["real programming concept"],
        }
        channel = {**cfg.CHANNELS["flirt"], "handle": ""}
        self.assertTrue(render.render_quote(entry, channel, self.out / "quote.jpg").exists())

    def test_the_longest_term_in_the_bank_fits_a_card(self):
        from src import flirt, render
        term = max((c["term"] for c in flirt.load_concepts()), key=len)
        entry = {"text": f"We had it all, until you became my {term} and everything went down with you.", "terms": [term]}
        channel = {**cfg.CHANNELS["flirt"], "handle": ""}
        self.assertTrue(render.render_quote(entry, channel, self.out / "long-term.jpg").exists())

    def test_news_card_renders_inside_its_margins(self):
        from src import brand, render
        channel = {**cfg.CHANNELS["news"], "handle": ""}
        self.assertTrue(render.render(brand.PROFILES["news"]["intro"], self.out / "news.jpg", channel=channel).exists())


class FakeGraph:
    """Scripted Meta responses for the Gate A assistant."""

    def __init__(self, respond):
        self.respond = respond
        self.calls: list[tuple[str, str]] = []

    def call(self, method, path, **params):
        self.calls.append((method, path))
        return self.respond(method, path, params)


def gate_graph(accounts=2, token="ok", media="ok"):
    from src import gate_a

    def respond(method, path, params):
        if path == "me":
            if token == "ok":
                return token_health.OK, {"id": "1", "name": "Instapost system user"}
            return token_health.BROKEN, {"reason": "token rejected (190): Malformed access token [redacted]"}
        if path == "me/permissions":
            return token_health.OK, {"data": [{"permission": p, "status": "granted"} for p in gate_a.REQUIRED_PERMISSIONS]}
        if path == "me/accounts":
            return token_health.OK, {"data": [
                {"name": f"Page {i}", "instagram_business_account": {"id": f"1784{i}", "username": f"acct{i}"}}
                for i in range(accounts)
            ]}
        if path.endswith("/content_publishing_limit"):
            return token_health.OK, {"data": [{"quota_usage": 0, "config": {"quota_total": 50}}]}
        if method == "POST" and path.endswith("/media"):
            if media == "ok":
                return token_health.OK, {"id": "c-" + path.split("/")[0]}
            return token_health.BROKEN, {"reason": "token lacks permission (10): Application does not have permission"}
        if path.startswith("c-"):
            return token_health.OK, {"status_code": "FINISHED"}
        if path.endswith("/media_publish"):
            return token_health.OK, {"id": "m-1"}
        raise AssertionError(f"unexpected call {method} {path}")

    return FakeGraph(respond)


class GateAssistant(unittest.TestCase):
    """Gate A decides whether the project works; the assistant must be exact and never leak the token."""

    TOKEN = "EAAsecret_token_that_must_never_print_123456"

    def setUp(self):
        from src import gate_a
        self.gate = gate_a

    def run_gate(self, graph, publish=False):
        out = io.StringIO()
        pick = lambda accounts: {"news": accounts[0], "flirt": accounts[1]}  # noqa: E731
        with mock.patch.object(self.gate.requests, "head", return_value=mock.MagicMock(status_code=200)), \
                mock.patch.object(sys, "stdout", out):
            code = self.gate.run(graph, self.gate.Report(), publish=publish, choose=pick,
                                 captions={"news": "n", "flirt": "f"})
        return code, out.getvalue()

    def test_missing_permissions_are_named(self):
        payload = {"data": [
            {"permission": "instagram_basic", "status": "granted"},
            {"permission": "instagram_content_publish", "status": "declined"},
        ]}
        self.assertEqual(self.gate.missing_permissions(payload),
                         ["instagram_content_publish", "pages_show_list", "pages_read_engagement"])

    def test_pages_without_an_instagram_account_are_skipped(self):
        payload = {"data": [{"name": "Empty page"}, {"name": "P", "instagram_business_account": {"id": "9", "username": "x"}}]}
        self.assertEqual([a.ig_id for a in self.gate.linked_accounts(payload)], ["9"])

    def test_container_states(self):
        self.assertEqual(self.gate.container_state({"status_code": "FINISHED"})[0], "ready")
        self.assertEqual(self.gate.container_state({"status_code": "IN_PROGRESS"})[0], "waiting")
        self.assertEqual(self.gate.container_state({"status_code": "ERROR", "status": "bad image"})[0], "failed")

    def test_accounts_are_matched_by_handle_case_insensitively(self):
        a, b = self.gate.Account("P1", "1", "DailyTechBrief"), self.gate.Account("P2", "2", "commitissues")
        self.assertEqual(self.gate.assign([a, b], {"news": "@dailytechbrief", "flirt": "@CommitIssues"}), {"news": a, "flirt": b})
        self.assertIsNone(self.gate.assign([a, b], {"news": "@someoneelse", "flirt": "@commitissues"}))

    def test_card_image_address_comes_from_the_publisher_config(self):
        self.assertTrue(self.gate.card_url("news").endswith("/rameshkumark24/Instapost/main/brand/news/pinned-post.jpg"))

    def test_a_full_pass_prints_both_ids_and_publishes_nothing(self):
        graph = gate_graph()
        code, output = self.run_gate(graph)
        self.assertEqual(code, 0)
        self.assertIn("Gate A passed", output)
        self.assertIn("IG_USER_ID_NEWS", output)
        self.assertIn("17840", output)
        self.assertIn("17841", output)
        self.assertFalse(any(path.endswith("media_publish") for _, path in graph.calls))

    def test_the_token_never_appears_in_the_output(self):
        for graph in (gate_graph(), gate_graph(token="broken"), gate_graph(media="broken")):
            with self.subTest():
                _, output = self.run_gate(graph)
                self.assertNotIn(self.TOKEN, output)

    def test_a_rejected_token_stops_at_the_first_step(self):
        graph = gate_graph(token="broken")
        code, output = self.run_gate(graph)
        self.assertEqual(code, 1)
        self.assertEqual(graph.calls, [("GET", "me")])
        self.assertIn("system-user token", output)

    def test_one_linked_account_is_not_enough(self):
        code, output = self.run_gate(gate_graph(accounts=1))
        self.assertEqual(code, 1)
        self.assertIn("this project needs 2", output)

    def test_an_unaccepted_tester_invite_is_explained(self):
        code, output = self.run_gate(gate_graph(media="broken"))
        self.assertEqual(code, 1)
        self.assertIn("Instagram Tester", output)

    def test_publish_mode_publishes_each_account_once(self):
        graph = gate_graph()
        code, _ = self.run_gate(graph, publish=True)
        self.assertEqual(code, 0)
        self.assertEqual(sum(path.endswith("media_publish") for _, path in graph.calls), 2)

    def test_network_errors_never_show_the_request_url(self):
        graph = self.gate.Graph(self.TOKEN)
        boom = self.gate.requests.ConnectionError(f"https://graph.facebook.com/me?access_token={self.TOKEN}")
        with mock.patch.object(self.gate.requests, "get", side_effect=boom):
            outcome, payload = graph.call("GET", "me")
        self.assertNotEqual(outcome, token_health.OK)
        self.assertNotIn(self.TOKEN, payload["reason"])


if __name__ == "__main__":
    unittest.main()
