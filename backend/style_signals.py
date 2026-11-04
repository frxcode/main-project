"""Local linguistic / statistical fingerprints for AI-writing detection."""

from __future__ import annotations

import math
import re
from collections import Counter
from typing import Dict, List

_SENT_SPLIT = re.compile(r"[.!?…]+|\n+")
_WORD_RE = re.compile(r"[A-Za-zА-Яа-яЁёІіЇїЄєҐґʼ']+", re.UNICODE)
_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
_AI_TRANSITIONS = {
    "furthermore",
    "moreover",
    "additionally",
    "consequently",
    "therefore",
    "nonetheless",
    "overall",
    "in conclusion",
    "it is important",
    "it is worth",
    "насамперед",
    "крім того",
    "отже",
    "таким чином",
    "підсумовуючи",
    "варто зазначити",
    "важливо підкреслити",
    "у сучасному світі",
    "в сучасному світі",
    "з одного боку",
    "з іншого боку",
}


def _sentences(text: str) -> List[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text or "") if p and p.strip()]
    return parts


def _words(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def compute(text: str) -> Dict:
    """Return numeric style features Claude can weigh (not a final score)."""
    text = (text or "").strip()
    sents = _sentences(text)
    words = _words(text)
    n_words = len(words)
    n_sents = max(len(sents), 1)
    lengths = [len(_words(s)) for s in sents] or [0]
    mean_len = sum(lengths) / n_sents
    var = sum((x - mean_len) ** 2 for x in lengths) / n_sents
    std_len = math.sqrt(var)
    # Burstiness: high std relative to mean = more human-like variation.
    burstiness = (std_len / mean_len) if mean_len > 0 else 0.0
    uniq = len(set(words))
    ttr = (uniq / n_words) if n_words else 0.0
    counts = Counter(words)
    hapax = sum(1 for _, c in counts.items() if c == 1)
    hapax_ratio = (hapax / n_words) if n_words else 0.0
    # Repeated 3-grams (template feel).
    trigrams = [" ".join(words[i : i + 3]) for i in range(max(0, len(words) - 2))]
    tri_counts = Counter(trigrams)
    repeated_trigrams = sum(1 for _, c in tri_counts.items() if c >= 2)
    lower_blob = " ".join(words)
    transition_hits = sum(1 for p in _AI_TRANSITIONS if p in lower_blob)
    years = len(_YEAR_RE.findall(text))
    avg_word_len = (sum(len(w) for w in words) / n_words) if n_words else 0.0
    # Soft local suspicion 0–100 (hint only; Claude decides).
    hint = 0
    if n_words >= 20:
        if burstiness < 0.35:
            hint += 22
        elif burstiness < 0.5:
            hint += 10
        if n_words >= 50:
            if ttr < 0.42:
                hint += 18
            elif ttr < 0.52:
                hint += 8
            if hapax_ratio < 0.35:
                hint += 12
        if transition_hits >= 3:
            hint += 20
        elif transition_hits >= 1:
            hint += 8
        if repeated_trigrams >= 2:
            hint += 10
        if 4.8 <= avg_word_len <= 6.2 and burstiness < 0.45:
            hint += 8
    hint = max(0, min(100, hint))
    return {
        "n_words": n_words,
        "n_sentences": len(sents),
        "mean_sentence_len": round(mean_len, 2),
        "std_sentence_len": round(std_len, 2),
        "burstiness": round(burstiness, 3),
        "type_token_ratio": round(ttr, 3),
        "hapax_ratio": round(hapax_ratio, 3),
        "repeated_trigrams": repeated_trigrams,
        "ai_transition_hits": transition_hits,
        "year_mentions": years,
        "avg_word_len": round(avg_word_len, 2),
        "local_suspicion_hint": hint,
    }


def format_for_prompt(stats: Dict) -> str:
    if not stats:
        return ""
    return (
        "Локальні статистичні сигнали (обчислені на сервері, не остаточна оцінка):\n"
        f"- слів={stats.get('n_words')}, речень={stats.get('n_sentences')}\n"
        f"- середня довжина речення={stats.get('mean_sentence_len')}, "
        f"σ={stats.get('std_sentence_len')}, burstiness={stats.get('burstiness')} "
        f"(низька <0.45 ≈ ШІ-рівність)\n"
        f"- TTR={stats.get('type_token_ratio')}, hapax={stats.get('hapax_ratio')}\n"
        f"- повторювані 3-грами={stats.get('repeated_trigrams')}, "
        f"AI-переходи={stats.get('ai_transition_hits')}, роки={stats.get('year_mentions')}\n"
        f"- локальний підозрілий hint={stats.get('local_suspicion_hint')}/100 "
        f"(лише підказка; зваж самостійно)"
    )
