"""Modul diagnostik sistem (/doctor) Study Second Brain."""
from __future__ import annotations

import os
from contextlib import closing
from pathlib import Path

import config
from db.database import TaskDB


def run_doctor() -> dict:
    """Jalankan pemeriksaan kesehatan sistem menyeluruh."""
    report = {
        "env_file": False,
        "pool_config": False,
        "api_keys": [],
        "db_integrity": "unknown",
        "rag_store_status": "ok",
        "orphan_files": [],
        "errors": []
    }

    # 1. Cek .env
    env_path = config.BASE_DIR / ".env"
    if env_path.exists():
        report["env_file"] = True
    else:
        report["errors"].append("File .env tidak ditemukan di root.")

    # 2. Cek pool.toml
    pool_path = config.BASE_DIR / "pool.toml"
    if pool_path.exists():
        report["pool_config"] = True
    else:
        report["errors"].append("File pool.toml tidak ditemukan (opsional tapi disarankan).")

    # 3. Cek API Keys
    if config.GEMINI_API_KEY:
        report["api_keys"].append({"provider": "gemini", "configured": True})
    else:
        report["api_keys"].append({"provider": "gemini", "configured": False})
        report["errors"].append("GEMINI_API_KEY belum diisi.")

    # 4. Cek Database SQLite Integrity
    try:
        db = TaskDB()
        with closing(db._conn()) as c:
            res = c.execute("PRAGMA integrity_check;").fetchone()
            report["db_integrity"] = res[0] if res else "failed"
            if res[0] != "ok":
                report["errors"].append(f"Integritas database bermasalah: {res[0]}")
    except Exception as e:
        report["db_integrity"] = "error"
        report["errors"].append(f"Gagal mengakses database: {e}")

    # 5. Cek RAG Store & materi
    if config.VECTOR_STORE_PATH.exists():
        report["rag_store_status"] = "exists"
    else:
        report["rag_store_status"] = "not_initialized"

    # Cek folder materi
    if config.MATERI_DIR.exists():
        materi_files = list(config.MATERI_DIR.rglob("*"))
        report["materi_files_count"] = len([f for f in materi_files if f.is_file()])
    else:
        report["materi_files_count"] = 0

    return report
