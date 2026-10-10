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

# Current Flash models spend "thinking" tokens from the same output limit as
# the answer, and the limit is a hard cutoff: it does not make the model think
# less, it only stops the answer. At 300 a reply could come back with no text
# at all. At 2,048 a reply for three memes arrived 316 characters long,
# mid-sentence, and three days ended with no meme ideas. Google's advice is
# not to set a small limit at all; this is only a ceiling on a runaway reply.
GEMINI_MIN_OUTPUT_TOKENS = 16384

# Groq's gpt-oss models reason before answering, and the reasoning counts
# against max_completion_tokens exactly as Gemini's thinking does. Low effort
# is plenty for a caption, and leaves the budget for the answer. The ceiling
# keeps a request inside the free plan's 8,000 tokens a minute for this model.
GROQ_MIN_OUTPUT_TOKENS = 2048
GROQ_MAX_OUTPUT_TOKENS = 4096

# An answer that hit the limit is not an answer: half a JSON object, or a
# summary that stops mid-sentence. It is reported as a failure, so the next
# model gets the question instead of the caller getting the fragment.
_CUT_OFF = "answer cut off at the output limit"

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
        candidate = r.json()["candidates"][0]
        if candidate.get("finishReason") == "MAX_TOKENS":
            return None, _OTHER, _CUT_OFF
        # The answer can arrive in more than one part; a thought is not part of it.
        text = "".join(p.get("text", "") for p in candidate["content"]["parts"] if not p.get("thought"))
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None, _OTHER, "no text in reply (output budget spent, or a safety block)"
    if not text.strip():
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
                "max_completion_tokens": min(max(max_tokens, GROQ_MIN_OUTPUT_TOKENS), GROQ_MAX_OUTPUT_TOKENS),
                **({"reasoning_effort": "low"} if model.startswith("openai/gpt-oss") else {}),
            },
            timeout=(cfg.LLM_CONNECT_TIMEOUT_S, cfg.LLM_TIMEOUT_S),
        )
    except requests.RequestException as exc:
        return None, _OTHER, f"network error ({type(exc).__name__})"

    if r.status_code != 200:
        kind, detail = _classify(r.status_code, r.text)
        return None, kind, detail
    try:
        choice = r.json()["choices"][0]
        if choice.get("finish_reason") == "length":
            return None, _OTHER, _CUT_OFF
        text = choice["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return None, _OTHER, "no text in reply"
    if not text or not text.strip():
        return None, _OTHER, "empty reply"
    return text, _OK, ""
