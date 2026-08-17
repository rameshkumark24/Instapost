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
from . import notify
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
DIST = ROOT / "dist"
POST_JSON = DIST / "post.json"
HOLD_FLAG = ROOT / "state" / "hold.flag"


def main() -> int:
    stage = "startup"
    try:
        today = datetime.now(cfg.TZ)
        log.info("build for %s (dry_run=%s)", today.date(), cfg.DRY_RUN)

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
            _clear_post()
            return 0

        stage = "compose"
        post = compose(winner)

        stage = "render"
        image = render(post)

        stage = "stage"
        post.update(
            {
                "date": today.strftime("%Y-%m-%d"),
                "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "image": image.name,
                "hold": HOLD_FLAG.exists(),
                "dry_run": cfg.DRY_RUN,
            }
        )
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


def _clear_post() -> None:
    """Stale post.json would let the Worker republish yesterday's card."""
    if POST_JSON.exists():
        POST_JSON.unlink()


if __name__ == "__main__":
    sys.exit(main())
