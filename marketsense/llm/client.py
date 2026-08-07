"""LLM gateway — the ONE entry point, mirroring ltp-monitor's settled
pattern: `ai_engine` = local (Ollama, default per user decision) / online
(Anthropic) / auto (local then online) / off. Every caller must degrade
gracefully when no engine is available — an LLM outage must never break
the classification path; the rule layer is the always-on fallback.

Strict-JSON contract: classify_json() sends a schema-carrying prompt,
parses the reply, validates types and ranges, and retries once with the
validation error appended. On second failure it returns None and the
caller falls back to rules. The LLM NEVER produces stored numbers other
than its own scores — extracted entities are echoed source text, and the
brief's zero-tolerance rule (LLM writes prose around computed values) is
enforced by what classifier.py chooses to persist.
"""
from __future__ import annotations

import json
import os
import re

import httpx

from marketsense.core.config import settings
from marketsense.core.logging import get_logger

log = get_logger("llm")

TIMEOUT = 60.0
ANTHROPIC_MODEL = "claude-haiku-4-5-20251001"  # triage tier; cheap, JSON-reliable


class LLMUnavailable(Exception):
    pass


def _ollama_generate(prompt: str) -> str:
    s = settings()
    r = httpx.post(
        f"{s.ollama_url}/api/generate",
        json={"model": s.ollama_model, "prompt": prompt, "stream": False,
              "format": "json",
              # keep the model resident between calls — without this every
              # classification pays a cold reload (~10-20s on an 8GB Mac),
              # which was the dominant cost of the backlog drain
              "keep_alive": "30m",
              "options": {"temperature": 0.1, "num_predict": 400}},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("response", "")


def _anthropic_generate(prompt: str) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise LLMUnavailable("no ANTHROPIC_API_KEY")
    r = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
        json={"model": ANTHROPIC_MODEL, "max_tokens": 500,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    return r.json()["content"][0]["text"]


def _generate(prompt: str) -> tuple[str, str]:
    """Returns (text, engine_used). Raises LLMUnavailable when every
    engine allowed by config is down."""
    engine = settings().ai_engine
    if engine == "off":
        raise LLMUnavailable("ai_engine=off")
    errors = []
    if engine in ("local", "auto"):
        try:
            return _ollama_generate(prompt), "local"
        except Exception as e:
            errors.append(f"local: {e}")
            if engine == "local":
                raise LLMUnavailable("; ".join(errors)) from e
    if engine in ("online", "auto"):
        try:
            return _anthropic_generate(prompt), "online"
        except Exception as e:
            errors.append(f"online: {e}")
    raise LLMUnavailable("; ".join(errors))


_JSON_BLOCK = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict | None:
    m = _JSON_BLOCK.search(text or "")
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return None


def _validate(obj: dict, schema: dict) -> str | None:
    """Minimal but strict: required keys, types, numeric ranges, enums.
    Returns an error string or None. Hand-rolled to stay dependency-free."""
    for key, spec in schema.items():
        if key not in obj:
            return f"missing key: {key}"
        val = obj[key]
        t = spec.get("type")
        if t == "number":
            if not isinstance(val, (int, float)) or isinstance(val, bool):
                return f"{key}: expected number, got {type(val).__name__}"
            lo, hi = spec.get("min"), spec.get("max")
            if lo is not None and val < lo:
                return f"{key}: {val} below {lo}"
            if hi is not None and val > hi:
                return f"{key}: {val} above {hi}"
        elif t == "string":
            if not isinstance(val, str):
                return f"{key}: expected string"
            if "enum" in spec and val not in spec["enum"]:
                return f"{key}: '{val}' not in allowed values"
            if "max_words" in spec and len(val.split()) > spec["max_words"]:
                return f"{key}: over {spec['max_words']} words"
        elif t == "object" and not isinstance(val, dict):
            return f"{key}: expected object"
    return None


def classify_json(prompt: str, schema: dict) -> tuple[dict, str] | None:
    """One validated-JSON completion. Returns (obj, engine) or None —
    None means 'no usable model output', and the caller must have a
    deterministic fallback. Never raises for model trouble."""
    try:
        text, engine = _generate(prompt)
    except LLMUnavailable as e:
        log.warning("llm_unavailable", error=str(e)[:200])
        return None

    obj = _extract_json(text)
    err = _validate(obj, schema) if obj is not None else "no JSON found in reply"
    if err is None:
        return obj, engine

    # one retry, with the validation error spelled out
    try:
        text, engine = _generate(
            prompt + f"\n\nYour previous reply was invalid ({err}). "
            "Reply with ONLY the corrected JSON object."
        )
    except LLMUnavailable:
        return None
    obj = _extract_json(text)
    if obj is not None and _validate(obj, schema) is None:
        return obj, engine
    log.warning("llm_invalid_after_retry", error=err)
    return None
