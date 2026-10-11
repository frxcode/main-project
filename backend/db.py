"""SQLite-сховище: вчителі, сесії, учні, роботи, налаштування.

Одна база `app.db` поруч із server.py. При першому запуску автоматично
переносить старі дані зі `students.json` та `auth.db`, якщо вони є.
"""

import json
import sqlite3
import time
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "app.db"
LEGACY_STUDENTS_JSON = BASE_DIR / "students.json"
LEGACY_AUTH_DB = BASE_DIR / "auth.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS teachers (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at    REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    teacher_id INTEGER NOT NULL REFERENCES teachers(id) ON DELETE CASCADE,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS students (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    teacher_id  INTEGER REFERENCES teachers(id) ON DELETE SET NULL,
    first_name  TEXT NOT NULL,
    last_name   TEXT NOT NULL,
    folder_name TEXT UNIQUE NOT NULL,
    created_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_students_teacher ON students(teacher_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_students_fullname_unique
    ON students (lower(trim(first_name)), lower(trim(last_name)));
CREATE TABLE IF NOT EXISTS works (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id  INTEGER NOT NULL REFERENCES students(id) ON DELETE CASCADE,
    file_hash   TEXT NOT NULL,
    filename    TEXT NOT NULL,
    ai_percent  INTEGER NOT NULL DEFAULT 0,
    description TEXT NOT NULL DEFAULT '',
    timestamp   REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_works_student ON works(student_id);
CREATE INDEX IF NOT EXISTS idx_works_student_hash ON works(student_id, file_hash);
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


class DuplicateStudentNameError(ValueError):
    """Повне ім'я (first + last) уже зайняте в БД."""

    def __init__(self, first_name: str, last_name: str):
        self.first_name = first_name
        self.last_name = last_name
        super().__init__(
            f'Student "{first_name} {last_name}" already exists. '
            "Full name (first + last) must be unique."
        )


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        conn.commit()
    _ensure_fullname_unique_index()
    _migrate_legacy_auth()
    _migrate_legacy_students()


def normalize_name_part(value: str) -> str:
    """Прибрати зайві пробіли в частині імені."""
    return " ".join(str(value or "").strip().split())


def full_name_key(first_name: str, last_name: str) -> str:
    """Ключ унікальності: lower(first) + '|' + lower(last)."""
    return f"{normalize_name_part(first_name).lower()}|{normalize_name_part(last_name).lower()}"


def _ensure_fullname_unique_index():
    """Гарантувати UNIQUE на (first_name, last_name) навіть для старих app.db."""
    with connect() as conn:
        conn.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_students_fullname_unique
               ON students (lower(trim(first_name)), lower(trim(last_name)))"""
        )
        conn.commit()


def find_duplicate_full_name(first_name: str, last_name: str, exclude_id: Optional[int] = None):
    """Повертає існуючого учня з тим самим повним ім'ям, або None."""
    key = full_name_key(first_name, last_name)
    if key == "|":
        return None
    with connect() as conn:
        rows = conn.execute("SELECT * FROM students").fetchall()
        for r in rows:
            if exclude_id is not None and r["id"] == exclude_id:
                continue
            if full_name_key(r["first_name"], r["last_name"]) == key:
                return _row_to_student(r, _works_for(conn, r["id"]))
    return None


def clear_students_and_works():
    """Повне очищення учнів і робіт (вчителі/сесії лишаються)."""
    with connect() as conn:
        conn.execute("DELETE FROM works")
        conn.execute("DELETE FROM students")
        conn.commit()
    set_active_student_id(None)
    print("[DB] cleared all students and works", flush=True)


# ------------------------------- міграція -------------------------------
def _migrate_legacy_auth():
    """Переносить вчителів/сесії зі старої auth.db (одноразово)."""
    if not LEGACY_AUTH_DB.exists() or get_setting("legacy_auth_imported"):
        return
    try:
        legacy = sqlite3.connect(str(LEGACY_AUTH_DB))
        legacy.row_factory = sqlite3.Row
        teachers = legacy.execute("SELECT * FROM teachers").fetchall()
        with connect() as conn:
            for t in teachers:
                conn.execute(
                    "INSERT OR IGNORE INTO teachers (email, password_hash, created_at) VALUES (?, ?, ?)",
                    (t["email"], t["password_hash"], t["created_at"]),
                )
            conn.commit()
        legacy.close()
    except Exception as exc:  # стара база може бути порожня/битою — не критично
        print(f"[DB] legacy auth import skipped: {exc}", flush=True)
    set_setting("legacy_auth_imported", "1")


def _migrate_legacy_students():
    """Переносить учнів і роботи зі students.json (одноразово).

    Учні імпортуються без власника (teacher_id NULL) — їх отримає
    перший зареєстрований вчитель (див. claim_orphan_students).
    """
    if not LEGACY_STUDENTS_JSON.exists() or get_setting("legacy_students_imported"):
        return
    try:
        data = json.loads(LEGACY_STUDENTS_JSON.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[DB] legacy students import skipped: {exc}", flush=True)
        set_setting("legacy_students_imported", "1")
        return

    students = data.get("students", []) if isinstance(data, dict) else []
    with connect() as conn:
        for s in students:
            try:
                cur = conn.execute(
                    """INSERT OR IGNORE INTO students
                       (id, teacher_id, first_name, last_name, folder_name, created_at)
                       VALUES (?, NULL, ?, ?, ?, ?)""",
                    (
                        int(s.get("id")),
                        str(s.get("first_name", "")).strip(),
                        str(s.get("last_name", "")).strip(),
                        s.get("folder_name") or f"{s.get('id')}_{s.get('first_name')}_{s.get('last_name')}",
                        time.time(),
                    ),
                )
                student_id = int(s.get("id"))
                if cur.rowcount == 0:
                    continue
                for w in s.get("works", []) or []:
                    conn.execute(
                        """INSERT INTO works
                           (student_id, file_hash, filename, ai_percent, description, timestamp)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (
                            student_id,
                            w.get("file_hash") or w.get("hash") or "",
                            w.get("filename") or f"{w.get('file_hash') or w.get('hash')}.png",
                            int(w.get("ai_percent", 0) or 0),
                            str(w.get("description", "") or ""),
                            float(w.get("timestamp") or time.time()),
                        ),
                    )
            except Exception as exc:
                print(f"[DB] skip legacy student {s!r}: {exc}", flush=True)
        conn.commit()

    active = data.get("active_student_id") if isinstance(data, dict) else None
    if active is not None:
        set_setting("active_student_id", str(active))
    set_setting("legacy_students_imported", "1")
    print(f"[DB] imported {len(students)} legacy students", flush=True)


# ------------------------------- settings -------------------------------
def get_setting(key: str) -> Optional[str]:
    with connect() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: Optional[str]):
    with connect() as conn:
        conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def get_active_student_id() -> Optional[int]:
    value = get_setting("active_student_id")
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def set_active_student_id(student_id: Optional[int]):
    set_setting("active_student_id", str(student_id) if student_id is not None else None)


# ------------------------------- teachers -------------------------------
def get_teacher_by_email(email: str):
    with connect() as conn:
        row = conn.execute("SELECT * FROM teachers WHERE email = ?", (email,)).fetchone()
    return dict(row) if row else None


def create_teacher(email: str, password_hash: str) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO teachers (email, password_hash, created_at) VALUES (?, ?, ?)",
            (email, password_hash, time.time()),
        )
        conn.commit()
        return cur.lastrowid


def count_teachers() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM teachers").fetchone()[0]


def claim_orphan_students(teacher_id: int) -> int:
    """Віддає учнів без власника (зі старого students.json) вчителю."""
    with connect() as conn:
        cur = conn.execute("UPDATE students SET teacher_id = ? WHERE teacher_id IS NULL", (teacher_id,))
        conn.commit()
        return cur.rowcount


# ------------------------------- sessions -------------------------------
def create_session(token: str, teacher_id: int):
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, teacher_id, created_at) VALUES (?, ?, ?)",
            (token, teacher_id, time.time()),
        )
        conn.commit()


def get_session(token: str):
    with connect() as conn:
        row = conn.execute(
            """SELECT t.id AS id, t.email AS email, s.created_at AS session_created
               FROM sessions s JOIN teachers t ON t.id = s.teacher_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    return dict(row) if row else None


def delete_session(token: str):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()


# ------------------------------- students -------------------------------
def _row_to_student(row, works=None) -> dict:
    return {
        "id": row["id"],
        "teacher_id": row["teacher_id"],
        "first_name": row["first_name"],
        "last_name": row["last_name"],
        "folder_name": row["folder_name"],
        "works": works or [],
    }


def _works_for(conn, student_id: int) -> list:
    rows = conn.execute(
        "SELECT * FROM works WHERE student_id = ? ORDER BY timestamp ASC, id ASC",
        (student_id,),
    ).fetchall()
    return [
        {
            "hash": r["file_hash"],
            "file_hash": r["file_hash"],
            "filename": r["filename"],
            "ai_percent": r["ai_percent"],
            "description": r["description"],
            "timestamp": r["timestamp"],
        }
        for r in rows
    ]


def list_students_for_teacher(teacher_id: int) -> list:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM students WHERE teacher_id = ? ORDER BY id ASC", (teacher_id,)
        ).fetchall()
        return [_row_to_student(r, _works_for(conn, r["id"])) for r in rows]


def list_all_students() -> list:
    with connect() as conn:
        rows = conn.execute("SELECT * FROM students ORDER BY id ASC").fetchall()
        return [_row_to_student(r, _works_for(conn, r["id"])) for r in rows]


def get_student(student_id: int):
    with connect() as conn:
        row = conn.execute("SELECT * FROM students WHERE id = ?", (student_id,)).fetchone()
        return _row_to_student(row, _works_for(conn, row["id"])) if row else None


def get_student_for_teacher(student_id: int, teacher_id: int):
    student = get_student(student_id)
    if student and student["teacher_id"] == teacher_id:
        return student
    return None


def find_student_by_name(name: str, prefer_id: Optional[int] = None):
    """Пошук за ім'ям / прізвищем / повним ім'ям (для Raspberry Pi).

    Якщо збігів кілька — перевага активному учню (prefer_id).
    """
    target = (name or "").strip().lower()
    if not target:
        return None
    with connect() as conn:
        rows = conn.execute("SELECT * FROM students").fetchall()
        matches = []
        for r in rows:
            first = r["first_name"].strip().lower()
            last = r["last_name"].strip().lower()
            candidates = {
                first,
                last,
                f"{first} {last}",
                f"{last} {first}",
            }
            if target in candidates:
                matches.append(r)
        if not matches:
            return None
        chosen = matches[0]
        if prefer_id is not None:
            for r in matches:
                if r["id"] == prefer_id:
                    chosen = r
                    break
        return _row_to_student(chosen, _works_for(conn, chosen["id"]))


def find_student_by_exact_full_name(first_name: str, last_name: str):
    """Точний збіг повного імені (first+last або last+first), без часткових збігів."""
    first = normalize_name_part(first_name)
    last = normalize_name_part(last_name)
    if not first or not last:
        return None
    target_keys = {
        full_name_key(first, last),
        full_name_key(last, first),
    }
    with connect() as conn:
        rows = conn.execute("SELECT * FROM students").fetchall()
        for r in rows:
            if full_name_key(r["first_name"], r["last_name"]) in target_keys:
                return _row_to_student(r, _works_for(conn, r["id"]))
    return None


def find_student_by_parts(
    first_name: Optional[str] = None,
    last_name: Optional[str] = None,
    full_name: Optional[str] = None,
    prefer_id: Optional[int] = None,
):
    """Пошук учня з AI-розпізнавання: спочатку точне повне ім'я, без авто-створення."""
    first = normalize_name_part(first_name or "")
    last = normalize_name_part(last_name or "")
    if first and last:
        exact = find_student_by_exact_full_name(first, last)
        if exact:
            return exact

    queries = []
    if full_name and str(full_name).strip():
        queries.append(normalize_name_part(str(full_name)))
    if first and last:
        queries.extend([f"{first} {last}", f"{last} {first}"])

    # Частковий збіг лише якщо AI дав лише одну частину
    if first and not last:
        queries.append(first)
    if last and not first:
        queries.append(last)

    seen = set()
    for q in queries:
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        student = find_student_by_name(q, prefer_id=prefer_id)
        if student:
            return student
    return None


def create_student(teacher_id: int, first_name: str, last_name: str) -> dict:
    first = normalize_name_part(first_name)
    last = normalize_name_part(last_name)
    if not first or not last:
        raise ValueError("first_name and last_name are required")

    existing = find_duplicate_full_name(first, last)
    if existing:
        raise DuplicateStudentNameError(first, last)

    try:
        with connect() as conn:
            cur = conn.execute(
                """INSERT INTO students (teacher_id, first_name, last_name, folder_name, created_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (teacher_id, first, last, f"pending_{time.time_ns()}", time.time()),
            )
            student_id = cur.lastrowid
            folder_name = f"{student_id}_{first}_{last}"
            conn.execute(
                "UPDATE students SET folder_name = ? WHERE id = ?",
                (folder_name, student_id),
            )
            conn.commit()
    except sqlite3.IntegrityError as exc:
        # унікальний індекс / гонка паралельних INSERT
        if find_duplicate_full_name(first, last):
            raise DuplicateStudentNameError(first, last) from exc
        raise
    return get_student(student_id)


def add_work(student_id: int, file_hash: str, filename: str, ai_percent: int, description: str, timestamp: float):
    with connect() as conn:
        conn.execute(
            """INSERT INTO works (student_id, file_hash, filename, ai_percent, description, timestamp)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (student_id, file_hash, filename, int(ai_percent), description, float(timestamp)),
        )
        conn.commit()


def find_work_by_hash(student_id: int, file_hash: str):
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM works WHERE student_id = ? AND file_hash = ? ORDER BY id DESC LIMIT 1",
            (student_id, file_hash),
        ).fetchone()
    return dict(row) if row else None
