"""Ringkasan harian tugas untuk CLI: dibangun langsung dari database, tanpa memanggil model."""
from __future__ import annotations

from datetime import date, timedelta

import config
from db.database import TaskDB
from prompts import DAYS, MONTHS
from tools.tasks import fmt_task


def _due(t: dict) -> date | None:
    try:
        return date.fromisoformat(t["due_date"]) if t.get("due_date") else None
    except ValueError:          # data rusak jangan sampai menggagalkan CLI
        return None


def build_daily_summary(tasks: TaskDB, days: int | None = None) -> str:
    """Terlambat, hari ini sampai `days` hari ke depan, dan hitungan sisanya."""
    days = config.SUMMARY_DUE_DAYS if days is None else days
    today = tasks.today()
    limit = today + timedelta(days=days)
    head = f"Ringkasan {DAYS[today.weekday()]}, {today.day} {MONTHS[today.month - 1]} {today.year}"

    pending = tasks.list("pending")
    if not pending:
        return f"{head}\n  Tidak ada tugas yang belum selesai."

    overdue, soon, later, nodue = [], [], 0, 0
    for t in pending:                       # sudah terurut tenggat terdekat
        d = _due(t)
        if d is None:
            nodue += 1
        elif d < today:
            overdue.append(t)
        elif d <= limit:
            soon.append(t)
        else:
            later += 1

    lines = [head]
    if overdue:
        lines.append(f"TERLAMBAT ({len(overdue)}):")
        lines += [f"  {fmt_task(t, today)}" for t in overdue]
    if soon:
        lines.append(f"Dalam {days} hari ke depan ({len(soon)}):")
        lines += [f"  {fmt_task(t, today)}" for t in soon]
    if not overdue and not soon:
        lines.append(f"  Tidak ada tugas mendesak (terlambat atau tenggat dalam {days} hari).")
    rest = []
    if later:
        rest.append(f"{later} bertenggat lebih jauh")
    if nodue:
        rest.append(f"{nodue} tanpa tenggat")
    if rest:
        lines.append("Lainnya: " + ", ".join(rest) + " (ketik /tasks untuk semua).")
    return "\n".join(lines)
