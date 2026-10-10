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
import shutil
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


class CardQueue(unittest.TestCase):
    """The queue you post from: each card goes out once, in the order drafted."""

    def setUp(self):
        from src import queue
        self.queue = queue

    def test_the_oldest_waiting_card_goes_next(self):
        entries = [
            {"concept": "a", "status": "sent", "text": "x"},
            {"concept": "b", "status": "pending", "text": "y"},
            {"concept": "c", "status": "pending", "text": "z"},
        ]
        self.assertEqual(self.queue.next_card(entries)["concept"], "b")

    def test_nothing_waiting_once_every_card_has_gone_out(self):
        self.assertIsNone(self.queue.next_card([{"concept": "a", "status": "sent", "text": "x"}]))

    def test_a_card_is_never_sent_twice(self):
        entries = [
            {"concept": "a", "status": "pending", "text": "x"},
            {"concept": "b", "status": "pending", "text": "y"},
        ]
        self.queue.mark_sent(entries, "a")
        self.assertEqual(entries[0]["status"], "sent")
        self.assertEqual(self.queue.next_card(entries)["concept"], "b")
        self.queue.mark_sent(entries, "a")          # a re-run must not resend it
        self.assertEqual([e["status"] for e in entries], ["sent", "pending"])

    def test_cards_from_the_old_approval_queue_are_not_stranded(self):
        # The live queue was written while ticking was still required.
        entries = [{"concept": "old", "status": "approved", "text": "x"}]
        self.assertEqual(self.queue.next_card(entries)["concept"], "old")
        self.queue.mark_sent(entries, "old")
        self.assertEqual(entries[0]["status"], "sent")

    def test_waiting_counts_only_the_cards_still_to_come(self):
        entries = [
            {"concept": "a", "status": "pending"},
            {"concept": "b", "status": "approved"},
            {"concept": "c", "status": "sent"},
            {"concept": "d", "status": "expired"},
        ]
        self.assertEqual(self.queue.waiting(entries), 2)


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

    def test_placeholder_handles_are_blocked_live(self):
        cfg.DRY_RUN = False
        for placeholder in ("@__news_handle__", "@__flirt_handle__"):
            with self.subTest(handle=placeholder):
                with self.assertRaises(RuntimeError):
                    cfg.assert_branding_ready({"handle": placeholder})

    def test_the_configured_handles_are_ready_to_go_live(self):
        cfg.DRY_RUN = False
        for name, channel in cfg.CHANNELS.items():
            with self.subTest(channel=name):
                cfg.assert_branding_ready(channel)      # raises on a placeholder or a malformed handle

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

    def test_meme_ideas_run_after_both_cards_whatever_happened(self):
        names = list(self.steps)
        self.assertLess(names.index("Build tech-metaphor card"), names.index("Build meme ideas"))
        self.assertLess(names.index("Build meme ideas"), names.index("Commit card and ledger"))
        self.assertIn("!cancelled()", str(self.steps["Build meme ideas"].get("if", "")))


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
        self.assertNotIn("llama-3.3-70b-versatile", cfg.GROQ_MODELS)    # shut down 16 Aug 2026

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

    def test_groq_reasoning_model_keeps_room_for_the_answer(self):
        # gpt-oss reasons before it answers, and the reasoning is charged to
        # the same cap. At the old 300 tokens it could spend the lot thinking.
        groq_ok = {"choices": [{"message": {"content": "hello"}}]}
        reply, fake = self.run_with({"GEMINI_API_KEY": "", "GROQ_API_KEY": "k"}, return_value=http(200, groq_ok))
        self.assertEqual(reply.text, "hello")
        body = fake.call_args.kwargs["json"]
        self.assertEqual(body["model"], cfg.GROQ_MODELS[0])
        self.assertGreaterEqual(body["max_completion_tokens"], 2048)
        self.assertEqual(body["reasoning_effort"], "low")
        self.assertNotIn("max_tokens", body)

    def test_gemini_falls_back_to_groq(self):
        replies = iter([http(429, text="quota")] * len(cfg.GEMINI_MODELS)
                       + [http(200, {"choices": [{"message": {"content": "from groq"}}]})])
        reply, fake = self.run_with({"GEMINI_API_KEY": "k", "GROQ_API_KEY": "k"},
                                    side_effect=lambda *a, **k: next(replies))
        self.assertEqual(reply.text, "from groq")
        self.assertEqual(fake.call_count, len(cfg.GEMINI_MODELS) + 1)


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

    def test_a_draft_bolds_the_term_as_its_line_spells_it(self):
        from src import flirt
        concept = {"id": "try-catch", "term": "TRY / CATCH", "meaning": "x", "domain": "code"}
        line = "You were my TRY/CATCH: every time I fell, you caught me and said it was fine."
        reply = flirt.llm.Reply(f'{{"text": "{line}", "terms": ["TRY / CATCH"]}}', None)
        with mock.patch.object(flirt.llm, "complete", return_value=reply):
            draft = flirt.generate(concept)
        self.assertEqual(draft["terms"], ["TRY/CATCH"])
        self.assertIn("<b>TRY/CATCH</b>", str(_highlight(draft["text"], draft["terms"])))


class QueueStock(unittest.TestCase):
    """Regression: every run drafted twelve more while the first twelve sat unsent."""

    def setUp(self):
        from src import pipeline_flirt, queue
        self.pipeline, self.queue = pipeline_flirt, queue

    @staticmethod
    def rows(pending: int = 0, sent: int = 0) -> list[dict]:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out = [{"concept": f"p{i}", "status": "pending", "text": "x", "drafted_at": now} for i in range(pending)]
        out += [{"concept": f"s{i}", "status": "sent", "text": "y", "drafted_at": now} for i in range(sent)]
        return out

    def test_cards_waiting_to_go_out_hold_off_a_refill(self):
        with mock.patch.object(self.pipeline, "generate_batch") as batch:
            self.pipeline._refill_if_low(self.rows(pending=12))
        batch.assert_not_called()

    def test_cards_already_sent_do_not_count_as_stock(self):
        with mock.patch.object(self.pipeline, "generate_batch", return_value=([], ["x: no usable line"])) as batch, \
                mock.patch.object(self.pipeline.notify, "notice"):
            self.pipeline._refill_if_low(self.rows(pending=3, sent=9))
        batch.assert_called_once()
        self.assertEqual(batch.call_args.kwargs["budget_s"], cfg.FLIRT_DRAFT_BUDGET_S)

    def test_expired_concepts_return_to_the_pool(self):
        rows = [{"concept": "gone", "status": "expired"}, {"concept": "live", "status": "pending"}]
        self.assertEqual(self.queue.used_concept_ids(rows), {"live"})

    def test_a_concept_already_sent_is_never_drafted_again(self):
        self.assertEqual(self.queue.used_concept_ids([{"concept": "used", "status": "sent"}]), {"used"})


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

    def test_new_drafts_are_announced_as_drafts_not_as_a_skip(self):
        from src import pipeline_flirt
        drafts = [{"concept": "fk", "term": "FOREIGN KEY", "domain": "sql", "text": "x", "terms": []}]
        with mock.patch.object(pipeline_flirt, "generate_batch", return_value=(drafts, [])), \
                mock.patch.object(pipeline_flirt.notify, "notice") as notice, \
                mock.patch.object(pipeline_flirt.notify, "skipped") as skipped:
            pipeline_flirt._refill_if_low([])
        skipped.assert_not_called()
        self.assertEqual(notice.call_args.args[0], "New cards drafted")


class Handoff(unittest.TestCase):
    """What lands on your phone has to be postable without editing anything."""

    post = {
        "channel": "flirt",
        "headline": "She was my FOREIGN KEY.",
        "caption": "She was my FOREIGN KEY.\n\n-\nFOREIGN KEY: a column that points at another table.\n\n#programmerhumor #devlife",
    }

    def sent(self, post):
        from src import notify
        image = ROOT / "brand" / "flirt" / "pinned-post.jpg"
        with mock.patch.object(notify, "_post") as post_call, \
                mock.patch.dict(os.environ, {"TG_TOKEN": "t", "TG_CHAT": "1"}):
            notify.handoff(post, image)
        return post_call.call_args_list

    def test_the_card_goes_as_a_file_so_telegram_does_not_recompress_it(self):
        method, _, files = self.sent(self.post)[0].args
        self.assertEqual(method, "sendDocument")
        self.assertIn("document", files)

    def test_the_caption_arrives_on_its_own_and_unchanged(self):
        call = self.sent(self.post)[1]
        self.assertEqual(call.args[0], "sendMessage")
        self.assertEqual(call.args[1]["text"], self.post["caption"])
        # No parse_mode: what you copy is exactly what Instagram receives.
        self.assertNotIn("parse_mode", call.args[1])

    def test_the_card_says_which_account_to_post_it_on(self):
        _, data, _ = self.sent(self.post)[0].args
        self.assertIn(cfg.CHANNELS["flirt"]["handle"], data["caption"])

    def test_a_news_card_carries_its_source(self):
        post = {**self.post, "channel": "news", "publication": "Hacker News",
                "url": "https://example.com/a", "score": 0.51, "llm_polished": True}
        _, data, _ = self.sent(post)[0].args
        self.assertIn("Hacker News", data["caption"])
        self.assertIn("https://example.com/a", data["caption"])


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
        # With both keys set, one call can wait on every Gemini model and then
        # every Groq model before it gives up.
        models = len(cfg.GEMINI_MODELS) + len(cfg.GROQ_MODELS)
        chain = (cfg.LLM_CONNECT_TIMEOUT_S + cfg.LLM_TIMEOUT_S) * models
        # install + news build with one timed-out chain + drafting budget
        # overrun by one more chain + every meme source timing out and every
        # meme attempt waiting on the whole chain + every meme image timing
        # out twice, once asking Imgflip and once fetching + commit and verify
        memes = (len(cfg.MEME_TREND_GEOS) + 1) * cfg.HTTP_TIMEOUT + cfg.MEME_ATTEMPTS * chain
        memes += cfg.MEME_IDEAS * 2 * cfg.HTTP_TIMEOUT
        worst = 180 + (120 + chain) + (cfg.FLIRT_DRAFT_BUDGET_S + chain) + memes + 60
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


def gate_graph(accounts=2, token="ok", media="ok", publish="ok"):
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
            if publish == "ok":
                return token_health.OK, {"id": "m-1"}
            return token_health.BROKEN, {"reason": "not allowed (10): Application does not have permission for this action"}
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

    def test_checks_alone_do_not_pass_gate_a(self):
        # Regression: without --publish nothing is published, yet it said "Gate A
        # passed" -- and some refusals only come at the publish call itself.
        graph = gate_graph()
        code, output = self.run_gate(graph)
        self.assertEqual(code, 0)
        self.assertNotIn("Gate A passed", output)
        self.assertIn("--publish", output)
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

    def test_gate_a_passes_once_meta_publishes_on_each_account(self):
        graph = gate_graph()
        code, output = self.run_gate(graph, publish=True)
        self.assertEqual(code, 0)
        self.assertEqual(sum(path.endswith("media_publish") for _, path in graph.calls), 2)
        self.assertIn("Gate A passed", output)
        self.assertIn("IG_USER_ID_NEWS", output)

    def test_a_refused_publish_does_not_pass_gate_a(self):
        code, output = self.run_gate(gate_graph(publish="broken"), publish=True)
        self.assertEqual(code, 1)
        self.assertNotIn("Gate A passed", output)
        self.assertIn("publishing failed", output)

    def test_network_errors_never_show_the_request_url(self):
        graph = self.gate.Graph(self.TOKEN)
        boom = self.gate.requests.ConnectionError(f"https://graph.facebook.com/me?access_token={self.TOKEN}")
        with mock.patch.object(self.gate.requests, "get", side_effect=boom):
            outcome, payload = graph.call("GET", "me")
        self.assertNotEqual(outcome, token_health.OK)
        self.assertNotIn(self.TOKEN, payload["reason"])


class Preflight(unittest.TestCase):
    """Meta's documented limits, checked when the card is built instead of at 19:45."""

    def setUp(self):
        import tempfile
        from src import preflight
        self.pre = preflight
        self.card = ROOT / "brand" / "news" / "pinned-post.jpg"
        self.tmp = Path(tempfile.mkdtemp())

    def fake_jpeg(self, width: int, height: int) -> Path:
        # SOI, then a baseline frame header: length, precision, height, width, components.
        header = (b"\xff\xd8\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big")
                  + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9)
        path = self.tmp / f"{width}x{height}.jpg"
        path.write_bytes(header)
        return path

    def test_a_real_card_passes(self):
        self.assertEqual(self.pre.check({"caption": "A short caption #tech"}, self.card), [])

    def test_jpeg_dimensions_are_read_from_the_file(self):
        self.assertEqual(self.pre.jpeg_size(self.card.read_bytes()), (1080, 1350))

    def test_a_caption_over_the_limit(self):
        self.assertTrue(any("2200" in p for p in self.pre.check({"caption": "x" * 2201}, self.card)))

    def test_too_many_hashtags(self):
        caption = " ".join(f"#tag{i}" for i in range(31))
        self.assertTrue(any("hashtags" in p for p in self.pre.check({"caption": caption}, self.card)))

    def test_too_many_mentions(self):
        caption = " ".join(f"@user{i}" for i in range(21))
        self.assertTrue(any("mentions" in p for p in self.pre.check({"caption": caption}, self.card)))

    def test_emails_and_url_fragments_are_not_counted(self):
        caption = "Write to a@b.com or see page.html#intro " * 40
        self.assertEqual(self.pre.check({"caption": caption[:2000]}, self.card), [])

    def test_a_shape_meta_refuses(self):
        problems = self.pre.check({"caption": ""}, self.fake_jpeg(1080, 2000))
        self.assertTrue(any("ratio" in p for p in problems))

    def test_both_edges_of_the_allowed_range_pass(self):
        for width, height in ((1080, 1350), (1910, 1000)):
            with self.subTest(size=f"{width}x{height}"):
                self.assertEqual(self.pre.check({"caption": ""}, self.fake_jpeg(width, height)), [])

    def test_a_png_is_refused(self):
        path = self.tmp / "card.png"
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 64)
        self.assertTrue(any("JPEG" in p for p in self.pre.check({"caption": ""}, path)))


class SkipMarker(unittest.TestCase):
    """Regression: a deliberate skip reached the publisher as a failed build."""

    def test_both_pipelines_leave_a_dated_skip_marker(self):
        import json
        import tempfile
        from src import pipeline, pipeline_flirt
        today = datetime.now(cfg.TZ)
        date = today.strftime("%Y-%m-%d")
        for module in (pipeline, pipeline_flirt):
            with self.subTest(pipeline=module.__name__):
                target = Path(tempfile.mkdtemp()) / "post.json"
                with mock.patch.object(module, "POST_JSON", target):
                    module._mark_skipped(today, "nothing worth posting")
                text = target.read_text(encoding="utf-8")
                marker = json.loads(text)
                self.assertEqual(marker["date"], date)
                self.assertEqual(marker["skip"], "nothing worth posting")
                # build.yml's "already built today" guard greps for exactly this.
                self.assertIn(f'"date": "{date}"', text)


class ConceptBank(unittest.TestCase):
    """The tech-metaphor account's runway: every concept usable, unique and reviewable."""

    def setUp(self):
        from src import flirt
        self.flirt = flirt
        self.concepts = flirt.load_concepts()

    def test_the_bank_lasts_months(self):
        self.assertGreaterEqual(len(self.concepts), 200)

    def test_ids_and_terms_are_unique(self):
        ids = [c["id"] for c in self.concepts]
        terms = [c["term"].lower() for c in self.concepts]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertEqual(len(terms), len(set(terms)))

    def test_ids_can_be_ticked_on_the_review_issue(self):
        # The queue reads approvals back only for ids of lowercase letters,
        # digits and hyphens; any other id could never be approved.
        for concept in self.concepts:
            with self.subTest(id=concept["id"]):
                self.assertRegex(concept["id"], r"^[a-z0-9-]+$")

    def test_no_term_trips_the_safety_gate(self):
        # Every line must contain its term, so a term holding a banned word
        # would get every draft rejected. KILL -9 was left out for exactly this.
        for concept in self.concepts:
            with self.subTest(term=concept["term"]):
                self.assertIsNone(self.flirt.BANNED.search(concept["term"]))

    def test_every_concept_has_a_meaning_to_teach(self):
        for concept in self.concepts:
            with self.subTest(id=concept["id"]):
                self.assertGreaterEqual(len(concept["meaning"]), 20)
                self.assertTrue(concept["domain"])

    def test_a_term_cannot_pass_the_personal_line_gate_by_itself(self):
        # Regression: WORKS ON MY MACHINE holds "my", so a dry definition that
        # merely named it passed as a line about a person and reached review.
        for concept in self.concepts:
            line = f"{concept['term']} is a common idea that shows up in real systems and codebases."
            with self.subTest(term=concept["term"]):
                with self.assertRaisesRegex(Rejected, "documentation"):
                    validate(line, concept)

    def test_every_term_is_recognised_however_its_punctuation_is_spaced(self):
        # Regression: a model writing TRY/CATCH for the bank's TRY / CATCH lost
        # the line on every attempt, each one a model call.
        import re
        for concept in self.concepts:
            term = concept["term"]
            spellings = {
                term,
                re.sub(r"\s*([^\w\s])\s*", r"\1", term),
                " ".join(re.sub(r"([^\w\s])", r" \1 ", term).split()),
            }
            for spelling in sorted(spellings):
                line = f"She said I was her {spelling}, and honestly that explains a lot about us."
                with self.subTest(spelling=spelling):
                    self.assertEqual(validate(line, concept), line)


@unittest.skipUnless(shutil.which("pwsh") or shutil.which("powershell"), "PowerShell is not installed")
class DeployAssistant(unittest.TestCase):
    """Regression: the deploy test could post a live card, and a failed test still printed Done.

    Each test runs worker/deploy.ps1 itself against stand-ins for node, npm,
    wrangler and the publisher (tests/deploy_harness.ps1), and judges it by what
    it ran and how it exited -- not by the words in its source.
    """

    PASSING = {
        "mode": "test",
        "posted": False,
        "telegram": "sent",
        "notes": [],
        "channels": {
            "news": {"account": "@dailytechbrief", "tonight": 'shadow mode, would publish "A headline"'},
            "flirt": {"account": "@commitissues", "tonight": "skips (nothing approved)"},
        },
    }

    def deploy(self, *, reply=None, deploy="ok", tests="ok", skip_secrets=False, channels=None):
        """Runs a copy of the script. Returns (exit code, output, commands it ran)."""
        import json
        import re
        import subprocess
        import tempfile

        work = Path(tempfile.mkdtemp())
        for name in ("deploy.ps1", "wrangler.toml", "package.json"):
            shutil.copy(ROOT / "worker" / name, work / name)
        if channels:
            toml = work / "wrangler.toml"
            text, count = re.subn(r'(?m)^CHANNELS\s*=\s*"[^"]*"', f'CHANNELS = "{channels}"',
                                  toml.read_text(encoding="utf-8"))
            self.assertEqual(count, 1)
            toml.write_text(text, encoding="utf-8")
        log = work / "commands.log"
        env = {
            **os.environ,
            "HARNESS_SCRIPT": str(work / "deploy.ps1"),
            "HARNESS_LOG": str(log),
            "HARNESS_ARGS": "-SkipSecrets" if skip_secrets else "none",
            "HARNESS_TESTS": tests,
            "HARNESS_DEPLOY": deploy,
            "HARNESS_RUN": reply if isinstance(reply, str) else json.dumps(reply or self.PASSING),
        }
        shell = shutil.which("pwsh") or shutil.which("powershell")
        done = subprocess.run(
            [shell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(ROOT / "tests" / "deploy_harness.ps1")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180,
        )
        commands = log.read_text(encoding="utf-8", errors="replace").splitlines() if log.exists() else []
        return done.returncode, done.stdout + done.stderr, commands

    def test_a_passing_test_exits_0_and_says_nothing_was_posted(self):
        code, output, commands = self.deploy()
        self.assertEqual(code, 0, output)
        self.assertIn("Test passed. Nothing was posted.", output)
        self.assertIn("GET https://instapost-publisher.example.workers.dev/run", commands)

    def test_every_secret_the_publisher_reads_is_stored(self):
        import re
        worker = (ROOT / "worker" / "src" / "index.js").read_text(encoding="utf-8")
        wrangler = (ROOT / "worker" / "wrangler.toml").read_text(encoding="utf-8")
        vars_block = wrangler.split("[vars]", 1)[1].split("\n[", 1)[0]
        plain = set(re.findall(r"^([A-Z][A-Z0-9_]+)\s*=", vars_block, re.MULTILINE))
        channels = re.search(r'^CHANNELS\s*=\s*"([^"]*)"', wrangler, re.MULTILINE).group(1).split(",")
        needed = set(re.findall(r"env\.([A-Z][A-Z0-9_]+)", worker))
        needed |= {f"IG_USER_ID_{channel.strip().upper()}" for channel in channels}
        needed -= plain

        code, output, commands = self.deploy()
        stored = set(re.findall(r"wrangler secret put (\S+)", "\n".join(commands)))
        self.assertEqual(code, 0, output)
        self.assertTrue(needed)
        self.assertEqual(needed - stored, set())

    def test_skip_secrets_stores_only_the_new_test_key(self):
        import re
        code, output, commands = self.deploy(skip_secrets=True)
        self.assertEqual(code, 0, output)
        self.assertEqual(re.findall(r"wrangler secret put (\S+)", "\n".join(commands)), ["MANUAL_KEY"])

    def test_any_problem_the_test_finds_fails_the_run(self):
        channels = self.PASSING["channels"]
        refused = {"error": "Meta refused the account check: OAuthException 190: Invalid token", "tonight": "held"}
        cases = {
            "an account Meta refuses": {**self.PASSING, "channels": {**channels, "flirt": refused}},
            "an account with no result": {**self.PASSING, "channels": {"news": channels["news"]}},
            "Telegram refusing the message": {**self.PASSING, "telegram": "refused by Telegram (400: Bad Request: chat not found)"},
            "a reply that is not from the test address": {"news": {"published": "17890"}, "flirt": {"skipped": "dry_run"}},
            "a publisher that cannot be reached": "unreachable",
        }
        for name, reply in cases.items():
            with self.subTest(name):
                code, output, _ = self.deploy(reply=reply)
                self.assertEqual(code, 1, output)
                self.assertNotIn("Test passed", output)

    def test_failing_publisher_tests_stop_the_deploy(self):
        code, output, commands = self.deploy(tests="fail")
        self.assertEqual(code, 1, output)
        self.assertEqual([c for c in commands if "wrangler" in c], [])

    def test_a_new_cloudflare_account_gets_to_choose_its_workers_dev_address(self):
        # Regression: captured output cannot answer wrangler's question, so the
        # first deploy on a brand-new account failed outright.
        code, output, commands = self.deploy(deploy="needs-subdomain")
        self.assertEqual(code, 0, output)
        self.assertEqual(commands.count("npx --no wrangler deploy"), 3)

    def test_an_account_added_to_wrangler_toml_is_asked_for_and_tested(self):
        code, output, commands = self.deploy(channels="news,flirt,quotes")
        self.assertIn("npx --no wrangler secret put IG_USER_ID_QUOTES", commands)
        self.assertEqual(code, 1, output)          # the passing reply has no result for it
        self.assertIn("quotes: the publisher returned no result", output)

    def test_wrangler_is_the_pinned_local_copy(self):
        import json
        pinned = json.loads((ROOT / "worker" / "package.json").read_text(encoding="utf-8"))["devDependencies"]["wrangler"]
        self.assertRegex(pinned, r"^\d+\.\d+\.\d+$")              # an exact version, never a range
        lock = json.loads((ROOT / "worker" / "package-lock.json").read_text(encoding="utf-8"))
        self.assertEqual(lock["packages"]["node_modules/wrangler"]["version"], pinned)
        _, _, commands = self.deploy()
        calls = [c for c in commands if "wrangler" in c]
        self.assertTrue(calls)
        for call in calls:
            with self.subTest(call=call):
                self.assertTrue(call.startswith("npx --no wrangler "), call)


TRENDS_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<rss xmlns:ht="https://trends.google.com/trending/rss" version="2.0"><channel>
<item><title>naman dhir</title><ht:approx_traffic>500+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>Naman Dhir picked for the ODI squad</ht:news_item_title></ht:news_item>
  <ht:news_item><ht:news_item_title>second headline</ht:news_item_title></ht:news_item></item>
<item><title>dog</title><ht:approx_traffic>200+</ht:approx_traffic>
  <ht:news_item><ht:news_item_title>Bear mauls couple, with tragic consequences</ht:news_item_title></ht:news_item></item>
</channel></rss>"""


class MemeIdeas(unittest.TestCase):
    """The third segment: every idea a dev joke, two on tech news, one on a trend."""

    DRAKE, BUTTONS, ALWAYS, SKELETON = "181913649", "87743020", "252600902", "4087833"

    def setUp(self):
        from src import memes, trends
        self.memes, self.trends = memes, trends
        self.found = trends.parse_trends(TRENDS_XML, "IN")
        self.tech = [trends.Trend("Postgres 19 ships a new planner", "tech", "Queries plan themselves now."),
                     trends.Trend("GitHub Actions adds ARM runners", "tech", "Cheaper CI minutes."),
                     trends.Trend("Rust 2.0 announced", "tech", "A new edition begins.")]
        self.templates = [trends.Template(self.DRAKE, "Drake Hotline Bling", 2),
                          trends.Template(self.BUTTONS, "Two Buttons", 3),
                          trends.Template(self.ALWAYS, "Always Has Been", 2),
                          trends.Template(self.SKELETON, "Waiting Skeleton", 2)]
        self.pool = memes.usable(self.tech + self.found, set())     # three tech, and naman dhir
        self.cat = memes.catalogue(self.pool)
        self.good = self.idea("G1")

    def idea(self, tid, template=DRAKE):
        boxes = ["Fix the flaky test", "Rerun CI", "Me"] if template == self.BUTTONS else \
                ["Waiting for code review", "Getting picked for the release"]
        return {"trend": tid, "template_id": template, "boxes": boxes,
                "caption": "Could not process it for ten minutes either.", "why": "Selection = merge."}

    def replies(self, *answers):
        """The model's answers in order: a list is a reply of ideas, a str is raw text, None is no reply."""
        import json
        Reply = self.memes.llm.Reply
        queue = iter(Reply(None, "gemini-3.8-flash: HTTP 429") if a is None
                     else Reply(a if isinstance(a, str) else json.dumps({"ideas": a})) for a in answers)
        return mock.patch.object(self.memes.llm, "complete", side_effect=lambda *a, **k: next(queue))

    # --- sources ---

    def test_a_trend_carries_its_first_headline(self):
        self.assertEqual([t.title for t in self.found], ["naman dhir", "dog"])
        self.assertEqual(self.found[0].context, "Naman Dhir picked for the ODI squad")
        self.assertEqual(self.found[0].traffic, "500+")

    def test_a_feed_that_declares_a_dtd_is_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel></channel></rss>'
        with self.assertRaises(ValueError):
            self.trends.parse_trends(bomb, "IN")

    def test_only_two_and_three_box_templates_are_offered(self):
        square = {"width": 1200, "height": 1200}
        payload = {"success": True, "data": {"memes": [
            {"id": 1, "name": "One", "box_count": 1, **square}, {"id": 2, "name": "Two", "box_count": 2, **square},
            {"id": 3, "name": "Three", "box_count": 3, **square}, {"id": 5, "name": "Five", "box_count": 5, **square}]}}
        self.assertEqual([t.name for t in self.trends.parse_templates(payload)], ["Two", "Three"])

    def test_only_templates_instagram_shows_whole_are_offered(self):
        def meme(name, width, height):
            return {"id": name, "name": name, "box_count": 2, "width": width, "height": height}
        payload = {"success": True, "data": {"memes": [
            meme("Drake Hotline Bling", 1200, 1200),
            meme("Two Buttons", 600, 908),               # too tall: Instagram would crop it
            meme("This Is Fine", 580, 282),              # too wide
            meme("Look At Me", 300, 300),                # too small to scale up cleanly
            meme("George Bush 9/11", 1200, 1200),        # not a subject for a meme page
            meme("Hide the Pain Harold", 480, 601),      # 0.799: Instagram's own tolerance lets it through
        ]}}
        self.assertEqual([t.name for t in self.trends.parse_templates(payload)],
                         ["Drake Hotline Bling", "Hide the Pain Harold"])

    def test_sensitive_and_recently_used_trends_are_left_out(self):
        self.assertEqual([t.title for t in self.memes.usable(self.found, set())], ["naman dhir"])
        self.assertEqual(self.memes.usable(self.found, {"naman dhir"}), [])

    def test_a_tech_story_is_screened_on_its_title_not_its_summary(self):
        # Summaries are long, and ordinary tech news mentions hospitals and victims.
        story = self.trends.Trend("Insurers say AI is raising healthcare costs", "tech",
                                  "Driven by hospital use of AI tools and ransomware victims.")
        grim = self.trends.Trend("Robotaxi crash: two killed", "tech", "The company paused its fleet.")
        kept = self.memes.usable([story, grim], set())
        self.assertEqual(kept, [story])

    def test_titles_the_page_cannot_use_are_left_out(self):
        latin = self.trends.latin
        self.assertTrue(latin("naman dhir"))
        self.assertTrue(latin("Café crème brûlée"))
        self.assertFalse(latin("आज का मौसम"))
        self.assertFalse(latin("装在手机上的对话副驾"))
        hindi = self.trends.Trend("आज का मौसम", "IN")
        self.assertNotIn(hindi, self.memes.usable(self.found + [hindi], set()))

    def test_only_todays_tech_stories_count(self):
        import json, tempfile
        from datetime import date
        path = Path(tempfile.mkdtemp()) / "trending.json"
        path.write_text(json.dumps({"date": "2026-09-26", "stories": [{"title": "Old news"}]}), encoding="utf-8")
        with mock.patch.object(self.trends, "TECH_FILE", path):
            self.assertEqual(self.trends.tech_stories(date(2026, 9, 27)), [])
            self.assertEqual(len(self.trends.tech_stories(date(2026, 9, 26))), 1)

    def saved_stories(self, items):
        import tempfile
        from src import pipeline
        path = Path(tempfile.mkdtemp()) / "trending.json"
        ledger = mock.MagicMock(contains=lambda k: False, blocked=lambda t: False,
                                recent_titles=lambda d: [], recent_sources=lambda n: [])
        today = datetime.now(cfg.TZ)
        with mock.patch.object(pipeline, "TRENDING_JSON", path), mock.patch.object(self.trends, "TECH_FILE", path):
            pipeline._save_trending(items, ledger, today)
            return {s.title: s for s in self.trends.tech_stories(today.date())}

    def story(self, title, source="hn", summary="", engagement=300):
        from src.harvest import Item
        return Item(title=title, url=f"https://example.com/{abs(hash(title))}", source=source,
                    publication={"hn": "Hacker News", "rss": "The Verge"}.get(source, source),
                    published=datetime.now(timezone.utc), engagement=engagement, summary=summary)

    def test_the_stories_the_news_build_leaves_are_the_ones_the_memes_read(self):
        stories = self.saved_stories([
            self.story("Postgres 19 ships a new planner",
                       summary="<p>The planner\n now   rewrites joins, <a href='https://x'>caf&#233;</a> style.</p>"),
            self.story("Rust 2.0 announced", summary="Rust 2.0 announced."),
            self.story("Python 4 lands", summary="Python 4 lands, with free threading on by default"),
            # left out: a repo, a paper, a busy post that is not tech, a title the page cannot use
            self.story("jev-ultrafast: fastest LLM web agent", source="github", engagement=900),
            self.story("A new LLM inference benchmark", source="arxiv"),
            self.story("I'm the mom in that viral Giants clip", engagement=900),
            self.story("装在手机上的对话副驾 LLM"),
            self.story("LLM &#x8FD0;&#x884C;&#x65F6;&#x8FD0;&#x884C;&#x65F6;"),   # reads as Latin until decoded
        ])
        self.assertEqual(set(stories), {"Postgres 19 ships a new planner", "Rust 2.0 announced", "Python 4 lands"})
        # HTML and entities are gone, and the cut is not mid-word.
        self.assertEqual(stories["Postgres 19 ships a new planner"].context, "The planner now rewrites joins, café style.")
        # A summary that only repeats its title says nothing: the site name stands in.
        self.assertEqual(stories["Rust 2.0 announced"].context, "Hacker News")
        # One that starts with its title and goes on is kept.
        self.assertEqual(stories["Python 4 lands"].context, "Python 4 lands, with free threading on by default")
        self.assertTrue(all(s.where == "tech" for s in stories.values()))

    def test_a_story_that_only_sounds_technical_is_not_a_tech_hook(self):
        # "enraged" holds "rag", "released" holds "release": substring matching
        # scored this as tech news.
        stories = self.saved_stories([self.story(
            "Pokémon card resellers have turned collecting into an online blood sport", source="rss",
            summary="Fans are enraged as new sets are released and resold within minutes.")])
        self.assertEqual(stories, {})

    def test_the_news_card_never_fails_over_the_meme_stories(self):
        from src import pipeline
        with mock.patch.object(pipeline, "rank", side_effect=RuntimeError("boom")):
            pipeline._save_trending([], None, datetime.now(cfg.TZ))     # logs, does not raise

    # --- the split ---

    def test_two_tech_and_one_trending_by_default(self):
        self.assertEqual(cfg.MEME_MIX, {"tech": 2, "current": 1})
        self.assertEqual(self.memes.quotas(self.pool), {"tech": 2, "current": 1})

    def test_a_short_pool_hands_its_share_to_the_other(self):
        trending_only = self.memes.usable(self.found, set()) + [self.trends.Trend("kl rahul", "IN"),
                                                                self.trends.Trend("padres score", "US")]
        self.assertEqual(self.memes.quotas(trending_only), {"tech": 0, "current": 3})
        self.assertEqual(self.memes.quotas(self.tech), {"tech": 3, "current": 0})
        self.assertEqual(self.memes.quotas(self.tech[:1] + trending_only), {"tech": 1, "current": 2})

    def test_the_split_is_enforced_not_just_asked_for(self):
        answer = [self.idea("T1", self.DRAKE), self.idea("T2", self.ALWAYS),
                  self.idea("T3", self.SKELETON), self.idea("G1", self.BUTTONS)]
        with self.replies(answer):
            ideas, rejects = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual([i["pool"] for i in ideas], ["tech", "tech", "current"])
        self.assertNotIn("Rust 2.0 announced", [i["trend"] for i in ideas])
        self.assertIn("one tech idea more than asked for, kept as a spare", rejects)

    def test_tech_comes_first_whatever_order_the_model_answers_in(self):
        with self.replies([self.idea("G1", self.DRAKE), self.idea("T2", self.ALWAYS), self.idea("T1", self.SKELETON)]):
            ideas, _ = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual([i["pool"] for i in ideas], ["tech", "tech", "current"])

    def test_the_second_try_asks_only_for_what_is_missing(self):
        with self.replies([self.idea("T1", self.DRAKE), self.idea("T2", self.ALWAYS)],
                          [self.idea("G1", self.SKELETON)]) as model:
            ideas, _ = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual(len(ideas), 3)
        second = model.call_args_list[1].args[0]
        self.assertIn("Trending searches today", second)
        self.assertNotIn("Tech news trending today", second)
        self.assertNotIn("Postgres", second)

    def test_a_failed_retry_keeps_the_ideas_already_made(self):
        with self.replies([self.idea("T1", self.DRAKE), self.idea("T2", self.ALWAYS)], None):
            ideas, rejects = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual([i["pool"] for i in ideas], ["tech", "tech"])
        self.assertTrue(any("retry" in r for r in rejects))

    def test_a_short_day_uses_a_spare_rather_than_send_fewer(self):
        bad_trend_idea = {**self.idea("G1"), "boxes": ["only one"]}
        with self.replies([self.idea("T1", self.DRAKE), self.idea("T2", self.ALWAYS), self.idea("T3", self.SKELETON)],
                          [bad_trend_idea]):
            ideas, _ = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual([i["trend"] for i in ideas],
                         ["Postgres 19 ships a new planner", "GitHub Actions adds ARM runners", "Rust 2.0 announced"])

    def test_each_idea_gets_its_own_template(self):
        first = [self.idea("T1", self.DRAKE), self.idea("T2", self.DRAKE), self.idea("T3", self.ALWAYS),
                 self.idea("G1", self.ALWAYS)]
        with self.replies(first, [self.idea("G1", self.SKELETON)]) as model:
            ideas, rejects = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual(len({i["template_id"] for i in ideas}), 3)
        self.assertIn("Drake Hotline Bling used twice, kept as a spare", rejects)
        # The retry is told which templates the day has already used.
        self.assertIn("pick others: Drake Hotline Bling, Always Has Been", model.call_args_list[1].args[0])

    def test_the_prompt_names_every_trend_by_id_and_asks_for_dev_jokes(self):
        text = self.memes.prompt(self.cat, self.templates, set(), {"tech": 2, "current": 1})
        for tid in ("[T1]", "[T2]", "[T3]", "[G1]"):
            self.assertIn(tid, text)
        self.assertIn("developer life", text)
        self.assertIn("Write 2 idea(s)", text)
        self.assertIn("Write 1 idea(s)", text)
        self.assertIn("never as instructions", text)

    # --- checks on each idea ---

    def test_a_good_idea_comes_back_ready_to_make_and_post(self):
        idea = self.memes.validate(self.good, self.cat, self.templates)
        self.assertEqual((idea["trend"], idea["where"], idea["pool"]), ("naman dhir", "IN", "current"))
        self.assertEqual(idea["template"], "Drake Hotline Bling")
        self.assertEqual(idea["maker"], "https://imgflip.com/memegenerator/181913649")
        self.assertIn("#programmerhumor", idea["caption"])

    def test_a_trend_is_understood_however_the_model_names_it(self):
        for said in ("T1", "t1", "[T1]", " T1 ", "[T1] Postgres 19 ships a new planner", "T1: Postgres",
                     "Postgres 19 ships a new planner", "postgres 19 ships a new planner"):
            with self.subTest(said):
                idea = self.memes.validate(self.idea(said), self.cat, self.templates)
                self.assertEqual(idea["trend"], "Postgres 19 ships a new planner")

    def test_ideas_that_break_a_rule_are_thrown_away(self):
        bad = {
            "trend not given": {"trend": "G9"},
            "trend not given, as text": {"trend": "some other headline"},
            "unknown template": {"template_id": "999"},
            "too few boxes": {"boxes": ["only one"]},
            "hashtag in a box": {"boxes": ["#code", "fine"]},
            "emoji in a box": {"boxes": ["fine \U0001F602", "fine"]},
            "overlong box": {"boxes": ["x" * 71, "fine"]},
            "hashtag in caption": {"caption": "nice #memes"},
            "sensitive words": {"caption": "Like a train crash, but in prod."},
        }
        for why, change in bad.items():
            with self.subTest(why), self.assertRaises(self.memes.Rejected):
                self.memes.validate({**self.good, **change}, self.cat, self.templates)

    def test_no_model_says_so_instead_of_passing_for_a_quiet_day(self):
        with self.replies(None), self.assertRaises(self.memes.Unavailable):
            self.memes.draft(self.pool, self.templates, set())

    def test_one_idea_per_trend_and_bad_json_is_survived(self):
        with self.replies("not json", [self.idea("T1", self.DRAKE), self.idea("T1", self.ALWAYS)]):
            ideas, rejects = self.memes.draft(self.pool, self.templates, set())
        self.assertEqual(len(ideas), 1)
        self.assertTrue(any("JSON" in r for r in rejects))
        self.assertIn("second idea on the same trend", rejects)

    # --- the log and the message ---

    def test_the_log_forgets_after_a_month_and_remembers_the_week(self):
        from datetime import date
        old = [{"date": "2026-08-01", "trend": "ancient", "template_id": "1"},
               {"date": "2026-09-25", "trend": "Naman Dhir", "template_id": "181913649"}]
        kept = self.memes.record(old, [], date(2026, 9, 27))
        self.assertEqual([e["trend"] for e in kept], ["Naman Dhir"])
        self.assertEqual(self.memes.recent(kept, "trend", 7, date(2026, 9, 27)), {"naman dhir"})

    def test_everything_in_the_message_is_escaped(self):
        from datetime import date
        from src import notify
        idea = self.memes.validate({**self.good, "boxes": ["<b>x</b> & y", "fine"]}, self.cat, self.templates)
        with mock.patch.object(notify, "_post") as post:
            notify.meme_ideas([idea], self.pool, date(2026, 9, 27), [])
        text = post.call_args_list[1].args[1]["text"]
        self.assertIn("&lt;b&gt;x&lt;/b&gt; &amp; y", text)
        self.assertIn("https://imgflip.com/memegenerator/181913649", text)
        self.assertIn("trending in India", text)
        self.assertEqual(post.call_count, 2)          # the trends, then one per idea

    def test_a_tech_idea_says_it_came_from_the_news(self):
        from datetime import date
        from src import notify
        idea = self.memes.validate(self.idea("T1"), self.cat, self.templates)
        with mock.patch.object(notify, "_post") as post:
            notify.meme_ideas([idea], self.pool, date(2026, 9, 27), [])
        self.assertIn("(tech news)", post.call_args_list[1].args[1]["text"])

    def test_a_model_outage_still_sends_the_trends(self):
        import json, tempfile
        from src import pipeline_memes
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(pipeline_memes.trends, "gather", return_value=(self.pool, self.templates, [])), \
                mock.patch.object(pipeline_memes, "IDEAS_JSON", tmp / "ideas.json"), \
                mock.patch.object(self.memes, "LOG_FILE", tmp / "log.json"), \
                self.replies(None), \
                mock.patch.object(pipeline_memes.notify, "notice") as notice:
            self.assertEqual(pipeline_memes.main(), 0)
        title, text = notice.call_args.args
        self.assertEqual(title, "Meme ideas failed")
        self.assertIn("naman dhir", text)
        self.assertIn("skip", json.loads((tmp / "ideas.json").read_text(encoding="utf-8")))

    def test_a_good_day_end_to_end(self):
        import json, tempfile
        from src import pipeline_memes
        tmp = Path(tempfile.mkdtemp())
        answer = [self.idea("T1", self.DRAKE), self.idea("T2", self.ALWAYS), self.idea("G1", self.SKELETON)]
        with mock.patch.object(pipeline_memes.trends, "gather", return_value=(self.pool, self.templates, [])), \
                mock.patch.object(pipeline_memes, "IDEAS_JSON", tmp / "ideas.json"), \
                mock.patch.object(self.memes, "LOG_FILE", tmp / "log.json"), \
                self.replies(answer), \
                mock.patch.object(pipeline_memes.notify, "_post") as post:
            self.assertEqual(pipeline_memes.main(), 0)
        saved = json.loads((tmp / "ideas.json").read_text(encoding="utf-8"))
        self.assertEqual([i["pool"] for i in saved["ideas"]], ["tech", "tech", "current"])
        logged = json.loads((tmp / "log.json").read_text(encoding="utf-8"))
        self.assertEqual({e["trend"] for e in logged},
                         {"Postgres 19 ships a new planner", "GitHub Actions adds ARM runners", "naman dhir"})
        self.assertEqual(post.call_count, 4)          # the trends, then three ideas


class NicheFit(unittest.TestCase):
    """A niche term has to start a word, on both the positive and negative side."""

    def fit(self, title, summary=""):
        from src.harvest import Item
        return score.niche_fit(Item(title=title, url="https://example.com", source="hn", publication="HN",
                                    published=datetime.now(timezone.utc), summary=summary))

    def test_terms_inside_other_words_do_not_count(self):
        self.assertEqual(self.fit("Fans enraged about storage and relationships", "trust, average, frustrated"), 0.0)

    def test_terms_that_start_a_word_still_count(self):
        self.assertGreater(self.fit("RAG pipelines in Rust"), 0.6)
        self.assertGreater(self.fit("Rust's borrow checker, released"), 0.3)
        self.assertGreater(self.fit("LLMs are getting cheaper"), 0.4)
        self.assertGreater(self.fit("ChatGPT gets a new memory mode"), 0.3)

    def test_a_negative_term_inside_another_word_costs_nothing(self):
        # "elon" in "belong", "ipo" in "tripod" used to pull tech stories down.
        self.assertEqual(self.fit("Where Postgres extensions belong"), self.fit("Where Postgres extensions live"))
        self.assertLess(self.fit("Postgres maker files for an IPO"), self.fit("Postgres maker ships a release"))


def jpeg_bytes(width: int, height: int) -> bytes:
    # SOI, then a baseline frame header: length, precision, height, width, components.
    return (b"\xff\xd8\xff\xc0\x00\x11\x08" + height.to_bytes(2, "big")
            + width.to_bytes(2, "big") + b"\x03" + b"\x00" * 9)


def png_bytes(width: int, height: int) -> bytes:
    return (b"\x89PNG\r\n\x1a\n" + (13).to_bytes(4, "big") + b"IHDR"
            + width.to_bytes(4, "big") + height.to_bytes(4, "big") + b"\x08\x02\x00\x00\x00")


class MemeImages(unittest.TestCase):
    """Finished memes from Imgflip: optional, checked, and never a reason to lose an idea."""

    KEY = "imgflip-secret-key-123"

    def setUp(self):
        from src import imgflip, pipeline_memes
        self.imgflip, self.pipeline = imgflip, pipeline_memes
        self.idea = {"trend": "Postgres 19 ships a new planner", "where": "tech", "pool": "tech", "context": "",
                     "template_id": "181913649", "template": "Drake Hotline Bling",
                     "maker": "https://imgflip.com/memegenerator/181913649",
                     "boxes": ["Reading the query plan", "Adding an index and hoping"],
                     "caption": "Every DBA, eventually.\n\n#programmerhumor #devlife", "why": "We all do it."}

    def ask(self, reply=None, image=None, key=KEY):
        """Run render() against a scripted Imgflip: the API's JSON, then the image bytes."""
        reply = {"success": True, "data": {"url": "https://i.imgflip.com/abc123.jpg"}} if reply is None else reply
        download = mock.MagicMock(status_code=200)
        download.__enter__.return_value = download
        download.iter_content.return_value = [jpeg_bytes(1200, 1200) if image is None else image]
        api = mock.MagicMock()
        api.json.return_value = reply
        self.post = mock.patch.object(self.imgflip.requests, "post", return_value=api).start()
        self.get = mock.patch.object(self.imgflip.requests, "get", return_value=download).start()
        self.addCleanup(mock.patch.stopall)
        with mock.patch.dict(os.environ, {"IMGFLIP_API_KEY": key}):
            return self.imgflip.render(self.idea)

    # --- off unless asked for ---

    def test_without_a_key_nothing_is_drawn_and_nothing_is_asked(self):
        problems = []
        with mock.patch.dict(os.environ, {"IMGFLIP_API_KEY": ""}), \
                mock.patch.object(self.imgflip.requests, "post") as post:
            self.assertFalse(self.imgflip.enabled())
            self.assertEqual(self.pipeline._render([self.idea], problems), {})
        post.assert_not_called()
        self.assertEqual(problems, [])              # not a fault: the feature is simply off

    def test_the_build_gives_the_key_to_the_meme_step_and_no_other(self):
        workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8"))
        holders = [s["name"] for s in workflow["jobs"]["build"]["steps"] if "IMGFLIP_API_KEY" in (s.get("env") or {})]
        self.assertEqual(holders, ["Build meme ideas"])

    # --- the request ---

    def test_the_key_travels_in_a_header_and_nowhere_else(self):
        self.ask()
        url, kwargs = self.post.call_args.args[0], self.post.call_args.kwargs
        self.assertEqual(kwargs["headers"]["Authorization"], f"Bearer {self.KEY}")
        self.assertNotIn(self.KEY, url)
        self.assertNotIn(self.KEY, str(kwargs["data"]))
        self.assertNotIn("params", kwargs)

    def test_two_boxes_are_top_and_bottom_and_three_go_as_boxes(self):
        self.assertEqual(self.imgflip._form(self.idea),
                         {"template_id": "181913649", "text0": "Reading the query plan",
                          "text1": "Adding an index and hoping"})
        three = self.imgflip._form({**self.idea, "boxes": ["a", "b", "c"]})
        self.assertEqual(three, {"template_id": "181913649", "boxes[0][text]": "a",
                                 "boxes[1][text]": "b", "boxes[2][text]": "c"})

    # --- what comes back ---

    def test_a_good_image_comes_back_with_its_type_and_where_imgflip_keeps_it(self):
        self.assertEqual(self.ask(), (jpeg_bytes(1200, 1200), "jpg", "https://i.imgflip.com/abc123.jpg"))
        self.assertEqual(self.ask(image=png_bytes(1024, 1024))[1], "png")

    def test_the_image_is_fetched_without_following_redirects(self):
        self.ask()
        self.assertEqual(self.get.call_args.args[0], "https://i.imgflip.com/abc123.jpg")
        self.assertIs(self.get.call_args.kwargs["allow_redirects"], False)

    def test_an_image_address_anywhere_but_imgflips_host_is_refused(self):
        for url in ("https://evil.example/x.jpg", "http://i.imgflip.com/x.jpg",
                    "https://i.imgflip.com.evil.example/x.jpg", "https://i.imgflip.com@evil.example/x.jpg",
                    "file:///etc/passwd", ""):
            with self.subTest(url), self.assertRaises(self.imgflip.Failed):
                self.ask(reply={"success": True, "data": {"url": url}})
            self.get.assert_not_called()

    def test_what_is_not_a_usable_image_is_refused(self):
        bad = {
            "a web page": b"<html>not found</html>",
            "a shape Instagram would crop": jpeg_bytes(600, 908),
            "a jpeg with no frame": b"\xff\xd8\xff\xe0\x00\x02",
        }
        for why, data in bad.items():
            with self.subTest(why), self.assertRaises(self.imgflip.Failed):
                self.ask(image=data)

    def test_an_oversized_image_stops_downloading(self):
        with mock.patch.object(self.imgflip.preflight, "IMAGE_MAX_BYTES", 10), self.assertRaises(self.imgflip.Failed):
            self.ask()

    def test_a_refusal_carries_imgflips_reason_and_never_the_key(self):
        with self.assertRaises(self.imgflip.Failed) as caught:
            self.ask(reply={"success": False, "error_message": "Invalid API key"})
        self.assertEqual(str(caught.exception), "Invalid API key")
        self.assertTrue(caught.exception.fatal)
        self.assertNotIn(self.KEY, str(caught.exception))
        with self.assertRaises(self.imgflip.Failed) as caught:
            self.ask(reply={"success": False, "error_message": "No texts specified"})
        self.assertFalse(caught.exception.fatal)

    # --- the day ---

    OK = (jpeg_bytes(1200, 1200), "jpg", "https://i.imgflip.com/abc123.jpg")

    def drawn(self, results):
        problems = []
        self.ideas = [dict(self.idea) for _ in range(3)]
        with mock.patch.dict(os.environ, {"IMGFLIP_API_KEY": self.KEY}), \
                mock.patch.object(self.imgflip, "render", side_effect=results) as render:
            images = self.pipeline._render(self.ideas, problems)
        return images, problems, render

    def test_one_idea_that_cannot_be_drawn_does_not_cost_the_others(self):
        images, problems, _ = self.drawn([self.OK, self.imgflip.Failed("template gone"), self.OK])
        self.assertEqual(sorted(images), [1, 3])
        self.assertEqual(problems, ["Imgflip could not draw idea 2 (template gone)"])
        self.assertEqual(images[1].read_bytes(), self.OK[0])
        # What was drawn is on record, with where to look at it; what was not, is not.
        self.assertEqual([i.get("drawn", False) for i in self.ideas], [True, False, True])
        self.assertEqual(self.ideas[0]["image_url"], "https://i.imgflip.com/abc123.jpg")
        self.assertNotIn("image_url", self.ideas[1])

    def test_a_bad_key_is_tried_once_not_three_times(self):
        images, problems, render = self.drawn([self.imgflip.Failed("Invalid API key", fatal=True)])
        self.assertEqual(images, {})
        self.assertEqual(render.call_count, 1)
        self.assertEqual(problems, ["Imgflip drew nothing (Invalid API key)"])

    def test_images_stay_out_of_the_repository(self):
        images, _, _ = self.drawn([self.OK] * 3)
        for path in images.values():
            self.assertNotIn(ROOT, path.resolve().parents)

    # --- what arrives ---

    def sent(self, images, refuse=()):
        from datetime import date
        from src import notify
        ideas = [dict(self.idea), {**self.idea, "template": "Always Has Been"}]
        with mock.patch.object(notify, "_post", side_effect=lambda m, d, f=None: m not in refuse) as post:
            notify.meme_ideas(ideas, [], date(2026, 10, 10), [], images)
        return [(c.args[0], c.args[1]) for c in post.call_args_list]

    def image_file(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "meme-1.jpg"
        path.write_bytes(jpeg_bytes(1200, 1200))
        return path

    def test_a_drawn_meme_arrives_as_a_file_then_its_caption_alone(self):
        calls = self.sent({1: self.image_file()})
        self.assertEqual([m for m, _ in calls], ["sendMessage", "sendDocument", "sendMessage", "sendMessage"])
        _, doc = calls[1]
        self.assertIn("Reading the query plan", doc["caption"])        # the boxes stay, to remake it
        self.assertIn("https://imgflip.com/memegenerator/181913649", doc["caption"])
        self.assertLessEqual(len(doc["caption"]), 1024)
        _, caption = calls[2]
        self.assertEqual(caption["text"], self.idea["caption"])
        self.assertNotIn("parse_mode", caption)                         # what you copy is what Instagram gets
        # The second idea had no image, and arrives as text as before.
        self.assertIn("<pre>", calls[3][1]["text"])
        self.assertIn("Always Has Been", calls[3][1]["text"])

    def test_a_file_telegram_refuses_falls_back_to_the_idea_as_text(self):
        calls = self.sent({1: self.image_file()}, refuse=("sendDocument",))
        self.assertEqual([m for m, _ in calls], ["sendMessage", "sendDocument", "sendMessage", "sendMessage"])
        self.assertIn("<pre>", calls[2][1]["text"])                     # idea 1, as text
        self.assertIn("Reading the query plan", calls[2][1]["text"])

    def test_a_long_idea_still_fits_a_file_caption(self):
        from src import notify
        long_idea = {**self.idea, "trend": "t" * 300, "boxes": ["b" * 70] * 3, "why": "w" * 200}
        with mock.patch.object(notify, "_post", return_value=True) as post:
            notify._send_meme(1, long_idea, self.image_file())
        self.assertLessEqual(len(post.call_args_list[0].args[1]["caption"]), 1024)

    # --- text fit for a picture ---

    def test_box_text_gets_punctuation_a_meme_font_can_draw(self):
        from src import memes, trends
        cat = {"T1": trends.Trend("Docker ships an agent wall", "tech")}
        templates = [trends.Template("181913649", "Drake Hotline Bling", 2)]
        idea = memes.validate({"trend": "T1", "template_id": "181913649",
                               "boxes": ["Docker pre‑installed", "It’s “fine”…  really"],
                               "caption": "A caption.", "why": "w"}, cat, templates)
        self.assertEqual(idea["boxes"], ["Docker pre-installed", "It's \"fine\"... really"])

    def test_png_dimensions_are_read_too(self):
        from src import preflight
        self.assertEqual(preflight.image_size(png_bytes(1024, 768)), (1024, 768))
        self.assertEqual(preflight.image_size(jpeg_bytes(1200, 900)), (1200, 900))


WORKFLOWS = ROOT / ".github" / "workflows"


def workflow(name: str) -> dict:
    return yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))


def steps_of(name: str, job: str) -> dict:
    return {s["name"]: s for s in workflow(name)["jobs"][job]["steps"] if s.get("name")}


class MemesOnTheirOwn(unittest.TestCase):
    """meme-ideas-now: the meme step without the two cards, safe beside the daily build."""

    def test_it_starts_from_the_actions_tab_or_from_a_commit(self):
        triggers = workflow("memes.yml").get("on") or workflow("memes.yml").get(True)   # yaml reads `on` as true
        self.assertIn("workflow_dispatch", triggers)
        self.assertEqual(triggers["push"]["paths"], [".github/run-memes"])
        self.assertTrue((ROOT / ".github" / "run-memes").is_file())

    def test_it_never_runs_at_the_same_time_as_the_daily_build(self):
        # Both commit the meme log and the day's ideas to main.
        self.assertEqual(workflow("memes.yml")["concurrency"]["group"], workflow("build.yml")["concurrency"]["group"])
        self.assertIs(workflow("memes.yml")["concurrency"]["cancel-in-progress"], False)

    def test_it_runs_the_meme_step_exactly_as_the_daily_build_does(self):
        alone, daily = steps_of("memes.yml", "memes")["Build meme ideas"], steps_of("build.yml", "build")["Build meme ideas"]
        self.assertEqual(alone["run"], daily["run"])
        self.assertEqual(alone["env"], daily["env"])

    def test_it_commits_the_ideas_and_nothing_of_the_cards(self):
        commit = steps_of("memes.yml", "memes")["Commit ideas and log"]["run"]
        self.assertIn("git add dist/memes state/meme_log.json", commit)
        self.assertNotIn("git add dist state", commit)

    def test_both_builds_pick_up_main_before_they_push(self):
        # A push that lands mid-run must not cost the run its saved state.
        for name, job, step in (("build.yml", "build", "Commit card and ledger"),
                                ("memes.yml", "memes", "Commit ideas and log")):
            with self.subTest(name):
                run = steps_of(name, job)[step]["run"]
                self.assertLess(run.index("git pull --rebase origin main"), run.index("git push"))

    def test_asking_for_memes_does_not_run_the_test_suite(self):
        triggers = workflow("tests.yml").get("on") or workflow("tests.yml").get(True)
        self.assertIn(".github/run-memes", triggers["push"]["paths-ignore"])


class FreeToRun(unittest.TestCase):
    """Nothing in this project may cost money.

    These hold it to services and models last confirmed free, so that adding
    a paid one, or drifting onto a paid tier, is a failing test before it is
    a bill. Passing them is not a price check: when a list below changes, the
    provider's pricing page is read first.
    """

    # Every host the running code names, and why it costs nothing.
    FREE_SERVICES = {
        "generativelanguage.googleapis.com": "Gemini API free tier; models held to FREE_GEMINI",
        "api.groq.com": "Groq free plan, no card; optional; models held to FREE_GROQ",
        "api.imgflip.com": "top templates and plain captions are free; the paid features are barred below",
        "imgflip.com": "a link to Imgflip's editor",
        "api.telegram.org": "Telegram Bot API",
        "trends.google.com": "public daily RSS feed",
        "hn.algolia.com": "public Hacker News search",
        "lobste.rs": "public JSON",
        "dev.to": "public API",
        "export.arxiv.org": "public API",
        "techcrunch.com": "public RSS",
        "feeds.arstechnica.com": "public RSS",
        "www.theverge.com": "public RSS",
        "api.github.com": "GitHub search with the job's own token",
        "github.com": "the address in the user-agent",
        "raw.githubusercontent.com": "this public repository's own files",
        "fonts.googleapis.com": "Google Fonts",
        "fonts.gstatic.com": "Google Fonts",
        "graph.facebook.com": "Meta's Graph API, for the publisher that is parked and never run",
    }
    # Confirmed "Free of charge" on ai.google.dev/gemini-api/docs/pricing, 10 Oct 2026.
    FREE_GEMINI = {"gemini-3.8-flash", "gemini-3.6-flash", "gemini-3.5-flash-lite"}
    # On the Free Plan table at console.groq.com/docs/rate-limits, 10 Oct 2026.
    FREE_GROQ = {"openai/gpt-oss-120b"}
    # What Imgflip bills for (imgflip.com/api): Premium endpoints and options.
    PAID_IMGFLIP = r"no_watermark|watermark_text|search_memes|caption_gif|automeme|ai_meme|get_meme\b"

    def running_code(self):
        return [*sorted((ROOT / "src").glob("*.py")), *sorted((ROOT / "templates").glob("*.html")),
                *sorted(WORKFLOWS.glob("*.yml"))]

    def test_every_service_the_code_reaches_is_a_known_free_one(self):
        import re
        for path in self.running_code():
            for host in set(re.findall(r"https?://([A-Za-z0-9.-]+)", path.read_text(encoding="utf-8"))):
                with self.subTest(file=path.name, host=host):
                    self.assertIn(host.rstrip("."), self.FREE_SERVICES,
                                  "a new service: confirm it is free, then list it in FREE_SERVICES with the reason")

    def test_only_models_confirmed_free_are_asked(self):
        self.assertLessEqual(set(cfg.GEMINI_MODELS), self.FREE_GEMINI)
        self.assertLessEqual(set(cfg.GROQ_MODELS), self.FREE_GROQ)

    def test_nothing_imgflip_charges_for_is_ever_named(self):
        import re
        for path in sorted((ROOT / "src").glob("*.py")):
            with self.subTest(file=path.name):
                self.assertIsNone(re.search(self.PAID_IMGFLIP, path.read_text(encoding="utf-8")))

    def test_imgflip_is_sent_the_template_and_the_text_and_nothing_else(self):
        from src import imgflip
        for boxes in (["a", "b"], ["a", "b", "c"]):
            fields = set(imgflip._form({"template_id": "1", "boxes": boxes}))
            self.assertTrue(all(f == "template_id" or f in ("text0", "text1") or f.endswith("][text]") for f in fields), fields)
        self.assertTrue(imgflip.API.endswith("/caption_image"))

    def test_every_job_runs_on_the_free_standard_runner(self):
        # Standard runners are free on a public repository; larger ones never are.
        for path in sorted(WORKFLOWS.glob("*.yml")):
            for job, spec in workflow(path.name)["jobs"].items():
                with self.subTest(workflow=path.name, job=job):
                    self.assertEqual(spec["runs-on"], "ubuntu-latest")


if __name__ == "__main__":
    unittest.main()
