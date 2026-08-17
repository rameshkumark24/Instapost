"""Render the post onto the card and screenshot it.

This is the Canva substitution. You design the card once in Canva, then this
reproduces it every night: real CSS, real webfonts, byte-reproducible output,
no API tier gate and no vendor in the hot path.

Every assertion here exists because a silently broken card is worse than no
post at all -- with nobody watching at 19:45, the render must refuse rather
than degrade.
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.sync_api import sync_playwright

from . import config as cfg

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "templates"
DIST = ROOT / "dist"

MIN_HEADLINE_PX = 46        # below this the card stops looking designed
MIN_BYTES = 20_000          # a blank/failed render is tiny
MAX_BYTES = 8 * 1024 * 1024  # Instagram's ceiling

_env = Environment(
    loader=FileSystemLoader(TEMPLATES),
    autoescape=select_autoescape(["html"]),
)


class RenderError(RuntimeError):
    """Raised when the card cannot be produced to publishable quality."""


def render(post: dict, out: Path | None = None) -> Path:
    out = out or DIST / "card.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    _assert_within_contract(post)

    html = _env.get_template("card.html").render(
        headline=post["headline"],
        body=post["body"],
        publication=post["publication"],
        date=datetime.now(cfg.TZ).strftime("%d %b %Y").upper(),
        brand=cfg.BRAND,
        w=cfg.CARD_W,
        h=cfg.CARD_H,
    )

    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--font-render-hinting=none"])
        page = browser.new_page(
            viewport={"width": cfg.CARD_W, "height": cfg.CARD_H},
            device_scale_factor=1,
        )
        page.set_content(html, wait_until="networkidle")

        # Webfonts must be resolved before the shot or Chromium silently
        # substitutes a fallback and the card ships in the wrong typeface.
        page.wait_for_function("document.fonts.ready.then(() => true)")
        page.wait_for_timeout(250)
        _assert_font_loaded(page)

        fit = page.evaluate("window.__fit")
        if not fit or fit["headlineSize"] < MIN_HEADLINE_PX:
            raise RenderError(
                f"headline shrank to {fit and fit['headlineSize']}px "
                f"(floor {MIN_HEADLINE_PX}px) -- headline too long for the card"
            )

        _assert_no_overflow(page)

        page.screenshot(path=str(out), type="jpeg", quality=cfg.CARD_QUALITY)
        browser.close()

    _assert_file_sane(out)
    log.info("rendered %s (%.0f KB, headline %dpx)", out.name, out.stat().st_size / 1024, fit["headlineSize"])
    return out


def _assert_within_contract(post: dict) -> None:
    """Enforce the length contract here, not just in compose.

    Over-long copy does not necessarily *overflow* -- the fit script will shrink
    it to 46px across five lines, which passes every geometric check and still
    looks nothing like the design. Only an explicit limit catches that, and it
    belongs at the render boundary so nothing upstream can quietly bypass it.
    """
    headline = post.get("headline", "")
    body = post.get("body", "")

    if not headline.strip():
        raise RenderError("empty headline")
    if len(headline) > cfg.HEADLINE_MAX_CHARS:
        raise RenderError(
            f"headline is {len(headline)} chars, over the {cfg.HEADLINE_MAX_CHARS} limit"
        )
    if len(body) > cfg.BODY_MAX_CHARS:
        raise RenderError(f"body is {len(body)} chars, over the {cfg.BODY_MAX_CHARS} limit")


def _assert_font_loaded(page) -> None:
    ok = page.evaluate(
        """() => document.fonts.check('700 100px "Bricolage Grotesque"')
                && document.fonts.check('400 27px "IBM Plex Mono"')"""
    )
    if not ok:
        raise RenderError("webfonts did not load; card would ship in a fallback typeface")


def _assert_no_overflow(page) -> None:
    """Nothing may spill outside the 1080x1350 canvas."""
    overflow = page.evaluate(
        """() => {
            const bad = [];
            for (const el of document.querySelectorAll('#headline, #body, .footer, .rail')) {
                const r = el.getBoundingClientRect();
                if (r.bottom > window.innerHeight + 1 || r.right > window.innerWidth + 1 || r.top < -1) {
                    bad.push(el.id || el.className);
                }
            }
            return bad;
        }"""
    )
    if overflow:
        raise RenderError(f"content overflows the canvas: {', '.join(overflow)}")


def _assert_file_sane(path: Path) -> None:
    size = path.stat().st_size
    if size < MIN_BYTES:
        raise RenderError(f"rendered file is only {size} bytes -- render probably failed")
    if size > MAX_BYTES:
        raise RenderError(f"rendered file is {size / 1e6:.1f} MB, over Instagram's 8 MB limit")
    with path.open("rb") as fh:
        if fh.read(2) != b"\xff\xd8":
            raise RenderError("output is not a JPEG; Meta's container endpoint will reject it")
