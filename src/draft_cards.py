"""Draft tech-metaphor cards now, outside the daily build.

The daily build already tops the queue up when it runs low. This is for
redrafting on demand -- after the brief changes, say -- without building a
news card and a meme batch to get there. It drafts only if the queue is low,
exactly as the daily build would, and the batch goes through the same editor.

Exit codes: 0 done or nothing needed, 1 failed.
"""
from __future__ import annotations

import logging
import sys

from . import notify, queue
from .pipeline_flirt import _refill_if_low

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)-16s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("draft-cards")


def main() -> int:
    try:
        entries = queue.load()
        before = queue.waiting(entries)
        _refill_if_low(entries)
        queue.save(entries)
        log.info("cards waiting: %d before, %d after", before, queue.waiting(entries))
        return 0
    except Exception as exc:
        log.exception("drafting failed")
        notify.failure("cards/draft", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
