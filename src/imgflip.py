"""Finished memes, drawn by Imgflip.

Optional. With IMGFLIP_API_KEY set, each checked idea is drawn on its own
template by Imgflip's caption API -- the same text positions its editor uses --
and arrives as an image ready to post. Without the key, or when a call fails,
the idea goes out as text exactly as before: a finished image is a
convenience, never a reason to lose the idea.

What comes back is not trusted any more than what the model writes:

  * the key travels in a header, never a URL or a log line;
  * the image is fetched only from Imgflip's own image host, without
    following redirects, and only up to Instagram's size limit;
  * it has to be a JPEG or PNG in a shape Instagram shows whole;
  * it is downloaded at once. Imgflip deletes images few people view, so the
    URL is never relied on afterwards.

Free accounts cannot remove the small "imgflip.com" mark; that is Imgflip's
price for the service.
"""
from __future__ import annotations

import logging
import os
from urllib.parse import urlsplit

import requests

from . import config as cfg
from . import preflight

log = logging.getLogger(__name__)

API = "https://api.imgflip.com/caption_image"
IMAGE_HOST = "i.imgflip.com"


class Failed(RuntimeError):
    """This idea could not be drawn. `fatal` means no other idea will be either."""

    def __init__(self, reason: str, fatal: bool = False):
        super().__init__(reason)
        self.fatal = fatal


def _key() -> str:
    return os.environ.get("IMGFLIP_API_KEY", "").strip()


def enabled() -> bool:
    return bool(_key())


def _form(idea: dict) -> dict[str, str]:
    """The request Imgflip's docs describe: two boxes are top and bottom text,
    which keeps each template's own capitals and placement; more go as boxes[],
    with no coordinates, so the editor's defaults apply."""
    boxes = idea["boxes"]
    form = {"template_id": idea["template_id"]}
    if len(boxes) == 2:
        form.update(text0=boxes[0], text1=boxes[1])
    else:
        form.update({f"boxes[{i}][text]": b for i, b in enumerate(boxes)})
    return form


def render(idea: dict) -> tuple[bytes, str]:
    """(image bytes, "jpg" or "png") for a checked idea, or Failed with the reason."""
    try:
        r = requests.post(
            API,
            headers={"Authorization": f"Bearer {_key()}", "User-Agent": cfg.USER_AGENT},
            data=_form(idea),
            timeout=cfg.HTTP_TIMEOUT,
        )
        reply = r.json()
    except requests.RequestException as exc:
        raise Failed(f"Imgflip unreachable ({type(exc).__name__})") from None
    except ValueError:
        raise Failed("Imgflip's reply was not JSON") from None

    if not isinstance(reply, dict):
        raise Failed("Imgflip's reply was not what its docs describe")
    if not reply.get("success"):
        # Imgflip answers HTTP 200 with a message, such as "Invalid API key".
        # A key problem will not get better on the next idea.
        message = str(reply.get("error_message") or "no reason given")[:120]
        raise Failed(message, fatal="key" in message.lower() or "auth" in message.lower())

    url = str((reply.get("data") or {}).get("url", ""))
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != IMAGE_HOST:
        raise Failed("Imgflip pointed somewhere that is not its image host")

    data = _download(url)
    if data[:2] == b"\xff\xd8":
        ext = "jpg"
    elif data[:8] == b"\x89PNG\r\n\x1a\n":
        ext = "png"
    else:
        raise Failed("what came back is not a JPEG or PNG")
    try:
        width, height = preflight.image_size(data)
    except ValueError as exc:
        raise Failed(f"image cannot be read ({exc})") from None
    if not preflight.ratio_ok(width, height):
        raise Failed(f"image is {width}x{height}, a shape Instagram would crop")
    return data, ext


def _download(url: str) -> bytes:
    try:
        with requests.get(url, timeout=cfg.HTTP_TIMEOUT, stream=True, allow_redirects=False,
                          headers={"User-Agent": cfg.USER_AGENT}) as r:
            if r.status_code != 200:
                raise Failed(f"image download answered HTTP {r.status_code}")
            data = b""
            for chunk in r.iter_content(64 * 1024):
                data += chunk
                if len(data) > preflight.IMAGE_MAX_BYTES:
                    raise Failed("image is larger than Instagram accepts")
            return data
    except requests.RequestException as exc:
        raise Failed(f"image download failed ({type(exc).__name__})") from None
