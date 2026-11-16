"""Modul ekspor kalender iCalendar (.ics) untuk jadwal dan tugas."""
from __future__ import annotations

from datetime import datetime, date
from pathlib import Path


def generate_ics(schedules: list[dict], tasks: list[dict]) -> str:
    """Hasilkan string format iCalendar (.ics) dari data jadwal dan tugas."""
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Study Second Brain//Kalender Kuliah//ID",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH"
    ]

    # Tambahkan jadwal kuliah (rekursif atau event tunggal)
    for s in schedules:
        course = s.get("course", "Kuliah")
        room = s.get("room", "")
        lecturer = s.get("lecturer", "")
        notes = s.get("notes", "")
        weekday = s.get("weekday", 0)  # 0=Senin
        start = s.get("start_time", "08:00").replace(":", "") + "00"
        end = s.get("end_time", "10:00").replace(":", "") + "00"
        
        # Buat deskripsi
        desc = f"Dosen: {lecturer}\\nRuangan: {room}\\nCatatan: {notes}"
        
        lines.extend([
            "BEGIN:VEVENT",
            f"SUMMARY:{course}",
            f"DESCRIPTION:{desc}",
            f"LOCATION:{room}",
            f"DTSTART:20261115T{start}",  # Tanggal contoh / placeholder awal
            f"DTEND:20261115T{end}",
            f"RRULE:FREQ=WEEKLY;BYDAY={['MO','TU','WE','TH','FR','SA','SU'][weekday]}",
            "END:VEVENT"
        ])

    # Tambahkan tugas dengan tenggat
    for t in tasks:
        due = t.get("due_date")
        if due:
            date_str = due.replace("-", "")
            title = t.get("title", "Tugas")
            course = t.get("course", "")
            desc = t.get("notes", "")
            lines.extend([
                "BEGIN:VEVENT",
                f"SUMMARY:Tenggat: {title} ({course})",
                f"DESCRIPTION:{desc}",
                f"DTSTART;VALUE=DATE:{date_str}",
                f"DTEND;VALUE=DATE:{date_str}",
                "END:VEVENT"
            ])

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


def export_ics_file(schedules: list[dict], tasks: list[dict], filepath: str | Path) -> Path:
    p = Path(filepath)
    p.parent.mkdir(parents=True, exist_ok=True)
    ics_content = generate_ics(schedules, tasks)
    p.write_text(ics_content, encoding="utf-8")
    return p
