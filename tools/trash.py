"""Tempat sampah: lihat, pulihkan, kosongkan. Dipakai CLI (/trash /restore /purge), tanpa LLM,
sehingga pemulihan tidak bergantung pada model."""
from __future__ import annotations

from tools.common import ToolContext
from tools.notes import _file_ref, _reindex
from providers.base import ProviderError


def list_trash(ctx: ToolContext) -> str:
    items = ctx.tasks.trash_list()
    if not items:
        return "Tempat sampah kosong."
    lines = [f"#{i['id']} [{'tugas' if i['kind'] == 'task' else 'catatan'}] {i['label']} | dihapus "
             f"{i['deleted_at'][:16].replace('T', ' ')} | kedaluwarsa {i['expires_at'][:10]}" for i in items]
    return f"{len(items)} item di tempat sampah (pulihkan: /restore NOMOR):\n" + "\n".join(lines)


def restore(ctx: ToolContext, trash_id: int) -> str:
    item = ctx.tasks.trash_get(trash_id)
    if item is None:
        return f"Tidak ada item #{trash_id} di tempat sampah (cek /trash)."
    if item["kind"] == "task":
        t = ctx.tasks.restore_task(trash_id)
        return f"Tugas dipulihkan: #{t['id']} {t['title']}"
    ctx.notes_dir.mkdir(parents=True, exist_ok=True)
    name = item["payload"]["file"]
    path = ctx.notes_dir / name
    n = 2
    while path.exists():                      # nama lama sudah dipakai catatan lain
        path = ctx.notes_dir / f"{name[:-3]}-pulih{n if n > 2 else ''}.md"
        n += 1
    path.write_text(item["payload"]["content"], encoding="utf-8")
    ctx.tasks.trash_remove(trash_id)
    try:
        _reindex(ctx, path)
        status = "dan masuk pencarian lagi"
    except ProviderError:
        status = "tetapi belum masuk pencarian (embedding tidak tersedia); jalankan /ingest nanti"
    return f"Catatan dipulihkan sebagai {path.name} {status}."


def purge(ctx: ToolContext) -> str:
    return f"{ctx.tasks.trash_empty()} item dibuang permanen dari tempat sampah."
