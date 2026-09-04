"""Nightly build for the tech-metaphor account.

Unlike the news pipeline there is no harvesting and no scoring: the editorial
decision was made when you ticked the box. This job only syncs your approvals,
tops the queue up when it runs low, and renders the oldest approved card.

That is deliberate. Nothing an LLM writes reaches Instagram on the same night
it was written -- the queue is the gap in which a human looked at it.

Exit codes: 0 built, 0 deliberately skipped, 1 failed.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import notify, queue
from .flirt import Rejected, caption, generate_batch
from .render import render_quote

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("flirt")

ROOT = Path(__file__).resolve().parent.parent
CHANNEL = cfg.CHANNELS["flirt"]
DIST = ROOT / "dist" / CHANNEL["dist"]
POST_JSON = DIST / "post.json"
HOLD_FLAG = ROOT / "state" / "hold.flag"


def main() -> int:
    stage = "startup"
    try:
        today = datetime.now(cfg.TZ)
        log.info("flirt build for %s (dry_run=%s)", today.date(), cfg.DRY_RUN)

        stage = "queue"
        entries = queue.load()

        # Approvals first: a box ticked this morning should be postable tonight.
        stage = "sync approvals"
        try:
            queue.sync_approvals(entries)
        except Exception as exc:
            # A GitHub API blip must not stop us posting something already
            # approved and sitting in the queue.
            log.warning("approval sync failed, continuing on local state: %s", exc)

        stage = "refill"
        _refill_if_low(entries)
        queue.save(entries)

        stage = "select"
        entry = queue.next_approved(entries)
        if entry is None:
            counts = queue.counts(entries)
            reason = (
                f"nothing approved to post. queue: {counts}. "
                f"Tick some boxes on the review issue."
            )
            log.warning(reason)
            notify.skipped(reason)
            _clear_post()
            return 0

        stage = "render"
        image = render_quote(entry, CHANNEL)

        stage = "stage"
        post = {
            "channel": "flirt",
            "concept": entry["concept"],
            "term": entry["term"],
            "headline": entry["text"],
            "caption": caption(entry),
            "date": today.strftime("%Y-%m-%d"),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "image": image.name,
            "hold": HOLD_FLAG.exists(),
            "dry_run": cfg.DRY_RUN,
        }
        POST_JSON.parent.mkdir(parents=True, exist_ok=True)
        POST_JSON.write_text(json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("staged %s", POST_JSON.relative_to(ROOT))

        # Marked posted at build time for the same reason the news ledger is:
        # the Worker cannot write back to git, and repeating a card is worse
        # than losing one. Flip it back to "approved" by hand if a publish fails.
        stage = "record"
        queue.mark_posted(entries, entry["concept"])
        queue.save(entries)

        stage = "notify"
        if cfg.NOTIFY_ON_SUCCESS:
            notify.receipt(post, image)

        log.info("done: %s", entry["text"][:70])
        return 0

    except Exception as exc:
        log.exception("failed during %s", stage)
        notify.failure(f"flirt/{stage}", exc)
        return 1


def _refill_if_low(entries: list[dict]) -> None:
    """Draft a new batch when the approved queue is running down."""
    approved = queue.counts(entries).get("approved", 0)
    if approved >= cfg.FLIRT_REFILL_BELOW:
        return

    log.info("approved queue at %d, drafting a new batch", approved)
    try:
        drafts, rejects = generate_batch(
            queue.used_concept_ids(entries), cfg.FLIRT_BATCH_SIZE
        )
    except Rejected as exc:
        log.warning("could not draft: %s", exc)
        notify.skipped(f"flirt refill failed: {exc}")
        return

    if not drafts:
        return

    queue.add(entries, drafts)
    try:
        number = queue.publish_issue(entries)
        notify.skipped(
            f"{len(drafts)} new cards drafted ({len(rejects)} rejected by gates). "
            f"Review issue #{number}."
        )
    except Exception as exc:
        log.warning("could not update review issue: %s", exc)


def _clear_post() -> None:
    """Stale post.json would let the Worker republish an old card."""
    if POST_JSON.exists():
        POST_JSON.unlink()


if __name__ == "__main__":
    sys.exit(main())
