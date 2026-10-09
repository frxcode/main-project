import ast
import hashlib
import json
import os
import re
import secrets
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from typing import Optional

from fastapi import Cookie, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from google import genai
from google.genai import types

app = FastAPI(title="MAN AI Checker API")

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploaded_images"
DATA_FILE = BASE_DIR / "students.json"
STATIC_DIR = BASE_DIR / "static"
PROMPT_FILE = BASE_DIR / "prompt.txt"
AUTH_DB = BASE_DIR / "auth.db"

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
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

last_pi_sync = 0.0


# ------------------------------- АВТОРИЗАЦІЯ (SQLite) -------------------------------
def get_auth_db():
    conn = sqlite3.connect(str(AUTH_DB))
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    with get_auth_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS teachers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                created_at REAL NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                token TEXT PRIMARY KEY,
                teacher_id INTEGER NOT NULL,
                created_at REAL NOT NULL,
                FOREIGN KEY (teacher_id) REFERENCES teachers (id) ON DELETE CASCADE
            )
            """
        )
        conn.commit()


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iterations, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return secrets.compare_digest(computed.hex(), digest)
    except Exception:
        return False


def create_session(teacher_id: int) -> str:
    token = secrets.token_urlsafe(32)
    with get_auth_db() as conn:
        conn.execute(
            "INSERT INTO sessions (token, teacher_id, created_at) VALUES (?, ?, ?)",
            (token, teacher_id, time.time()),
        )
        conn.commit()
    return token


def get_teacher_for_token(token: Optional[str]):
    if not token:
        return None
    with get_auth_db() as conn:
        row = conn.execute(
            """
            SELECT t.id AS id, t.email AS email, s.created_at AS session_created
            FROM sessions s JOIN teachers t ON t.id = s.teacher_id
            WHERE s.token = ?
            """,
            (token,),
        ).fetchone()
    if not row:
        return None
    if time.time() - float(row["session_created"]) > SESSION_TTL:
        delete_session(token)
        return None
    return {"id": row["id"], "email": row["email"]}


def delete_session(token: Optional[str]):
    if not token:
        return
    with get_auth_db() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


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

    with get_auth_db() as conn:
        exists = conn.execute("SELECT id FROM teachers WHERE email = ?", (email,)).fetchone()
        if exists:
            raise HTTPException(status_code=409, detail="Вчитель з такою поштою вже існує")
        cur = conn.execute(
            "INSERT INTO teachers (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, hash_password(password), time.time()),
        )
        conn.commit()
        teacher_id = cur.lastrowid

    token = create_session(teacher_id)
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/"
    )
    return {"status": "success", "email": email}


@app.post("/api/auth/login")
async def auth_login(request: Request, response: Response):
    email, password = await read_credentials(request)
    with get_auth_db() as conn:
        row = conn.execute("SELECT * FROM teachers WHERE email = ?", (email,)).fetchone()
    if not row or not verify_password(password, row["password_hash"]):
        raise HTTPException(status_code=401, detail="Невірна пошта або пароль")

    token = create_session(row["id"])
    response.set_cookie(
        SESSION_COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax", path="/"
    )
    return {"status": "success", "email": row["email"]}


@app.post("/api/auth/logout")
def auth_logout(response: Response, session_token: Optional[str] = Cookie(default=None)):
    delete_session(session_token)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"status": "success"}


@app.get("/api/auth/me")
def auth_me(session_token: Optional[str] = Cookie(default=None)):
    teacher = get_teacher_for_token(session_token)
    if not teacher:
        raise HTTPException(status_code=401, detail="Не авторизовано")
    return {"status": "success", "email": teacher["email"]}


init_auth_db()


# ----------------------------------------------------------------------------------
def ensure_data_file():
    if not DATA_FILE.exists():
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"active_student_id": None, "students": []}, f, ensure_ascii=False, indent=2)


def load_data():
    ensure_data_file()
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        return {"active_student_id": None, "students": []}
    data.setdefault("active_student_id", None)
    data.setdefault("students", [])
    return data


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


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


def normalize_name(value):
    if value is None:
        return ""
    return str(value).strip().lower()


def find_student_by_id(data, student_id):
    if student_id is None:
        return None
    try:
        target_id = int(student_id)
    except (TypeError, ValueError):
        return None
    for student in data.get("students", []):
        if int(student.get("id", -1)) == target_id:
            return student
    return None


def find_student_by_name(data, student_name):
    target_name = normalize_name(student_name)
    if not target_name:
        return None
    for student in data.get("students", []):
        candidates = {
            normalize_name(student.get("first_name")),
            normalize_name(student.get("last_name")),
            normalize_name(f"{student.get('first_name', '')} {student.get('last_name', '')}"),
        }
        if target_name in candidates:
            return student
    return None


@app.get("/")
def read_root():
    if (STATIC_DIR / "index.html").exists():
        return FileResponse(STATIC_DIR / "index.html")
    return {"status": "ok", "message": "Static frontend not found"}


@app.get("/api/students")
def get_students():
    return load_data()


@app.post("/api/students/add")
def add_student(first_name: str = Form(...), last_name: str = Form(...)):
    data = load_data()
    first = (first_name or "").strip()
    last = (last_name or "").strip()
    if not first or not last:
        raise HTTPException(status_code=400, detail="first_name and last_name are required")

    existing_ids = [int(student.get("id", 0)) for student in data.get("students", [])]
    new_id = max(existing_ids, default=0) + 1
    student = {
        "id": new_id,
        "first_name": first,
        "last_name": last,
        "folder_name": f"{new_id}_{first}_{last}",
    }
    data.setdefault("students", []).append(student)
    if data.get("active_student_id") is None:
        data["active_student_id"] = new_id

    student_path = UPLOAD_DIR / student["folder_name"]
    (student_path / "verifier").mkdir(parents=True, exist_ok=True)
    (student_path / "examples").mkdir(parents=True, exist_ok=True)
    save_data(data)
    return {"status": "success", "student": student}


@app.post("/api/students/select")
async def select_student(request: Request, student_id: Optional[int] = Form(None), student_name: Optional[str] = Form(None)):
    data = load_data()
    resolved_student_id = student_id

    if resolved_student_id is None:
        try:
            body = await request.json()
            if isinstance(body, dict):
                resolved_student_id = body.get("student_id") or body.get("id")
                student_name = student_name or body.get("student_name")
        except Exception:
            resolved_student_id = None

    if resolved_student_id is None:
        try:
            form_data = await request.form()
            resolved_student_id = form_data.get("student_id")
            student_name = student_name or form_data.get("student_name")
        except Exception:
            resolved_student_id = None

    if resolved_student_id is None and student_name:
        student = find_student_by_name(data, student_name)
        if student:
            resolved_student_id = student.get("id")

    if resolved_student_id is None:
        return {"status": "error", "message": "student_id or student_name is required"}

    student = find_student_by_id(data, resolved_student_id)
    if not student:
        return {"status": "error", "message": "student not found"}

    data["active_student_id"] = int(student["id"])
    save_data(data)
    return {
        "status": "success",
        "student_id": student["id"],
        "student_name": f"{student['first_name']} {student['last_name']}",
    }


@app.get("/api/students/{student_id}/files")
def get_student_files(student_id: int):
    data = load_data()
    student = find_student_by_id(data, student_id)
    if not student:
        return {"verifier": [], "examples": []}

    student_path = UPLOAD_DIR / student["folder_name"]
    verifier_dir = student_path / "verifier"
    examples_dir = student_path / "examples"

    verifier_dir.mkdir(exist_ok=True)
    examples_dir.mkdir(exist_ok=True)

    verifier_photos = [
        f"/uploads/{student['folder_name']}/verifier/{filename}"
        for filename in sorted(os.listdir(verifier_dir), reverse=True)
        if filename.lower().endswith((".png", ".jpg", ".jpeg"))
    ]
    examples_photos = [
        f"/uploads/{student['folder_name']}/examples/{filename}"
        for filename in sorted(os.listdir(examples_dir), reverse=True)
        if filename.lower().endswith((".png", ".jpg", ".jpeg"))
    ]
    return {"verifier": verifier_photos, "examples": examples_photos}


@app.post("/api/students/{student_id}/upload")
async def upload_to_folder(student_id: int, folder: str = Form(...), file: UploadFile = File(...)):
    data = load_data()
    student = find_student_by_id(data, student_id)
    if not student or folder not in ["verifier", "examples"]:
        return {"status": "error", "message": "student not found or invalid folder"}

    target_dir = UPLOAD_DIR / student["folder_name"] / folder
    target_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    save_path = target_dir / f"work_{timestamp}.png"
    contents = await file.read()
    if not contents:
        return {"status": "error", "message": "empty file"}

    with open(save_path, "wb") as f:
        f.write(contents)

    return {"status": "success", "filename": save_path.name}


@app.post("/api/photo/move-to-examples")
def move_to_examples(student_id: int = Form(...), photo_url: str = Form(...)):
    data = load_data()
    student = find_student_by_id(data, student_id)
    if not student:
        return {"status": "error", "message": "student not found"}

    filename = Path(photo_url).name
    student_path = UPLOAD_DIR / student["folder_name"]
    src = student_path / "verifier" / filename
    dst = student_path / "examples" / filename

    if src.exists():
        shutil.move(str(src), str(dst))
        return {"status": "success"}
    return {"status": "error", "message": "source photo not found"}


@app.post("/api/photo/delete")
def delete_photo(student_id: int = Form(...), photo_url: str = Form(...)):
    data = load_data()
    student = find_student_by_id(data, student_id)
    if not student:
        return {"status": "error", "message": "student not found"}

    filename = Path(photo_url).name
    student_path = UPLOAD_DIR / student["folder_name"]
    for sub in ["verifier", "examples"]:
        p = student_path / sub / filename
        if p.exists():
            p.unlink()
            return {"status": "success"}
    return {"status": "error", "message": "photo not found"}


def run_ai_analysis(target_photo_path, examples_dir):
    example_photos = []
    if examples_dir.exists():
        example_photos = [
            examples_dir / filename
            for filename in sorted(os.listdir(examples_dir))
            if filename.lower().endswith((".png", ".jpg", ".jpeg"))
        ]

    prompt = load_prompt()

    contents = [prompt]
    if example_photos:
        contents.append("Еталонні роботи учня:")
        for ex in example_photos:
            try:
                with open(ex, "rb") as f:
                    contents.append(types.Part.from_bytes(data=f.read(), mime_type="image/png"))
            except Exception:
                continue
    else:
        contents.append("Увага: еталонних робіт немає, оцінюй за загальними критеріями.")

    contents.append("Робота для перевірки:")
    try:
        with open(target_photo_path, "rb") as f:
            contents.append(types.Part.from_bytes(data=f.read(), mime_type="image/png"))
    except Exception as exc:
        return {"ai_percent": 0, "description": f"Помилка читання фото: {str(exc)}"}

    try:
        api_key = os.getenv("GEMINI_API_KEY")
        ai_client = genai.Client(api_key=api_key) if api_key else genai.Client()
        response = ai_client.models.generate_content(
            model="gemini-3.5-flash-lite",
            contents=contents,
        )
        text_resp = (response.text or "").strip()
        if not text_resp:
            return {"ai_percent": 0, "description": "Gemini повернула порожню відповідь"}

        text_resp = re.sub(r"^```(?:json)?\s*", "", text_resp, flags=re.IGNORECASE)
        text_resp = re.sub(r"\s*```\s*$", "", text_resp, flags=re.IGNORECASE).strip()

        if "{" in text_resp and "}" in text_resp:
            start = text_resp.find("{")
            end = text_resp.rfind("}")
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

        return {
            "ai_percent": int(payload.get("ai_percent", 0)),
            "description": str(payload.get("description", "")),
        }
    except Exception as exc:
        return {"ai_percent": 0, "description": f"Помилка аналізу ШІ: {str(exc)}"}


@app.post("/api/photo/analyze")
def analyze_single_photo(student_id: int = Form(...), photo_url: str = Form(...)):
    data = load_data()
    student = find_student_by_id(data, student_id)
    if not student:
        return {"status": "error", "message": "Учня не знайдено"}

    filename = Path(photo_url).name
    target_photo = UPLOAD_DIR / student["folder_name"] / "verifier" / filename
    examples_dir = UPLOAD_DIR / student["folder_name"] / "examples"

    if not target_photo.exists():
        return {"status": "error", "message": "Фото не знайдено"}

    result = run_ai_analysis(target_photo, examples_dir)
    return {"status": "success", "result": result}


@app.get("/api/pi/sync")
def pi_sync():
    global last_pi_sync
    last_pi_sync = time.time()
    data = load_data()
    active_student_id = data.get("active_student_id")
    student = find_student_by_id(data, active_student_id)
    student_name = f"{student['first_name']} {student['last_name']}" if student else "None"
    return {"status": "ok", "active_student_name": student_name}


@app.get("/api/pi/status")
def pi_status():
    return {"status": "ok", "is_online": (time.time() - last_pi_sync) < 10}


@app.get("/api/prompt")
def get_current_prompt():
    return {"status": "success", "prompt": load_prompt()}


@app.post("/api/prompt")
async def set_current_prompt(request: Request):
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


@app.post("/upload/")
async def upload_image(request: Request, file: UploadFile = File(...), student_name: Optional[str] = Form(None), student_id: Optional[int] = Form(None)):
    data = load_data()

    if not student_name:
        student_name = request.query_params.get("student_name")
    if not student_name:
        try:
            form_data = await request.form()
            student_name = form_data.get("student_name")
            student_id = form_data.get("student_id")
        except Exception:
            pass

    if student_id is None and request.query_params.get("student_id"):
        try:
            student_id = int(request.query_params.get("student_id"))
        except ValueError:
            student_id = None

    if student_id is not None:
        student = find_student_by_id(data, student_id)
    elif student_name:
        student = find_student_by_name(data, student_name)
    else:
        student = None

    if not student:
        return {"status": "error", "message": "active student is not selected"}

    student_path = UPLOAD_DIR / student["folder_name"]
    verifier_dir = student_path / "verifier"
    examples_dir = student_path / "examples"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    examples_dir.mkdir(parents=True, exist_ok=True)

    contents = await file.read()
    if not contents:
        return {"status": "error", "message": "empty file"}

    file_hash = hashlib.md5(contents).hexdigest()
    for work in student.get("works", []):
        if work.get("hash") == file_hash or work.get("file_hash") == file_hash:
            sidecar_path = verifier_dir / f"{file_hash}.json"
            payload = None
            if sidecar_path.exists():
                try:
                    with open(sidecar_path, "r", encoding="utf-8") as f:
                        payload = json.load(f)
                except Exception:
                    payload = None
            if payload:
                return {
                    "status": "success",
                    "student_name": f"{student['first_name']} {student['last_name']}",
                    "ai_percent": int(payload.get("ai_percent", 0)),
                    "description": str(payload.get("description", "")),
                    "cached": True,
                }
            return {
                "status": "success",
                "student_name": f"{student['first_name']} {student['last_name']}",
                "ai_percent": int(work.get("ai_percent", 0)),
                "description": str(work.get("description", "")),
                "cached": True,
            }

    filename = f"{file_hash}.png"
    save_path = verifier_dir / filename
    with open(save_path, "wb") as f:
        f.write(contents)

    ai_result = run_ai_analysis(save_path, examples_dir)
    ai_percent = int(ai_result.get("ai_percent", 0))
    description = str(ai_result.get("description", ""))

    verdict_payload = {
        "student_name": f"{student['first_name']} {student['last_name']}",
        "filename": filename,
        "file_hash": file_hash,
        "ai_percent": ai_percent,
        "description": description,
        "timestamp": time.time(),
    }
    verdict_path = verifier_dir / f"{file_hash}.json"
    with open(verdict_path, "w", encoding="utf-8") as f:
        json.dump(verdict_payload, f, ensure_ascii=False, indent=2)

    student.setdefault("works", []).append({
        "hash": file_hash,
        "file_hash": file_hash,
        "filename": filename,
        "path": str(save_path),
        "ai_percent": ai_percent,
        "description": description,
        "timestamp": verdict_payload["timestamp"],
    })
    save_data(data)

    return {
        "status": "success",
        "student_name": f"{student['first_name']} {student['last_name']}",
        "ai_percent": ai_percent,
        "description": description,
        "cached": False,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
