"""Telegram receipts.

With nobody watching the pipeline, silence on success is indistinguishable from
a pipeline that died three weeks ago. So this reports both outcomes, and the
absence of a message is itself the alarm.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

import requests

from . import config as cfg

log = logging.getLogger(__name__)

API = "https://api.telegram.org/bot{token}/{method}"


def _creds() -> tuple[str, str] | None:
    token, chat = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")
    if not token or not chat:
        log.info("telegram not configured, skipping notification")
        return None
    return token, chat


def _post(method: str, data: dict, files: dict | None = None) -> None:
    creds = _creds()
    if not creds:
        return
    token, chat = creds
    try:
        r = requests.post(
            API.format(token=token, method=method),
            data={"chat_id": chat, **data},
            files=files,
            timeout=cfg.HTTP_TIMEOUT,
        )
        r.raise_for_status()
    except Exception as exc:
        # Never let a failed notification take down the run it was reporting on.
        log.warning("telegram %s failed: %s", method, exc)


def receipt(post: dict, image: Path) -> None:
    mode = "SHADOW - will not publish" if post["dry_run"] else f"publishes {cfg.PUBLISH_AT_LOCAL}"
    caption = (
        f"<b>{_esc(post['headline'])}</b>\n"
        f"<i>{_esc(post['publication'])}</i> · score {post['score']:.3f}"
        f"{' · llm' if post['llm_polished'] else ' · template'}\n\n"
        f"{_esc(post['url'])}\n\n"
        f"<code>{mode}</code>"
    )
    with image.open("rb") as fh:
        _post("sendPhoto", {"caption": caption, "parse_mode": "HTML"}, {"photo": fh})


def failure(stage: str, exc: BaseException) -> None:
    _post(
        "sendMessage",
        {
            "text": (
                f"<b>Instapost failed</b>\n"
                f"stage: <code>{_esc(stage)}</code>\n"
                f"{_esc(type(exc).__name__)}: {_esc(str(exc)[:500])}"
            ),
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        },
    )


def skipped(reason: str) -> None:
    notice("Instapost skipped tonight", reason)


def notice(title: str, text: str) -> None:
    """A titled message.

    "Skipped tonight" is kept for nights with nothing to post. An informational
    update such as "12 new cards drafted" arriving under that title read like a
    failure.
    """
    _post(
        "sendMessage",
        {
            "text": f"<b>{_esc(title)}</b>\n{_esc(text)}",
            "parse_mode": "HTML",
        },
    )


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
