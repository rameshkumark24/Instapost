"""Profile kit for the two accounts: names, bios, profile pictures, pinned posts.

Each account needs an identity before its first automated post lands. A grid
that opens on a machine-made card with no bio and a blank avatar reads as
abandoned or as a bot, and people decide whether to follow in a second or two.

So each account gets:
  * display-name and handle ideas (you check availability when signing up),
  * a bio within Instagram's 150 characters,
  * a profile picture that still reads when shrunk to 40px and cropped round,
  * a pinned first post, in that account's own card style, saying what it is.

Run:  python -m src.brand
Writes brand/<account>/avatar.jpg, brand/<account>/pinned-post.jpg and
brand/PROFILES.md. Re-run once the real handles are set, so the pinned posts
carry them; until then the handle slot on those posts is left blank.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from . import config as cfg
from .render import render, render_avatar, render_quote

log = logging.getLogger("brand")

ROOT = Path(__file__).resolve().parent.parent
BRAND_DIR = ROOT / "brand"

BIO_MAX = 150     # Instagram's bio limit
NAME_MAX = 30     # Instagram's display-name limit

PROFILES = {
    "news": {
        "display_names": ["Daily Tech Brief", "One Tech Story", "The 7:45 Brief"],
        "handle_ideas": ["@dailytechbrief", "@techbrief.daily", "@onetechstory", "@the745brief"],
        "bio": (
            "One tech story that matters, every day at 7:45 PM IST.\n"
            "Summarised in plain words, source credited in the caption."
        ),
        "mark": (">", "_"),
        "intro": {
            "headline": "One tech story that matters. Every day at 7:45 PM.",
            "body": (
                "Picked from Hacker News, GitHub, arXiv and the tech press. Summarised in "
                "plain words, with the source credited in every caption."
            ),
            "publication": "",
            "date_label": "PINNED",
        },
    },
    "flirt": {
        "display_names": ["Commit Issues", "Tech, But Personal", "Primary Key Feelings"],
        "handle_ideas": ["@commitissues", "@tech.but.personal", "@foreignkeyfeelings", "@mergeconflicts.daily"],
        "bio": (
            "Programming concepts, explained through relationships.\n"
            "One card a day, 7:45 PM IST. // tech, but make it personal"
        ),
        "mark": ("<", "3"),
        "intro": {
            "text": (
                "Every card here is a real programming concept, explained through a "
                "relationship. If the joke lands, you just learned what the term means."
            ),
            "terms": ["real programming concept"],
        },
    },
}


def check_profiles() -> list[str]:
    """Everything in PROFILES that Instagram would refuse. Empty means ready."""
    problems = []
    if set(PROFILES) != set(cfg.CHANNELS):
        problems.append(f"profiles {sorted(PROFILES)} do not match channels {sorted(cfg.CHANNELS)}")
    for name, profile in PROFILES.items():
        if len(profile["bio"]) > BIO_MAX:
            problems.append(f"{name}: bio is {len(profile['bio'])} characters, over {BIO_MAX}")
        for display in profile["display_names"]:
            if len(display) > NAME_MAX:
                problems.append(f"{name}: display name {display!r} is over {NAME_MAX} characters")
        for handle in profile["handle_ideas"]:
            if not cfg._HANDLE.fullmatch(handle):
                problems.append(f"{name}: handle idea {handle!r} is not a valid Instagram handle")
    return problems


def _channel_for_kit(name: str) -> dict:
    """The account's channel settings, with a placeholder handle left blank.

    Printing "@__news_handle__" on a post someone uploads by hand would be
    exactly the mistake the branding guard exists to stop.
    """
    channel = dict(cfg.CHANNELS[name])
    if cfg._PLACEHOLDER.fullmatch(channel.get("handle", "")):
        channel["handle"] = ""
    return channel


# Optical centring. The prompt's underscore rests on the baseline, so ">_"
# centred by its line box sits visibly low in the circle; lift it by eye.
# "<3" is balanced as it is.
MARK_OFFSET_EM = {"news": -0.12}


def build() -> dict[str, dict[str, Path]]:
    written: dict[str, dict[str, Path]] = {}
    for name, profile in PROFILES.items():
        channel = _channel_for_kit(name)
        out = BRAND_DIR / name
        avatar = render_avatar(
            *profile["mark"], channel, out / "avatar.jpg", offset_em=MARK_OFFSET_EM.get(name, 0.0)
        )
        if name == "news":
            pinned = render(profile["intro"], out / "pinned-post.jpg", channel=channel)
        else:
            pinned = render_quote(profile["intro"], channel, out / "pinned-post.jpg")
        written[name] = {"avatar": avatar, "pinned": pinned, "handle": channel["handle"], "handle_set": bool(channel["handle"])}
    return written


def write_profiles_md(written: dict[str, dict]) -> Path:
    titles = {"news": "News account", "flirt": "Tech-metaphor account"}
    lines = [
        "# Account profiles",
        "",
        "Generated by `python -m src.brand`. Use these when creating each account",
        "(go-live step A1). Handles are ideas only: Instagram tells you at sign-up",
        "whether one is free.",
        "",
    ]
    for name, profile in PROFILES.items():
        files = written[name]
        lines += [
            f"## {titles.get(name, name)}",
            "",
            "**Display name** (pick one): " + " · ".join(profile["display_names"]),
            "",
            f"**Handle:** `{files['handle']}`" if files["handle_set"]
            else "**Handle ideas:** " + " · ".join(f"`{h}`" for h in profile["handle_ideas"]),
            "",
            "**Bio** (copy exactly):",
            "",
            "```",
            profile["bio"],
            "```",
            "",
            f"**Profile picture:** `brand/{name}/avatar.jpg`",
            "",
            f"**Pinned first post:** `brand/{name}/pinned-post.jpg`. Post it by hand and pin",
            "it from the post's menu, so the grid opens on something that says what the",
            "account is.",
            "",
        ]
        if not files["handle_set"]:
            lines += [
                "> The handle slot on the pinned post is blank because the real handle is not",
                "> set yet. Once it is, re-run `python -m src.brand` to add it.",
                "",
            ]
    path = BRAND_DIR / "PROFILES.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)-12s %(message)s", datefmt="%H:%M:%S")
    if problems := check_profiles():
        for problem in problems:
            log.error(problem)
        return 1
    written = build()
    path = write_profiles_md(written)
    log.info("wrote %s", path.relative_to(ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
