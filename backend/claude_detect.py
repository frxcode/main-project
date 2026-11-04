"""Pass-2 Claude Sonnet vision AI detector: analyze the IMAGE (not OCR text)."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional, Union

def _load_deep_prompt() -> str:
    path = Path(__file__).resolve().parent / "prompt.txt"
    try:
        text = path.read_text(encoding="utf-8").strip()
        if len(text) > 200:
            return text
    except Exception:
        pass
    return (
        "Агресивний vision-детектор AI. Повтори/шаблони, галюцинації (web_search), "
        "watermark-проксі, сирий LLM-стиль. Ігноруй OCR і носій (рукопис/друк). "
        "Очевидний AI → 92–100. JSON: ai_percent, description (3–8 слів), signals, normalized_content."
    )


DEEP_SYSTEM_PROMPT = _load_deep_prompt()


def _cfg():
    api_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("CLAUDE_API_KEY")
        or ""
    ).strip()
    model = os.getenv("CLAUDE_DETECT_MODEL") or os.getenv("CLAUDE_OCR_MODEL") or "claude-sonnet-5-5"
    return api_key, model


def available() -> bool:
    api_key, _model = _cfg()
    return bool(api_key)


def _mime(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    return mime or "image/png"


def _parse_json(text: str) -> dict:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\s*```\s*$", "", text, flags=re.IGNORECASE).strip()
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        text = text[start : end + 1]
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _collect_text(content) -> str:
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


def _image_block(path: Path) -> dict:
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    return {
        "type": "image",
        "source": {
            "type": "base64",
            "media_type": _mime(path),
            "data": b64,
        },
    }


def _api_call(payload: dict, api_key: str, timeout: float) -> dict:
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2026-10-21",
            "User-Agent": "man-backend/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        raise RuntimeError(f"Claude detect HTTP {exc.code}: {detail}") from exc


def detect_image(
    work_image: Union[str, Path],
    etalon_images: Optional[List[Union[str, Path]]] = None,
    system_prompt: Optional[str] = None,
    use_web_search: bool = True,
    timeout: float = 52.0,
) -> dict:
    """
    Pass 2: send the homework IMAGE (+ optional etalon images) to Claude.
    Never score from OCR text — OCR artifacts must not affect ai_percent.
    """
    api_key, model = _cfg()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY / CLAUDE_API_KEY is not set")

    work_path = Path(work_image)
    if not work_path.exists():
        return {"ai_percent": 0, "description": "немає фото", "signals": [], "normalized_content": ""}

    content: list = [
        _image_block(work_path),
        {
            "type": "text",
            "text": (
                "PASS 2. Це ФОТО роботи для перевірки на AI (не OCR-текст).\n"
                "Ігноруй OCR/шум/рукопис vs друк. Оцінюй лише зміст і стиль письма.\n"
                "Будь агресивним: очевидний AI → 92–100. description 3–8 слів."
            ),
        },
    ]

    etalons = [Path(p) for p in (etalon_images or []) if Path(p).exists()][:4]
    for idx, ep in enumerate(etalons, start=1):
        content.append(_image_block(ep))
        content.append(
            {
                "type": "text",
                "text": f"Еталон/портфоліо фото #{idx} ({ep.name}) — для порівняння стилю учня.",
            }
        )
    if not etalons:
        content.append(
            {
                "type": "text",
                "text": "Еталонних фото немає — оцінюй лише роботу за LLM-ознаками + web_search.",
            }
        )

    if system_prompt and len(system_prompt.strip()) > 200 and system_prompt.strip() != DEEP_SYSTEM_PROMPT:
        prompt = DEEP_SYSTEM_PROMPT + "\n\nДодаткові вказівки вчителя:\n" + system_prompt.strip()
    else:
        prompt = DEEP_SYSTEM_PROMPT

    tools = []
    if use_web_search:
        tools.append(
            {
                "type": "web_search_20260318",
                "name": "web_search",
                "max_uses": 3,
            }
        )

    messages = [{"role": "user", "content": content}]
    payload = {
        "model": model,
        "max_tokens": 1200,
        "system": prompt,
        "messages": messages,
        "output_config": {"effort": "medium"},
    }
    if tools:
        payload["tools"] = tools

    body = None
    try:
        for round_i in range(3):
            body = _api_call(payload, api_key, timeout=timeout)
            stop = body.get("stop_reason")
            blocks = body.get("content") or []
            print(
                f"[DETECT] vision round={round_i} stop={stop} model={model} "
                f"etalons={len(etalons)} blocks={len(blocks)}",
                flush=True,
            )
            if stop != "pause_turn":
                break
            messages.append({"role": "assistant", "content": blocks})
            messages.append(
                {
                    "role": "user",
                    "content": "Поверни фінальний JSON з ai_percent, description, normalized_content.",
                }
            )
            payload["messages"] = messages
    except RuntimeError as exc:
        if use_web_search and "web search" in str(exc).lower():
            print(f"[DETECT] web_search unavailable, retry without: {exc}", flush=True)
            return detect_image(
                work_image,
                etalon_images=etalon_images,
                system_prompt=system_prompt,
                use_web_search=False,
                timeout=timeout,
            )
        raise

    parsed = _parse_json(_collect_text((body or {}).get("content")))
    if not parsed:
        raise RuntimeError(
            f"Claude detect non-JSON: {_collect_text((body or {}).get('content'))[:400]}"
        )

    pct = max(0, min(100, int(parsed.get("ai_percent", 0) or 0)))
    # Aggressive post-floor: clear mid-high scores that models soft-ball → push up.
    if pct >= 55 and pct < 80:
        pct = max(pct, 82)
    elif pct >= 40 and pct < 55:
        pct = max(pct, 70)
    desc = str(parsed.get("description", "") or "").strip()
    signals = parsed.get("signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    signals = list(signals) + ["vision_image", "format_ignored", "ocr_ignored"]
    if use_web_search:
        signals.append("web_search_enabled")
    norm = str(parsed.get("normalized_content") or "").strip()
    print(f"[DETECT] vision ai_percent={pct} signals={signals[:8]}", flush=True)
    return {
        "ai_percent": pct,
        "description": desc,
        "signals": signals,
        "normalized_content": norm,
    }


def detect_deep(
    work_text: str,
    portfolio_blocks: Optional[list] = None,
    system_prompt: Optional[str] = None,
    use_web_search: bool = True,
) -> dict:
    """Legacy text path — prefer detect_image. Kept for rare fallbacks only."""
    # Text path is discouraged (OCR text biases the model). Return soft refusal to force image path.
    _ = (work_text, portfolio_blocks, system_prompt, use_web_search)
    return {
        "ai_percent": 0,
        "description": "потрібне фото не текст",
        "signals": ["text_path_disabled"],
        "normalized_content": "",
    }
