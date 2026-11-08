"""SQLite untuk tugas kuliah (data/app.db)."""
from __future__ import annotations

import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    due_date TEXT,                 -- YYYY-MM-DD atau NULL
    course TEXT,
    notes TEXT,
    status TEXT NOT NULL DEFAULT 'pending',   -- pending | done
    created_at TEXT NOT NULL,
    done_at TEXT);
CREATE INDEX IF NOT EXISTS idx_tasks_status_due ON tasks(status, due_date);
"""


def norm(s: str | None) -> str:
    """Huruf kecil, tanpa tanda baca/spasi ganda: untuk membandingkan judul dan mata kuliah."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


class TaskDB:
    def __init__(self, path: str | Path | None = None, today_fn: Callable[[], date] | None = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.today = today_fn or date.today
        with closing(self._conn()) as c, c:
            c.executescript(SCHEMA)

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self.path))
        c.row_factory = sqlite3.Row
        return c

    def _now(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    # ------------------------------------------------------------ tulis
    def add(self, title: str, due_date: str | None = None, course: str | None = None,
            notes: str | None = None) -> tuple[dict, bool]:
        """Tambah tugas. Kembalikan (tugas, dibuat_baru). Duplikat (judul + tenggat sama,
        masih pending) tidak dibuat ulang."""
        with closing(self._conn()) as c, c:
            for r in c.execute("SELECT * FROM tasks WHERE status='pending' "
                               "AND IFNULL(due_date,'')=?", (due_date or "",)):
                if norm(r["title"]) == norm(title):
                    return dict(r), False
            cur = c.execute(
                "INSERT INTO tasks(title, due_date, course, notes, created_at) VALUES (?,?,?,?,?)",
                (title, due_date, course, notes, self._now()))
            return dict(c.execute("SELECT * FROM tasks WHERE id=?", (cur.lastrowid,)).fetchone()), True

    def complete(self, task_id: int) -> tuple[dict, bool] | None:
        """Tandai selesai. Kembalikan (tugas, sudah_selesai_sebelumnya) atau None bila id tidak ada."""
        with closing(self._conn()) as c, c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None:
                return None
            if r["status"] == "done":
                return dict(r), True
            c.execute("UPDATE tasks SET status='done', done_at=? WHERE id=?", (self._now(), task_id))
            return dict(c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()), False

    # ------------------------------------------------------------ baca
    def get(self, task_id: int) -> dict | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return dict(r) if r else None

    def list(self, status: str = "pending", due_within_days: int | None = None,
             course: str | None = None) -> list[dict]:
        q, args = "SELECT * FROM tasks", []
        where = []
        if status in ("pending", "done"):
            where.append("status=?")
            args.append(status)
        if due_within_days is not None:
            limit = self.today().toordinal() + int(due_within_days)
            where.append("due_date IS NOT NULL AND due_date<=?")
            args.append(date.fromordinal(limit).isoformat())
        if where:
            q += " WHERE " + " AND ".join(where)
        q += " ORDER BY done_at DESC, id DESC" if status == "done" else \
            " ORDER BY due_date IS NULL, due_date, id"
        with closing(self._conn()) as c:
            rows = [dict(r) for r in c.execute(q, args)]
        if course:
            want = norm(course)
            rows = [r for r in rows if norm(r["course"]) == want or want in norm(r["course"])]
        return rows

    def find_pending(self, text: str) -> list[dict]:
        """Tugas pending yang judulnya cocok dengan `text` (semua kata kunci ada di judul)."""
        words = norm(text).split()
        if not words:
            return []
        out = []
        for r in self.list("pending"):
            t = norm(r["title"])
            if t == norm(text) or all(w in t for w in words):
                out.append(r)
        exact = [r for r in out if norm(r["title"]) == norm(text)]
        return exact or out
