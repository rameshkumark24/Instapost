"""Confirm the publishing token still works, and warn before it stops.

Runs from token-health.yml every ten days. It never tries to renew the token:
doing that from CI means writing an encrypted GitHub secret and a Cloudflare
Worker secret, which is two more long-lived credentials guarding a third. The
system-user token with no expiry (go-live step A6) is the real fix.

Every check lands in one of three outcomes, kept deliberately apart, because a
health check that cries wolf teaches you to ignore it:

  ok            the token works
  broken        Graph says the token is invalid, expired, or lacks permission
  inconclusive  the check could not finish -- a network fault, an outage, or a
                Graph error that says nothing about the token itself

Expiry is only visible through an *app* token (META_APP_ID|META_APP_SECRET).
Meta's debug_token endpoint takes an app token or an app developer's user
token; a system-user token inspecting itself is not a documented case, so
without those two secrets this confirms the token works but cannot see when it
will stop.

Secrets: Graph echoes the offending token back inside some error messages
("Malformed access token EAAB..."). GitHub masks secrets in its own logs, but
nothing masks a Telegram message, so token-shaped strings are scrubbed from
every Graph message *before* it is truncated -- a real token is ~200
characters, and cutting first would leak a fragment no exact match could find
-- and every known secret is scrubbed again at the one place output leaves.
Standard library only.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

GRAPH = "https://graph.facebook.com"
DEFAULT_VERSION = "v26.0"
WARN_DAYS = 14
ATTEMPTS = 3

OK, BROKEN, INCONCLUSIVE = "ok", "broken", "inconclusive"

# Graph error codes about the token itself.
_AUTH_CODES = {102, 190}
# Codes meaning the token is valid but not allowed to do this.
_PERMISSION_CODES = {10} | set(range(200, 300))

# Meta user, page and system-user tokens start "EAA"; app tokens are
# "<app id>|<secret>"; anything else long and opaque is treated as a token too.
_TOKEN_SHAPES = re.compile(
    r"EAA[A-Za-z0-9_-]{8,}"
    r"|\b\d{6,}\|[A-Za-z0-9_-]{8,}"
    r"|[A-Za-z0-9_-]{60,}"
)
_REDACTED = "[redacted]"


def _redact(text: str, secrets: tuple[str, ...] = ()) -> str:
    for secret in secrets:
        if secret and len(secret) >= 8:
            text = text.replace(secret, _REDACTED)
    return _TOKEN_SHAPES.sub(_REDACTED, text)


def _known_secrets() -> tuple[str, ...]:
    env = os.environ.get
    app_id, app_secret = env("META_APP_ID", "").strip(), env("META_APP_SECRET", "").strip()
    return (
        env("IG_TOKEN", "").strip(),
        app_secret,
        f"{app_id}|{app_secret}" if app_id and app_secret else "",
        env("TG_TOKEN", "").strip(),
    )


def classify(status: int | None, body: str | None) -> tuple[str, dict]:
    """Map one Graph response to (outcome, payload). Pure: no I/O."""
    if status is None or body is None:
        return INCONCLUSIVE, {"reason": "no response from the Graph API"}
    try:
        payload = json.loads(body)
    except ValueError:
        return INCONCLUSIVE, {"reason": f"non-JSON response (HTTP {status})"}
    if not isinstance(payload, dict):
        return INCONCLUSIVE, {"reason": "unexpected response shape"}

    error = payload.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        # Redact before truncating: see the module docstring.
        message = _redact(str(error.get("message") or "no detail"))[:200]
        if code in _AUTH_CODES:
            return BROKEN, {"reason": f"token rejected ({code}): {message}"}
        if code in _PERMISSION_CODES:
            return BROKEN, {"reason": f"token lacks permission ({code}): {message}"}
        return INCONCLUSIVE, {"reason": f"Graph error {code}: {message}"}

    if status >= 500:
        return INCONCLUSIVE, {"reason": f"Graph API returned HTTP {status}"}
    if status != 200:
        return INCONCLUSIVE, {"reason": f"unexpected HTTP {status}"}
    return OK, payload


def expiry_days(data: dict, now: float) -> float | None:
    """Days until publishing stops, or None if nothing is counting down.

    Two independent clocks: `expires_at` (the token) and
    `data_access_expires_at` (the app's access to the account's data). Either
    lapsing stops publishing, so the earlier one is what matters. A value of 0
    or no value means that clock is not running.
    """
    stamps = []
    for key in ("expires_at", "data_access_expires_at"):
        try:
            value = int(data.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if value > 0:
            stamps.append(value)
    if not stamps:
        return None
    return (min(stamps) - now) / 86400


def _get(path: str, params: dict) -> tuple[int | None, str | None]:
    url = f"{GRAPH}{path}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Graph reports token problems as 4xx with a JSON body worth reading.
        try:
            return exc.code, exc.read().decode("utf-8", "replace")
        except Exception:
            return exc.code, None
    except (urllib.error.URLError, TimeoutError, OSError):
        return None, None


def probe(path: str, params: dict) -> tuple[str, dict]:
    """classify(), retried -- but only while the answer is inconclusive."""
    outcome, payload = INCONCLUSIVE, {"reason": "not attempted"}
    for attempt in range(ATTEMPTS):
        outcome, payload = classify(*_get(path, params))
        if outcome != INCONCLUSIVE:
            break
        if attempt < ATTEMPTS - 1:
            time.sleep(5 * (attempt + 1))
    return outcome, payload


def _report(message: str, status: int) -> int:
    # The single exit for everything this module says. Scrub first, then send
    # the alert before anything local that could fail: printing first meant a
    # console unable to encode the emoji crashed the run and lost the alert.
    message = _redact(message, _known_secrets())

    tg_token = os.environ.get("TG_TOKEN", "").strip()
    chat = os.environ.get("TG_CHAT", "").strip()
    if tg_token and chat:
        data = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode()
        try:
            urllib.request.urlopen(
                f"https://api.telegram.org/bot{tg_token}/sendMessage", data=data, timeout=15
            ).close()
        except Exception:
            # No traceback: its frames would include the bot URL.
            print("::warning::could not deliver the Telegram alert")
    _print(message)
    return status


def _print(message: str) -> None:
    """print(), degrading unencodable characters instead of raising."""
    try:
        print(message)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(message.encode(encoding, "replace").decode(encoding))


def main() -> int:
    token = os.environ.get("IG_TOKEN", "").strip()
    version = os.environ.get("GRAPH_VERSION", "").strip() or DEFAULT_VERSION
    if not token:
        return _report("🔑 Token health: the IG_TOKEN secret is not set, so nothing was checked.", 1)

    outcome, payload = probe(f"/{version}/me", {"fields": "id", "access_token": token})
    if outcome == BROKEN:
        return _report(
            f"🔑 Instagram token is BROKEN — {payload['reason']}. "
            f"Neither account can post until it is re-issued.",
            1,
        )
    if outcome == INCONCLUSIVE:
        return _report(
            f"🔑 Token health check could not complete — {payload['reason']}. "
            f"This is not evidence the token is bad; the check runs again in ten days.",
            1,
        )

    lines = ["🔑 Instagram token works."]
    status = 0

    for label in ("NEWS", "FLIRT"):
        ig_id = os.environ.get(f"IG_USER_ID_{label}", "").strip()
        if not ig_id:
            continue
        outcome, payload = probe(f"/{version}/{ig_id}", {"fields": "username", "access_token": token})
        if outcome == OK:
            lines.append(f"  {label.lower()}: reaches @{payload.get('username', '?')}")
        elif outcome == BROKEN:
            lines.append(f"  {label.lower()}: CANNOT reach the account — {payload['reason']}")
            status = 1
        else:
            lines.append(f"  {label.lower()}: not checked — {payload['reason']}")

    app_id = os.environ.get("META_APP_ID", "").strip()
    app_secret = os.environ.get("META_APP_SECRET", "").strip()
    if not (app_id and app_secret):
        lines.append("  expiry: not visible — add META_APP_ID and META_APP_SECRET to monitor it")
    else:
        outcome, payload = probe(
            f"/{version}/debug_token",
            {"input_token": token, "access_token": f"{app_id}|{app_secret}"},
        )
        data = payload.get("data") if outcome == OK else None
        if not isinstance(data, dict):
            # A failure here is about the app credentials, not the IG token.
            lines.append(
                f"  expiry: could not read — {payload.get('reason', 'no data')}. "
                f"Check META_APP_ID and META_APP_SECRET."
            )
        elif data.get("is_valid") is False:
            lines.append("  expiry: Meta reports the token as INVALID")
            status = 1
        else:
            days = expiry_days(data, time.time())
            if days is None:
                lines.append("  expiry: never")
            elif days < WARN_DAYS:
                lines.append(
                    f"  expiry: {max(days, 0):.0f} days — re-issue it now; "
                    f"once it lapses it cannot be recovered"
                )
                status = 1
            else:
                lines.append(f"  expiry: {days:.0f} days")

    return _report("\n".join(lines), status)


if __name__ == "__main__":
    sys.exit(main())
