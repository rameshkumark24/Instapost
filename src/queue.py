"""The card queue for the tech-metaphor account.

Candidates are drafted in batches and kept here. Each build renders the oldest
one that has not gone out yet, and Telegram delivers it for you to post by
hand. The human gate is the moment you post, so nothing has to be approved in
advance.

Statuses: pending -> sent. "approved" and "posted" are the older names, from
when ticks on a GitHub issue chose the cards, and are read as pending and sent.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "state" / "flirt_queue.json"

# Drafted and still to go out. The second name is the legacy one.
WAITING = ("pending", "approved")


def load() -> list[dict]:
    if not QUEUE.exists():
        return []
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save(entries: list[dict]) -> None:
    QUEUE.parent.mkdir(exist_ok=True)
    QUEUE.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")


def used_concept_ids(entries: list[dict]) -> set[str]:
    """Concepts already drafted and still live, so none is drafted twice.

    An expired draft gives its concept back: a line nobody ever saw is not a
    verdict on the concept.
    """
    return {e["concept"] for e in entries if e["status"] != "expired"}


def counts(entries: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        out[e["status"]] = out.get(e["status"], 0) + 1
    return out


def waiting(entries: list[dict]) -> int:
    """How many drafted cards have yet to go out."""
    return sum(1 for e in entries if e["status"] in WAITING)


def add(entries: list[dict], drafts: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in drafts:
        entries.append({**d, "status": "pending", "drafted_at": now})
    return entries


def next_card(entries: list[dict]) -> dict | None:
    """The oldest card still waiting, so the queue drains in the order drafted."""
    for e in entries:
        if e["status"] in WAITING:
            return e
    return None


def mark_sent(entries: list[dict], concept_id: str) -> None:
    """Recorded when the card is built, not when you post it.

    The build is the only thing that can write to the queue, and sending the
    same card twice is worse than losing one. Set it back to "pending" by hand
    if a card never reached you.
    """
    for e in entries:
        if e["concept"] == concept_id and e["status"] in WAITING:
            e["status"] = "sent"
            e["sent_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return
