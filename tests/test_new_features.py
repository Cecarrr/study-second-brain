"""Test suite komprehensif untuk fitur baru (A sampai G)."""
from __future__ import annotations

import tempfile
import pytest
from pathlib import Path

import config
from db.database import TaskDB
from backup import create_backup, restore_backup
from doctor import run_doctor
from tools.calendar_sync import generate_ics, export_ics_file
from rag.importers import import_markdown_folder, ImportFailed


def test_flashcards_spaced_repetition_comprehensive():
    with tempfile.TemporaryDirectory() as tmp:
        db = TaskDB(Path(tmp) / "test.db")
        card = db.flashcard_add("Apa itu Neural Network?", "Jaringan syaraf tiruan")
        assert card["id"] is not None
        assert card["repetitions"] == 0

        # Review gagal (quality = 1 < 3)
        updated_fail = db.flashcard_update_review(card["id"], quality=1)
        assert updated_fail["repetitions"] == 0
        assert updated_fail["interval"] == 1.0

        # Review berhasil berulang (quality = 5)
        updated_succ1 = db.flashcard_update_review(card["id"], quality=5)
        assert updated_succ1["repetitions"] == 1

        updated_succ2 = db.flashcard_update_review(card["id"], quality=4)
        assert updated_succ2["repetitions"] == 2
        assert updated_succ2["interval"] > 1.0

        all_cards = db.flashcard_list(due_only=False)
        assert len(all_cards) == 1
        due_cards = db.flashcard_list(due_only=True)
        assert isinstance(due_cards, list)


def test_memory_and_prefs_comprehensive():
    with tempfile.TemporaryDirectory() as tmp:
        db = TaskDB(Path(tmp) / "test.db")
        db.memory_add("ses_A", "user", "Tanya apa kabar?")
        db.memory_add("ses_A", "assistant", "Baik!")
        db.memory_add("ses_B", "user", "Topik lain")

        mem_a = db.memory_list("ses_A", limit=10)
        assert len(mem_a) == 2
        assert mem_a[0]["role"] == "user"

        mem_all = db.memory_list(limit=5)
        assert len(mem_all) == 3

        db.pref_set("language", "id")
        db.pref_set("mode", "strict")
        assert db.pref_get("language") == "id"
        assert db.pref_get("mode") == "strict"
        assert db.pref_get("nonexistent") is None


def test_calendar_sync_ics_comprehensive():
    schedules = [
        {
            "course": "Pemrograman",
            "weekday": 0,
            "start_time": "09:00",
            "end_time": "11:30",
            "room": "Lab Komputer",
            "lecturer": "Prof. Andi"
        },
        {
            "course": "Basis Data",
            "weekday": 2,
            "start_time": "13:00",
            "end_time": "15:00",
            "room": "Ruang 301",
            "lecturer": "Dr. Sinta"
        }
    ]
    tasks = [
        {
            "title": "Praktikum 1",
            "due_date": "2026-11-25",
            "course": "Pemrograman"
        },
        {
            "title": "UTS Basis Data",
            "due_date": "2026-11-28",
            "course": "Basis Data"
        }
    ]
    ics = generate_ics(schedules, tasks)
    assert "BEGIN:VCALENDAR" in ics
    assert "Pemrograman" in ics
    assert "Basis Data" in ics
    assert "Praktikum 1" in ics
    assert "UTS Basis Data" in ics

    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "schedule.ics"
        res = export_ics_file(schedules, tasks, p)
        assert res.exists()
        assert "BEGIN:VCALENDAR" in res.read_text(encoding="utf-8")


def test_doctor_and_backup_comprehensive():
    with tempfile.TemporaryDirectory() as tmp:
        old_data = config.DATA_DIR
        old_db = config.DB_PATH
        config.DATA_DIR = Path(tmp) / "data"
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        config.DB_PATH = config.DATA_DIR / "app.db"

        try:
            db = TaskDB(config.DB_PATH)
            db.add("Tugas Uji Komprehensif")

            doc = run_doctor()
            assert doc["db_integrity"] == "ok"
            assert doc["env_file"] is False or doc["env_file"] is True

            backup_zip = create_backup(Path(tmp) / "full_backup.zip")
            assert backup_zip.exists()

            # Test restore
            restore_backup(backup_zip)
        finally:
            config.DATA_DIR = old_data
            config.DB_PATH = old_db


def test_markdown_folder_importer():
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "vault"
        folder.mkdir()
        (folder / "catatan1.md").write_text("# Topik A\nIsi catatan pertama.", encoding="utf-8")
        (folder / "catatan2.md").write_text("# Topik B\nIsi catatan kedua.", encoding="utf-8")

        results = import_markdown_folder(folder, course="Kalkulus")
        assert len(results) == 2

        with pytest.raises(ImportFailed):
            import_markdown_folder(Path(tmp) / "nonexistent")
