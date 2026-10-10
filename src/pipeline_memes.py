"""Daily meme ideas: trends in, three checked ideas out to Telegram.

Runs after the two cards, and never touches Instagram. With an Imgflip key
each idea arrives as a finished image with its caption; without one, or for
an idea Imgflip could not draw, it arrives as text with a link that opens the
template in Imgflip. Either way you post the one you like.

Exit codes: 0 sent, 0 deliberately skipped (and said so), 1 failed.
"""
from __future__ import annotations

import json
import logging
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from . import config as cfg
from . import imgflip, memes, notify, trends

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("memes")

ROOT = Path(__file__).resolve().parent.parent
IDEAS_JSON = ROOT / "dist" / "memes" / "ideas.json"


def main() -> int:
    stage = "startup"
    try:
        today = datetime.now(cfg.TZ).date()
        log.info("meme ideas for %s", today)

        stage = "trends"
        found, templates, problems = trends.gather(today)
        entries = memes.load_log()
        fresh = memes.usable(found, memes.recent(entries, "trend", cfg.MEME_TREND_COOLDOWN_DAYS, today))
        if not fresh or not templates:
            why = "; ".join(problems) or "no trend today was usable: each was off-limits, not in English, or used this week"
            return _skip(today, f"no meme ideas today: {why}")

        stage = "draft"
        avoid = memes.recent(entries, "template_id", cfg.MEME_TEMPLATE_COOLDOWN_DAYS, today)
        try:
            ideas, rejects = memes.draft(fresh, templates, avoid)
        except memes.Unavailable as exc:
            # The trends are still worth having on a day the model is down.
            notify.notice("Meme ideas failed", f"No model answered ({exc}). Trending today: {_titles(fresh)}")
            return _skip(today, f"no model answered: {exc}", notified=True)
        if not ideas:
            notify.notice("Meme ideas failed",
                          f"All {len(rejects)} ideas failed a check, first: {rejects[0] if rejects else '-'}. "
                          f"Trending today: {_titles(fresh)}")
            return _skip(today, "every idea failed a check", notified=True)
        if rejects:
            log.info("%d ideas thrown away: %s", len(rejects), "; ".join(rejects)[:300])

        stage = "render"
        images = _render(ideas, problems)

        stage = "stage"
        IDEAS_JSON.parent.mkdir(parents=True, exist_ok=True)
        IDEAS_JSON.write_text(json.dumps({
            "date": today.isoformat(),
            "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ideas": ideas,
            "trends": [{"title": t.title, "where": t.where} for t in fresh],
            "problems": problems,
        }, indent=2, ensure_ascii=False), encoding="utf-8")
        memes.save_log(memes.record(entries, ideas, today))

        stage = "notify"
        notify.meme_ideas(ideas, fresh, today, problems, images)
        log.info("done: %d ideas, %d drawn", len(ideas), len(images))
        return 0

    except Exception as exc:
        log.exception("failed during %s", stage)
        notify.failure(f"memes/{stage}", exc)
        return 1


def _render(ideas: list[dict], problems: list[str]) -> dict[int, Path]:
    """Finished images by idea number, for the ideas Imgflip could draw.

    Kept out of dist/: the images go to Telegram and nowhere else, and three a
    day committed to git would grow the repo for nothing. Any idea that cannot
    be drawn still goes out as text, and the day's message says why.
    """
    if not imgflip.enabled():
        log.info("no IMGFLIP_API_KEY: ideas go out as text")
        return {}
    folder = Path(tempfile.mkdtemp(prefix="instapost-memes-"))
    images: dict[int, Path] = {}
    for n, idea in enumerate(ideas, 1):
        try:
            data, ext = imgflip.render(idea)
        except imgflip.Failed as exc:
            log.warning("idea %d not drawn: %s", n, exc)
            if exc.fatal:
                problems.append(f"Imgflip drew nothing ({exc})")
                break
            problems.append(f"Imgflip could not draw idea {n} ({exc})")
            continue
        images[n] = folder / f"meme-{n}.{ext}"
        images[n].write_bytes(data)
        idea["drawn"] = True
    return images


def _titles(found: list[trends.Trend], n: int = 8) -> str:
    return ", ".join(t.title for t in found[:n]) or "none"


def _skip(today, reason: str, notified: bool = False) -> int:
    """Say why there are no ideas today, and leave a dated marker saying so."""
    log.warning(reason)
    if not notified:
        notify.notice("Meme ideas skipped", reason)
    IDEAS_JSON.parent.mkdir(parents=True, exist_ok=True)
    IDEAS_JSON.write_text(json.dumps({
        "date": today.isoformat(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skip": reason,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
