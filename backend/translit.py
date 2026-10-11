"""Ukrainian/Russian name transliteration into Latin ASCII.

Used before a scanned or typed name is matched to the student database.
An AI call may replace this table; the table is the offline fallback.
"""

from __future__ import annotations

_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "h",
    "ґ": "g",
    "д": "d",
    "е": "e",
    "є": "ye",
    "ж": "zh",
    "з": "z",
    "и": "y",
    "і": "i",
    "ї": "yi",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ю": "yu",
    "я": "ya",
    "ы": "y",
    "э": "e",
    "ё": "yo",
    "ъ": "",
    "ь": "",
}

_LATIN_EXTRA = set(" -'`’")


def has_cyrillic(value: str) -> bool:
    return any("А" <= ch <= "я" or ch in "ІіЇїЄєҐґЁёЫыЭэЪъЬь" for ch in str(value or ""))


def is_latin_name(value: str) -> bool:
    text = " ".join(str(value or "").strip().split())
    if not text:
        return False
    return all(ch.isascii() and (ch.isalpha() or ch in _LATIN_EXTRA) for ch in text)


def transliterate(value: str) -> str:
    """Convert one name part to Latin, keeping hyphens and spaces."""
    words = []
    for word in str(value or "").replace("’", "'").replace("`", "'").split():
        pieces = []
        for chunk in word.split("-"):
            pieces.append(_transliterate_token(chunk))
        converted = "-".join(piece for piece in pieces if piece)
        if converted:
            words.append(converted)
    return " ".join(words)


def _transliterate_token(token: str) -> str:
    out = []
    for ch in token:
        lower = ch.lower()
        if lower in _MAP:
            piece = _MAP[lower]
        elif ch.isascii() and (ch.isalpha() or ch in "-'"):
            piece = lower
        else:
            continue
        if not piece:
            continue
        if ch.isupper():
            piece = piece[:1].upper() + piece[1:]
        out.append(piece)
    return "".join(out)


def latinize_parts(first_name: str, last_name: str) -> tuple[str, str, bool]:
    """Return Latin first and last names, and whether Cyrillic was converted."""
    first = " ".join(str(first_name or "").strip().split())
    last = " ".join(str(last_name or "").strip().split())
    changed = has_cyrillic(first) or has_cyrillic(last)
    if not is_latin_name(first):
        first = transliterate(first)
    if not is_latin_name(last):
        last = transliterate(last)
    return first, last, changed


def prepare_for_match(recognized: dict) -> dict:
    """Force the name used for database matching into Latin display form."""
    out = dict(recognized or {})
    first_en = str(out.get("first_name_en") or "").strip()
    last_en = str(out.get("last_name_en") or "").strip()
    if not is_latin_name(first_en) or not is_latin_name(last_en):
        first_en, last_en, _changed = latinize_parts(
            str(out.get("first_name") or first_en),
            str(out.get("last_name") or last_en),
        )
    out["first_name_en"] = first_en
    out["last_name_en"] = last_en
    out["display_name"] = f"{last_en} {first_en}".strip()
    return out
