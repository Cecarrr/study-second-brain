"""Uji offline jadwal kuliah: waktu/jam kelas, tool, bentrok, integrasi (undo, sampah, ringkasan, dasbor, kurs), impor.
python -m unittest tests.test_jadwal -v"""
import sys
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import schedule_import as si  # noqa: E402
import timetable as tt  # noqa: E402
from dashboard import build_dashboard  # noqa: E402
from prompts import build_system_prompt  # noqa: E402
from tests.test_fragmen_a import Base  # noqa: E402
from tools import trash  # noqa: E402
from tools.summary import build_daily_summary  # noqa: E402

BD = dict(course="Basis Data", day="Senin", start="07:00", end="08:40", room="B204", lecturer="Dr. Budi",
          class_name="A", semester=5, sks=3, code="if2101")


class TimeTests(unittest.TestCase):
    def test_parse_and_slots(self):
        self.assertEqual(tt.parse_time("7.30"), "07:30")
        self.assertEqual(tt.parse_time("pukul 7"), "07:00")
        self.assertEqual(tt.slots_to_range("1"), ("07:00", "07:50"))
        self.assertEqual(tt.slots_to_range("jam ke 3-4"), ("08:40", "10:20"))
        for bad in ("25:00", "abc"):
            with self.assertRaises(ValueError):
                tt.parse_time(bad)
        with self.assertRaises(ValueError):
            tt.slots_to_range("40")
        with self.assertRaises(ValueError):
            tt.resolve_times("09:00", "08:00")

    def test_custom_periods_from_config(self):
        config.CLASS_PERIODS = {1: ("08:00", "08:45"), 2: ("08:45", "09:30")}
        try:
            self.assertEqual(tt.slots_to_range("1-2"), ("08:00", "09:30"))
            with self.assertRaises(ValueError):
                tt.slots_to_range("3")
        finally:
            del config.CLASS_PERIODS

    def test_day_refs(self):
        from datetime import date
        d = date(2026, 11, 9)                                         # Senin
        self.assertEqual(tt.parse_day_ref("besok", d)[1], date(2026, 11, 10))
        self.assertEqual(tt.parse_day_ref("Jum'at", d)[2], date(2026, 11, 13))
        self.assertEqual(tt.parse_day_ref("senin", d)[2], d)          # hari ini bila harinya sama
        self.assertEqual(tt.parse_day_ref("minggu ini", d), ("week",))
        self.assertIsNone(tt.parse_day_ref("kapan-kapan", d))


class ToolTests(Base):
    def setUp(self):
        super().setUp()
        self.ctx.tasks.now_fn = lambda: datetime(2026, 11, 9, 8, 0)    # Senin 08:00

    def add(self, **kw):
        out, err = self.call("add_class", **kw)
        self.assertFalse(err, out)
        return out

    def test_add_full_details_and_duplicates(self):
        out = self.add(**BD)
        for part in ("#1", "Senin 07:00-08:40", "Basis Data (IF2101) kelas A", "ruang B204", "Dr. Budi",
                     "semester 5", "3 SKS"):
            self.assertIn(part, out)
        self.assertIn("sudah ada", self.call("add_class", **BD)[0])
        self.assertEqual(len(self.ctx.tasks.sched_list()), 1)

    def test_slots_and_adjacent_sessions_do_not_conflict(self):
        self.add(course="Algoritma", day="Senin", slots="1-2")         # 07:00-08:40
        out = self.add(course="Jaringan", day="Senin", start="08:40", end="10:20")
        self.assertNotIn("bentrok", out.lower())

    def test_conflict_rejected_unless_forced(self):
        self.add(**BD)
        out, err = self.call("add_class", course="Algoritma", day="Senin", start="08:00", end="09:00")
        self.assertTrue(err and "Bentrok" in out and "Basis Data" in out)
        self.assertIn("CATATAN: bentrok", self.add(course="Algoritma", day="Senin", start="08:00", end="09:00", force=True))

    def test_validation_errors(self):
        for kw, frag in ((dict(course="X", day="Hari Kiamat", start="07:00", end="08:00"), "tidak dikenali"),
                         (dict(course="X", day="Senin"), "Isi jam"),
                         (dict(course="X", day="Senin", start="09:00", end="08:00"), "harus setelah"),
                         (dict(course="X", day="Senin", start="07:00", end="08:00", kind="aneh"), "salah satu dari")):
            out, err = self.call("add_class", **kw)
            self.assertTrue(err and frag in out, (kw, out))

    def test_list_today_status_next_and_week(self):
        self.add(**BD)                                                  # Senin 07:00-08:40 (sedang berlangsung)
        self.add(course="Jaringan", day="Senin", start="10:00", end="11:40", sks=3)
        self.add(course="Algoritma", day="Selasa", start="09:00", end="10:40", sks=2)
        out, _ = self.call("list_schedule", day="hari ini")
        self.assertIn("2 sesi", out)
        self.assertIn("[sedang berlangsung]", out)
        out, _ = self.call("list_schedule", day="berikutnya")
        self.assertIn("Jaringan", out)
        self.assertIn("2 jam", out)
        self.ctx.tasks.now_fn = lambda: datetime(2026, 11, 9, 20, 0)
        out, _ = self.call("list_schedule", day="berikutnya")
        self.assertIn("besok", out)
        self.assertIn("Algoritma", out)
        out, _ = self.call("list_schedule")
        self.assertIn("3 sesi, total 8 SKS", out)
        self.assertIn("Selasa:", out)
        self.assertIn("Tidak ada kelas pada Rabu", self.call("list_schedule", day="Rabu")[0])

    def test_filters_and_due_footer(self):
        self.add(**BD)
        self.add(course="Jaringan", day="Selasa", start="09:00", end="10:40", lecturer="Ir. Sari", semester=3)
        self.assertIn("Ir. Sari", self.call("list_schedule", lecturer="sari")[0])
        self.assertNotIn("Jaringan", self.call("list_schedule", semester=5)[0])
        self.assertIn("Basis Data", self.call("list_schedule", course="if2101")[0])
        self.call("add_task", title="Laporan jaringan", due_date="2026-11-10")
        self.assertIn("Tenggat tugas hari itu: #1 Laporan jaringan", self.call("list_schedule", day="besok")[0])

    def test_update_conflict_archive_and_ambiguity(self):
        self.add(**BD)
        self.add(course="Algoritma", day="Selasa", start="07:00", end="08:40")
        out, err = self.call("update_class", course="algoritma", new_day="Senin")
        self.assertTrue(err and "bentrok" in out.lower())
        out, err = self.call("update_class", course="algoritma", new_day="Rabu", room="C1")
        self.assertFalse(err, out)
        self.assertIn("Rabu", out)
        self.assertIn("Tidak ada yang berubah", self.call("update_class", class_id=2, room="C1")[0])
        self.call("update_class", course="basis", active=False)
        self.assertNotIn("Basis Data", self.call("list_schedule")[0])
        self.assertIn("(diarsipkan)", self.call("list_schedule", archived=True)[0])
        self.call("update_class", class_id=1, active=True)
        self.add(course="Basis Data", day="Kamis", start="07:00", end="08:40")
        out, _ = self.call("update_class", course="basis", room="Z9")
        self.assertIn("tanyakan ke pengguna", out)

    def test_delete_trash_restore_and_undo(self):
        self.add(**BD)
        out, _ = self.call("delete_class", course="basis data")
        self.assertIn("/restore", out)
        self.assertEqual(self.ctx.tasks.sched_list(), [])
        self.assertIn("[kelas]", trash.list_trash(self.ctx))
        self.assertIn("Sesi kuliah dipulihkan", trash.restore(self.ctx, 1))
        self.assertEqual(self.ctx.tasks.sched_list()[0]["id"], 1)
        self.call("delete_class", class_id=1)
        self.reg.journal.undo(self.ctx)                                 # batalkan hapus
        self.assertEqual(len(self.ctx.tasks.sched_list()), 1)
        self.assertEqual(self.ctx.tasks.trash_list(), [])
        self.reg.journal.undo(self.ctx)                                 # batalkan hapus pertama (kelas ada lagi)
        self.reg.journal.undo(self.ctx)                                 # batalkan add_class awal
        self.assertEqual(self.ctx.tasks.sched_list(), [])
        self.assertIn("Tidak ada aksi", self.reg.journal.undo(self.ctx))

    def test_integration_courses_summary_dashboard_prompt(self):
        self.add(**BD)
        self.assertEqual(self.ctx.known_courses(), ["basis-data"])     # nama jadwal digabung ke nama indeks (norm sama)
        self.assertEqual(self.ctx.resolve_course("Kewirausahaan"), ("Kewirausahaan", False))
        self.add(course="Kewirausahaan", day="Senin", start="13:00", end="14:40")
        self.assertEqual(self.ctx.resolve_course("kewirausahaan"), ("Kewirausahaan", True))
        summ = build_daily_summary(self.ctx.tasks)
        self.assertIn("Kelas hari ini (2)", summ)
        self.assertIn("07:00-08:40 Basis Data kelas A | ruang B204 | Dr. Budi", summ)
        self.assertIn("Jadwal: 2 sesi aktif (2 mata kuliah)", build_dashboard(self.ctx, self.tmp / "t.jsonl", usage_report=lambda: ""))
        p = build_system_prompt(None, self.ctx.known_courses())
        self.assertIn("list_schedule", p)
        self.assertIn("Kewirausahaan", p)


class ImportTests(Base):
    CSV = ("Mata Kuliah;Kode;Kelas;Semester;SKS;Dosen;Hari;Mulai;Selesai;Ruang;Jenis\n"
           "Basis Data;IF2101;A;5;3;Dr. Budi;Senin;07:00;08:40;B204;teori\n"
           "Praktikum Basis Data;IF2102;A1;5;1;Ir. Sari;Rabu;jam 3-4;;Lab 2;praktikum\n"
           "Aljabar;IF1;B;2;3;Pak X;Hari Kiamat;07:00;08:00;C1;teori\n"
           "Jaringan;IF3;A;5;3;Bu Y;Senin;08:00;09:00;D1;teori\n")

    def test_csv_import_reports_per_row_and_is_idempotent(self):
        out = si.import_text(self.ctx, self.CSV, "csv")
        self.assertIn("2 sesi ditambahkan, 0 sudah ada, 2 bermasalah", out)
        self.assertIn("Hari Kiamat", out)
        self.assertIn("Bentrok", out)
        rows = self.ctx.tasks.sched_list()
        self.assertEqual([(r["course"], r["start_time"], r["end_time"], r["kind"]) for r in rows],
                         [("Basis Data", "07:00", "08:40", "teori"), ("Praktikum Basis Data", "08:40", "10:20", "praktikum")])
        self.assertIn("2 sudah ada", si.import_text(self.ctx, self.CSV, "csv"))

    def test_csv_header_errors_and_template(self):
        self.assertIn("judul kolom tidak lengkap", si.import_text(self.ctx, "a,b\n1,2\n", "csv"))
        self.assertIn("2 sesi ditambahkan", si.import_text(self.ctx, si.TEMPLATE, "csv"))

    def test_ics_import_weekly_rule_and_utc(self):
        ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Algoritma\r\nDTSTART:20260907T000000Z\r\n"
               "DTEND:20260907T014000Z\r\nRRULE:FREQ=WEEKLY;BYDAY=MO,WE\r\nLOCATION:B101\r\n"
               "DESCRIPTION:Dosen: Dr. Ani\\nSemester 3\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Rusak\r\nEND:VEVENT\r\n"
               "END:VCALENDAR\r\n")
        out = si.import_text(self.ctx, ics, "ics")
        self.assertIn("2 sesi ditambahkan", out)
        self.assertIn("1 bermasalah", out)
        rows = self.ctx.tasks.sched_list()
        self.assertEqual([(r["weekday"], r["start_time"], r["end_time"], r["room"], r["lecturer"]) for r in rows],
                         [(0, "07:00", "08:40", "B101", "Dr. Ani"), (2, "07:00", "08:40", "B101", "Dr. Ani")])


if __name__ == "__main__":
    unittest.main()
