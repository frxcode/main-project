"""Claude Sonnet vision: Pass 1 name-only scan (image stays separate)."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Optional, Union

CLAUDE_NAME_PROMPT = (
    "You read a photo of a student paper. The page may be rotated.\n"
    "Extract ONLY the student name visible on the page. Do NOT OCR the homework body.\n"
    "Return ONLY JSON (no markdown):\n"
    "{\n"
    '  "found": true/false,\n'
    '  "first_name": "",\n'
    '  "last_name": "",\n'
    '  "full_name": "",\n'
    '  "first_name_en": "Latin ASCII",\n'
    '  "last_name_en": "Latin ASCII",\n'
    '  "display_name": "Surname Name"\n'
    "}\n"
    "Rules:\n"
    "- found=true only if a real student name is visible.\n"
    "- first_name_en/last_name_en/display_name must be Latin ASCII for a 16-char LCD.\n"
    "- Ignore homework body text entirely.\n"
)

# Back-compat alias
CLAUDE_SCAN_PROMPT = CLAUDE_NAME_PROMPT


def _cfg():
    api_key = (
        os.getenv("ANTHROPIC_API_KEY")
        or os.getenv("CLAUDE_API_KEY")
        or ""
    ).strip()
    model = os.getenv("CLAUDE_OCR_MODEL") or "claude-sonnet-5-5"
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


def _empty(error: Optional[str] = None) -> dict:
    out = {
        "found": False,
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "first_name_en": "",
        "last_name_en": "",
        "display_name": "",
        "document_text": "",
        "ocr_text": "",
    }
    if error:
        out["error"] = error
    return out


def scan_name(path: Union[str, Path]) -> dict:
    """Pass 1: vision name extraction only (no homework body OCR)."""
    api_key, model = _cfg()
    if not api_key:
        return _empty("ANTHROPIC_API_KEY / CLAUDE_API_KEY is not set")

    path = Path(path)
    raw = path.read_bytes()
    b64 = base64.standard_b64encode(raw).decode("ascii")
    payload = {
        "model": model,
        "max_tokens": 512,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": _mime(path),
                            "data": b64,
                        },
                    },
                    {"type": "text", "text": CLAUDE_NAME_PROMPT},
                ],
            }
        ],
        "output_config": {"effort": "low"},
    }
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
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:800]
        return _empty(f"Claude HTTP {exc.code}: {detail}")
    except Exception as exc:
        return _empty(f"Claude request failed: {exc}")

    parts = []
    for block in body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    parsed = _parse_json("\n".join(parts))
    if not parsed:
        return _empty("Claude returned non-JSON name payload")

    first_en = str(parsed.get("first_name_en") or "").strip()
    last_en = str(parsed.get("last_name_en") or "").strip()
    display = str(parsed.get("display_name") or "").strip()
    if not display and (first_en or last_en):
        display = f"{last_en} {first_en}".strip()

    result = {
        "found": bool(parsed.get("found", bool(parsed.get("full_name") or parsed.get("last_name")))),
        "first_name": str(parsed.get("first_name") or "").strip(),
        "last_name": str(parsed.get("last_name") or "").strip(),
        "full_name": str(parsed.get("full_name") or "").strip(),
        "first_name_en": first_en,
        "last_name_en": last_en,
        "display_name": display,
        "document_text": "",
        "ocr_text": "",
    }
    print(f"[OCR] name-only model={model} found={result['found']} display={display!r}", flush=True)
    return result


def scan_image(path: Union[str, Path]) -> dict:
    """Back-compat: Pass 1 name-only (no body OCR)."""
    return scan_name(path)


def document_text(path: Union[str, Path]) -> str:
    """Deprecated for Pass 2 — body is not OCR'd on Pass 1."""
    return ""
