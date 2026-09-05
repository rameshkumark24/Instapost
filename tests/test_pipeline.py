"""Regression tests for the parts that fail expensively.

Nobody watches this pipeline at 19:45, so a silent regression posts publicly
before anyone notices. These tests cover the failures that would actually
reach an audience: the safety gates, the scoring bug that filled the account
with famous repos, the approval queue that must never promote something a
human did not tick, and the escaping on LLM-written text.

Run: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from src import config as cfg
from src import score
from src.enrich import _extract, _safe_url
from src.flirt import Rejected, validate
from src.harvest import Item
from src.render import _highlight


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
    """Regression: 13 of the first 17 picks were famous evergreen repos.

    The cause was using pushed_at as the item date. vscode is pushed hourly,
    so it scored a perfect recency every night and nothing new could win.
    """

    def test_old_repo_scores_zero_recency(self):
        ancient = item(source="github", published=datetime.now(timezone.utc) - timedelta(days=3650))
        self.assertEqual(score.recency(ancient), 0.0)

    def test_new_repo_scores_well(self):
        fresh = item(source="github", published=datetime.now(timezone.utc) - timedelta(days=3))
        self.assertGreater(score.recency(fresh), 0.85)

    def test_github_window_is_wider_than_news(self):
        """A 20-day-old repo is still new; a 20-day-old news story is not."""
        age = datetime.now(timezone.utc) - timedelta(days=20)
        self.assertGreater(score.recency(item(source="github", published=age)), 0.0)
        self.assertEqual(score.recency(item(source="hn", published=age)), 0.0)


class Diversity(unittest.TestCase):
    """No single source may quietly become the whole account."""

    def test_dominant_source_is_penalised(self):
        recent = ["github"] * 9 + ["hn"]
        self.assertLess(score.diversity(item(source="github"), recent), 0.75)

    def test_minority_source_is_untouched(self):
        recent = ["github"] * 9 + ["hn"]
        self.assertEqual(score.diversity(item(source="hn"), recent), 1.0)

    def test_empty_history_is_neutral(self):
        self.assertEqual(score.diversity(item(source="github"), []), 1.0)

    def test_penalty_never_zeroes_a_source(self):
        """A dominant source should be pulled back, not silenced."""
        worst = score.diversity(item(source="github"), ["github"] * 10)
        self.assertGreater(worst, 0.6)


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
        entries = [{"concept": "a", "status": "pending", "text": "x"}]
        self.assertIsNone(self.queue.next_approved(entries))

    def test_issue_body_lists_only_pending(self):
        entries = [
            {"concept": "a", "status": "pending", "text": "line a"},
            {"concept": "b", "status": "posted", "text": "line b"},
        ]
        body = self.queue.render_issue_body(entries)
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
        out = _highlight("She was my PRIMARY KEY today.", ["PRIMARY KEY"])
        self.assertIn("<b>PRIMARY KEY</b>", out)


class Enrichment(unittest.TestCase):
    """The one place we touch an arbitrary third-party URL."""

    def test_private_and_local_addresses_are_refused(self):
        for bad in [
            "http://localhost/x", "http://127.0.0.1/x", "http://192.168.1.5/x",
            "http://10.0.0.1/", "http://172.16.0.1/", "file:///etc/passwd",
            "ftp://example.com/a", "http://169.254.169.254/latest/meta-data/",
        ]:
            with self.subTest(url=bad):
                self.assertFalse(_safe_url(bad))

    def test_public_https_is_allowed(self):
        self.assertTrue(_safe_url("https://arstechnica.com/story"))

    def test_prefers_og_description(self):
        html = (
            '<meta name="description" content="Generic site tagline that is long enough to pass.">'
            '<meta property="og:description" content="The specific summary of this particular article here.">'
        )
        self.assertEqual(_extract(html), "The specific summary of this particular article here.")

    def test_rejects_boilerplate(self):
        self.assertIsNone(_extract('<meta name="description" content="Please enable JavaScript to continue">'))

    def test_returns_none_when_absent(self):
        self.assertIsNone(_extract("<html><head><title>x</title></head></html>"))


class BrandingGuard(unittest.TestCase):
    """A card carrying '@yourhandle' must never reach a live account."""

    def setUp(self):
        self._dry = cfg.DRY_RUN

    def tearDown(self):
        cfg.DRY_RUN = self._dry

    def test_live_build_blocks_placeholder(self):
        cfg.DRY_RUN = False
        with self.assertRaises(RuntimeError):
            cfg.assert_branding_ready({"handle": "@yourhandle"})

    def test_shadow_build_allows_placeholder(self):
        cfg.DRY_RUN = True
        cfg.assert_branding_ready({"handle": "@yourhandle"})

    def test_real_handle_passes_live(self):
        cfg.DRY_RUN = False
        cfg.assert_branding_ready({"handle": "@some_real_account"})


if __name__ == "__main__":
    unittest.main()
