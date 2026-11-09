"""Tool: list_notes (daftar catatan hasil add_note)."""
from __future__ import annotations

import re
from datetime import datetime

import config
import privacy
from db.database import TRASH_DAYS, norm, title_matches
from providers.base import ProviderError, ToolSpec
from rag.indexer import index_document
from rag.ingest import FileRef, parse_frontmatter, read_document, sha256_file
from tools.common import ToolContext, ToolError

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


# ---------------------------------------------------------------- ubah / hapus catatan
UPDATE_SPEC = ToolSpec(
    name="update_note",
    description=(
        "Mengubah catatan yang SUDAH ADA (bukan membuat baru). GUNAKAN saat pengguna ingin mengoreksi, "
        "menambah, atau memperbarui catatan lama ('ubah catatan UTS', 'tambahkan ke catatan basis data', "
        "'koreksi tanggal di catatan ...', 'pindahkan catatan itu ke mata kuliah ...'). Tentukan catatan "
        "dengan 'title' (kata kunci judul LAMA) atau 'file' (nama file dari list_notes). Untuk isi pilih "
        "SATU: append_text = MENAMBAH di akhir; new_content = MENGGANTI seluruh isi (tulis isi baru "
        "lengkap). Pencarian ikut diperbarui otomatis. Bila hasilnya daftar kandidat, tanyakan mana yang "
        "dimaksud. JANGAN dipakai untuk catatan baru (add_note) atau tugas (update_task)."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Kata kunci judul catatan yang akan diubah."},
        "file": {"type": "string", "description": "Nama file catatan (dari list_notes), alternatif title."},
        "new_title": {"type": "string", "description": "Judul baru (maks 8 kata)."},
        "new_content": {"type": "string", "description": "Isi baru LENGKAP; menggantikan seluruh isi lama."},
        "append_text": {"type": "string", "description": "Teks yang ditambahkan di akhir isi lama."},
        "course": {"type": "string", "description": "Mata kuliah baru."},
        "tags": {"type": "array", "items": {"type": "string"},
                 "description": "Label baru (menggantikan label lama)."}}})

DELETE_SPEC = ToolSpec(
    name="delete_note",
    description=(
        "Menghapus satu catatan dari daftar dan dari pencarian. Catatan dipindah ke tempat sampah dan "
        f"bisa dipulihkan pengguna selama {TRASH_DAYS} hari lewat perintah /restore di CLI. GUNAKAN hanya "
        "bila pengguna jelas meminta menghapus atau membuang catatan ('hapus catatan ...'). JANGAN "
        "dipakai untuk mengoreksi isi (update_note) atau menghapus tugas (delete_task). Tentukan dengan "
        "'title' atau 'file'; satu catatan per permintaan; bila hasilnya daftar kandidat, tanyakan mana "
        "yang dimaksud."),
    parameters={"type": "object", "properties": {
        "title": {"type": "string", "description": "Kata kunci judul catatan yang akan dihapus."},
        "file": {"type": "string", "description": "Nama file catatan (dari list_notes), alternatif title."}}})


def _pick_note(ctx: ToolContext, a: dict, tool: str) -> tuple[dict | None, str | None]:
    """Tentukan satu catatan dari file atau title. (catatan, None) atau (None, daftar kandidat);
    raise ToolError bila tidak ada yang cocok."""
    f, title = a.get("file"), a.get("title")
    if not f and not title:
        raise ToolError("Isi title (kata kunci judul catatan) atau file (nama file dari list_notes).")
    rows = _read_notes(ctx)
    if f:
        hit = [r for r in rows if r["file"].lower() == f.strip().lower()]
        if not hit:
            raise ToolError(f"Tidak ada catatan dengan file '{f}'. Panggil list_notes untuk melihat daftarnya.")
        return hit[0], None
    words = norm(title).split()
    out = [r for r in rows if norm(r["title"]) == norm(title)
           or title_matches(words, r["title"] + " " + r["file"].rsplit(".", 1)[0])]
    exact = [r for r in out if norm(r["title"]) == norm(title)]
    out = exact or out
    if not out:
        raise ToolError(f"Tidak ada catatan yang cocok dengan '{title}'. Panggil list_notes untuk "
                        "melihat daftar, atau beri tahu pengguna.")
    if len(out) > 1:
        lines = [f"- {r['created']} | {r['title']} | {r['course']} | {r['file']}" for r in out]
        return None, (f"Ada beberapa catatan yang cocok; tanyakan ke pengguna yang mana, lalu panggil "
                      f"{tool} dengan file:\n" + "\n".join(lines))
    return out[0], None


def _write_note(path, meta: dict, body: str) -> None:
    head = "\n".join(f"{k}: {' '.join(str(v).split())}" for k, v in meta.items() if str(v).strip())
    path.write_text(f"---\n{head}\n---\n{body.strip()}\n", encoding="utf-8")


def _file_ref(path) -> FileRef:
    return FileRef(path=path, rel_path=path.name, course="umum", source_type="catatan", ext=".md",
                   size=path.stat().st_size, sha256=sha256_file(path))


def _reindex(ctx: ToolContext, path) -> str:
    """Indeks ulang satu catatan. Kembalikan kalimat status untuk dilaporkan ke pengguna."""
    doc = read_document(_file_ref(path))
    if not doc.ok:
        raise ToolError(f"Catatan tersimpan di {path.name} tetapi gagal dibaca: {doc.error}")
    try:
        index_document(doc, ctx.store, ctx.embedder)
        return "dan pencarian sudah diperbarui"
    except ProviderError as e:
        return ("tetapi pencarian BELUM diperbarui karena embedding tidak tersedia "
                f"({str(e).splitlines()[0][:120]}); jalankan /ingest nanti")


def update_note(ctx: ToolContext, a: dict) -> str:
    new_title, new_content, extra = a.get("new_title"), a.get("new_content"), a.get("append_text")
    if new_content and extra:
        raise ToolError("Pilih salah satu: new_content (ganti seluruh isi) atau append_text (tambah di akhir).")
    if not any([new_title, new_content, extra, a.get("course"), a.get("tags")]):
        raise ToolError("Tidak ada perubahan yang diminta. Isi minimal satu: new_title, new_content, "
                        "append_text, course, atau tags.")
    if new_content and len(new_content.strip()) < 5:
        raise ToolError("new_content terlalu pendek; tulis isi baru yang lengkap.")
    row, cands = _pick_note(ctx, a, "update_note")
    if cands:
        return cands

    path = ctx.notes_dir / row["file"]
    meta, body = parse_frontmatter(path.read_bytes().decode("utf-8-sig", errors="replace").replace("\r\n", "\n"))
    meta = dict(meta)
    changed = []
    if new_title:
        meta["title"] = new_title.strip()
        changed.append("judul")
    if a.get("course"):
        meta["course"], _ = ctx.resolve_course(a["course"])
        changed.append("mata kuliah")
    if a.get("tags"):
        meta["tags"] = ", ".join(t.strip() for t in a["tags"] if t.strip())
        changed.append("label")
    if new_content:
        body = new_content.strip()
        changed.append("isi (diganti)")
    elif extra:
        body = body.rstrip() + "\n\n" + extra.strip()
        changed.append("isi (ditambah)")
    meta["updated"] = ctx.today().isoformat()
    meta.setdefault("created", row["created"])
    _write_note(path, meta, body)
    status = _reindex(ctx, path)
    return (f"Catatan '{meta.get('title') or row['title']}' ({row['file']}) diperbarui: "
            f"{', '.join(changed)} {status}." + privacy.note_warning(body))


def delete_note(ctx: ToolContext, a: dict) -> str:
    row, cands = _pick_note(ctx, a, "delete_note")
    if cands:
        return cands
    path = ctx.notes_dir / row["file"]
    content = path.read_bytes().decode("utf-8-sig", errors="replace")
    tid = ctx.tasks.trash_put("note", row["title"], {"file": row["file"], "content": content})
    try:
        ref = _file_ref(path)
        path.unlink()
    except OSError as e:
        ctx.tasks.trash_remove(tid)
        raise ToolError(f"Catatan {row['file']} gagal dihapus: {e}")
    ctx.store.delete_document(ref.doc_id)
    return (f"Catatan '{row['title']}' ({row['file']}) dihapus dari daftar dan pencarian. Tersimpan di "
            f"tempat sampah #{tid} selama {TRASH_DAYS} hari; pengguna bisa memulihkannya dengan /restore {tid}.")
