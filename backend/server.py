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
from google import genai
from google.genai import types

import db

app = FastAPI(title="MAN AI Checker API")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploaded_images"
STATIC_DIR = BASE_DIR / "static"
PROMPT_FILE = BASE_DIR / "prompt.txt"

SESSION_COOKIE = "session_token"
SESSION_TTL = 60 * 60 * 24 * 30  # 30 днів
GMAIL_RE = re.compile(r"^[^\s@]+@gmail\.com$", re.IGNORECASE)

DEFAULT_PROMPT = (
    "Ти — провідний експерт із цифрової криміналістики, лінгвістики та виявлення академічного плагіату. "
    "Проведи глибокий аналіз учнівської роботи, зробленої від руки або переписаної з ШІ, і визнач, чи була робота згенерована нейромережею. "
    "Не вважай, що написаний від руки текст автоматично є доведенням самостійності: учні можуть переписувати готові відповіді з телефона або з ШІ. "
    "Аналізуй словниковий запас, синтаксис, довжину речень, логіку аргументації, стилістичну однорідність та наявність ознак генерації. "
    "Порівнюй з характерними ознаками людського письма та ознаками LLM-рефератів. "
    "Оціни ймовірність використання ШІ у відсотках від 0 до 100. "
    "Пиши коротко, чітко і структуровано, без води. Опис має бути 1-2 речення або 2-3 короткі тезові фрази. "
    "Відповідь має бути тільки JSON у форматі: {\"ai_percent\": 15, \"description\": \"Короткий аргументований вердикт українською мовою\"}"
)

AI_SYSTEM_PROMPT = DEFAULT_PROMPT

UPLOAD_DIR.mkdir(exist_ok=True)
db.init_db()
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

last_pi_sync = 0.0


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


@app.post("/api/students/add")
def add_student(first_name: str = Form(...), last_name: str = Form(...), teacher=Depends(require_teacher)):
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first or not last:
        raise HTTPException(status_code=400, detail="first_name and last_name are required")

    student = db.create_student(teacher["id"], first, last)
    student_dirs(student)
    if db.get_active_student_id() is None:
        db.set_active_student_id(student["id"])
    return {"status": "success", "student": student}


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
    return {"status": "success", "student_id": student["id"], "student_name": full_name(student)}


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


@app.post("/api/photo/move-to-examples")
def move_to_examples(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "student not found"}

    verifier_dir, examples_dir = student_dirs(student)
    filename = Path(photo_url).name
    src, dst = verifier_dir / filename, examples_dir / filename
    if src.exists():
        shutil.move(str(src), str(dst))
        return {"status": "success"}
    return {"status": "error", "message": "source photo not found"}


@app.post("/api/photo/delete")
def delete_photo(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "student not found"}

    filename = Path(photo_url).name
    for sub_dir in student_dirs(student):
        p = sub_dir / filename
        if p.exists():
            p.unlink()
            return {"status": "success"}
    return {"status": "error", "message": "photo not found"}


# ------------------------------- АНАЛІЗ ШІ -------------------------------
def run_ai_analysis(target_photo_path, examples_dir):
    example_photos = []
    if examples_dir.exists():
        example_photos = [
            examples_dir / filename
            for filename in sorted(os.listdir(examples_dir))
            if filename.lower().endswith((".png", ".jpg", ".jpeg"))
        ]

    contents = [load_prompt()]
    if example_photos:
        contents.append("Еталонні роботи учня:")
        for ex in example_photos:
            try:
                contents.append(types.Part.from_bytes(data=ex.read_bytes(), mime_type="image/png"))
            except Exception:
                continue
    else:
        contents.append("Увага: еталонних робіт немає, оцінюй за загальними критеріями.")

    contents.append("Робота для перевірки:")
    try:
        contents.append(types.Part.from_bytes(data=Path(target_photo_path).read_bytes(), mime_type="image/png"))
    except Exception as exc:
        return {"ai_percent": 0, "description": f"Помилка читання фото: {exc}"}

    try:
        api_key = os.getenv("GEMINI_API_KEY")
        ai_client = genai.Client(api_key=api_key) if api_key else genai.Client()
        response = ai_client.models.generate_content(model="gemini-3.5-flash-lite", contents=contents)
        text_resp = (response.text or "").strip()
        if not text_resp:
            return {"ai_percent": 0, "description": "Gemini повернула порожню відповідь"}

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
                    payload = {"ai_percent": 0, "description": text_resp}

        if not isinstance(payload, dict):
            payload = {"ai_percent": 0, "description": str(payload)}

        return {"ai_percent": int(payload.get("ai_percent", 0)), "description": str(payload.get("description", ""))}
    except Exception as exc:
        return {"ai_percent": 0, "description": f"Помилка аналізу ШІ: {exc}"}


@app.post("/api/photo/analyze")
def analyze_single_photo(student_id: int = Form(...), photo_url: str = Form(...), teacher=Depends(require_teacher)):
    student = db.get_student_for_teacher(student_id, teacher["id"])
    if not student:
        return {"status": "error", "message": "Учня не знайдено"}

    verifier_dir, examples_dir = student_dirs(student)
    target_photo = verifier_dir / Path(photo_url).name
    if not target_photo.exists():
        return {"status": "error", "message": "Фото не знайдено"}

    return {"status": "success", "result": run_ai_analysis(target_photo, examples_dir)}


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
    return {"status": "ok", "active_student_name": full_name(student) if student else "None"}


@app.get("/api/pi/status")
def pi_status():
    return {"status": "ok", "is_online": (time.time() - last_pi_sync) < 10}


@app.post("/upload/")
async def upload_image(
    request: Request,
    file: UploadFile = File(...),
    student_name: Optional[str] = Form(None),
    student_id: Optional[int] = Form(None),
):
    if not student_name:
        student_name = request.query_params.get("student_name")
    if student_id is None and request.query_params.get("student_id"):
        try:
            student_id = int(request.query_params.get("student_id"))
        except ValueError:
            student_id = None

    active_id = db.get_active_student_id()
    if student_id is not None:
        student = db.get_student(student_id)
    elif student_name:
        student = db.find_student_by_name(student_name, prefer_id=active_id)
    elif active_id is not None:
        student = db.get_student(active_id)
    else:
        student = None

    if not student:
        return {"status": "error", "message": "active student is not selected"}

    verifier_dir, examples_dir = student_dirs(student)
    contents = await file.read()
    if not contents:
        return {"status": "error", "message": "empty file"}

    file_hash = hashlib.md5(contents).hexdigest()
    cached = db.find_work_by_hash(student["id"], file_hash)
    if cached:
        return {
            "status": "success",
            "student_name": full_name(student),
            "ai_percent": int(cached["ai_percent"]),
            "description": cached["description"],
            "cached": True,
        }

    filename = f"{file_hash}.png"
    save_path = verifier_dir / filename
    save_path.write_bytes(contents)

    ai_result = run_ai_analysis(save_path, examples_dir)
    ai_percent = int(ai_result.get("ai_percent", 0))
    description = str(ai_result.get("description", ""))
    timestamp = time.time()

    verdict_payload = {
        "student_name": full_name(student),
        "filename": filename,
        "file_hash": file_hash,
        "ai_percent": ai_percent,
        "description": description,
        "timestamp": timestamp,
    }
    (verifier_dir / f"{file_hash}.json").write_text(json.dumps(verdict_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    db.add_work(student["id"], file_hash, filename, ai_percent, description, timestamp)

    return {
        "status": "success",
        "student_name": full_name(student),
        "ai_percent": ai_percent,
        "description": description,
        "cached": False,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
