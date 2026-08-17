"""Durable memory of what we've already posted.

Committed to git, so history and rollback come free and no database is needed.
At one post a day this file stays trivially small for years.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

log = logging.getLogger(__name__)

STATE = Path(__file__).resolve().parent.parent / "state"
LEDGER_PATH = STATE / "ledger.json"
BLOCKLIST_PATH = STATE / "blocklist.txt"


class Ledger:
    def __init__(self) -> None:
        STATE.mkdir(exist_ok=True)
        self.entries: list[dict] = []
        if LEDGER_PATH.exists():
            self.entries = json.loads(LEDGER_PATH.read_text(encoding="utf-8"))
        self._keys = {e["key"] for e in self.entries}

        self.blocklist: list[str] = []
        if BLOCKLIST_PATH.exists():
            self.blocklist = [
                line.strip().lower()
                for line in BLOCKLIST_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            ]

    def contains(self, key: str) -> bool:
        return key in self._keys

    def blocked(self, title: str) -> bool:
        low = title.lower()
        return any(term in low for term in self.blocklist)

    def recent_titles(self, days: int) -> list[str]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        out = []
        for e in self.entries:
            try:
                when = datetime.fromisoformat(e["posted_at"])
            except (KeyError, ValueError):
                continue
            if when >= cutoff:
                out.append(e.get("title", ""))
        return out

    def record(self, *, key: str, title: str, url: str, source: str) -> None:
        self.entries.append(
            {
                "key": key,
                "title": title,
                "url": url,
                "source": source,
                "posted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
        )
        self._keys.add(key)
        self.save()

    def save(self) -> None:
        LEDGER_PATH.write_text(
            json.dumps(self.entries, indent=1, ensure_ascii=False), encoding="utf-8"
        )
        log.info("ledger: %d entries", len(self.entries))
