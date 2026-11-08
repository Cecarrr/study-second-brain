"""Uji model untuk Study Second Brain: ketepatan tool calling (pakai enam tool asli proyek)
dan uji serah-terima riwayat tool call antar model (yang terjadi saat failover).

  python eval_models.py                          -> model bawaan (4 model berkuota besar), ~12 request/model
  python eval_models.py gemini-3.1-flash-lite gemma-4-31b-it
  python eval_models.py --no-handoff             -> lewati uji serah-terima

Memakai GEMINI_API_KEY (satu account) lewat GeminiProvider milik proyek. Model berkuota 20 RPD
(gemini-3.x-flash, 2.5-flash) boleh dimasukkan, tapi satu run menghabiskan sebagian besar jatah harian
account itu; gunakan hanya bila perlu.
"""
import os
import sys
import time

from providers.base import Message, ProviderError, RateLimitError, ToolSpec

TODAY = "Minggu, 2026-11-08"
SYSTEM = (
    "Kamu adalah asisten belajar mahasiswa. Hari ini: " + TODAY + ". Ubah tanggal relatif ("
    "besok, lusa, Jumat depan) menjadi format YYYY-MM-DD sebelum memanggil tool. Bila pengguna "
    "menyebut nama tugas untuk diselesaikan, panggil list_tasks dulu untuk mencari id-nya. Jawab singkat "
    "dalam bahasa Indonesia. Pertanyaan pengetahuan umum dijawab langsung tanpa tool.")

DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it"]
RPM = {"gemini-3.1-flash-lite": 15, "gemini-3.5-flash-lite": 15, "gemma-4-31b-it": 30,
       "gemma-4-26b-a4b-it": 30}          # selain ini dianggap 5 RPM (jeda 13 dtk)


def _p(**props):
    return {"type": "object", "properties": props, "required": [k for k, v in props.items() if v.pop("_req", False)]}


def _s(desc, req=False):
    return {"type": "string", "description": desc, **({"_req": True} if req else {})}


def _i(desc, req=False):
    return {"type": "integer", "description": desc, **({"_req": True} if req else {})}


TOOLS = [
    ToolSpec("search_knowledge",
             "Mencari isi materi kuliah dan catatan. Pakai untuk pertanyaan tentang isi materi atau catatan, "
             "bukan pengetahuan umum.",
             _p(query=_s("Kata kunci atau pertanyaan", True), course=_s("Nama mata kuliah"),
                top_k=_i("Jumlah hasil"))),
    ToolSpec("add_note", "Menyimpan catatan baru dan memasukkannya ke indeks pencarian.",
             _p(content=_s("Isi catatan", True), title=_s("Judul"), course=_s("Mata kuliah"),
                tags={"type": "array", "items": {"type": "string"}, "description": "Tag"})),
    ToolSpec("add_task", "Menambah tugas atau deadline.",
             _p(title=_s("Judul tugas", True), due_date=_s("Tenggat format YYYY-MM-DD"),
                course=_s("Mata kuliah"), notes=_s("Catatan tambahan"))),
    ToolSpec("list_tasks", "Menampilkan daftar tugas.",
             _p(status={"type": "string", "enum": ["pending", "done", "all"], "description": "Status"},
                due_within_days=_i("Tenggat dalam N hari"), course=_s("Mata kuliah"))),
    ToolSpec("complete_task", "Menandai tugas selesai berdasarkan id.", _p(task_id=_i("Id tugas", True))),
    ToolSpec("make_quiz", "Membuat soal latihan dari materi tertentu.",
             _p(topic=_s("Topik", True), num_questions=_i("Jumlah soal"),
                type={"type": "string", "enum": ["multiple_choice", "short_answer"], "description": "Jenis"},
                course=_s("Mata kuliah"))),
]

FAKE_RESULTS = {
    "list_tasks": "Tugas (pending):\n[3] Laporan basis data, deadline 2026-11-10\n[4] Kuis jaringan, deadline 2026-11-12",
    "complete_task": "Tugas 3 ditandai selesai.",
    "add_task": "Tugas ditambahkan dengan id 7.",
    "add_note": "Catatan disimpan dengan id n12.",
    "search_knowledge": "[1] (basis-data/normalisasi.pdf hlm 3, skor 0.71) Normalisasi adalah proses merapikan "
                        "tabel untuk mengurangi redundansi: 1NF, 2NF, 3NF.",
    "make_quiz": "Kuis dibuat: 5 soal.",
}


def _int_eq(v, n):
    try:
        return float(v) == n
    except (TypeError, ValueError):
        return False


# (nama, prompt, fungsi cek(calls, text) -> None bila lulus, selain itu alasan gagal)
def _names(calls):
    return [c[0] for c in calls]


def chk_note_task(calls, text):
    n = _names(calls)
    if "add_task" not in n or "add_note" not in n:
        return f"butuh add_task dan add_note, dapat {n}"
    d = [a.get("due_date") for nm, a in calls if nm == "add_task"][0]
    return None if d == "2026-12-12" else f"due_date={d!r}, harusnya '2026-12-12'"


def chk_list(calls, text):
    return None if calls and calls[0][0] == "list_tasks" else f"panggilan pertama {_names(calls)[:1]}"


def chk_complete(calls, text):
    n = _names(calls)
    if "complete_task" not in n:
        return f"tidak memanggil complete_task ({n})"
    if "list_tasks" not in n or n.index("list_tasks") > n.index("complete_task"):
        return "tidak memanggil list_tasks dulu"
    tid = [a.get("task_id") for nm, a in calls if nm == "complete_task"][0]
    return None if _int_eq(tid, 3) else f"task_id={tid!r}, harusnya 3"


def chk_quiz(calls, text):
    q = [a for nm, a in calls if nm == "make_quiz"]
    if not q:
        return f"tidak memanggil make_quiz ({_names(calls)})"
    a = q[0]
    if "normalisasi" not in str(a.get("topic", "")).lower():
        return f"topic={a.get('topic')!r}"
    return None if _int_eq(a.get("num_questions"), 5) else f"num_questions={a.get('num_questions')!r}"


def chk_search(calls, text):
    q = [a for nm, a in calls if nm == "search_knowledge"]
    if not q:
        return f"tidak memanggil search_knowledge ({_names(calls)})"
    return None if "normalisasi" in str(q[0].get("query", "")).lower() else f"query={q[0].get('query')!r}"


def chk_notool(calls, text):
    if calls:
        return f"memanggil tool tanpa perlu: {_names(calls)}"
    return None if text.strip() else "jawaban kosong"


def chk_rel_date(calls, text):
    q = [a for nm, a in calls if nm == "add_task"]
    if not q:
        return f"tidak memanggil add_task ({_names(calls)})"
    d = q[0].get("due_date")
    return None if d == "2026-11-10" else f"due_date={d!r}, harusnya '2026-11-10'"


CASES = [
    ("catatan+deadline", "Catat: dosen bilang UTS bab 1-4, tanggal 12 Des", chk_note_task),
    ("daftar tugas", "Apa yang harus kukerjakan hari ini?", chk_list),
    ("selesaikan tugas by nama", "Tugas laporan basis data sudah selesai", chk_complete),
    ("kuis", "Kuis aku soal normalisasi, 5 soal", chk_quiz),
    ("tanya materi", "Jelasin apa itu normalisasi di materi basis data kita", chk_search),
    ("tanpa tool", "Ibu kota Prancis apa?", chk_notool),
    ("tanggal relatif", "Tambah tugas: laporan praktikum jaringan, deadline lusa", chk_rel_date),
]


def make_provider(model):
    import config  # noqa: F401  (memuat .env)
    from providers.gemini import GeminiProvider
    return GeminiProvider(model, api_key=os.getenv("GEMINI_API_KEY"))


class Pacer:
    def __init__(self, model, sleep=time.sleep, clock=time.monotonic):
        self.gap = 60.0 / RPM.get(model, 5) * 1.1
        self.last, self.sleep, self.clock = 0.0, sleep, clock

    def wait(self):
        d = self.last + self.gap - self.clock()
        if d > 0:
            self.sleep(d)
        self.last = self.clock()


def run_case(provider, pacer, prompt, max_steps=4):
    msgs = [Message.system(SYSTEM), Message.user(prompt)]
    calls, text, reqs, lat = [], "", 0, 0.0
    for _ in range(max_steps):
        pacer.wait()
        t0 = time.monotonic()
        res = provider.chat(msgs, TOOLS)
        lat += time.monotonic() - t0
        reqs += 1
        text = res.text
        if not res.tool_calls:
            break
        msgs.append(res.to_message())
        for c in res.tool_calls:
            calls.append((c.name, c.args))
            msgs.append(Message.tool(c, FAKE_RESULTS.get(c.name, "ok")))
    return calls, text, reqs, lat


def eval_model(model, provider_factory=make_provider, pacer_factory=Pacer):
    prov, pacer = provider_factory(model), pacer_factory(model)
    passed, reqs, lat, fails = 0, 0, 0.0, []
    for name, prompt, check in CASES:
        try:
            calls, text, r, l = run_case(prov, pacer, prompt)
        except RateLimitError as e:
            fails.append((name, f"429: {str(e)[:100]}"))
            if not e.retryable:
                fails.append(("(dihentikan)", "kuota harian habis"))
                break
            continue
        except ProviderError as e:
            fails.append((name, f"{type(e).__name__}: {str(e)[:140]}"))
            continue
        reqs += r
        lat += l
        why = check(calls, text)
        if why is None:
            passed += 1
        else:
            fails.append((name, why))
    return {"model": model, "passed": passed, "total": len(CASES), "reqs": reqs,
            "avg": (lat / reqs if reqs else None), "fails": fails}


def handoff(models, provider_factory=make_provider, pacer_factory=Pacer):
    """Riwayat tool call dari model A dikirim ke model B (yang terjadi saat failover)."""
    out = []
    provs = {m: provider_factory(m) for m in models}
    pacers = {m: pacer_factory(m) for m in models}
    prompt = "Tambah tugas: laporan praktikum jaringan, deadline lusa"
    for src in [m for m in models if m.startswith("gemini-3")]:
        msgs = [Message.system(SYSTEM), Message.user(prompt)]
        try:
            pacers[src].wait()
            r1 = provs[src].chat(msgs, TOOLS)
        except ProviderError as e:
            out.append((src, "-", f"sumber gagal: {str(e)[:100]}"))
            continue
        if not r1.tool_calls:
            out.append((src, "-", "sumber tidak memanggil tool, dilewati"))
            continue
        msgs.append(r1.to_message())
        for c in r1.tool_calls:
            msgs.append(Message.tool(c, FAKE_RESULTS.get(c.name, "ok")))
        for dst in models:
            try:
                pacers[dst].wait()
                r2 = provs[dst].chat(msgs, TOOLS)
                out.append((src, dst, "OK" if (r2.text or r2.tool_calls) else "respons kosong"))
            except ProviderError as e:
                out.append((src, dst, f"GAGAL {type(e).__name__}: {str(e)[:160]}"))
    return out


def main(argv):
    do_handoff = "--no-handoff" not in argv
    models = [a for a in argv if not a.startswith("--")] or DEFAULT_MODELS
    if not os.getenv("GEMINI_API_KEY") and "config" not in sys.modules:
        import config  # noqa: F401
    results = []
    for m in models:
        print(f"== {m}", flush=True)
        r = eval_model(m)
        results.append(r)
        avg = f"{r['avg']:.1f}s/req" if r["avg"] else "-"
        print(f"   lulus {r['passed']}/{r['total']} | {r['reqs']} request | {avg}")
        for name, why in r["fails"]:
            print(f"   - gagal [{name}]: {why}")
    print("\nUrutan (lulus terbanyak, lalu tercepat):")
    for r in sorted(results, key=lambda r: (-r["passed"], r["avg"] or 99)):
        print(f"  {r['passed']}/{r['total']}  {r['model']}")
    if do_handoff:
        print("\nSerah-terima riwayat tool call (sumber -> tujuan):")
        for src, dst, status in handoff(models):
            print(f"  {src} -> {dst}: {status}")


if __name__ == "__main__":
    main(sys.argv[1:])
