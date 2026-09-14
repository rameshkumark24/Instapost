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
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import notify, preflight, queue
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
        cfg.assert_branding_ready(CHANNEL)

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

        stage = "expire"
        if expired := queue.expire_stale(entries, cfg.FLIRT_PENDING_EXPIRE_DAYS):
            log.info("%d unreviewed drafts expired; their concepts return to the pool", expired)

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
            _mark_skipped(today, reason)
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
        # Checked before anything is committed: a card Meta would refuse is
        # caught at 09:30, with hours to fix it, rather than at 19:45.
        stage = "preflight"
        if problems := preflight.check(post, image):
            raise RuntimeError("Meta would refuse this post: " + "; ".join(problems))

        stage = "stage"
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
    """Draft more only when the stock -- approved plus awaiting review -- runs low.

    Counting approved cards alone meant every nightly run drafted another
    twelve while the first twelve sat unreviewed: the review issue grew by a
    dozen a night, the 64-concept bank would be spent within a week, and each
    run gave ten minutes of a fifteen-minute job to drafting.
    """
    counts = queue.counts(entries)
    stocked = counts.get("approved", 0) + counts.get("pending", 0)
    if stocked >= cfg.FLIRT_REFILL_BELOW:
        return

    log.info("queue stock at %d (approved + awaiting review), drafting more", stocked)
    started = time.monotonic()
    try:
        drafts, rejects = generate_batch(
            queue.used_concept_ids(entries),
            cfg.FLIRT_BATCH_SIZE,
            budget_s=cfg.FLIRT_DRAFT_BUDGET_S,
        )
    except Rejected as exc:
        log.warning("could not draft: %s", exc)
        notify.notice("Drafting failed", f"flirt refill failed: {exc}")
        return
    took = _duration(time.monotonic() - started)

    if not drafts:
        # Say why. An empty queue with no message is how a retired model could
        # go unnoticed: every concept failed and nothing was ever reported.
        notify.notice("Drafting failed", f"flirt drafted nothing in {took}: {rejects[0] if rejects else 'no candidates'}")
        return

    queue.add(entries, drafts)
    try:
        number = queue.publish_issue(entries)
        notify.notice(
            "New cards to review",
            f"{len(drafts)} drafted in {took} ({len(rejects)} concepts gave no usable line). "
            f"Tick the ones worth posting on issue #{number}.",
        )
    except Exception as exc:
        log.warning("could not update review issue: %s", exc)


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _mark_skipped(today: datetime, reason: str) -> None:
    """Record a deliberate skip for today in place of a card.

    Deleting post.json made the publisher report a chosen skip as a failed
    build at 19:45 -- a second, misleading message after the morning's honest
    one. A dated marker says the skip was deliberate; a missing or stale file
    still means the build really did not run.
    """
    POST_JSON.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "channel": "flirt",
        "date": today.strftime("%Y-%m-%d"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skip": reason,
    }
    POST_JSON.write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
