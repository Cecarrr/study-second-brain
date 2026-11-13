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


def _class_lines(tasks: TaskDB, today) -> list[str]:
    """Kelas hari ini (kosong bila jadwal belum diisi)."""
    import timetable as tt
    if not tasks.sched_list():
        return []
    rows = tasks.sched_list(weekday=today.weekday())
    if not rows:
        return ["  Kelas hari ini: tidak ada."]
    return [f"  Kelas hari ini ({len(rows)}):"] + [f"    {tt.fmt_brief(r)}" for r in rows]


def build_daily_summary(tasks: TaskDB, days: int | None = None) -> str:
    """Terlambat, hari ini sampai `days` hari ke depan, dan hitungan sisanya."""
    days = config.SUMMARY_DUE_DAYS if days is None else days
    today = tasks.today()
    limit = today + timedelta(days=days)
    head = f"Ringkasan {DAYS[today.weekday()]}, {today.day} {MONTHS[today.month - 1]} {today.year}"

    classes = _class_lines(tasks, today)
    pending = tasks.list("pending")
    if not pending:
        return "\n".join([head, *classes, "  Tidak ada tugas yang belum selesai."])

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

    lines = [head, *classes]
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
