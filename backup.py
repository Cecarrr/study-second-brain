"""Modul backup, restore, dan versioning database Study Second Brain."""
from __future__ import annotations

import hashlib
import json
import zipfile
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

import config
from db.database import TaskDB

SCHEMA_VERSION = 1


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192):
            h.update(chunk)
    return h.hexdigest()


def create_backup(dest_path: str | Path | None = None) -> Path:
    """Buat arsip zip dari seluruh DATA_DIR (database, vector store, materi, notes)."""
    config.ensure_dirs()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = Path(dest_path or (config.BASE_DIR / f"ssb_backup_{timestamp}.zip"))
    
    # Ambil schema version dari DB
    db = TaskDB()
    with closing_db_version(db) as ver:
        version = ver

    meta = {
        "timestamp": datetime.now().isoformat(),
        "schema_version": version,
        "files": {}
    }

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in config.DATA_DIR.rglob("*"):
            if p.is_file():
                rel = p.relative_to(config.DATA_DIR)
                zf.write(p, arcname=str(rel))
                meta["files"][str(rel)] = _sha256(p)
        
        zf.writestr("meta.json", json.dumps(meta, indent=2))

    return dest


def closing_db_version(db: TaskDB):
    from contextlib import contextmanager
    @contextmanager
    def _ver():
        with closing(db._conn()) as c:
            try:
                row = c.execute("SELECT version FROM schema_metadata LIMIT 1").fetchone()
                yield row["version"] if row else 1
            except Exception:
                yield 1
    return _ver()


def restore_backup(zip_path: str | Path) -> bool:
    """Pulihkan data dari arsip zip backup setelah memvalidasi schema version."""
    src = Path(zip_path)
    if not src.exists():
        raise FileNotFoundError(f"File backup tidak ditemukan: {src}")

    with zipfile.ZipFile(src, "r") as zf:
        if "meta.json" not in zf.namelist():
            raise ValueError("Arsip backup tidak valid (meta.json hilang).")
        meta = json.loads(zf.read("meta.json").decode("utf-8"))
        ver = meta.get("schema_version", 1)
        if ver > SCHEMA_VERSION:
            raise ValueError(f"Versi skema backup ({ver}) lebih baru dari versi aplikasi ({SCHEMA_VERSION}).")

        config.ensure_dirs()
        for name in zf.namelist():
            if name == "meta.json":
                continue
            zf.extract(name, config.DATA_DIR)
    return True
