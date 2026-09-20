"""Daily build for the tech-metaphor card.

Unlike the news pipeline there is no harvesting and no scoring: candidates were
drafted in advance, and this renders the oldest one that has not gone out yet.
Telegram then delivers the card and its caption, and you post it.

You are the editor at the moment you post, which is why nothing here waits to
be approved first.

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
        log.info("flirt build for %s", today.date())
        cfg.assert_branding_ready(CHANNEL)

        stage = "queue"
        entries = queue.load()

        stage = "refill"
        _refill_if_low(entries)
        queue.save(entries)

        stage = "select"
        entry = queue.next_card(entries)
        if entry is None:
            # Only reachable when drafting has failed for days on end; the
            # refill above keeps a fortnight of cards in hand.
            reason = f"no cards left in the queue: {queue.counts(entries)}"
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
        # Instagram's own limits, checked while there is still time to fix the
        # card -- and still true when you post it by hand.
        stage = "preflight"
        if problems := preflight.check(post, image):
            raise RuntimeError("Instagram would refuse this post: " + "; ".join(problems))

        stage = "stage"
        POST_JSON.parent.mkdir(parents=True, exist_ok=True)
        POST_JSON.write_text(json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("staged %s", POST_JSON.relative_to(ROOT))

        # Marked before the card is sent: the build is the only thing that can
        # write to the queue, and the same card arriving twice is worse than
        # one lost.
        stage = "record"
        queue.mark_sent(entries, entry["concept"])
        queue.save(entries)

        stage = "notify"
        if cfg.NOTIFY_ON_SUCCESS:
            notify.handoff(post, image)

        log.info("done: %s", entry["text"][:70])
        return 0

    except Exception as exc:
        log.exception("failed during %s", stage)
        notify.failure(f"flirt/{stage}", exc)
        return 1


def _refill_if_low(entries: list[dict]) -> None:
    """Draft more only when the cards waiting to go out run low.

    One card goes out a day, so a batch of twelve is about a fortnight. Drafting
    every night instead would spend the concept bank in a week and hand ten
    minutes of a fifteen-minute job to the model.
    """
    stocked = queue.waiting(entries)
    if stocked >= cfg.FLIRT_REFILL_BELOW:
        return

    log.info("%d cards waiting, drafting more", stocked)
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
    notify.notice(
        "New cards drafted",
        f"{len(drafts)} drafted in {took} ({len(rejects)} concepts gave no usable line). "
        f"One arrives each day, and you see it before anyone else does.",
    )


def _duration(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}m{secs:02d}s" if minutes else f"{secs}s"


def _mark_skipped(today: datetime, reason: str) -> None:
    """Record a deliberate skip for today in place of a card.

    A dated marker says the skip was deliberate; a missing or stale file still
    means the build really did not run.
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
