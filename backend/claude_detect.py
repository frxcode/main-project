"""Deep Claude Sonnet AI-writing detector (text after OCR) with live web search."""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Optional

import style_signals
import web_search

DEEP_SYSTEM_PROMPT = """Ти — суворий криміналістичний детектор AI-тексту для учнівських робіт (UA/EN/RU).
Мета: виявити навіть сильно «humanized» AI (ChatGPT, Claude, Gemini, Qwen тощо), який інші детектори занижують до ~15%.

ГЛИБОКИЙ АНАЛІЗ (внутрішньо, слово-за-словом і фраза-за-фразою; у відповідь НЕ виливай довгий звіт):
1) Лексика: шаблонні кліше, «академічний глянець», відсутність особистого голосу.
2) Синтаксис / ритм: рівномірна довжина речень, низька burstiness, передбачувані переходи.
3) Статистика: використай локальні метрики (TTR, burstiness, hapax, 3-грами, AI-переходи).
4) Семантика: порожні узагальнення без конкретики учня.
5) Факти: ОБОВ'ЯЗКОВО використай інструмент web_search для підозрілих тверджень, імен, дат, цитат, «досліджень». Звірь з відкритими джерелами. Галюцинації / неіснуючі джерела → підвищуй %.
6) Портфоліо: різкий стрибок стилю vs еталони = підозра.
7) Патерни моделей + humanize-сервіси (штучні скорочення при рівному ритмі).
8) OCR-помилки ≠ сленг і ≠ «людяність».

ШКАЛА (не занижуй «бо humanize»):
- 90–100 явні LLM-маркери; 75–89 гладкий AI/humanize; 60–74 сильні стат/стиль сигнали;
- 45–59 мікс; 0–35 справжнє учнівське. Сильні AI-сигнали → не нижче 60.

ВІДПОВІДЬ: лише один JSON-об'єкт, без markdown:
{"ai_percent":85,"description":"шаблонний humanize","signals":["burstiness low","web mismatch"]}
description = 3–8 слів українською для UI. signals = короткі внутрішні мітки."""


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
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts)


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


def analyze_text(
    system_prompt: str,
    user_content: str,
    timeout: float = 150.0,
    use_web_search: bool = True,
) -> dict:
    """Return {"ai_percent": int, "description": str, "signals": list} from Claude."""
    api_key, model = _cfg()
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY / CLAUDE_API_KEY is not set")

    # Sonnet 5.5: no temperature; effort + optional server-side web_search.
    tools = []
    if use_web_search:
        tools.append(
            {
                "type": "web_search_20260318",
                "name": "web_search",
                "max_uses": 5,
            }
        )

    messages = [{"role": "user", "content": user_content[:70000]}]
    payload = {
        "model": model,
        "max_tokens": 1600,
        "system": system_prompt,
        "messages": messages,
        "output_config": {"effort": "high"},
    }
    if tools:
        payload["tools"] = tools

    # Server tools may pause_turn; continue a few rounds with the same tools.
    body = None
    for round_i in range(4):
        body = _api_call(payload, api_key, timeout=timeout)
        stop = body.get("stop_reason")
        content = body.get("content") or []
        print(
            f"[DETECT] claude round={round_i} stop={stop} model={model} "
            f"blocks={len(content)}",
            flush=True,
        )
        if stop != "pause_turn":
            break
        # Continue: append assistant content and ask model to finish JSON.
        messages.append({"role": "assistant", "content": content})
        messages.append(
            {
                "role": "user",
                "content": "Продовж аналіз і поверни фінальний JSON з ai_percent і description.",
            }
        )
        payload["messages"] = messages

    parsed = _parse_json(_collect_text((body or {}).get("content")))
    if not parsed:
        raise RuntimeError(
            f"Claude detect non-JSON: {_collect_text((body or {}).get('content'))[:400]}"
        )

    pct = int(parsed.get("ai_percent", 0) or 0)
    pct = max(0, min(100, pct))
    desc = str(parsed.get("description", "") or "").strip()
    signals = parsed.get("signals") or []
    if not isinstance(signals, list):
        signals = [str(signals)]
    if use_web_search:
        signals = list(signals) + ["web_search_enabled"]
    print(f"[DETECT] claude model={model} ai_percent={pct} signals={signals[:8]}", flush=True)
    return {"ai_percent": pct, "description": desc, "signals": signals}


def detect_deep(
    work_text: str,
    portfolio_blocks: Optional[list] = None,
    system_prompt: Optional[str] = None,
    use_web_search: bool = True,
) -> dict:
    """
    Pass-2 forensic detector: style stats + optional local research pack +
    Claude Sonnet with native web_search tool + etalon portfolio.
    UI gets only a short description.
    """
    work_text = (work_text or "").strip()
    if not work_text:
        return {"ai_percent": 0, "description": "немає тексту", "signals": []}

    stats = style_signals.compute(work_text)
    stats_block = style_signals.format_for_prompt(stats)

    # Local DDG/Wikipedia pack as backup context; Claude also has live web_search.
    web_ctx = ""
    try:
        web_ctx = web_search.research_for_detector(work_text)
    except Exception as exc:
        print(f"[DETECT] local web research skipped: {exc}", flush=True)

    user_parts = [
        "Це PASS 2 глибокої перевірки. Текст уже витягнуто з фото (OCR) і прив'язано до зображення.",
        "Порівняй з еталонами. Для фактів використовуй web_search.",
        "У JSON лише короткий description (3–8 слів).",
        stats_block,
    ]
    if web_ctx:
        user_parts.append("Додатковий локальний веб/wiki контекст (орієнтир):\n" + web_ctx)
    if portfolio_blocks:
        user_parts.append("Еталони / портфоліо учня:")
        user_parts.extend(portfolio_blocks)
    else:
        user_parts.append("Еталонів немає — оцінюй за стилем, статистикою та веб-фактчеком.")
    user_parts.append("Текст роботи для перевірки:")
    user_parts.append(work_text)

    if system_prompt and len(system_prompt.strip()) > 200 and system_prompt.strip() != DEEP_SYSTEM_PROMPT:
        prompt = DEEP_SYSTEM_PROMPT + "\n\nДодаткові вказівки вчителя:\n" + system_prompt.strip()
    else:
        prompt = DEEP_SYSTEM_PROMPT

    try:
        result = analyze_text(
            prompt,
            "\n\n".join(user_parts),
            timeout=150.0,
            use_web_search=use_web_search,
        )
    except RuntimeError as exc:
        # If org disabled web search, retry without the tool.
        if use_web_search and "web search" in str(exc).lower():
            print(f"[DETECT] web_search tool unavailable, retry without: {exc}", flush=True)
            result = analyze_text(
                prompt,
                "\n\n".join(user_parts),
                timeout=120.0,
                use_web_search=False,
            )
        else:
            raise

    hint = int(stats.get("local_suspicion_hint") or 0)
    pct = int(result.get("ai_percent") or 0)
    if hint >= 55 and pct < 60:
        pct = max(pct, min(78, hint + 10))
        result["ai_percent"] = pct
        signals = list(result.get("signals") or [])
        signals.append("local_floor_applied")
        result["signals"] = signals
        print(f"[DETECT] local floor → ai_percent={pct} (hint={hint})", flush=True)
    elif hint >= 40 and pct < 45:
        pct = max(pct, 55)
        result["ai_percent"] = pct
        signals = list(result.get("signals") or [])
        signals.append("local_floor_soft")
        result["signals"] = signals
    result["style_stats"] = stats
    return result
