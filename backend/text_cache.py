"""Cache detector verdicts by normalized content fingerprint (exact same text)."""

from __future__ import annotations

import hashlib
import json
import re
import time
from pathlib import Path
from typing import Optional

_CACHE_DIR: Optional[Path] = None
_NON_WORD = re.compile(r"[^\w\s]+", re.UNICODE)
_SPACE = re.compile(r"\s+")


def init(base_dir: Path):
    global _CACHE_DIR
    _CACHE_DIR = Path(base_dir) / "_verdict_by_text"
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)


def normalize_text(text: str) -> str:
    t = (text or "").lower().replace("ё", "е")
    t = _NON_WORD.sub(" ", t)
    t = _SPACE.sub(" ", t).strip()
    return t


def content_hash(text: str) -> str:
    norm = normalize_text(text)
    if len(norm) < 40:
        return ""
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()


def _path(key: str) -> Path:
    if _CACHE_DIR is None:
        raise RuntimeError("text_cache.init() was not called")
    return _CACHE_DIR / f"{key}.json"


def lookup(text: str) -> Optional[dict]:
    key = content_hash(text)
    if not key:
        return None
    path = _path(key)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    if "ai_percent" not in data:
        return None
    print(f"[CACHE] text hit key={key[:12]}… ai={data.get('ai_percent')}", flush=True)
    return {
        "ai_percent": int(data.get("ai_percent", 0) or 0),
        "description": str(data.get("description") or ""),
        "content_hash": key,
        "cached": True,
    }


def store(text: str, ai_percent: int, description: str) -> Optional[str]:
    key = content_hash(text)
    if not key:
        return None
    path = _path(key)
    payload = {
        "content_hash": key,
        "ai_percent": int(ai_percent),
        "description": str(description or ""),
        "stored_at": time.time(),
        "preview": normalize_text(text)[:160],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[CACHE] text store key={key[:12]}… ai={ai_percent}", flush=True)
    return key
