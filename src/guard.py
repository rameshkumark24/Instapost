"""What today's daily build has already made, so a second run makes only the rest.

Two crons fire every day (build.yml), hours late on the free scheduler, and
the first to fire builds. "Today" used to be the date in India, and a backup
that drifted past midnight IST -- four Monday nights between 12 Oct and 16 Nov
2026 -- built the next day's batch at 1 AM. That day's two runs then found it
done and skipped: eleven messages in the night, then nothing until the
evening after.

Both crons are UTC times, and neither has ever drifted out of its own UTC day
(the worst was 10.6 hours late), so the day a run belongs to is its UTC date.
A day whose first run is dropped still gets its batch from the second, late,
instead of losing it.

Each step is judged on its own output. The guard used to read only the news
card's, so a news step that failed on the first run would have had the second
send another tech-metaphor card and three more memes beside the news it
retried. Memes count only when the daily build made them: a batch asked for
by hand (meme-ideas-now) is extra, not the day's.

Standard library only: it runs before anything is installed, so a run with
nothing left to make ends in seconds. An output that cannot be read counts as
not made -- a duplicate is better than a missing day.

Run:  python -m src.guard     (writes news=, flirt=, memes= to GITHUB_OUTPUT)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# build.yml's name, which GitHub hands every step as GITHUB_WORKFLOW.
DAILY_WORKFLOW = "build-nightly-post"

# Where each step leaves its output, and the field that says when it was made.
OUTPUTS = {
    "news": (ROOT / "dist" / "news" / "post.json", "built_at"),
    "flirt": (ROOT / "dist" / "flirt" / "post.json", "built_at"),
    "memes": (ROOT / "dist" / "memes" / "ideas.json", "daily_at"),
}


def made_on(path: Path, field: str) -> date | None:
    """The UTC day an output was made, or None when it is missing or unreadable.

    A file written before the field existed -- the day's memes from the run
    that went out the day this shipped -- has only built_at, which stands in;
    otherwise that evening's backup would send a second batch.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        when = datetime.fromisoformat(data[field] if field in data else data["built_at"])
    except (OSError, ValueError, TypeError, KeyError):
        return None
    if when.tzinfo is None:
        return None         # a stamp with no zone cannot be placed in a day
    return when.astimezone(timezone.utc).date()


def plan(now: datetime, event: str) -> dict[str, bool]:
    """Which steps still have to run today. A run started by hand runs them all."""
    if event == "workflow_dispatch":
        return {step: True for step in OUTPUTS}
    today = now.astimezone(timezone.utc).date()
    return {step: made_on(path, field) != today for step, (path, field) in OUTPUTS.items()}


def main() -> int:
    try:
        todo = plan(datetime.now(timezone.utc), os.environ.get("GITHUB_EVENT_NAME", ""))
    except Exception as exc:
        # Never the reason a day goes missing: when in doubt, make everything.
        print(f"::warning::guard could not read today's outputs ({type(exc).__name__}); building everything")
        todo = {step: True for step in OUTPUTS}
    answers = [f"{step}={'build' if run else 'done'}" for step, run in todo.items()]
    print(" ".join(answers) + ("" if any(todo.values()) else " -- today is already built"))
    if out := os.environ.get("GITHUB_OUTPUT"):
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(answers) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
