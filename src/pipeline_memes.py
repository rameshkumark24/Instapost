"""Daily memes: trends in, the editor's best three out to Telegram, drawn.

Runs after the two cards, and never touches Instagram. Each meme arrives as a
finished image with its caption; one that cannot be drawn to standard arrives
as text, with a link that opens the template in Imgflip. You post the ones
you like.

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
from . import meme_render, memes, notify, trends

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
        found, problems = trends.gather(today)
        entries = memes.load_log()
        fresh = memes.usable(found, memes.recent(entries, "trend", cfg.MEME_TREND_COOLDOWN_DAYS, today))
        if not fresh:
            why = "; ".join(problems) or "no trend today was usable: each was off-limits, not in English, or used this week"
            return _skip(today, f"no meme ideas today: {why}")

        stage = "draft"
        formats = memes.offered(memes.recent(entries, "template_id", cfg.MEME_TEMPLATE_COOLDOWN_DAYS, today))
        try:
            ideas, rejects = memes.draft(fresh, formats)
        except memes.Unavailable as exc:
            # The trends are still worth having on a day the model is down.
            notify.notice("Meme ideas failed", f"No model answered ({exc}). Trending today: {_titles(fresh)}")
            return _skip(today, f"no model answered: {exc}", notified=True)
        if not ideas:
            notify.notice("Meme ideas failed",
                          f"All {len(rejects)} ideas failed a check, first: {rejects[0] if rejects else '-'}. "
                          f"Trending today: {_titles(fresh)}")
            return _skip(today, "every idea failed a check", notified=True, rejects=rejects)
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
            "rejects": rejects,
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


def _render(ideas: list[dict], problems: list[str], draw=None) -> dict[int, Path]:
    """Finished images by idea number, for the memes that could be drawn.

    Kept out of dist/: the images go to Telegram and nowhere else, and three a
    day committed to git would grow the repo for nothing. The drawing is
    deterministic, so ideas.json is enough to draw any of them again. A meme
    that cannot be drawn -- or a browser that will not start -- costs the day
    nothing but the picture: the idea still goes out as text, and the day's
    message says why.
    """
    folder = Path(tempfile.mkdtemp(prefix="instapost-memes-"))
    try:
        images, issues = (draw or meme_render.draw)(ideas, folder)
    except Exception as exc:
        log.exception("drawing failed")
        problems.append(f"no meme could be drawn ({type(exc).__name__}: {str(exc)[:100]})")
        return {}
    problems.extend(issues)
    for n in images:
        ideas[n - 1]["drawn"] = True
    return images


def _titles(found: list[trends.Trend], n: int = 8) -> str:
    return ", ".join(t.title for t in found[:n]) or "none"


def _skip(today, reason: str, notified: bool = False, rejects: list[str] | None = None) -> int:
    """Say why there are no ideas today, and leave a dated marker saying so.

    The marker keeps every reason an idea was thrown away. Three days went
    by with "every idea failed a check" and nothing on record to say which.
    """
    log.warning(reason)
    for r in rejects or []:
        log.warning("  thrown away: %s", r)
    if not notified:
        notify.notice("Meme ideas skipped", reason)
    IDEAS_JSON.parent.mkdir(parents=True, exist_ok=True)
    IDEAS_JSON.write_text(json.dumps({
        "date": today.isoformat(),
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "skip": reason,
        "rejects": rejects or [],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
