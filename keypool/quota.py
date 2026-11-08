"""Penghitung request per kuota (project, model, jenis) + rate limiter + status blokir.

Disimpan di SQLite supaya status "habis" bertahan antar restart. Jendela per-menit
(RPM/TPM) cukup di memori. Counter lokal hanya perkiraan; jawaban server (429)
selalu menang, lihat llm.py.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from collections import defaultdict, deque
from datetime import date, datetime, timedelta, timezone, time as dtime
from pathlib import Path

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None

# Cadangan bila database zona waktu tidak ada (di Windows: pip install tzdata).
_FALLBACK_TZ = {
    "America/Los_Angeles": timezone(timedelta(hours=-8)),
    "Asia/Jakarta": timezone(timedelta(hours=7)),
    "UTC": timezone.utc,
}


def get_tz(name: str):
    if ZoneInfo is not None:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    return _FALLBACK_TZ.get(name, timezone.utc)


def day_key(ts: float, tzname: str) -> str:
    return datetime.fromtimestamp(ts, get_tz(tzname)).strftime("%Y-%m-%d")


def next_reset(ts: float, tzname: str) -> float:
    tz = get_tz(tzname)
    d = datetime.fromtimestamp(ts, tz).date() + timedelta(days=1)
    return datetime.combine(d, dtime(0), tzinfo=tz).timestamp()


def fmt_ts(ts: float) -> str:
    return datetime.fromtimestamp(ts, get_tz("Asia/Jakarta")).strftime("%d %b %H:%M WIB")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS usage(
  qkey TEXT NOT NULL, day TEXT NOT NULL,
  requests INTEGER NOT NULL DEFAULT 0, tokens INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY(qkey, day));
CREATE TABLE IF NOT EXISTS blocked(qkey TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT);
CREATE TABLE IF NOT EXISTS disabled(sid TEXT PRIMARY KEY, until REAL NOT NULL, reason TEXT);
"""


class UsageStore:
    def __init__(self, path: str = ":memory:", clock=time.time):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript(_SCHEMA)
        self.clock = clock
        self._lock = threading.RLock()
        self._win = defaultdict(deque)       # qkey -> deque[(ts, count, tokens)]
        self.consec429 = defaultdict(int)    # qkey -> 429 beruntun tanpa kejelasan
        self.fails = defaultdict(int)        # sid  -> error transient beruntun

    # ---------- blokir / nonaktif ----------

    def _until(self, table, col, key, now):
        row = self.db.execute(f"SELECT until, reason FROM {table} WHERE {col}=?", (key,)).fetchone()
        if not row:
            return None
        if row[0] <= now:
            self.db.execute(f"DELETE FROM {table} WHERE {col}=?", (key,))
            self.db.commit()
            return None
        return row

    def block(self, qkey, until, reason):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO blocked VALUES(?,?,?)", (qkey, until, reason))
            self.db.commit()

    def disable(self, sid, until, reason):
        with self._lock:
            self.db.execute("INSERT OR REPLACE INTO disabled VALUES(?,?,?)", (sid, until, reason))
            self.db.commit()

    def clear_blocks(self):
        with self._lock:
            self.db.execute("DELETE FROM blocked")
            self.db.execute("DELETE FROM disabled")
            self.db.commit()
            self.consec429.clear()
            self.fails.clear()

    # ---------- hitungan ----------

    def used_today(self, qkey, tzname, now=None):
        now = self.clock() if now is None else now
        row = self.db.execute("SELECT requests, tokens FROM usage WHERE qkey=? AND day=?",
                              (qkey, day_key(now, tzname))).fetchone()
        return (row[0], row[1]) if row else (0, 0)

    def record(self, slot, tokens=0, count=1, now=None):
        now = self.clock() if now is None else now
        with self._lock:
            self.db.execute(
                "INSERT INTO usage(qkey, day, requests, tokens) VALUES(?,?,?,?) "
                "ON CONFLICT(qkey, day) DO UPDATE SET requests=requests+excluded.requests, "
                "tokens=tokens+excluded.tokens",
                (slot.qkey, day_key(now, slot.reset_tz), count, tokens))
            self.db.commit()
            self._win[slot.qkey].append((now, count, tokens))

    def _window(self, qkey, now):
        w = self._win[qkey]
        while w and w[0][0] <= now - 60:
            w.popleft()
        return w

    @staticmethod
    def _wait_for_room(w, now, field_idx, total, cap):
        """Detik sampai jendela 60 dtk punya ruang untuk satu permintaan lagi."""
        need = total - cap + 1
        acc = 0
        for entry in w:
            acc += entry[field_idx]
            if acc >= need:
                return max(entry[0] + 60 - now, 0.1)
        return 1.0

    # ---------- gerbang ----------

    def check(self, slot, safety, now=None):
        """-> (boleh, detik_tunggu, alasan). Tidak mengubah hitungan."""
        now = self.clock() if now is None else now
        with self._lock:
            for key in (f"{slot.account}|*", slot.sid):
                row = self._until("disabled", "sid", key, now)
                if row:
                    return False, row[0] - now, f"nonaktif: {row[1]}"
            row = self._until("blocked", "qkey", slot.qkey, now)
            if row:
                return False, row[0] - now, f"diblokir: {row[1]}"
            lim = slot.limits
            if lim.rpd:
                cap = max(1, int(lim.rpd * safety["rpd"]))
                used, _ = self.used_today(slot.qkey, slot.reset_tz, now)
                if used >= cap:
                    return False, next_reset(now, slot.reset_tz) - now, f"RPD {used}/{cap}"
            w = self._window(slot.qkey, now)
            if lim.rpm:
                cap = max(1, int(lim.rpm * safety["rpm"]))
                total = sum(e[1] for e in w)
                if total >= cap:
                    return False, self._wait_for_room(w, now, 1, total, cap), f"RPM {total}/{cap}"
            if lim.tpm:
                cap = max(1, int(lim.tpm * safety["tpm"]))
                total = sum(e[2] for e in w)
                if total >= cap:
                    return False, self._wait_for_room(w, now, 2, total, cap), f"TPM {total}/{cap}"
            return True, 0.0, "ok"

    # ---------- laporan ----------

    def rows(self, slots, safety, now=None):
        now = self.clock() if now is None else now
        out = []
        for s in slots:
            ok, wait, why = self.check(s, safety, now)
            used, tokens = self.used_today(s.qkey, s.reset_tz, now)
            w = self._window(s.qkey, now)
            out.append({"label": s.label, "kind": s.kind, "used": used, "rpd": s.limits.rpd,
                        "tokens": tokens, "rpm_now": sum(e[1] for e in w), "rpm": s.limits.rpm,
                        "ok": ok, "wait": wait, "why": why, "qkey": s.qkey})
        return out
