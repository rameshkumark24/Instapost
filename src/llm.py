"""The one place that asks a free-tier language model for text.

compose.py and flirt.py used to carry their own copy of this call, each with a
model id pasted in. Both still named gemini-2.0-flash three months after
Google shut it down on 1 June 2026, and both swallowed the resulting error --
so adding a Gemini key would have looked like it worked while the
tech-metaphor account drafted nothing and said nothing.

What this module does differently:

  * model ids live in config as ordered lists. A model that answers "not
    found" has been retired, so the next one is tried and a retirement
    degrades instead of breaking;
  * a rejected key stops that provider's chain, since none of its other
    models will accept the same key;
  * every reply carries the reason it failed, so callers can tell "no key"
    from "every model retired" from "network blip", and report it;
  * the API key travels in a header, never in the URL. requests puts the URL
    into its exception messages, and the old code logged those messages.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

import requests

from . import config as cfg

log = logging.getLogger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Current Flash models spend "thinking" tokens from the same output budget as
# the answer. A 300-token cap can be exhausted before a word of the answer is
# written, which comes back as a reply with no text at all.
GEMINI_MIN_OUTPUT_TOKENS = 2048

_OK, _RETIRED, _AUTH, _OTHER = "ok", "retired", "auth", "other"


@dataclass(frozen=True)
class Reply:
    text: str | None
    error: str | None = None      # why there is no text; None whenever text is set


def complete(prompt: str, temperature: float, max_tokens: int = 300) -> Reply:
    """The first model that answers, or the reasons none did. Never raises."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    groq_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not (gemini_key or groq_key):
        return Reply(None, "no LLM key configured")

    providers = []
    if gemini_key:
        providers.append((_gemini, gemini_key, cfg.GEMINI_MODELS))
    if groq_key:
        providers.append((_groq, groq_key, cfg.GROQ_MODELS))

    errors: list[str] = []
    for call, key, models in providers:
        for model in models:
            started = time.monotonic()
            text, kind, detail = call(model, key, prompt, temperature, max_tokens)
            took = time.monotonic() - started
            if kind == _OK:
                log.info("%s answered in %.1fs", model, took)
                return Reply(text)
            errors.append(f"{model}: {detail}")
            if kind == _RETIRED:
                log.warning("%s looks retired (%s); trying the next model", model, detail)
            elif kind == _AUTH:
                break
            else:
                # Logged so a slow or flaky model shows up in the Actions log
                # instead of silently costing time on every card.
                log.info("%s failed after %.1fs (%s); trying the next model", model, took, detail)
    return Reply(None, "; ".join(errors) or "no model configured")


def _classify(status: int, body: str) -> tuple[str, str]:
    """Why a non-200 happened. The body is inspected but never repeated."""
    low = body.lower()
    if status == 404 or "decommissioned" in low or "does not exist" in low:
        return _RETIRED, f"model not found (HTTP {status})"
    if status in (401, 403) or (status == 400 and ("api key" in low or "api_key" in low)):
        return _AUTH, f"key rejected (HTTP {status})"
    return _OTHER, f"HTTP {status}"


def _gemini(model: str, key: str, prompt: str, temperature: float, max_tokens: int):
    try:
        r = requests.post(
            GEMINI_URL.format(model=model),
            headers={"x-goog-api-key": key},
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {
                    "temperature": temperature,
                    "maxOutputTokens": max(max_tokens, GEMINI_MIN_OUTPUT_TOKENS),
                },
            },
            timeout=(cfg.LLM_CONNECT_TIMEOUT_S, cfg.LLM_TIMEOUT_S),
        )
    except requests.RequestException as exc:
        return None, _OTHER, f"network error ({type(exc).__name__})"

    if r.status_code != 200:
        kind, detail = _classify(r.status_code, r.text)
        return None, kind, detail
    try:
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, _OTHER, "no text in reply (output budget spent, or a safety block)"
    if not text or not text.strip():
        return None, _OTHER, "empty reply"
    return text, _OK, ""


def _groq(model: str, key: str, prompt: str, temperature: float, max_tokens: int):
    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
            timeout=(cfg.LLM_CONNECT_TIMEOUT_S, cfg.LLM_TIMEOUT_S),
        )
    except requests.RequestException as exc:
        return None, _OTHER, f"network error ({type(exc).__name__})"

    if r.status_code != 200:
        kind, detail = _classify(r.status_code, r.text)
        return None, kind, detail
    try:
        text = r.json()["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        return None, _OTHER, "no text in reply"
    if not text or not text.strip():
        return None, _OTHER, "empty reply"
    return text, _OK, ""
