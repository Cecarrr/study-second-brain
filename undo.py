"""Pembatalan aksi tulis (/undo): potret data sebelum tiap tool tulis, kembalikan saat diminta.

Yang dipotret: tabel tugas + tempat sampah (app.db) dan semua file catatan. Dipakai bersama oleh
add/update/delete untuk tugas dan catatan, jadi tidak perlu logika kebalikan per tool.
Riwayat hanya di memori (hilang saat CLI ditutup); kedalaman UNDO_DEPTH (bawaan 10).
"""
from __future__ import annotations

import config
from providers.base import ProviderError
from rag.ingest import FileRef


def _doc_id(path) -> str:
    return FileRef(path=path, rel_path=path.name, course="umum", source_type="catatan",
                   ext=".md", size=0, sha256="").doc_id


class Journal:
    def __init__(self, depth: int | None = None):
        self.depth = depth or int(getattr(config, "UNDO_DEPTH", 10))
        self.stack: list[tuple[str, dict]] = []

    @staticmethod
    def snapshot(ctx) -> dict:
        notes = {p.name: p.read_bytes() for p in ctx.notes_dir.glob("*.md")} if ctx.notes_dir.exists() else {}
        return {"db": ctx.tasks.dump_state(), "notes": notes}

    def push(self, label: str, before: dict) -> None:
        self.stack.append((label, before))
        del self.stack[:-self.depth]

    def labels(self) -> list[str]:
        return [lbl for lbl, _ in reversed(self.stack)]

    def undo(self, ctx) -> str:
        if not self.stack:
            return "Tidak ada aksi yang bisa dibatalkan."
        label, snap = self.stack.pop()
        ctx.tasks.load_state(snap["db"])
        now, saved = self.snapshot(ctx)["notes"], snap["notes"]
        for name in set(now) - set(saved):                       # catatan yang ditambahkan aksi itu
            path = ctx.notes_dir / name
            ctx.store.delete_document(_doc_id(path))
            path.unlink()
        redo, failed = [], []
        for name, data in saved.items():                          # catatan yang diubah/dihapus aksi itu
            if now.get(name) != data:
                path = ctx.notes_dir / name
                path.write_bytes(data)
                redo.append(path)
        from tools.notes import _reindex                          # impor lambat: hindari impor melingkar
        for path in redo:
            try:
                _reindex(ctx, path)
            except (ProviderError, Exception):                    # noqa: BLE001
                failed.append(path.name)
        msg = f"Dibatalkan: {label}."
        if failed:
            msg += f" Pencarian belum diperbarui untuk {', '.join(failed)} (jalankan /ingest)."
        return msg
