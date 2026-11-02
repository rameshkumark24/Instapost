"""Draw the memes ourselves: a blank template, our text where the format puts it.

Imgflip's caption API placed boxes by its own defaults, in its own fonts, at
about 500 pixels, with its mark in the corner -- and the model could not see
where any box would land. Drawing here instead means:

  * every box sits where meme_formats says, measured on the template itself;
  * one look throughout: white Anton capitals with a black edge on photos,
    black Archivo on white panels, each shrunk until every word fits;
  * 1080 pixels wide, padded only when Instagram would otherwise crop;
  * the page's own handle in the corner instead of someone else's mark.

The blank template is fetched from Imgflip's image host and nowhere else,
without following redirects, and is checked to be an image before it is used.
A meme that cannot be drawn to standard is refused; its idea still goes out as
text, as before.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from urllib.parse import urlsplit

import requests
from playwright.sync_api import sync_playwright

from . import config as cfg
from . import preflight
from .meme_formats import BY_ID, DARK, OUTLINE, Format
from .render import RenderError, _assert_font_loaded, _env

log = logging.getLogger(__name__)

WIDTH = 1080
IMAGE_HOST = "i.imgflip.com"
QUALITY = 90

# The largest a box's text may grow, and the smallest it may shrink to before
# the meme is refused rather than sent unreadable. Pixels on a 1080-wide image.
MAX_PX = {OUTLINE: 96, DARK: 64}
MIN_PX = {OUTLINE: 28, DARK: 22}

# A webfont loads only once the page uses it, so only the faces a meme uses are
# checked: Archivo always, for the handle; Anton when a box is drawn in outline.
FONT_FOR = {OUTLINE: '400 40px "Anton"', DARK: '700 40px "Archivo"'}
HANDLE_FONT = '700 22px "Archivo"'


class Failed(RuntimeError):
    """This meme could not be drawn to standard."""


def fetch(url: str) -> bytes:
    """A blank template, from Imgflip's image host only."""
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.hostname != IMAGE_HOST:
        raise Failed(f"template address is not on {IMAGE_HOST}")
    try:
        with requests.get(url, timeout=cfg.HTTP_TIMEOUT, stream=True, allow_redirects=False,
                          headers={"User-Agent": cfg.USER_AGENT}) as r:
            if r.status_code != 200:
                raise Failed(f"template download answered HTTP {r.status_code}")
            data = b""
            for chunk in r.iter_content(64 * 1024):
                data += chunk
                if len(data) > preflight.IMAGE_MAX_BYTES:
                    raise Failed("template is larger than any meme needs")
    except requests.RequestException as exc:
        raise Failed(f"template download failed ({type(exc).__name__})") from None
    _kind(data)
    return data


def _kind(data: bytes) -> str:
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    raise Failed("template is not a JPEG or PNG")


def layout(fmt: Format) -> dict[str, int]:
    """The canvas, and where the template sits on it.

    1080 wide. A template Instagram would crop is padded with white instead:
    a tall one gets side margins at 4:5, a wide one top and bottom at 1.91:1.
    """
    ratio = fmt.width / fmt.height
    if ratio < preflight.ASPECT_MIN:
        ch = round(WIDTH / preflight.ASPECT_MIN)
        iw = round(ch * ratio)
        return {"cw": WIDTH, "ch": ch, "iw": iw, "ih": ch, "ix": (WIDTH - iw) // 2, "iy": 0}
    if ratio > preflight.ASPECT_MAX:
        ch = round(WIDTH / preflight.ASPECT_MAX)
        ih = round(WIDTH / ratio)
        return {"cw": WIDTH, "ch": ch, "iw": WIDTH, "ih": ih, "ix": 0, "iy": (ch - ih) // 2}
    ch = round(WIDTH / ratio)
    return {"cw": WIDTH, "ch": ch, "iw": WIDTH, "ih": ch, "ix": 0, "iy": 0}


def page(fmt: Format, texts: list[str], image: bytes, handle: str) -> str:
    """The HTML for one meme. Every word in it is escaped by the template."""
    if len(texts) != len(fmt.boxes):
        raise Failed(f"{fmt.name} takes {len(fmt.boxes)} boxes, got {len(texts)}")
    geo = layout(fmt)
    boxes = []
    for box, text in zip(fmt.boxes, texts):
        box_px = geo["ih"] * box.h / 100
        cap = box.max_px or MAX_PX.get(box.style, MAX_PX[OUTLINE])
        boxes.append({"x": box.x, "y": box.y, "w": box.w, "h": box.h, "style": box.style, "text": text,
                      "max_px": round(min(cap, box_px * (0.7 if box.style == OUTLINE else 0.55)))})
    src = f"data:{_kind(image)};base64,{base64.b64encode(image).decode()}"
    return _env.get_template("meme.html").render(image=src, boxes=boxes, handle=handle,
                                                 corner=handle_corner(fmt), **geo)


# Where the handle fits, as (side, end), in the order they are tried.
CORNERS = (("right", "bottom"), ("left", "bottom"), ("right", "top"), ("left", "top"))
HANDLE_ZONE = (240, 46)     # what the handle needs, in pixels on the canvas


def handle_corner(fmt: Format) -> tuple[str, str]:
    """The first corner no text box reaches into, so the handle never sits on a joke."""
    geo = layout(fmt)
    rects = [(geo["ix"] + geo["iw"] * b.x / 100, geo["iy"] + geo["ih"] * b.y / 100,
              geo["ix"] + geo["iw"] * (b.x + b.w) / 100, geo["iy"] + geo["ih"] * (b.y + b.h) / 100)
             for b in fmt.boxes]
    zw, zh = HANDLE_ZONE
    for side, end in CORNERS:
        x0 = geo["cw"] - zw if side == "right" else 0
        y0 = geo["ch"] - zh if end == "bottom" else 0
        if not any(x0 < r[2] and r[0] < x0 + zw and y0 < r[3] and r[1] < y0 + zh for r in rects):
            return side, end
    return CORNERS[0]


def draw(ideas: list[dict], folder: Path, get=fetch) -> tuple[dict[int, Path], list[str]]:
    """Finished images by idea number, and what could not be drawn and why."""
    images: dict[int, Path] = {}
    problems: list[str] = []
    blanks: dict[str, bytes] = {}
    handle = cfg.CHANNELS["flirt"]["handle"]
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--font-render-hinting=none"])
        try:
            for n, idea in enumerate(ideas, 1):
                fmt = BY_ID.get(str(idea.get("template_id")))
                try:
                    if fmt is None:
                        raise Failed(f"no format {idea.get('template_id')!r}")
                    if fmt.id not in blanks:
                        blanks[fmt.id] = get(fmt.image)
                    path = folder / f"meme-{n}.jpg"
                    sizes = _shoot(browser, page(fmt, list(idea["boxes"]), blanks[fmt.id], handle), fmt, path)
                    images[n] = path
                    log.info("drew idea %d on %s (text %s px)", n, fmt.name, sizes)
                except (Failed, RenderError) as exc:
                    log.warning("idea %d not drawn: %s", n, exc)
                    problems.append(f"could not draw idea {n} ({exc})")
        finally:
            browser.close()
    return images, problems


def _shoot(browser, html: str, fmt: Format, out: Path) -> list[int]:
    geo = layout(fmt)
    tab = browser.new_page(viewport={"width": geo["cw"], "height": geo["ch"]}, device_scale_factor=1)
    try:
        tab.set_content(html, wait_until="networkidle")
        # Asked for by name and waited on: fonts.ready alone can settle before
        # the browser has even started fetching a face, and the meme would ship
        # in a fallback that one run in twenty-three caught.
        needed = sorted({HANDLE_FONT, *(FONT_FOR[b.style] for b in fmt.boxes)})
        tab.evaluate("specs => Promise.all(specs.map(s => document.fonts.load(s)))", needed)
        tab.wait_for_function("document.fonts.ready.then(() => true)")
        _assert_font_loaded(tab, needed)
        tab.evaluate("fit()")
        fit = tab.evaluate("window.__fit")
        for box, got in zip(fmt.boxes, fit):
            if not got["fits"] or got["size"] < MIN_PX.get(box.style, MIN_PX[OUTLINE]):
                raise Failed(f"text for '{box.role}' needs {got['size']}px to fit; too long for the box")
        tab.screenshot(path=str(out), type="jpeg", quality=QUALITY)
    finally:
        tab.close()
    data = out.read_bytes()
    width, height = preflight.image_size(data)
    if (width, height) != (geo["cw"], geo["ch"]) or not preflight.ratio_ok(width, height):
        raise Failed(f"drew {width}x{height}, not the {geo['cw']}x{geo['ch']} planned")
    if len(data) > preflight.IMAGE_MAX_BYTES:
        raise Failed("image is larger than Instagram accepts")
    return [f["size"] for f in fit]
