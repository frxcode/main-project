"""Local Indra OCR (CPU). Models download from Hugging Face on first use.

Pi camera frames are often 640x480 — too small for reliable OCR.
We upscale short sides before recognition and let Indra handle rotation.
"""

from __future__ import annotations

import os
import tempfile
import threading
from pathlib import Path
from typing import Optional, Union

# Cap CPU so OCR does not starve the rest of the server.
OCR_THREADS = max(1, int(os.getenv("INDRA_OCR_THREADS", "10")))
# Minimum long-side pixels after upscale (Pi 640x480 → ~1600).
OCR_MIN_LONG_SIDE = max(800, int(os.getenv("INDRA_OCR_MIN_LONG_SIDE", "1600")))

_lock = threading.Lock()
_converter = None
_load_error: Optional[str] = None


def _load_converter():
    global _converter, _load_error
    if _converter is not None:
        return _converter
    if _load_error:
        raise RuntimeError(_load_error)
    with _lock:
        if _converter is not None:
            return _converter
        if _load_error:
            raise RuntimeError(_load_error)
        try:
            import indra_ocr

            model_dir = os.getenv("INDRA_OCR_MODEL_DIR")
            kwargs = {"threads": OCR_THREADS, "mode": "ocr"}
            if model_dir:
                kwargs["path"] = model_dir
            _converter = indra_ocr.load(**kwargs)
            print(
                f"[OCR] Indra loaded threads={OCR_THREADS} "
                f"min_long_side={OCR_MIN_LONG_SIDE} model_dir={model_dir or 'default'}",
                flush=True,
            )
            return _converter
        except Exception as exc:
            _load_error = f"Indra OCR unavailable: {exc}"
            print(f"[OCR] load failed: {_load_error}", flush=True)
            raise RuntimeError(_load_error) from exc


def _preprocess_bgr(bgr):
    """Upscale small camera frames and boost contrast for OCR."""
    import cv2

    h, w = bgr.shape[:2]
    long_side = max(h, w)
    if long_side < OCR_MIN_LONG_SIDE:
        scale = OCR_MIN_LONG_SIDE / float(long_side)
        bgr = cv2.resize(bgr, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    gray = cv2.convertScaleAbs(gray, alpha=1.35, beta=8)
    return gray


def _clean_ocr_text(text: str) -> str:
    """Drop lone dash/noise lines Indra sometimes emits on low-res scans."""
    lines = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if set(line) <= set("-–—_= "):
            continue
        lines.append(line)
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines).strip()


def _ocr_bgr(bgr) -> str:
    import cv2

    conv = _load_converter()
    gray = _preprocess_bgr(bgr)
    page = conv.ocr(gray)
    text = (getattr(page, "text", None) or getattr(page, "markdown", None) or "").strip()
    return _clean_ocr_text(text)


def ocr_image(path: Union[str, Path]) -> str:
    """Return plain text for one image path (reading order)."""
    import cv2

    path = Path(path)
    bgr = cv2.imread(str(path))
    if bgr is None:
        # PDF / non-image: fall back to Indra convert path API.
        conv = _load_converter()
        pages_text = []
        for _i, page in conv.convert(str(path)):
            text = (getattr(page, "text", None) or getattr(page, "markdown", None) or "").strip()
            if text:
                pages_text.append(_clean_ocr_text(text))
        return "\n\n".join(pages_text).strip()
    return _ocr_bgr(bgr)


def ocr_bytes(data: bytes, suffix: str = ".png") -> str:
    """OCR in-memory image bytes."""
    import cv2
    import numpy as np

    arr = np.frombuffer(data, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
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
    return _ocr_bgr(bgr)


def warmup() -> None:
    """Preload models at process start (optional)."""
    try:
        _load_converter()
    except Exception as exc:
        print(f"[OCR] warmup skipped: {exc}", flush=True)
