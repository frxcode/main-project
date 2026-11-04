import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import claude_detect
import claude_ocr
import db
import ocr_engine
import qwen_client
import text_cache
import translit

app = FastAPI(title="MAN Checker API")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploaded_images"
STATIC_DIR = BASE_DIR / "static"
PROMPT_FILE = BASE_DIR / "prompt.txt"


def _load_env_file(path: Path):
    """Load KEY=VALUE pairs into os.environ without overriding existing vars."""
    if not path.exists():
        return
    try:
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except Exception as exc:
        print(f"[WARN] Failed to load env file {path}: {exc}", flush=True)


# API keys / secrets: ~/.env then backend/.env (never commit secrets)
_load_env_file(Path.home() / ".env")
_load_env_file(BASE_DIR / ".env")

SESSION_COOKIE = "session_token"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 днів
GMAIL_RE = re.compile(r"^[^\s@]+@gmail\.com$", re.IGNORECASE)

DEFAULT_PROMPT = claude_detect.DEEP_SYSTEM_PROMPT

AI_SYSTEM_PROMPT = DEFAULT_PROMPT

UPLOAD_DIR.mkdir(exist_ok=True)
db.init_db()
text_cache.init(UPLOAD_DIR)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

last_pi_sync = 0.0


@app.on_event("startup")
def reset_selected_student_on_startup():
    """Restart must not keep the previous student. The stand returns to standby."""
    db.set_active_student_id(None)
    print(
        f"[START] name_scan={claude_ocr.available()} "
        f"analyze={claude_detect.available()} fallback={qwen_client.available()}",
        flush=True,
    )


# ------------------------------- АВТОРИЗАЦІЯ -------------------------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations))
        return secrets.compare_digest(computed.hex(), digest)
    except Exception:
        return False


def get_teacher_for_token(token: Optional[str]):
    if not token:
        return None
    session = db.get_session(token)
    if not session:
        return None
    if time.time() - float(session["session_created"]) > SESSION_TTL:
        db.delete_session(token)
        return None
    return {"id": session["id"], "email": session["email"]}


def require_teacher(session_token: Optional[str] = Cookie(default=None)):
    teacher = get_teacher_for_token(session_token)
    if not teacher:
        raise HTTPException(status_code=401, detail="Не авторизовано")
    return teacher


def set_session_cookie(response: Response, token: str):
    response.set_cookie(SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/")


async def read_credentials(request: Request):
    email = password = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            email = body.get("email")
            password = body.get("password")
    except Exception:
        pass
    if email is None:
        try:
            form = await request.form()
            email = form.get("email")
            password = form.get("password")
        except Exception:
            pass
    return (email or "").strip().lower(), (password or "")


@app.post("/api/auth/register")
async def auth_register(request: Request, response: Response):
    email, password = await read_credentials(request)
    if not GMAIL_RE.match(email):
        raise HTTPException(status_code=400, detail="Введіть коректну адресу Gmail")
    if len(password) < 6:
        raise HTTPException(status_code=400, detail="Пароль має містити щонайменше 6 символів")
    if db.get_teacher_by_email(email):
        raise HTTPException(status_code=409, detail="Вчитель з такою поштою вже існує")

    is_first_teacher = db.count_teachers() == 0
    teacher_id = db.create_teacher(email, hash_password(password))
    if is_first_teacher:
        claimed = db.claim_orphan_students(teacher_id)
        if claimed:
            print(f"[AUTH] перший вчитель {email} отримав {claimed} учнів зі старої бази", flush=True)

    token = secrets.token_urlsafe(32)
    db.create_session(token, teacher_id)
    set_session_cookie(response, token)
    return {"status": "success", "email": email}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    email, password = await read_credentials(request)
    teacher = db.get_teacher_by_email(email)
    if not teacher or not verify_password(password, teacher["password_hash"]):
        raise HTTPException(status_code=401, detail="Невірна пошта або пароль")

    token = secrets.token_urlsafe(32)
    db.create_session(token, teacher["id"])
    set_session_cookie(response, token)
    return {"status": "success", "email": teacher["email"]}


@app.post("/api/auth/logout")
def auth_logout(response: Response, session_token: Optional[str] = Cookie(default=None)):
    if session_token:
        db.delete_session(session_token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "success"}


@app.get("/api/auth/me")
def auth_me(teacher=Depends(require_teacher)):
    return {"status": "success", "email": teacher["email"]}


# ------------------------------- ПРОМПТ -------------------------------
def load_prompt():
    global AI_SYSTEM_PROMPT
    if PROMPT_FILE.exists():
        try:
            text = PROMPT_FILE.read_text(encoding="utf-8").strip()
            if text:
                AI_SYSTEM_PROMPT = text
                return AI_SYSTEM_PROMPT
        except Exception:
            pass
    AI_SYSTEM_PROMPT = DEFAULT_PROMPT
    PROMPT_FILE.write_text(AI_SYSTEM_PROMPT, encoding="utf-8")
    return AI_SYSTEM_PROMPT


def save_prompt(new_prompt: str):
    global AI_SYSTEM_PROMPT
    text = (new_prompt or "").strip()
    if not text:
        raise ValueError("Prompt cannot be empty")
    AI_SYSTEM_PROMPT = text
    PROMPT_FILE.write_text(AI_SYSTEM_PROMPT, encoding="utf-8")
    return AI_SYSTEM_PROMPT


# ------------------------------- ДОПОМІЖНЕ -------------------------------
def student_dirs(student):
    base = UPLOAD_DIR / student["folder_name"]
    verifier_dir = base / "verifier"
    examples_dir = base / "examples"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    examples_dir.mkdir(parents=True, exist_ok=True)
    return verifier_dir, examples_dir


def own_student_or_404(student_id: int, teacher) -> dict:
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        raise HTTPException(status_code=404, detail="Учня не знайдено")
    return student


def full_name(student) -> str:
    return f"{student['first_name']} {student['last_name']}"


@app.get("/")
def read_root():
    if (STATIC_DIR / "index.html").exists():
        return FileResponse(STATIC_DIR / "index.html")
    return {"status": "ok", "message": "Static frontend not found"}


# ------------------------------- УЧНІ (тільки свої) -------------------------------
@app.get("/api/students")
def get_students(teacher=Depends(require_teacher)):
    students = db.list_students_for_teacher(teacher["id"])
    active_id = db.get_active_student_id()
    if active_id is not None and not any(s["id"] == active_id for s in students):
        active_id = None  # активний учень іншого вчителя — не показуємо
    return {"active_student_id": active_id, "students": students}


def latin_display_name(student) -> str:
    """Short Latin name for the 16-character LCD."""
    if not student:
        return ""
    first, last, _changed = translit.latinize_parts(student["first_name"], student["last_name"])
    return f"{last} {first}".strip()[:16]


def ensure_latin_name(first_name: str, last_name: str) -> tuple[str, str, bool]:
    """Transliterate a Cyrillic name with AI, then the local table."""
    first = db.normalize_name_part(first_name)
    last = db.normalize_name_part(last_name)
    if not (translit.has_cyrillic(first) or translit.has_cyrillic(last)):
        return first, last, False
    ai_name = ai_transliterate_name(first, last)
    if ai_name:
        return ai_name[0], ai_name[1], True
    latin_first, latin_last, changed = translit.latinize_parts(first, last)
    return latin_first, latin_last, changed


def ai_transliterate_name(first_name: str, last_name: str):
    """Ask Qwen for a Latin spelling. None when API is unavailable."""
    if not qwen_client.available():
        return None
    try:
        text = qwen_client.chat(
            [
                {
                    "role": "system",
                    "content": (
                        "Transliterate this personal name from Cyrillic into Latin ASCII letters. "
                        "Use a common English spelling. Return ONLY JSON: "
                        '{"first_name_en":"Ivan","last_name_en":"Petrenko"}. No markdown.'
                    ),
                },
                {
                    "role": "user",
                    "content": f"first_name: {first_name}\nlast_name: {last_name}",
                },
            ],
            temperature=0.0,
            timeout=30.0,
        )
        payload = _parse_json_response(text)
        first_en = db.normalize_name_part(payload.get("first_name_en", ""))
        last_en = db.normalize_name_part(payload.get("last_name_en", ""))
        if translit.is_latin_name(first_en) and translit.is_latin_name(last_en):
            return first_en, last_en
    except Exception as exc:
        print(f"[WARN] name transliteration AI failed: {exc}", flush=True)
    return None


@app.post("/api/students/add")
def add_student(first_name: str = Form(...), last_name: str = Form(...), teacher=Depends(require_teacher)):
    original_first = db.normalize_name_part(first_name)
    original_last = db.normalize_name_part(last_name)
    if not original_first or not original_last:
        raise HTTPException(status_code=400, detail="first_name and last_name are required")

    first, last, transliterated = ensure_latin_name(original_first, original_last)
    if not first or not last:
        raise HTTPException(status_code=400, detail="Could not transliterate the student name")

    try:
        student = db.create_student(teacher["id"], first, last)
    except db.DuplicateStudentNameError as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    student_dirs(student)
    original_name = f"{original_first} {original_last}".strip()
    stored_name = full_name(student)
    warning = None
    if transliterated and original_name.lower() != stored_name.lower():
        warning = (
            "Ім'я автоматично переведено латиницею для сумісності системи: "
            f"{original_name} → {stored_name}"
        )
    return {
        "status": "success",
        "student": student,
        "student_id": student["id"],
        "student_name": stored_name,
        "transliterated": bool(warning),
        "original_name": original_name,
        "warning": warning,
    }


@app.post("/api/students/select")
async def select_student(
    request: Request,
    student_id: Optional[int] = Form(None),
    student_name: Optional[str] = Form(None),
    teacher=Depends(require_teacher),
):
    resolved_id = student_id
    if resolved_id is None:
        try:
            body = await request.json()
            if isinstance(body, dict):
                resolved_id = body.get("student_id") or body.get("id")
                student_name = student_name or body.get("student_name")
        except Exception:
            pass

    if resolved_id is None and student_name:
        for s in db.list_students_for_teacher(teacher["id"]):
            if full_name(s).lower() == student_name.strip().lower():
                resolved_id = s["id"]
                break

    if resolved_id is None:
        return {"status": "error", "message": "student_id or student_name is required"}

    try:
        student = own_student_or_404(int(resolved_id), teacher)
    except (TypeError, ValueError):
        return {"status": "error", "message": "student not found"}
    except HTTPException:
        return {"status": "error", "message": "student not found"}

    db.set_active_student_id(student["id"])
    return {
        "status": "success",
        "student_id": student["id"],
        "student_name": full_name(student),
        "display_name": latin_display_name(student),
    }


@app.post("/api/students/clear")
def clear_selected_student(teacher=Depends(require_teacher)):
    """Teacher cleared the stand. The LCD returns to the standby message."""
    active_id = db.get_active_student_id()
    if active_id is not None:
        student = db.get_student_for_teacher(active_id, teacher["id"])
        if student or not db.get_student(active_id):
            db.set_active_student_id(None)
    return {"status": "success", "active_student_id": None, "display_name": ""}


@app.get("/api/students/{student_id}/files")
def get_student_files(student_id: int, teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"verifier": [], "examples": []}

    verifier_dir, examples_dir = student_dirs(student)
    folder = student["folder_name"]
    exts = (".png", ".jpg", ".jpeg")
    return {
        "verifier": [f"/uploads/{folder}/verifier/{f}" for f in sorted(os.listdir(verifier_dir), reverse=True) if f.lower().endswith(exts)],
        "examples": [f"/uploads/{folder}/examples/{f}" for f in sorted(os.listdir(examples_dir), reverse=True) if f.lower().endswith(exts)],
    }


@app.post("/api/students/{student_id}/upload")
async def upload_to_folder(
    student_id: int,
    folder: str = Form(...),
    file: UploadFile = File(...),
    teacher=Depends(require_teacher),
):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student or folder not in ("verifier", "examples"):
        return {"status": "error", "message": "student not found or invalid folder"}

    verifier_dir, examples_dir = student_dirs(student)
    target_dir = verifier_dir if folder == "verifier" else examples_dir
    contents = await file.read()
    if not contents:
        return {"status": "error", "message": "empty file"}

    save_path = target_dir / f"work_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.png"
    save_path.write_bytes(contents)
    return {"status": "success", "filename": save_path.name}


def _photo_file(student, photo_url: str, folder: Optional[str] = None):
    """Resolve a student photo from its public URL. Filename only, inside that folder."""
    filename = Path(str(photo_url or "")).name
    if not filename or filename in {".", ".."} or filename.startswith("."):
        return None
    verifier_dir, examples_dir = student_dirs(student)
    url = str(photo_url).replace("\\", "/")
    if folder is None:
        if "/examples/" in url:
            folder = "examples"
        elif "/verifier/" in url:
            folder = "verifier"
    if folder == "examples":
        return examples_dir / filename
    if folder == "verifier":
        return verifier_dir / filename
    return None


@app.post("/api/photo/move-to-examples")
def move_to_examples(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "student not found"}

    _verifier_dir, examples_dir = student_dirs(student)
    src = _photo_file(student, photo_url, "verifier")
    if src is None or not src.exists():
        return {"status": "error", "message": "source photo not found"}
    dst = examples_dir / src.name
    if dst.exists():
        dst = examples_dir / f"{src.stem}_{datetime.now().strftime('%H%M%S%f')}{src.suffix}"
    shutil.move(str(src), str(dst))
    return {"status": "success"}


@app.post("/api/photo/move-to-verifier")
def move_to_verifier(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "student not found"}

    verifier_dir, _examples_dir = student_dirs(student)
    src = _photo_file(student, photo_url, "examples")
    if src is None or not src.exists():
        return {"status": "error", "message": "source photo not found"}
    dst = verifier_dir / src.name
    if dst.exists():
        dst = verifier_dir / f"{src.stem}_{datetime.now().strftime('%H%M%S%f')}{src.suffix}"
    shutil.move(str(src), str(dst))
    return {"status": "success"}


@app.post("/api/photo/delete")
def delete_photo(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "student not found"}

    target = _photo_file(student, photo_url)
    if target is not None and target.exists():
        target.unlink()
        return {"status": "success"}

    filename = Path(str(photo_url or "")).name
    for sub_dir in student_dirs(student):
        p = sub_dir / filename
        if filename and p.exists():
            p.unlink()
            return {"status": "success"}
    return {"status": "error", "message": "photo not found"}


# ------------------------------- АНАЛІЗ -------------------------------
def _short_verdict(text: str, max_words: int = 8, max_chars: int = 64) -> str:
    """Keep explanation tiny for the web layout."""
    cleaned = " ".join(str(text or "").replace("\n", " ").split())
    if not cleaned:
        return ""
    words = cleaned.split(" ")
    if len(words) > max_words:
        cleaned = " ".join(words[:max_words])
    if len(cleaned) > max_chars:
        cleaned = cleaned[: max_chars - 1].rstrip(" ,.;:") + "…"
    return cleaned


def _empty_name_result(error: Optional[str] = None) -> dict:
    payload = {
        "found": False,
        "first_name": "",
        "last_name": "",
        "full_name": "",
        "first_name_en": "",
        "last_name_en": "",
        "display_name": "",
        "ocr_text": "",
    }
    if error:
        payload["error"] = error
    return payload


def _match_student_in_ocr_text(ocr_text: str) -> Optional[dict]:
    """Pick a database student whose name appears in OCR text."""
    text = (ocr_text or "").lower()
    if not text.strip():
        return None
    best = None
    best_score = 0
    for student in db.list_all_students():
        first = str(student.get("first_name") or "").strip()
        last = str(student.get("last_name") or "").strip()
        if not first or not last:
            continue
        latin_first, latin_last, _ = translit.latinize_parts(first, last)
        variants = {
            f"{first} {last}".lower(),
            f"{last} {first}".lower(),
            f"{latin_first} {latin_last}".lower(),
            f"{latin_last} {latin_first}".lower(),
            last.lower(),
            latin_last.lower(),
        }
        score = 0
        for variant in variants:
            if variant and variant in text:
                # Prefer full-name hits over surname-only.
                score = max(score, 3 if " " in variant else 1)
        if score > best_score:
            best_score = score
            best = student
    return best if best_score > 0 else None


def _recognized_from_student(student: dict, ocr_text: str = "") -> dict:
    first = str(student.get("first_name") or "").strip()
    last = str(student.get("last_name") or "").strip()
    latin_first, latin_last, _ = translit.latinize_parts(first, last)
    display = f"{latin_last} {latin_first}".strip()[:32]
    return {
        "found": True,
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}".strip(),
        "first_name_en": latin_first,
        "last_name_en": latin_last,
        "display_name": display,
        "ocr_text": ocr_text,
        "matched_from_db": True,
    }


def _parse_json_response(text_resp: str) -> dict:
    text_resp = (text_resp or "").strip()
    text_resp = re.sub(r"^```(?:json)?\s*", "", text_resp, flags=re.IGNORECASE)
    text_resp = re.sub(r"\s*```\s*$", "", text_resp, flags=re.IGNORECASE).strip()
    start, end = text_resp.find("{"), text_resp.rfind("}")
    if start >= 0 and end > start:
        text_resp = text_resp[start : end + 1]
    try:
        payload = json.loads(text_resp)
    except json.JSONDecodeError:
        try:
            payload = ast.literal_eval(text_resp)
        except (ValueError, SyntaxError):
            try:
                payload = json.loads(text_resp.replace("'", '"'))
            except Exception:
                return {}
    return payload if isinstance(payload, dict) else {}


def _sidecar_json_path(image_path: Path) -> Path:
    return image_path.with_suffix(".json")


PENDING_DIR = UPLOAD_DIR / "_pending"


def _pending_paths(student_id: int) -> tuple[Path, Path]:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    base = PENDING_DIR / str(int(student_id))
    return base.with_suffix(".png"), base.with_suffix(".json")


def _save_pass1_binding(student_id: int, image_bytes: bytes, recognized: dict) -> Path:
    """Pass 1: keep photo + bind NAME only (no homework OCR)."""
    img_path, side_path = _pending_paths(student_id)
    img_path.write_bytes(image_bytes)
    name_block = {
        "found": bool(recognized.get("found")),
        "first_name": str(recognized.get("first_name") or ""),
        "last_name": str(recognized.get("last_name") or ""),
        "full_name": str(recognized.get("full_name") or ""),
        "first_name_en": str(recognized.get("first_name_en") or ""),
        "last_name_en": str(recognized.get("last_name_en") or ""),
        "display_name": str(recognized.get("display_name") or ""),
    }
    payload = {
        "pass": 1,
        "student_id": int(student_id),
        "filename": img_path.name,
        "name": name_block,
        "bound_at": time.time(),
    }
    side_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[PASS1] name-only bound student={student_id} "
        f"name={name_block.get('display_name')!r} → {img_path.name}",
        flush=True,
    )
    return img_path


def _load_pass1_binding(student_id: int, max_age_s: float = 30 * 60) -> dict:
    """Load Pass-1 name/text binding for this student if still fresh."""
    _img_path, side_path = _pending_paths(student_id)
    if not side_path.exists():
        return {}
    try:
        data = json.loads(side_path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    bound_at = float(data.get("bound_at") or 0)
    if bound_at and (time.time() - bound_at) > max_age_s:
        return {}
    return data


def _clear_pass1_binding(student_id: int):
    img_path, side_path = _pending_paths(student_id)
    for p in (img_path, side_path):
        try:
            if p.exists():
                p.unlink()
        except Exception:
            pass


def _write_image_binding(image_path: Path, payload: dict):
    """Bind analysis/OCR fields to an image via sidecar JSON."""
    side = _sidecar_json_path(Path(image_path))
    existing = {}
    if side.exists():
        try:
            existing = json.loads(side.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except Exception:
            existing = {}
    existing.update(payload or {})
    existing.setdefault("filename", Path(image_path).name)
    side.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_bound_ocr_text(image_path: Path) -> str:
    """Text bound to an image (sidecar JSON), if previously saved."""
    side = _sidecar_json_path(Path(image_path))
    if not side.exists():
        return ""
    try:
        data = json.loads(side.read_text(encoding="utf-8"))
    except Exception:
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("ocr_text") or data.get("document_text") or "").strip()


def _scan_name(path: Path) -> dict:
    """Pass 1: name-only vision scan (no homework body OCR)."""
    if claude_ocr.available():
        return claude_ocr.scan_name(path)
    # Last resort: legacy OCR blob → try to match a name in DB (not used for AI %).
    try:
        text = ocr_engine.ocr_image(path)
    except Exception as exc:
        return _empty_name_result(str(exc))
    matched_name = _match_student_in_ocr_text(text)
    if matched_name:
        return _recognized_from_student(matched_name, ocr_text="")
    return _empty_name_result()


def _portfolio_images(student: dict, exclude_path: Optional[Path] = None, limit: int = 4) -> list[Path]:
    """Etalon + portfolio IMAGE paths for Pass-2 vision compare (not OCR text)."""
    verifier_dir, examples_dir = student_dirs(student)
    exclude = exclude_path.resolve() if exclude_path else None
    out: list[Path] = []

    def add_dir(folder: Path):
        if not folder.exists():
            return
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith((".png", ".jpg", ".jpeg")):
                continue
            img = folder / name
            if exclude and img.resolve() == exclude:
                continue
            out.append(img)
            if len(out) >= limit:
                return

    add_dir(examples_dir)
    if len(out) < limit:
        add_dir(verifier_dir)
    return out[:limit]


def _cache_lookup_from_image(path: Path) -> Optional[dict]:
    """Optional light OCR used ONLY as a content-hash cache key — never for scoring."""
    # Prefer previously bound normalized_content / ocr_text if present.
    bound = _read_bound_ocr_text(path)
    if bound:
        hit = text_cache.lookup(bound)
        if hit:
            return hit
    try:
        rough = ocr_engine.ocr_image(path)
    except Exception:
        return None
    if not rough or len(rough.strip()) < 40:
        return None
    return text_cache.lookup(rough)


def run_name_recognition(target_photo_path) -> dict:
    """Pi Pass 1: scan photo for NAME only → match student in DB."""
    scanned = _scan_name(Path(target_photo_path))
    if scanned.get("error") and not scanned.get("found"):
        return _empty_name_result(str(scanned.get("error")))

    name_blob = " ".join(
        [
            str(scanned.get("full_name") or ""),
            str(scanned.get("display_name") or ""),
            str(scanned.get("first_name") or ""),
            str(scanned.get("last_name") or ""),
            str(scanned.get("first_name_en") or ""),
            str(scanned.get("last_name_en") or ""),
        ]
    )
    matched = _match_student_in_ocr_text(name_blob)
    if matched:
        out = _recognized_from_student(matched, ocr_text="")
        out["document_text"] = ""
        return out

    if not scanned.get("found"):
        return _empty_name_result()

    first_en = str(scanned.get("first_name_en") or "").strip()
    last_en = str(scanned.get("last_name_en") or "").strip()
    display = str(scanned.get("display_name") or "").strip()
    if not display and (first_en or last_en):
        display = f"{last_en} {first_en}".strip()
    return {
        "found": True,
        "first_name": str(scanned.get("first_name") or "").strip(),
        "last_name": str(scanned.get("last_name") or "").strip(),
        "full_name": str(scanned.get("full_name") or "").strip(),
        "first_name_en": first_en,
        "last_name_en": last_en,
        "display_name": display,
        "ocr_text": "",
        "document_text": "",
        "matched_from_db": False,
    }


def run_ai_analysis(
    target_photo_path,
    examples_dir,
    student: Optional[dict] = None,
    preferred_text: Optional[str] = None,
):
    """Pass 2: homework IMAGE + etalon images → short verdict."""
    target = Path(target_photo_path)
    if not target.exists():
        return {"ai_percent": 0, "description": "Фото не знайдено", "ocr_text": ""}

    # Content-hash cache (same text already scored). OCR only as lookup key.
    cached = None
    if preferred_text:
        cached = text_cache.lookup(preferred_text)
    if not cached:
        cached = _cache_lookup_from_image(target)
    if cached:
        desc = _short_verdict(str(cached.get("description", "")))
        _write_image_binding(
            target,
            {
                "ai_percent": int(cached["ai_percent"]),
                "description": desc,
                "pass": 2,
                "cached_text": True,
                "bound_at": time.time(),
            },
        )
        return {
            "ai_percent": int(cached["ai_percent"]),
            "description": desc,
            "ocr_text": preferred_text or _read_bound_ocr_text(target) or "",
            "cached": True,
        }

    etalon_imgs: list[Path] = []
    if student:
        etalon_imgs = _portfolio_images(student, exclude_path=target, limit=4)
    elif examples_dir and Path(examples_dir).exists():
        for name in sorted(os.listdir(examples_dir)):
            if name.lower().endswith((".png", ".jpg", ".jpeg")):
                etalon_imgs.append(Path(examples_dir) / name)
            if len(etalon_imgs) >= 4:
                break

    print(f"[PASS2] image={target.name} refs={len(etalon_imgs)}", flush=True)
    if not claude_detect.available():
        return {
            "ai_percent": 0,
            "description": _short_verdict("Сервіс недоступний"),
            "ocr_text": "",
        }

    try:
        result = claude_detect.detect_image(
            target,
            etalon_images=etalon_imgs,
            system_prompt=load_prompt(),
            use_web_search=True,
            timeout=52.0,
        )
        pct = int(result.get("ai_percent", 0) or 0)
        desc = _short_verdict(str(result.get("description", "")))
        norm = str(result.get("normalized_content") or "").strip()
        if norm:
            text_cache.store(norm, pct, desc)
        _write_image_binding(
            target,
            {
                "ai_percent": pct,
                "description": desc,
                "normalized_content": norm,
                "ocr_text": norm,
                "document_text": norm,
                "pass": 2,
                "bound_at": time.time(),
            },
        )
        return {
            "ai_percent": pct,
            "description": desc,
            "ocr_text": norm,
            "cached": False,
        }
    except Exception as exc:
        print(f"[PASS2] analyze failed: {exc}", flush=True)
        return {
            "ai_percent": 0,
            "description": _short_verdict("Помилка аналізу"),
            "ocr_text": "",
        }


@app.post("/api/photo/analyze")
def analyze_single_photo(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "Учня не знайдено"}

    verifier_dir, examples_dir = student_dirs(student)
    target_photo = verifier_dir / Path(photo_url).name
    if not target_photo.exists():
        return {"status": "error", "message": "Фото не знайдено"}

    result = run_ai_analysis(target_photo, examples_dir, student=student)
    ai_percent = int(result.get("ai_percent", 0) or 0)
    description = str(result.get("description", "") or "")
    ocr_text = str(result.get("ocr_text", "") or "")
    timestamp = time.time()
    file_hash = target_photo.stem
    filename = target_photo.name

    verdict_payload = {
        "student_id": int(student["id"]),
        "student_name": full_name(student),
        "filename": filename,
        "file_hash": file_hash,
        "ai_percent": ai_percent,
        "description": description,
        "ocr_text": ocr_text,
        "document_text": ocr_text,
        "timestamp": timestamp,
    }
    (verifier_dir / f"{file_hash}.json").write_text(
        json.dumps(verdict_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    db.add_work(int(student["id"]), file_hash, filename, ai_percent, description, timestamp)
    db.set_active_student_id(None)
    result = {
        **result,
        "ai_percent": ai_percent,
        "description": description,
        "ocr_text": ocr_text,
        "timestamp": timestamp,
    }
    return {"status": "success", "result": result}


# ------------------------------- ПРОМПТ (API) -------------------------------
@app.get("/api/prompt")
def get_current_prompt(teacher=Depends(require_teacher)):
    return {"status": "success", "prompt": load_prompt()}


@app.post("/api/prompt")
async def set_current_prompt(request: Request, teacher=Depends(require_teacher)):
    try:
        body = await request.json()
    except Exception:
        body = {}
    prompt_text = body.get("prompt") if isinstance(body, dict) else None
    if prompt_text is None:
        try:
            form_data = await request.form()
            prompt_text = form_data.get("prompt")
        except Exception:
            prompt_text = None
    if not isinstance(prompt_text, str):
        raise HTTPException(status_code=400, detail="prompt is required")
    return {"status": "success", "prompt": save_prompt(prompt_text)}


# ------------------------------- RASPBERRY PI (без авторизації) -------------------------------
@app.get("/api/pi/sync")
def pi_sync():
    global last_pi_sync
    last_pi_sync = time.time()
    active_id = db.get_active_student_id()
    student = db.get_student(active_id) if active_id is not None else None
    students = db.list_all_students()
    return {
        "status": "ok",
        "active_student_id": active_id,
        "active_student_name": full_name(student) if student else "None",
        "display_name": latin_display_name(student),
        "students": [{"id": s["id"], "name": full_name(s)} for s in students],
    }


@app.get("/api/pi/status")
def pi_status():
    return {"status": "ok", "is_online": (time.time() - last_pi_sync) < 10}


@app.post("/api/pi/clear_session")
def pi_clear_session():
    """Скинути активну сесію стенду (без авто-вибору учня)."""
    global last_pi_sync
    last_pi_sync = time.time()
    db.set_active_student_id(None)
    return {"status": "ok", "active_student_id": None, "active_student_name": "None"}


def _display_name_latin(recognized: dict, student) -> str:
    display = (recognized.get("display_name") or "").strip()
    if not display:
        first_en = (recognized.get("first_name_en") or "").strip()
        last_en = (recognized.get("last_name_en") or "").strip()
        display = f"{last_en} {first_en}".strip()
    if not display and student:
        raw = full_name(student)
        ascii_only = "".join(ch if ord(ch) < 128 else "" for ch in raw).strip()
        display = " ".join(ascii_only.split()) or raw
    return display[:32]


@app.post("/api/pi/recognize_student")
async def recognize_student(file: UploadFile = File(...)):
    """Pi Pass 1: name-only vision scan → match student → LCD name.

    Does NOT OCR homework body and does NOT run AI %. Pass 2 analyzes the IMAGE.
    """
    global last_pi_sync
    last_pi_sync = time.time()

    contents = await file.read()
    if not contents:
        return {"status": "error", "message": "empty file"}

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    tmp_path = UPLOAD_DIR / f"_recognize_{int(time.time() * 1000)}.png"
    tmp_path.write_bytes(contents)
    try:
        recognized = run_name_recognition(tmp_path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass

    if recognized.get("error"):
        return {"status": "error", "message": recognized["error"], "recognized": recognized}
    if not recognized.get("found"):
        return {"status": "not_found", "message": "name not found on photo", "recognized": recognized}

    source_first = recognized.get("first_name") or recognized.get("first_name_en") or ""
    source_last = recognized.get("last_name") or recognized.get("last_name_en") or ""
    if translit.has_cyrillic(source_first) or translit.has_cyrillic(source_last):
        ai_name = ai_transliterate_name(source_first, source_last)
        if ai_name:
            recognized["first_name_en"], recognized["last_name_en"] = ai_name
            recognized["display_name"] = f"{ai_name[1]} {ai_name[0]}".strip()
    recognized = translit.prepare_for_match(recognized)
    student = db.find_student_by_parts(
        first_name=recognized.get("first_name_en"),
        last_name=recognized.get("last_name_en"),
        full_name=recognized.get("display_name"),
    ) or db.find_student_by_latin(
        recognized.get("first_name_en") or "",
        recognized.get("last_name_en") or "",
    )
    if not student:
        return {"status": "not_found", "message": "student not in database", "recognized": recognized}

    # Bind Pass-1 photo + name only (homework body is NOT OCR'd here).
    _save_pass1_binding(int(student["id"]), contents, recognized)
    db.set_active_student_id(student["id"])
    return {
        "status": "success",
        "student_id": student["id"],
        "student_name": full_name(student),
        "display_name": _display_name_latin(recognized, student),
        "recognized": recognized,
        "pass": 1,
    }


@app.post("/upload/")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    student_id: Optional[int] = Form(None),
):
    """Pi Pass 2 (second button): homework IMAGE + etalon images → short verdict.

    Compatible with existing Pi client (/upload/ + student_id).
    """
    if student_id is None and request.query_params.get("student_id"):
        try:
            student_id = int(request.query_params.get("student_id"))
        except ValueError:
            student_id = None

    if student_id is None:
        return {"status": "error", "message": "student_id is required"}

    student = db.get_student(int(student_id))
    if not student:
        return {"status": "error", "message": f"student_id {student_id} not found"}

    bound_id = int(student["id"])
    verifier_dir, examples_dir = student_dirs(student)
    contents = await file.read()
    if not contents:
        return {"status": "error", "message": "empty file"}

    file_hash = hashlib.md5(contents).hexdigest()
    cached = db.find_work_by_hash(bound_id, file_hash)
    if cached:
        db.set_active_student_id(None)
        _clear_pass1_binding(bound_id)
        cached_ocr = ""
        sidecar = verifier_dir / f"{file_hash}.json"
        if sidecar.exists():
            try:
                cached_ocr = str(json.loads(sidecar.read_text(encoding="utf-8")).get("ocr_text") or "")
            except Exception:
                cached_ocr = ""
        return {
            "status": "success",
            "student_id": bound_id,
            "student_name": full_name(student),
            "ai_percent": int(cached["ai_percent"]),
            "description": cached["description"],
            "ocr_text": cached_ocr,
            "cached": True,
            "pass": 2,
        }

    filename = f"{file_hash}.png"
    save_path = verifier_dir / filename
    save_path.write_bytes(contents)

    pending = _load_pass1_binding(bound_id)
    pending_name = pending.get("name") if isinstance(pending.get("name"), dict) else {}

    ai_result = run_ai_analysis(save_path, examples_dir, student=student)
    ai_percent = int(ai_result.get("ai_percent", 0))
    description = str(ai_result.get("description", ""))
    ocr_text = str(ai_result.get("ocr_text", "") or "")
    timestamp = time.time()

    verdict_payload = {
        "pass": 2,
        "student_id": bound_id,
        "student_name": full_name(student),
        "filename": filename,
        "file_hash": file_hash,
        "ai_percent": ai_percent,
        "description": description,
        "ocr_text": ocr_text,
        "document_text": ocr_text,
        "name": pending_name,
        "cached": bool(ai_result.get("cached")),
        "timestamp": timestamp,
    }
    _write_image_binding(save_path, verdict_payload)
    db.add_work(bound_id, file_hash, filename, ai_percent, description, timestamp)
    _clear_pass1_binding(bound_id)
    db.set_active_student_id(None)

    return {
        "status": "success",
        "student_id": bound_id,
        "student_name": full_name(student),
        "ai_percent": ai_percent,
        "description": description,
        "ocr_text": ocr_text,
        "cached": bool(ai_result.get("cached")),
        "pass": 2,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=4576)
