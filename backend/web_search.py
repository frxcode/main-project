"""Web + Wikipedia research helpers for deep AI-text detection."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import List, Tuple

_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґʼ']+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_CLAIMISH = re.compile(
    r"(?:\d+\s*%|\b(?:згідно|за даними|досліджен|вчені|wikipedia|вікіпеді|"
    r"according to|research shows|studies show)\b)",
    re.IGNORECASE,
)


def _enabled() -> bool:
    return (os.getenv("QWEN_WEB_SEARCH") or "1").strip().lower() not in {"0", "false", "no"}


def _get(url: str, timeout: float = 12.0) -> str:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "man-backend/1.0 (+research)"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _ddg_html(query: str, limit: int = 4) -> List[str]:
    url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
    html = _get(url)
    snippets = re.findall(
        r'class="result__snippet[^"]*"[^>]*>(.*?)</(?:a|td|div)',
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    clean = []
    for raw in snippets:
        text = re.sub(r"<[^>]+>", " ", raw)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > 40:
            clean.append(text)
        if len(clean) >= limit:
            break
    return clean


def wikipedia_summary(title: str, lang: str = "uk") -> str:
    """Fetch a short Wikipedia extract; try uk then en."""
    title = (title or "").strip()
    if len(title) < 3:
        return ""
    for code in (lang, "en") if lang != "en" else ("en",):
        api = (
            f"https://{code}.wikipedia.org/api/rest_v1/page/summary/"
            + urllib.parse.quote(title.replace(" ", "_"))
        )
        try:
            raw = _get(api, timeout=10)
            data = json.loads(raw)
        except Exception:
            continue
        if data.get("type") == "disambiguation":
            continue
        extract = (data.get("extract") or "").strip()
        if extract:
            label = data.get("title") or title
            return f"[{code}.wiki:{label}] {extract[:500]}"
    return ""


def _candidate_entities(text: str, limit: int = 6) -> List[str]:
    """Heuristic entities / topics worth Wikipedia checks."""
    cands: List[str] = []
    # Quoted titles
    for m in re.findall(r"[«\"“]([^»\"”]{4,80})[»\"”]", text or ""):
        cands.append(m.strip())
    # Capitalized multi-word (Latin / Cyrillic)
    for m in re.findall(
        r"(?:(?<=^)|(?<=[\.\!\?\n]))\s*([A-ZА-ЯЁІЇЄҐ][\wʼ']+(?:\s+[A-ZА-ЯЁІЇЄҐ][\wʼ']+){0,4})",
        text or "",
        flags=re.MULTILINE,
    ):
        if len(m.split()) >= 1 and len(m) >= 4:
            cands.append(m.strip())
    # Noun-ish phrases near years
    for m in re.finditer(_YEAR_RE, text or ""):
        start = max(0, m.start() - 60)
        end = min(len(text), m.end() + 40)
        window = re.sub(r"\s+", " ", text[start:end]).strip()
        if window:
            cands.append(window[:90])
    # Dedup preserve order
    seen = set()
    out = []
    for c in cands:
        key = c.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
        if len(out) >= limit:
            break
    return out


def _claim_sentences(text: str, limit: int = 5) -> List[str]:
    parts = re.split(r"[.!?…\n]+", text or "")
    scored: List[Tuple[int, str]] = []
    for p in parts:
        s = re.sub(r"\s+", " ", p).strip()
        if len(s) < 35 or len(s) > 280:
            continue
        score = 0
        if _YEAR_RE.search(s):
            score += 2
        if _CLAIMISH.search(s):
            score += 3
        if re.search(r"\d", s):
            score += 1
        if score:
            scored.append((score, s))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


def research_ai_writing(limit: int = 4) -> str:
    """Fetch short public notes about LLM-writing markers."""
    if not _enabled():
        return ""
    queries = [
        "признаки текста сгенерированного ChatGPT академическая работа humanize",
        "detect AI generated student essay linguistic markers burstiness perplexity",
        "ChatGPT Claude Gemini writing fingerprints academic Ukrainian",
    ]
    blocks = []
    for q in queries:
        try:
            hits = _ddg_html(q, limit=2)
        except Exception as exc:
            print(f"[WEB] search failed for {q!r}: {exc}", flush=True)
            continue
        for h in hits:
            blocks.append(f"- {h}")
    if not blocks:
        return ""
    return "Веб-контекст: відомі патерни LLM-письма:\n" + "\n".join(blocks[:limit])


def research_for_detector(work_text: str, limit: int = 10) -> str:
    """
    Deep research pack for the detector:
    - Wikipedia extracts for entities in the student text
    - Web snippets for factual claims
    - Known AI-writing pattern notes
    """
    if not _enabled():
        return ""

    sections: List[str] = []

    # 1) Wikipedia entity checks
    wiki_blocks = []
    for ent in _candidate_entities(work_text, limit=5):
        try:
            summary = wikipedia_summary(ent)
        except Exception as exc:
            print(f"[WEB] wiki failed for {ent!r}: {exc}", flush=True)
            summary = ""
        if summary:
            wiki_blocks.append(f"- запит «{ent}»: {summary}")
        else:
            # Fall back to web search on the entity
            try:
                hits = _ddg_html(f"{ent} wikipedia", limit=1)
                if hits:
                    wiki_blocks.append(f"- запит «{ent}» (web): {hits[0]}")
            except Exception:
                pass
    if wiki_blocks:
        sections.append("Перевірка сутностей / фактів (Wikipedia/web):\n" + "\n".join(wiki_blocks[:6]))

    # 2) Claim sentences vs web
    claim_blocks = []
    for claim in _claim_sentences(work_text, limit=4):
        q = claim[:120]
        try:
            hits = _ddg_html(q, limit=2)
        except Exception as exc:
            print(f"[WEB] claim search failed: {exc}", flush=True)
            continue
        if not hits:
            continue
        claim_blocks.append(f"- твердження: «{claim[:160]}»")
        for h in hits:
            claim_blocks.append(f"  · {h}")
    if claim_blocks:
        sections.append("Крос-чек підозрілих тверджень:\n" + "\n".join(claim_blocks[:12]))

    # 3) Generic AI pattern research
    ai_notes = research_ai_writing(limit=4)
    if ai_notes:
        sections.append(ai_notes)

    if not sections:
        return ""
    return "\n\n".join(sections)[:9000]
