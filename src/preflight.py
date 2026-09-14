"""Check a staged post against Meta's publishing limits before it is committed.

Without this, the first live night would be the first time Meta sees our
output. These are the limits Meta documents for single-image posts through the
Instagram content publishing API. Checking them when the card is built turns a
19:45 rejection into a 09:30 alert, with hours left to fix it.

Width is deliberately not checked: Meta scales images outside 320-1440px
rather than refusing them.
"""
from __future__ import annotations

import re
from pathlib import Path

CAPTION_MAX = 2200
HASHTAGS_MAX = 30
MENTIONS_MAX = 20
IMAGE_MAX_BYTES = 8 * 1024 * 1024
ASPECT_MIN = 4 / 5      # the tallest shape allowed, width / height
ASPECT_MAX = 1.91       # the widest

_HASHTAG = re.compile(r"(?<![\w&])#\w+")
_MENTION = re.compile(r"(?<![\w.])@[A-Za-z0-9._]+")

# Frame headers that carry the image size: every SOFn marker except DHT (C4),
# JPG (C8) and DAC (CC), which share the range but mean something else.
_SOF_MARKERS = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}


def jpeg_size(data: bytes) -> tuple[int, int]:
    """(width, height) read from a JPEG's frame header, with no imaging library."""
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i = 2
    while i + 9 <= len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:                                  # padding between markers
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:  # markers with no length
            i += 2
            continue
        length = int.from_bytes(data[i + 2:i + 4], "big")
        if marker in _SOF_MARKERS:
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            return width, height
        i += 2 + length
    raise ValueError("no frame header found")


def check(post: dict, image: Path) -> list[str]:
    """Everything Meta would refuse about this post. Empty means it can go."""
    problems: list[str] = []

    caption = post.get("caption") or ""
    if len(caption) > CAPTION_MAX:
        problems.append(f"caption is {len(caption)} characters, over Meta's {CAPTION_MAX}")
    if (hashtags := len(_HASHTAG.findall(caption))) > HASHTAGS_MAX:
        problems.append(f"caption has {hashtags} hashtags, over Meta's {HASHTAGS_MAX}")
    if (mentions := len(_MENTION.findall(caption))) > MENTIONS_MAX:
        problems.append(f"caption has {mentions} mentions, over Meta's {MENTIONS_MAX}")

    data = Path(image).read_bytes()
    if len(data) > IMAGE_MAX_BYTES:
        problems.append(f"image is {len(data) / 1e6:.1f} MB, over Meta's 8 MB")
    try:
        width, height = jpeg_size(data)
    except ValueError as exc:
        problems.append(f"image cannot be used ({exc}); Meta accepts JPEG only")
        return problems

    ratio = width / height
    if not ASPECT_MIN - 0.005 <= ratio <= ASPECT_MAX + 0.005:
        problems.append(f"image is {width}x{height} (ratio {ratio:.2f}); Meta accepts 0.80 to 1.91")
    return problems
