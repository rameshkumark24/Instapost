"""The approval queue that sits between the LLM and your public feed.

Candidates are drafted in batches, published to a GitHub Issue as a task list,
and you tick the ones you like from your phone. The nightly job posts the
oldest approved entry and nothing else. That keeps the account genuinely
unattended day to day, while guaranteeing no line reaches Instagram that you
have not read.

A GitHub Issue is the whole review UI: checkboxes are tappable on mobile, the
state lives in the issue body, and it costs nothing to run.

Statuses: pending -> approved -> posted, or pending -> expired
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from . import config as cfg

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
QUEUE = ROOT / "state" / "flirt_queue.json"

API = "https://api.github.com"
MARKER = "<!-- instapost-queue -->"
_ROW = re.compile(r"^- \[(?P<tick>[ xX])\]\s+`(?P<id>[a-z0-9-]+)`", re.MULTILINE)


# --- local queue -----------------------------------------------------------


def load() -> list[dict]:
    if not QUEUE.exists():
        return []
    return json.loads(QUEUE.read_text(encoding="utf-8"))


def save(entries: list[dict]) -> None:
    QUEUE.parent.mkdir(exist_ok=True)
    QUEUE.write_text(json.dumps(entries, indent=1, ensure_ascii=False), encoding="utf-8")


def used_concept_ids(entries: list[dict]) -> set[str]:
    """Concepts already drafted and still live, so none is drafted twice.

    Expired drafts give their concept back: a line nobody reviewed is not a
    verdict on the concept, and the bank holds only 64.
    """
    return {e["concept"] for e in entries if e["status"] != "expired"}


def expire_stale(entries: list[dict], days: float) -> int:
    """Expire drafts that have waited longer than `days` for review.

    Without this, a batch nobody ticks blocks refills for good -- there is no
    other way to say "none of these". Called after approvals are synced, so a
    box ticked on the last day still counts.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    expired = 0
    for e in entries:
        if e["status"] != "pending":
            continue
        try:
            drafted = datetime.fromisoformat(e["drafted_at"])
        except (KeyError, ValueError):
            continue
        if drafted < cutoff:
            e["status"] = "expired"
            expired += 1
    return expired


def counts(entries: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for e in entries:
        out[e["status"]] = out.get(e["status"], 0) + 1
    return out


def add(entries: list[dict], drafts: list[dict]) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for d in drafts:
        entries.append({**d, "status": "pending", "drafted_at": now})
    return entries


def next_approved(entries: list[dict]) -> dict | None:
    """Oldest approved entry, so the queue drains in the order you approved it."""
    approved = [e for e in entries if e["status"] == "approved"]
    return approved[0] if approved else None


def mark_posted(entries: list[dict], concept_id: str) -> None:
    for e in entries:
        if e["concept"] == concept_id and e["status"] == "approved":
            e["status"] = "posted"
            e["posted_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            return


# --- GitHub Issue as the review UI -----------------------------------------


def _gh(method: str, path: str, **kw):
    token = os.environ.get("GH_TOKEN")
    if not token:
        raise RuntimeError("GH_TOKEN not set; cannot reach the review issue")
    r = requests.request(
        method,
        f"{API}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        timeout=20,
        **kw,
    )
    r.raise_for_status()
    return r.json() if r.text else {}


def _repo() -> str:
    repo = os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        raise RuntimeError("GITHUB_REPOSITORY not set")
    return repo


def render_issue_body(entries: list[dict]) -> str:
    """Pending entries as a tappable task list. Ticked == approved."""
    pending = [e for e in entries if e["status"] == "pending"]
    lines = [
        MARKER,
        "### Approve tonight's queue",
        "",
        "Tick the ones worth posting and leave the rest. Unticked cards expire",
        f"after {cfg.FLIRT_PENDING_EXPIRE_DAYS} days and their concepts go back into the pool.",
        "",
        f"_{len(pending)} awaiting review_",
        "",
    ]
    for e in pending:
        lines.append(f"- [ ] `{e['concept']}`")
        lines.append(f"  > {e['text']}")
        lines.append("")
    return "\n".join(lines)


def find_issue() -> dict | None:
    """The newest review issue, open or closed.

    Closing it is a natural way to say "done reviewing", and the ticks on it
    must still count. Reading only open issues cut the queue off from a closed
    one without a word: issue #1 was closed with all twelve cards still on it.
    """
    issues = _gh("GET", f"/repos/{_repo()}/issues?state=all&labels=queue&sort=created&direction=desc&per_page=10")
    for issue in issues:
        if MARKER in (issue.get("body") or ""):
            return issue
    return None


def publish_issue(entries: list[dict]) -> int:
    """Create or update the review issue. Returns its number."""
    body = render_issue_body(entries)
    existing = find_issue()
    if existing:
        # Reopened as well: new cards on a closed issue would never be seen.
        _gh("PATCH", f"/repos/{_repo()}/issues/{existing['number']}", json={"body": body, "state": "open"})
        log.info("updated review issue #%d", existing["number"])
        return existing["number"]

    created = _gh(
        "POST",
        f"/repos/{_repo()}/issues",
        json={"title": "Approve queued cards", "body": body, "labels": ["queue"]},
    )
    log.info("opened review issue #%d", created["number"])
    return created["number"]


def sync_approvals(entries: list[dict]) -> tuple[int, int]:
    """Read ticks back from the issue. Returns (approved, still_pending).

    Only ever promotes pending -> approved. A previously approved or posted
    entry is never demoted by an unticked box, because the box disappears from
    the issue once the entry leaves pending and an absent box must not be read
    as a rejection.
    """
    issue = find_issue()
    if not issue:
        log.info("no review issue yet; nothing to sync")
        return 0, len([e for e in entries if e["status"] == "pending"])

    ticked = {
        m.group("id") for m in _ROW.finditer(issue.get("body") or "")
        if m.group("tick").lower() == "x"
    }

    approved = 0
    for e in entries:
        if e["status"] == "pending" and e["concept"] in ticked:
            e["status"] = "approved"
            e["approved_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            approved += 1

    pending = len([e for e in entries if e["status"] == "pending"])
    log.info("synced approvals: +%d approved, %d still pending", approved, pending)
    return approved, pending
