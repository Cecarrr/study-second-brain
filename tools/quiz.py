"""Tool: make_quiz. Soal dibuat LLM hanya dari potongan materi hasil retrieval."""
from __future__ import annotations

import json
import re

from providers.base import Message, ProviderError, ToolSpec
from tools.common import ToolContext, ToolError

SPEC = ToolSpec(
    name="make_quiz",
    description=(
        "Membuat soal latihan dari materi dan catatan pengguna (soal hanya berdasar isi materi itu). "
        "GUNAKAN untuk 'kuis aku', 'beri soal latihan', 'uji pemahamanku tentang X'. Hasilnya memuat "
        "soal DAN kunci jawaban: tampilkan SOALNYA SAJA lebih dulu, simpan kunci sampai pengguna "
        "menjawab atau memintanya. JANGAN dipakai untuk menjelaskan materi (itu search_knowledge)."),
    parameters={"type": "object", "properties": {
        "topic": {"type": "string", "description": "Topik soal, mis. 'normalisasi database'."},
        "num_questions": {"type": "integer", "minimum": 1, "maximum": 10,
                          "description": "Jumlah soal (bawaan 5)."},
        "question_type": {"type": "string", "enum": ["multiple_choice", "short_answer"],
                          "description": "multiple_choice (bawaan) atau short_answer."},
        "course": {"type": "string", "description": "Mata kuliah, bila disebut pengguna."}},
        "required": ["topic"]})

_SYSTEM = (
    "Kamu pembuat soal latihan kuliah. Buat soal HANYA berdasarkan KONTEKS yang diberikan; jangan "
    "memakai pengetahuan lain dan jangan membuat soal yang jawabannya tidak ada di konteks. "
    "Gunakan bahasa konteks (utamanya Indonesia). Balas HANYA dengan satu objek JSON valid, tanpa "
    "teks lain dan tanpa code fence, dengan bentuk:\n"
    '{"questions":[{"question":"...","options":["...","...","...","..."],"answer":"B",'
    '"explanation":"satu kalimat","source":"nama file"}]}\n'
    'Untuk pilihan ganda: tepat 4 opsi (tanpa awalan huruf), "answer" = huruf A-D. '
    'Untuk jawaban singkat: hilangkan "options", "answer" = jawaban singkat yang benar.')


def _parse(text: str) -> list[dict]:
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.I)
    i, j = t.find("{"), t.rfind("}")
    if i == -1 or j <= i:
        raise ValueError("tidak ada JSON")
    data = json.loads(t[i:j + 1])
    qs = data.get("questions") if isinstance(data, dict) else None
    if not isinstance(qs, list) or not qs:
        raise ValueError("daftar soal kosong")
    return qs


def make_quiz(ctx: ToolContext, a: dict) -> str:
    topic = a["topic"]
    n = a.get("num_questions") or 5
    qtype = a.get("question_type") or "multiple_choice"
    course, known = ctx.resolve_course(a.get("course"))
    if course and not known:
        course = None
    res = ctx.retriever.search(topic, k=8, course=course)
    if not res.hits:
        best = f" (skor tertinggi {res.best_score:.2f})" if res.best_score is not None else ""
        return (f"Tidak ada materi atau catatan yang cukup relevan dengan topik '{topic}'{best}, "
                "jadi kuis tidak dibuat. Minta pengguna menyebut topik atau mata kuliah lain, "
                "atau menambah materi.")
    ctx_text, used = "", []
    for i, h in enumerate(res.hits, 1):
        block = f"[{i}] sumber: {h.source}\n{h.text}\n\n"
        if len(ctx_text) + len(block) > 7000:
            break
        ctx_text += block
        if h.source not in used:
            used.append(h.source)
    kind = "pilihan ganda" if qtype == "multiple_choice" else "jawaban singkat"
    user = (f"Topik: {topic}\nJumlah soal: {n}\nJenis: {kind}\n\nKONTEKS:\n{ctx_text}"
            f"Buat {n} soal {kind} tentang topik tersebut sesuai format JSON.")
    try:
        resp = ctx.chat([Message.system(_SYSTEM), Message.user(user)])
        qs = _parse(resp.text)
    except ProviderError:
        raise
    except (ValueError, json.JSONDecodeError):
        raise ToolError("Model gagal menghasilkan soal dalam format yang benar. Coba panggil sekali "
                        "lagi; bila masih gagal, beri tahu pengguna.")

    qs = qs[:n]
    out = [f"Kuis: {topic} ({len(qs)} soal, {kind}). Sumber materi: {', '.join(used)}.",
           "[Untuk asisten: tampilkan hanya SOAL ke pengguna; simpan KUNCI sampai pengguna menjawab "
           "atau memintanya.]", ""]
    key = []
    for i, q in enumerate(qs, 1):
        out.append(f"{i}. {str(q.get('question', '')).strip()}")
        ans = str(q.get("answer", "")).strip()
        opts = q.get("options") if qtype == "multiple_choice" else None
        if isinstance(opts, list) and opts:
            for letter, o in zip("ABCD", opts):
                o = re.sub(r"^[A-Da-d][.)]\s*", "", str(o).strip())
                out.append(f"   {letter}. {o}")
        expl = str(q.get("explanation", "")).strip()
        src = str(q.get("source", "")).strip()
        key.append(f"{i}. {ans}" + (f" - {expl}" if expl else "") + (f" (sumber: {src})" if src else ""))
    out += ["", "=== KUNCI JAWABAN (jangan tampilkan sebelum pengguna menjawab/meminta) ===", *key]
    return "\n".join(out)
