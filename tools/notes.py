"""Tool: list_notes (daftar catatan hasil add_note)."""
from __future__ import annotations

import re
from datetime import datetime

from db.database import norm
from providers.base import ToolSpec
from rag.ingest import FileRef, parse_frontmatter, sha256_file
from tools.common import ToolContext

SPEC = ToolSpec(
    name="list_notes",
    description=(
        "Menampilkan daftar catatan pribadi yang pernah disimpan (lewat add_note), terbaru dulu: "
        "judul, mata kuliah, tanggal, nama file, dan cuplikan isi. GUNAKAN untuk 'catatan apa saja "
        "yang kupunya', 'daftar catatanku', 'catatan terbaru', 'catatan basis data apa saja'. JANGAN "
        "dipakai untuk mencari isi atau menjawab pertanyaan dari catatan (itu search_knowledge), dan "
        "bukan untuk materi kuliah atau daftar tugas (list_tasks)."),
    parameters={"type": "object", "properties": {
        "course": {"type": "string", "description": "Saring berdasarkan mata kuliah, bila disebut."},
        "limit": {"type": "integer", "minimum": 1, "maximum": 30,
                  "description": "Jumlah catatan terbaru yang ditampilkan (bawaan 10)."}}})


def _preview(body: str, n: int = 90) -> str:
    one = re.sub(r"\s+", " ", body).strip()
    return one if len(one) <= n else one[:n].rstrip() + "..."


def _read_notes(ctx: ToolContext) -> list[dict]:
    rows = []
    if not ctx.notes_dir.exists():
        return rows
    for path in ctx.notes_dir.glob("*.md"):
        try:
            text = path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n")
            meta, body = parse_frontmatter(text)
            ref = FileRef(path=path, rel_path=path.name, course="umum", source_type="catatan",
                          ext=".md", size=path.stat().st_size, sha256=sha256_file(path))
        except OSError:
            continue
        created = meta.get("created") or datetime.fromtimestamp(path.stat().st_mtime).date().isoformat()
        rows.append({
            "file": path.name,
            "title": meta.get("title") or path.stem,
            "course": meta.get("course") or "umum",
            "created": created[:10],
            "tags": meta.get("tags", ""),
            "preview": _preview(body),
            # sudah masuk indeks dengan isi file yang sekarang?
            "indexed": ctx.store.doc_hash(ref.doc_id) == ref.sha256,
        })
    rows.sort(key=lambda r: (r["created"], r["file"]), reverse=True)
    return rows


def list_notes(ctx: ToolContext, a: dict) -> str:
    rows = _read_notes(ctx)
    course = a.get("course")
    if course:
        canon, _ = ctx.resolve_course(course)
        want = norm(canon)
        rows = [r for r in rows if norm(r["course"]) == want or want in norm(r["course"])]
    if not rows:
        return f"Tidak ada catatan untuk mata kuliah '{course}'." if course else "Belum ada catatan."
    limit = a.get("limit") or 10
    shown = rows[:limit]
    head = f"{len(rows)} catatan" + (f", {len(shown)} terbaru ditampilkan" if len(rows) > len(shown) else "") + ":"
    lines = []
    for r in shown:
        tags = f" | tag: {r['tags']}" if r["tags"] else ""
        flag = "" if r["indexed"] else " | BELUM ter-index (jalankan /ingest)"
        lines.append(f"- {r['created']} | {r['title']} | {r['course']} | {r['file']}{tags}{flag}\n"
                     f"  \"{r['preview']}\"")
    return head + "\n" + "\n".join(lines)
