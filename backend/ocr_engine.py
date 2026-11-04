"""Image → text OCR for MAN backend.

Default: Gemini vision (high-quality transcription only).
Analysis of that text still goes to Groq separately.

Optional local fallback: INDRA (CPU) if Gemini is unavailable.
Set OCR_BACKEND=gemini|indra (default: gemini when GEMINI_API_KEY exists).
"""

from __future__ import annotations

import mimetypes
import os
import tempfile
import threading
from pathlib import Path
from typing import Optional, Union

OCR_THREADS = max(1, int(os.getenv("INDRA_OCR_THREADS", "10")))
OCR_MIN_LONG_SIDE = max(800, int(os.getenv("INDRA_OCR_MIN_LONG_SIDE", "1600")))

_lock = threading.Lock()
_indra = None
_indra_error: Optional[str] = None

OCR_TRANSCRIBE_PROMPT = (
    "You are a careful OCR engine for a student homework photo. "
    "The page may be rotated or sideways — mentally upright it first. "
    "Transcribe ALL readable text in the original language (Ukrainian/Russian/English). "
    "Include the student name if visible. "
    "Prefer complete real words: if a letter is cut off at the edge, restore the most likely "
    "Ukrainian/Russian word from sentence context (do not invent new sentences). "
    "Do not summarize, translate, judge, or add commentary. "
    "Return only the transcribed text in normal reading order."
)


def _backend() -> str:
    forced = (os.getenv("OCR_BACKEND") or "").strip().lower()
    if forced in {"gemini", "indra"}:
        return forced
    if (os.getenv("GEMINI_API_KEY") or "").strip():
        return "gemini"
    return "indra"


def _clean_ocr_text(text: str) -> str:
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if set(line) <= set("-–—_=*` "):
            continue
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def _mime_for(path: Path) -> str:
    mime, _ = mimetypes.guess_type(str(path))
    if mime:
        return mime
    return "image/png"


def _upright_image_bytes(path: Path) -> tuple[bytes, str]:
    """Upscale and rotate so text is upright before vision OCR."""
    import cv2

    bgr = cv2.imread(str(path))
    if bgr is None:
        return path.read_bytes(), _mime_for(path)

    h, w = bgr.shape[:2]
    long_side = max(h, w)
    if long_side < OCR_MIN_LONG_SIDE:
        scale = OCR_MIN_LONG_SIDE / float(long_side)
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)

    # Prefer Indra's orientation when available; else keep original.
    rotation = 0
    try:
        conv = _load_indra()
        page = conv.ocr(_preprocess_bgr(bgr.copy()))
        rotation = int(getattr(page, "rotation", 0) or 0) % 360
    except Exception as exc:
        print(f"[OCR] orientation probe skipped: {exc}", flush=True)

    rot_map = {
        90: cv2.ROTATE_90_COUNTERCLOCKWISE,  # undo Indra's applied turn
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_CLOCKWISE,
    }
    # Indra reports the turn it applied to read the page; apply the same for Gemini.
    apply = {
        90: cv2.ROTATE_90_CLOCKWISE,
        180: cv2.ROTATE_180,
        270: cv2.ROTATE_90_COUNTERCLOCKWISE,
    }.get(rotation)
    if apply is not None:
        bgr = cv2.rotate(bgr, apply)
        print(f"[OCR] upright rotate={rotation}", flush=True)

    ok, buf = cv2.imencode(".png", bgr)
    if not ok:
        return path.read_bytes(), _mime_for(path)
    return buf.tobytes(), "image/png"


def _ocr_gemini(path: Path) -> str:
    import time

    from google import genai
    from google.genai import types

    api_key = (os.getenv("GEMINI_API_KEY") or "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY is not set")

    primary = os.getenv("OCR_GEMINI_MODEL") or "gemini-3.5-flash-lite"
    candidates = []
    for name in (primary, "gemini-3.5-flash-lite", "gemini-3.8-flash", "gemini-2.0-flash"):
        if name and name not in candidates:
            candidates.append(name)

    client = genai.Client(api_key=api_key)
    data, mime = _upright_image_bytes(path)
    part = types.Part.from_bytes(data=data, mime_type=mime)
    last_error: Optional[Exception] = None

    for model in candidates:
        for attempt in range(3):
            try:
                response = client.models.generate_content(
                    model=model,
                    contents=[OCR_TRANSCRIBE_PROMPT, part],
                )
                text = _clean_ocr_text(response.text or "")
                if not text:
                    raise RuntimeError("Gemini OCR returned empty text")
                print(f"[OCR] gemini model={model} chars={len(text)}", flush=True)
                return text
            except Exception as exc:
                last_error = exc
                msg = str(exc).lower()
                if "503" in msg or "unavailable" in msg or "high demand" in msg:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
    raise RuntimeError(f"Gemini OCR failed: {last_error}")


def _load_indra():
    global _indra, _indra_error
    if _indra is not None:
        return _indra
    if _indra_error:
        raise RuntimeError(_indra_error)
    with _lock:
        if _indra is not None:
            return _indra
        try:
            import indra_ocr

            model_dir = os.getenv("INDRA_OCR_MODEL_DIR")
            kwargs = {"threads": OCR_THREADS, "mode": "ocr"}
            if model_dir:
                kwargs["path"] = model_dir
            _indra = indra_ocr.load(**kwargs)
            print(f"[OCR] Indra loaded threads={OCR_THREADS}", flush=True)
            return _indra
        except Exception as exc:
            _indra_error = f"Indra OCR unavailable: {exc}"
            raise RuntimeError(_indra_error) from exc


def _preprocess_bgr(bgr):
    import cv2

    h, w = bgr.shape[:2]
    long_side = max(h, w)
    if long_side < OCR_MIN_LONG_SIDE:
        scale = OCR_MIN_LONG_SIDE / float(long_side)
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return cv2.convertScaleAbs(gray, alpha=1.35, beta=8)


def _ocr_indra_path(path: Path) -> str:
    import cv2

    conv = _load_indra()
    bgr = cv2.imread(str(path))
    if bgr is None:
        pages = []
        for _i, page in conv.convert(str(path)):
            text = (getattr(page, "text", None) or getattr(page, "markdown", None) or "").strip()
            if text:
                pages.append(_clean_ocr_text(text))
        return "\n\n".join(pages).strip()
    page = conv.ocr(_preprocess_bgr(bgr))
    text = (getattr(page, "text", None) or getattr(page, "markdown", None) or "").strip()
    return _clean_ocr_text(text)


def ocr_image(path: Union[str, Path]) -> str:
    """Return plain text for one image path."""
    path = Path(path)
    backend = _backend()
    errors = []

    if backend == "gemini":
        try:
            return _ocr_gemini(path)
        except Exception as exc:
            errors.append(f"gemini: {exc}")
            print(f"[OCR] gemini failed, trying indra: {exc}", flush=True)

    try:
        return _ocr_indra_path(path)
    except Exception as exc:
        errors.append(f"indra: {exc}")
        raise RuntimeError("; ".join(errors)) from exc


def ocr_bytes(data: bytes, suffix: str = ".png") -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    try:
        return ocr_image(tmp_path)
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except Exception:
            pass


def warmup() -> None:
    """Preload local OCR only when Gemini is not the primary backend."""
    if _backend() == "indra":
        try:
            _load_indra()
        except Exception as exc:
            print(f"[OCR] warmup skipped: {exc}", flush=True)
    else:
        print(f"[OCR] backend={_backend()} (vision transcription)", flush=True)
