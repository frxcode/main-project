"""Qwen chat API (OpenAI-compatible). Text only — images are OCR'd locally first."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Optional


def _cfg():
    # Prefer Groq when GROQ_API_KEY is set (OpenAI-compatible).
    groq_key = (os.getenv("GROQ_API_KEY") or "").strip()
    api_key = (
        groq_key
        or os.getenv("QWEN_API_KEY")
        or os.getenv("DASHSCOPE_API_KEY")
        or ""
    ).strip()
    if groq_key and not os.getenv("QWEN_BASE_URL"):
        base_url = "https://api.groq.com/openai/v1"
    else:
        base_url = (
            os.getenv("QWEN_BASE_URL")
            or "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
        ).rstrip("/")
    if groq_key and not os.getenv("QWEN_MODEL"):
        # Qwen on Groq (fast text model).
        model = "qwen/qwen3.8-27b"
    else:
        model = os.getenv("QWEN_MODEL") or "qwen-plus"
    return api_key, base_url.rstrip("/"), model


def chat(messages: list[dict], temperature: float = 0.2, timeout: float = 60.0) -> str:
    """Call Qwen chat completions. Returns assistant text or raises RuntimeError."""
    api_key, base_url, model = _cfg()
    if not api_key:
        raise RuntimeError("QWEN_API_KEY (or DASHSCOPE_API_KEY) is not set")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/chat/completions",
        data=data,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            # Groq sits behind Cloudflare; bare urllib UA gets 403/1010.
            "User-Agent": "man-backend/1.0 (+https://github.com/frxcode/main-project)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"Qwen API HTTP {exc.code}: {detail}") from exc
    except Exception as exc:
        raise RuntimeError(f"Qwen API request failed: {exc}") from exc

    try:
        return (body["choices"][0]["message"]["content"] or "").strip()
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Qwen response: {body!r}") from exc


def available() -> bool:
    api_key, _base, _model = _cfg()
    return bool(api_key)
