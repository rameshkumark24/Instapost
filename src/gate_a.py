"""Gate A assistant: check that Meta will let this project post to both accounts.

Gate A decides whether the project works at all, and doing it by hand means
typing IDs and tokens into curl commands. This asks for the token once -- hidden,
never saved, never printed -- then checks what the publisher needs, in the order
problems usually show up:

  1. the token is valid
  2. it carries the four permissions publishing needs
  3. both Instagram accounts are Professional, linked to a Page, and visible
  4. each account allows content publishing and has quota left
  5. Meta can fetch a real card image and prepare a post for each account

Step 5 stops short of publishing. Meta builds a post container, and an unused
container is discarded after 24 hours, so nothing appears on either profile.
Pass --publish to post each account's pinned intro card for real instead -- the
test post you have to make anyway, made useful. Gate A passes only then: some
refusals come at the publish call itself, and go-live night is too late to meet
them.

Run:  .venv/Scripts/python -m src.gate_a
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import requests

from . import config as cfg
from .token_health import BROKEN, DEFAULT_VERSION, OK, _redact, classify

ROOT = Path(__file__).resolve().parent.parent
GRAPH = "https://graph.facebook.com"

REQUIRED_PERMISSIONS = (
    "instagram_basic",
    "instagram_content_publish",
    "pages_show_list",
    "pages_read_engagement",
)

CHANNEL_LABELS = {"news": "news", "flirt": "tech-metaphor"}


# --- what the answers mean (pure, tested) ------------------------------------


@dataclass(frozen=True)
class Account:
    page: str
    ig_id: str
    username: str


def missing_permissions(payload: dict) -> list[str]:
    granted = {p.get("permission") for p in payload.get("data", []) if p.get("status") == "granted"}
    return [p for p in REQUIRED_PERMISSIONS if p not in granted]


def linked_accounts(payload: dict) -> list[Account]:
    """Instagram accounts reachable through the token's Pages."""
    out = []
    for page in payload.get("data", []):
        ig = page.get("instagram_business_account") or {}
        if ig.get("id"):
            out.append(Account(page=page.get("name", "?"), ig_id=str(ig["id"]), username=ig.get("username", "?")))
    return out


def container_state(payload: dict) -> tuple[str, str]:
    """('ready' | 'waiting' | 'failed', detail) for a post container."""
    code = payload.get("status_code")
    if code == "FINISHED":
        return "ready", "ready to publish"
    if code in ("ERROR", "EXPIRED"):
        return "failed", f"{code}: {payload.get('status') or 'no detail'}"
    return "waiting", str(code or "no status yet")


def assign(accounts: list[Account], handles: dict[str, str]) -> dict[str, Account] | None:
    """Match accounts to channels by handle, when every channel's handle is known."""
    by_handle = {f"@{a.username}".lower(): a for a in accounts}
    picked = {channel: by_handle.get(handle.lower()) for channel, handle in handles.items()}
    if picked and len(picked) == len(cfg.CHANNELS) and all(picked.values()):
        return picked
    return None


def repo_slug() -> str:
    text = (ROOT / "worker" / "wrangler.toml").read_text(encoding="utf-8")
    m = re.search(r'^REPO\s*=\s*"([^"]+)"', text, re.MULTILINE)
    if not m:
        raise RuntimeError("REPO not found in worker/wrangler.toml")
    return m.group(1)


def card_url(channel: str) -> str:
    return f"https://raw.githubusercontent.com/{repo_slug()}/main/brand/{channel}/pinned-post.jpg"


# --- talking to Meta --------------------------------------------------------


class Graph:
    def __init__(self, token: str, version: str = DEFAULT_VERSION):
        self.token = token
        self.version = version

    def call(self, method: str, path: str, **params) -> tuple[str, dict]:
        url = f"{GRAPH}/{self.version}/{path.lstrip('/')}"
        params["access_token"] = self.token
        try:
            if method == "GET":
                r = requests.get(url, params=params, timeout=30)
            else:
                r = requests.post(url, data=params, timeout=60)
        except requests.RequestException as exc:
            # The exception text can contain the URL, and for GET the URL
            # contains the token, so only its type is ever shown.
            return "inconclusive", {"reason": f"network error ({type(exc).__name__})"}
        outcome, payload = classify(r.status_code, r.text)
        if outcome != OK:
            payload = {**payload, "reason": _redact(payload.get("reason", ""), (self.token,))}
        return outcome, payload


class Report:
    def __init__(self) -> None:
        self.failures = 0

    @staticmethod
    def step(number: int, title: str) -> None:
        print(f"\n{number}. {title}")

    @staticmethod
    def ok(message: str) -> None:
        print(f"   [ok]   {message}")

    @staticmethod
    def warn(message: str, fix: str = "") -> None:
        print(f"   [warn] {message}")
        if fix:
            print(f"          -> {fix}")

    def fail(self, message: str, fix: str = "") -> None:
        self.failures += 1
        print(f"   [FAIL] {message}")
        if fix:
            print(f"          -> {fix}")


# --- the checks -------------------------------------------------------------


def run(graph: Graph, report: Report, *, publish: bool, choose, captions: dict[str, str]) -> int:
    report.step(1, "Token")
    outcome, me = graph.call("GET", "me", fields="id,name")
    if outcome != OK:
        if outcome == BROKEN:
            report.fail(f"Meta rejected the token: {me['reason']}",
                        "Generate a new system-user token (go-live step A5) and paste it again.")
        else:
            report.fail(f"could not check the token: {me['reason']}", "Check your connection and run this again.")
        return 1
    report.ok(f"valid, belongs to {me.get('name') or me.get('id', '?')}")

    report.step(2, "Permissions")
    outcome, perms = graph.call("GET", "me/permissions")
    if outcome == OK:
        if missing := missing_permissions(perms):
            report.fail("missing " + ", ".join(missing),
                        "Regenerate the token with all four permissions ticked (go-live step A5).")
        else:
            report.ok("all four publishing permissions are granted")
    else:
        report.warn(f"could not list permissions ({perms['reason']})",
                    "Continuing: the checks below show whether anything is really missing.")

    report.step(3, "Instagram accounts")
    outcome, pages = graph.call("GET", "me/accounts", fields="name,instagram_business_account{id,username}")
    if outcome != OK:
        report.fail(f"could not list Facebook Pages ({pages['reason']})",
                    "The token needs pages_show_list, and both Pages must be assigned to the system user.")
        return _summary(report, {}, published=False)
    accounts = linked_accounts(pages)
    for account in accounts:
        report.ok(f"@{account.username} (Instagram ID {account.ig_id}), linked to Page '{account.page}'")
    if len(accounts) < len(cfg.CHANNELS):
        report.fail(
            f"found {len(accounts)} linked Instagram account(s); this project needs {len(cfg.CHANNELS)}",
            "Each account must be Professional and linked to its own Page, and both Pages "
            "assigned to the system user (go-live steps A1, A2, A5).",
        )
        return _summary(report, {}, published=False)

    handles = {ch: c["handle"] for ch, c in cfg.CHANNELS.items() if not cfg._PLACEHOLDER.fullmatch(c["handle"])}
    mapping = assign(accounts, handles) or choose(accounts)
    for channel, account in mapping.items():
        report.ok(f"{CHANNEL_LABELS.get(channel, channel)} account is @{account.username}")

    report.step(4, "Publishing allowed")
    for channel, account in mapping.items():
        outcome, limit = graph.call("GET", f"{account.ig_id}/content_publishing_limit", fields="quota_usage,config")
        if outcome == OK:
            row = (limit.get("data") or [{}])[0]
            used = row.get("quota_usage", "?")
            total = (row.get("config") or {}).get("quota_total", "?")
            report.ok(f"@{account.username}: publishing enabled, {used} of {total} posts used in the last 24 hours")
        else:
            report.fail(f"@{account.username}: {limit['reason']}",
                        "The token needs instagram_content_publish and the account must be Professional.")

    report.step(5, "Meta prepares a post" + (" and publishes it" if publish else " (nothing is published)"))
    for channel, account in mapping.items():
        _prepare(graph, report, channel, account, publish, captions.get(channel, ""))

    return _summary(report, mapping, published=publish)


def _prepare(graph: Graph, report: Report, channel: str, account: Account, publish: bool, caption: str) -> None:
    image = card_url(channel)
    try:
        status = requests.head(image, timeout=20, allow_redirects=True).status_code
    except requests.RequestException as exc:
        report.fail(f"could not reach the card image ({type(exc).__name__})", "Check your connection and run this again.")
        return
    if status == 429:
        report.warn("GitHub is rate-limiting this computer; Meta fetches the image from its own network, so continuing")
    elif status != 200:
        report.fail(f"card image not reachable (HTTP {status}): {image}", "Push the brand/ folder to GitHub, then run this again.")
        return

    outcome, container = graph.call(
        "POST", f"{account.ig_id}/media",
        image_url=image,
        caption=caption if publish else "Gate A check. Not published.",
    )
    if outcome != OK or "id" not in container:
        report.fail(
            f"@{account.username}: Meta would not prepare a post ({container.get('reason', 'no container returned')})",
            "Usually the account is not an Instagram Tester on the app, or the invite was not accepted (go-live step A4).",
        )
        return

    state, detail = "waiting", "no status yet"
    for _ in range(10):
        outcome, status_payload = graph.call("GET", container["id"], fields="status_code,status")
        if outcome == OK:
            state, detail = container_state(status_payload)
            if state != "waiting":
                break
        time.sleep(3)
    if state != "ready":
        report.fail(f"@{account.username}: the post container is {detail}", "Send this output to Claude.")
        return

    if not publish:
        report.ok(f"@{account.username}: Meta fetched the card and prepared a post; left unpublished, it expires in 24 hours")
        return

    outcome, published = graph.call("POST", f"{account.ig_id}/media_publish", creation_id=container["id"])
    if outcome != OK:
        report.fail(f"@{account.username}: publishing failed ({published['reason']})", "Send this output to Claude.")
        return
    report.ok(f"@{account.username}: published the pinned intro card (media {published.get('id')}); pin it from the post's menu")


def _summary(report: Report, mapping: dict[str, Account], *, published: bool) -> int:
    print("\nResult")
    if report.failures:
        print(f"   Gate A is not passed yet: {report.failures} problem(s) above. Fix them and run this again.")
        return 1
    if not published:
        print("   Every check passed. Gate A is passed only once Meta publishes a post on each account:")
        print("   run this again with --publish to post each account's pinned intro card.")
        return 0
    print("   Gate A passed. Add these as repository secrets (go-live step B4):")
    print("     IG_TOKEN           the token you pasted (never shown here)")
    for channel, account in mapping.items():
        print(f"     IG_USER_ID_{channel.upper():<8}{account.ig_id}")
    print("   Then send Claude the handles: " + ", ".join(
        f"{CHANNEL_LABELS.get(ch, ch)} @{a.username}" for ch, a in mapping.items()))
    return 0


def choose_interactively(accounts: list[Account]) -> dict[str, Account]:
    print("\n   Which account is which?")
    for i, account in enumerate(accounts, 1):
        print(f"     {i}. @{account.username}")
    picked: dict[str, Account] = {}
    for channel in cfg.CHANNELS:
        label = CHANNEL_LABELS.get(channel, channel)
        while True:
            raw = input(f"   Number for the {label} account: ").strip()
            if raw.isdigit() and 1 <= int(raw) <= len(accounts) and accounts[int(raw) - 1] not in picked.values():
                picked[channel] = accounts[int(raw) - 1]
                break
            print("     Enter one of the numbers above, a different one for each account.")
    return picked


def pinned_captions() -> dict[str, str]:
    from .brand import PROFILES
    return {name: profile["bio"] for name, profile in PROFILES.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check that Meta will let this project post to both Instagram accounts.")
    parser.add_argument("--publish", action="store_true", help="publish each account's pinned intro card for real")
    parser.add_argument("--token-env", metavar="NAME", help="read the token from this environment variable instead of asking")
    args = parser.parse_args(argv)

    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass

    if args.token_env:
        token = os.environ.get(args.token_env, "").strip()
    else:
        token = getpass.getpass("Paste your Meta token (hidden, never saved): ").strip()
    if not token:
        print("No token given.")
        return 1

    if args.publish:
        answer = input("This publishes a real post on both accounts. Type PUBLISH to continue: ").strip()
        if answer != "PUBLISH":
            print("Nothing published. Run without --publish to check without posting.")
            return 1

    print(f"Checking against Meta Graph API {DEFAULT_VERSION}")
    return run(Graph(token), Report(), publish=args.publish, choose=choose_interactively, captions=pinned_captions())


if __name__ == "__main__":
    sys.exit(main())
