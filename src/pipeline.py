"""Nightly build: harvest -> select -> compose -> render -> stage.

Runs on GitHub Actions at 18:30 IST with 75 minutes of slack before the Worker
publishes at 19:45. Nothing here talks to Instagram; this job's only output is
dist/card.jpg and dist/post.json committed to the repo.

Exit codes: 0 built, 0 deliberately skipped, 1 failed.
"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import notify, preflight
from .compose import compose
from .harvest import harvest_all
from .ledger import Ledger
from .render import render
from .score import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-14s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("pipeline")

ROOT = Path(__file__).resolve().parent.parent
CHANNEL = cfg.CHANNELS["news"]
DIST = ROOT / "dist" / CHANNEL["dist"]
POST_JSON = DIST / "post.json"
HOLD_FLAG = ROOT / "state" / "hold.flag"


def main() -> int:
    stage = "startup"
    try:
        today = datetime.now(cfg.TZ)
        log.info("build for %s (dry_run=%s)", today.date(), cfg.DRY_RUN)
        cfg.assert_branding_ready(CHANNEL)

        stage = "harvest"
        items = harvest_all()
        log.info("harvested %d unique candidates", len(items))

        stage = "select"
        ledger = Ledger()
        try:
            winner = select(items, ledger)
        except LookupError as exc:
            # Not an error. Publishing nothing beats publishing filler.
            log.warning("no post tonight: %s", exc)
            notify.skipped(str(exc))
            _mark_skipped(today, str(exc))
            return 0

        stage = "compose"
        post = compose(winner)

        stage = "render"
        image = render(post, DIST / "card.jpg")

        stage = "stage"
        post.update(
            {
                "channel": "news",
                "date": today.strftime("%Y-%m-%d"),
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "image": image.name,
                "hold": HOLD_FLAG.exists(),
                "dry_run": cfg.DRY_RUN,
            }
        )
        # Checked before anything is committed: a card Meta would refuse is
        # caught at 09:30, with hours to fix it, rather than at 19:45.
        stage = "preflight"
        if problems := preflight.check(post, image):
            raise RuntimeError("Meta would refuse this post: " + "; ".join(problems))

        stage = "stage"
        POST_JSON.parent.mkdir(parents=True, exist_ok=True)
        POST_JSON.write_text(json.dumps(post, indent=2, ensure_ascii=False), encoding="utf-8")
        log.info("staged %s", POST_JSON.relative_to(ROOT))

        if post["hold"]:
            log.warning("hold.flag present -- Worker will not publish tonight")

        # Recorded now, not after publishing: the Worker cannot commit to git,
        # and a duplicate post is a worse outcome than a burned story. Clear the
        # entry by hand if a publish fails and you want the topic back.
        stage = "ledger"
        ledger.record(key=post["key"], title=post["title"], url=post["url"], source=post["source"])

        stage = "notify"
        if cfg.NOTIFY_ON_SUCCESS:
            notify.receipt(post, image)

        log.info("done")
        return 0

    except Exception as exc:
        log.exception("failed during %s", stage)
        notify.failure(stage, exc)
        return 1


def _mark_skipped(today: datetime, reason: str) -> None:
    """Record a deliberate skip for today in place of a card.

    Deleting post.json made the publisher report a chosen skip as a failed
    build at 19:45 -- a second, misleading message after the morning's honest
    one. A dated marker says the skip was deliberate; a missing or stale file
    still means the build really did not run.
    """
    POST_JSON.parent.mkdir(parents=True, exist_ok=True)
    marker = {
        "channel": "news",
        "date": today.strftime("%Y-%m-%d"),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skip": reason,
    }
    POST_JSON.write_text(json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
