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


def handoff(post: dict, image: Path) -> None:
    """Today's card, ready to post by hand: the file first, then its caption alone.

    The card goes as a document, not a photo: Telegram re-compresses photos, and
    Instagram compresses again on the way in. The caption is a message of its
    own so that one long-press copies exactly what Instagram needs, with no
    headline or score mixed into it.
    """
    handle = cfg.CHANNELS.get(post.get("channel", ""), {}).get("handle", "")
    lines = [f"<b>{_esc(post['headline'])}</b>"]
    if post.get("publication"):
        lines.append(f"<i>{_esc(post['publication'])}</i> · score {post.get('score', 0):.3f}"
                     f"{' · llm' if post.get('llm_polished') else ' · template'}")
    if post.get("url"):
        lines.append(_esc(post["url"]))
    lines.append(f"<code>Post on {_esc(handle)} at {cfg.PUBLISH_AT_LOCAL}. Caption below, hold to copy.</code>")

    with image.open("rb") as fh:
        _post("sendDocument", {"caption": "\n\n".join(lines), "parse_mode": "HTML"}, {"document": fh})
    # No parse_mode: what arrives is exactly what goes into Instagram.
    _post("sendMessage", {"text": post["caption"], "disable_web_page_preview": "true"})


_WHERE = {"IN": "India", "US": "US", "tech": "Tech"}


def meme_ideas(ideas: list[dict], trends: list, day, problems: list[str]) -> None:
    """Today's trends in one message, then one message per idea.

    Everything in these messages came from a trend feed or a model, so all of
    it is escaped. Box text is in <code>, which Telegram copies on a tap; the
    caption is a <pre> block, which gets a copy button. The Imgflip link is
    left to preview, so the template's picture shows under the idea.
    """
    lines = [f"<b>Meme ideas · {day:%a %d %b}</b>"]
    for where, label in _WHERE.items():
        titles = [_short(t.title) for t in trends if t.where == where][: 4 if where == "tech" else 6]
        if titles:
            lines.append(f"<i>{label}:</i> {_esc(' · '.join(titles))}")
    if problems:
        lines.append(f"<i>Missing today:</i> {_esc('; '.join(problems))}")
    _post("sendMessage", {"text": "\n".join(lines), "parse_mode": "HTML", "disable_web_page_preview": "true"})

    for n, idea in enumerate(ideas, 1):
        parts = [
            f"<b>{n}. {_esc(idea['template'])}</b>",
            f"on <i>{_esc(idea['trend'])}</i> "
            f"({'tech news' if idea['where'] == 'tech' else 'trending in ' + _esc(_WHERE.get(idea['where'], idea['where']))})",
            "",
            *(f"Box {i}: <code>{_esc(b)}</code>" for i, b in enumerate(idea["boxes"], 1)),
        ]
        if idea.get("why"):
            parts += ["", f"<i>{_esc(idea['why'])}</i>"]
        parts += ["", "Caption:", f"<pre>{_esc(idea['caption'])}</pre>", f"Make it: {_esc(idea['maker'])}"]
        _post("sendMessage", {"text": "\n".join(parts), "parse_mode": "HTML"})


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


def _short(text: str, limit: int = 48) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
