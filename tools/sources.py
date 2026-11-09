"""/source N: tampilkan potongan asli di balik jawaban (hasil search_knowledge terakhir). Tanpa LLM."""
from __future__ import annotations


def show_source(ctx, arg: str = "") -> str:
    hits = getattr(ctx, "last_sources", [])
    if not hits:
        return "Belum ada hasil pencarian. Ajukan pertanyaan tentang materi dulu."
    if not arg.strip():
        lines = []
        for i, h in enumerate(hits, 1):
            where = h.source + (f" ({h.location})" if h.location else "")
            head = h.metadata.get("heading")
            lines.append(f"[{i}] {where}" + (f" | {head}" if head else ""))
        return "Sumber pencarian terakhir (lihat isinya: /source NOMOR):\n" + "\n".join(lines)
    if not arg.strip().lstrip("#").isdigit() or not 1 <= int(arg.strip().lstrip("#")) <= len(hits):
        return f"Pilih nomor 1-{len(hits)} (contoh: /source 1)."
    h = hits[int(arg.strip().lstrip("#")) - 1]
    where = h.source + (f" ({h.location})" if h.location else "")
    via = {"vec+kw": "makna + kata kunci", "vec": "makna", "kw": "kata kunci"}.get(h.metadata.get("_via", ""), "")
    meta = f"{where} | skor {h.score:.2f}" + (f" | ditemukan lewat {via}" if via else "")
    return f"{meta}\n{'-' * 60}\n{h.text}"
