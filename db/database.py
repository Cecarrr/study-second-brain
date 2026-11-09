"""SQLite untuk tugas kuliah (data/app.db)."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta
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
CREATE TABLE IF NOT EXISTS trash (            -- tempat sampah: tugas dan catatan terhapus
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,                        -- task | note
    label TEXT NOT NULL,                       -- judul untuk ditampilkan
    payload TEXT NOT NULL,                     -- JSON: tugas = barisnya; catatan = {file, content}
    deleted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL);
"""

TRASH_DAYS = int(getattr(config, "TRASH_RETENTION_DAYS", 30))   # lama sampah disimpan sebelum dibuang


def norm(s: str | None) -> str:
    """Huruf kecil, tanpa tanda baca/spasi ganda: untuk membandingkan judul dan mata kuliah."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


_UNSET = object()   # pembeda "tidak diubah" dari None ("kosongkan")


class TaskConflict(Exception):
    """Perubahan akan membuat dua tugas pending dengan judul + tenggat sama."""

    def __init__(self, other: dict):
        super().__init__(f"tugas #{other['id']} sudah memakai judul dan tenggat itu")
        self.other = other


def title_matches(words: list[str], title: str) -> bool:
    """Semua kata kunci harus menjadi AWAL dari suatu kata di judul ('uas' cocok 'UAS Basis Data',
    tidak cocok 'Evaluasi'; 'lap' cocok 'Laporan'). Pencocokan substring sebelumnya salah di kasus pertama."""
    toks = norm(title).split()
    return all(any(t.startswith(w) for t in toks) for w in words)


class TaskDB:
    def __init__(self, path: str | Path | None = None, today_fn: Callable[[], date] | None = None):
        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.today = today_fn or date.today
        with closing(self._conn()) as c, c:
            c.executescript(SCHEMA)
            c.execute("DELETE FROM trash WHERE expires_at < ?", (self._now(),))   # buang yang kedaluwarsa

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

    def update(self, task_id: int, title=_UNSET, due_date=_UNSET, course=_UNSET, notes=_UNSET,
               status: str | None = None) -> tuple[dict, dict] | None:
        """Ubah field tugas. Argumen yang tidak diberikan tidak disentuh; None = kosongkan.
        Kembalikan (lama, baru) atau None bila id tidak ada. Raise TaskConflict bila hasilnya
        menduplikasi tugas pending lain (judul + tenggat sama)."""
        with closing(self._conn()) as c, c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None:
                return None
            old, new = dict(r), dict(r)
            for key, val in (("title", title), ("due_date", due_date), ("course", course),
                             ("notes", notes)):
                if val is not _UNSET:
                    new[key] = val
            if status in ("pending", "done") and status != old["status"]:
                new["status"] = status
                new["done_at"] = self._now() if status == "done" else None
            if new["status"] == "pending":
                for o in c.execute("SELECT * FROM tasks WHERE status='pending' AND id!=? "
                                   "AND IFNULL(due_date,'')=?", (task_id, new["due_date"] or "")):
                    if norm(o["title"]) == norm(new["title"]):
                        raise TaskConflict(dict(o))
            c.execute("UPDATE tasks SET title=?, due_date=?, course=?, notes=?, status=?, done_at=? "
                      "WHERE id=?", (new["title"], new["due_date"], new["course"], new["notes"],
                                     new["status"], new["done_at"], task_id))
            return old, new

    def delete(self, task_id: int) -> dict | None:
        """Pindahkan ke tempat sampah (bisa dipulihkan TRASH_DAYS hari). Kembalikan tugasnya,
        atau None bila id tidak ada."""
        with closing(self._conn()) as c, c:
            r = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if r is None:
                return None
            row = dict(r)
            self._trash_insert(c, "task", row["title"], row)
            c.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            return row

    # ------------------------------------------------------------ potret untuk /undo
    def dump_state(self) -> dict:
        with closing(self._conn()) as c:
            return {"tasks": [dict(r) for r in c.execute("SELECT * FROM tasks ORDER BY id")],
                    "trash": [dict(r) for r in c.execute("SELECT * FROM trash ORDER BY id")]}

    def load_state(self, state: dict) -> None:
        """Ganti isi tabel tugas dan sampah dengan potret `dump_state`."""
        with closing(self._conn()) as c, c:
            for table in ("tasks", "trash"):
                c.execute(f"DELETE FROM {table}")
                for row in state[table]:
                    cols = list(row)
                    c.execute(f"INSERT INTO {table}({','.join(cols)}) VALUES ({','.join(':' + k for k in cols)})", row)

    # ------------------------------------------------------------ tempat sampah
    def _trash_insert(self, c: sqlite3.Connection, kind: str, label: str, payload: dict) -> int:
        now = datetime.now()
        cur = c.execute(
            "INSERT INTO trash(kind, label, payload, deleted_at, expires_at) VALUES (?,?,?,?,?)",
            (kind, label, json.dumps(payload, ensure_ascii=False), now.isoformat(timespec="seconds"),
             (now + timedelta(days=TRASH_DAYS)).isoformat(timespec="seconds")))
        return cur.lastrowid

    def trash_put(self, kind: str, label: str, payload: dict) -> int:
        with closing(self._conn()) as c, c:
            return self._trash_insert(c, kind, label, payload)

    def trash_list(self) -> list[dict]:
        with closing(self._conn()) as c:
            return [dict(r) for r in c.execute(
                "SELECT id, kind, label, deleted_at, expires_at FROM trash "
                "WHERE expires_at >= ? ORDER BY id DESC", (self._now(),))]

    def trash_get(self, trash_id: int) -> dict | None:
        with closing(self._conn()) as c:
            r = c.execute("SELECT * FROM trash WHERE id=?", (trash_id,)).fetchone()
        if r is None:
            return None
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        return d

    def trash_remove(self, trash_id: int) -> None:
        with closing(self._conn()) as c, c:
            c.execute("DELETE FROM trash WHERE id=?", (trash_id,))

    def trash_empty(self) -> int:
        with closing(self._conn()) as c, c:
            return c.execute("DELETE FROM trash").rowcount

    def restore_task(self, trash_id: int) -> dict | None:
        """Pulihkan tugas dari sampah (id lama dipakai lagi bila masih bebas). None bila tidak ada."""
        item = self.trash_get(trash_id)
        if item is None or item["kind"] != "task":
            return None
        t = item["payload"]
        with closing(self._conn()) as c, c:
            taken = c.execute("SELECT 1 FROM tasks WHERE id=?", (t["id"],)).fetchone()
            cols = ["title", "due_date", "course", "notes", "status", "created_at", "done_at"]
            vals = [t.get(k) for k in cols]
            if taken:
                cur = c.execute(f"INSERT INTO tasks({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", vals)
                new_id = cur.lastrowid
            else:
                c.execute(f"INSERT INTO tasks(id,{','.join(cols)}) VALUES (?,{','.join('?' * len(cols))})",
                          [t["id"], *vals])
                new_id = t["id"]
            c.execute("DELETE FROM trash WHERE id=?", (trash_id,))
            return dict(c.execute("SELECT * FROM tasks WHERE id=?", (new_id,)).fetchone())

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

    def find(self, text: str, status: str = "pending") -> list[dict]:
        """Tugas dengan status tertentu yang judulnya cocok dengan `text` (lihat title_matches).
        Judul yang persis sama didahulukan."""
        q = norm(text)
        words = q.split()
        if not words:
            return []
        out = [r for r in self.list(status) if norm(r["title"]) == q or title_matches(words, r["title"])]
        exact = [r for r in out if norm(r["title"]) == q]
        return exact or out

    def find_pending(self, text: str) -> list[dict]:
        return self.find(text, "pending")

    def find_any(self, text: str) -> list[dict]:
        """Untuk ubah/hapus: tugas belum-selesai yang cocok; bila tidak ada, yang sudah selesai."""
        return self.find(text, "pending") or self.find(text, "done")
