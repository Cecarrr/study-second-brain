"""Evaluasi model untuk Study Second Brain: tabel hasil per model untuk demo.

Menjalankan AGENT ASLI (prompt sistem, 11 tool, validasi argumen, pencegah panggilan ganda) pada
skenario di eval_scenarios.py. Tiap skenario memakai DB, folder catatan, dan indeks materi
SEMENTARA (embedding palsu offline), jadi data asli tidak tersentuh. Lulus/gagal ditentukan dari
isi DB/file dan argumen tool, bukan dari teks jawaban model.

  python eval_models.py --mock                              -> offline, 0 request (MockProvider berskrip)
  python eval_models.py                                     -> 4 model bawaan, ~2-3 request/skenario
  python eval_models.py --models gemini-3.1-flash-lite,gemma-4-31b-it
  python eval_models.py --max-requests 80 --out hasil.md    -> berhenti bila anggaran habis; tulis tabel
  python eval_models.py --out hasil.csv                     -> format panjang (satu baris per model x skenario)
  python eval_models.py --only tugas-jumat,hapus-tugas      -> sebagian skenario
  python eval_models.py --list                              -> daftar skenario
  python eval_models.py --handoff                           -> tambahan: uji serah-terima riwayat tool call
  python eval_models.py --no-subset                         -> kirim SEMUA tool (config.TOOL_SUBSET=False), pembanding subset

Argumen posisi (python eval_models.py gemini-3.1-flash-lite) tetap diterima, seperti versi lama.
Pemakaian: GEMINI_API_KEY (satu account) lewat GeminiProvider; kuis (make_quiz) memanggil model yang
sama dan ikut dihitung. Model berkuota 20 RPD boleh dimasukkan, tetapi satu run menghabiskan sebagian
besar jatah hariannya. Bila kuota harian habis (429 tidak bisa diulang, sama seperti PoolExhausted di
keypool), sisa skenario model itu dilewati, bukan dihitung gagal. Status per skenario:
lulus | gagal | error (API/infrastruktur, tidak dihitung dalam tingkat lulus) | lewat (anggaran/kuota habis).
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from providers.base import (ChatResponse, Message, ProviderError, RateLimitError)

from agent import WRITE_TOOLS, Agent
from db.database import TaskDB
from eval_scenarios import MATERI, SCENARIOS, TODAY, Call, Run, Scenario
from prompts import build_system_prompt
from providers.mock import MockProvider
from rag.embedder import Embedder
from rag.indexer import index_document
from rag.ingest import discover_files, read_document
from rag.retriever import Retriever
from rag.store import VectorStore
from tools import knowledge
from tools.common import ToolContext
from tools.registry import build_registry

DEFAULT_MODELS = ["gemini-3.1-flash-lite", "gemini-3.5-flash-lite", "gemma-4-31b-it", "gemma-4-26b-a4b-it"]
RPM = {"gemini-3.1-flash-lite": 15, "gemini-3.5-flash-lite": 15, "gemma-4-31b-it": 30,
       "gemma-4-26b-a4b-it": 30}          # selain ini dianggap 5 RPM (jeda 13 dtk)
MOCK_MODELS = ["mock-ideal", "mock-lazy"]   # ideal = menjalankan script tiap skenario; lazy = hanya membalas teks

PASS, FAIL, ERROR, SKIP = "lulus", "gagal", "error", "lewat"


# ---------------------------------------------------------------- anggaran, jeda, pencatat request
class BudgetExhausted(RateLimitError):
    """Anggaran --max-requests habis. Subclass RateLimitError(retryable=False), seperti PoolExhausted."""

    def __init__(self, used: int, limit: int):
        super().__init__(f"Anggaran --max-requests={limit} habis ({used} request terpakai).",
                         provider="eval", retryable=False)


class Budget:
    def __init__(self, limit: int | None = None):
        self.limit, self.used = limit, 0

    def take(self) -> None:
        if self.limit is not None and self.used >= self.limit:
            raise BudgetExhausted(self.used, self.limit)
        self.used += 1


class Pacer:
    def __init__(self, model, sleep=time.sleep, clock=time.monotonic):
        self.gap = 60.0 / RPM.get(model, 5) * 1.1
        self.last, self.sleep, self.clock = 0.0, sleep, clock

    def wait(self):
        d = self.last + self.gap - self.clock()
        if d > 0:
            self.sleep(d)
        self.last = self.clock()


class NullPacer:
    def wait(self):
        pass


class Meter:
    """Pembungkus model: hitung request dan latensi, jaga anggaran dan jeda, catat error provider.

    `chat` dipakai agent; `aux_chat` dipakai tool yang memanggil LLM (make_quiz) lewat ctx.llm_chat.
    """

    def __init__(self, pacer, budget: Budget, clock=time.monotonic):
        self.pacer, self.budget, self.clock = pacer, budget, clock
        self.provider = None
        self.aux = None                      # None = pakai provider yang sama
        self.requests, self.latency = 0, 0.0
        self.errors: list[ProviderError] = []

    def _send(self, target, messages, tools):
        try:
            self.budget.take()
        except ProviderError as e:
            self.errors.append(e)
            raise
        self.pacer.wait()
        t0 = self.clock()
        self.requests += 1
        try:
            return target.chat(messages, tools)
        except ProviderError as e:
            self.errors.append(e)
            raise
        finally:
            self.latency += self.clock() - t0

    def chat(self, messages, tools=None):
        return self._send(self.provider, messages, tools)

    def aux_chat(self, messages, tools=None):
        return self._send(self.aux or self.provider, messages, tools)

    def mark(self) -> tuple[int, float, int]:
        return self.requests, self.latency, len(self.errors)


# ---------------------------------------------------------------- lingkungan sementara per skenario
class _QuietEmbedder(Embedder):
    def _record(self, n_items, tokens, ok):      # jangan menulis data/usage.json untuk embedding palsu
        pass


class Env:
    """DB tugas, folder catatan, dan indeks materi sementara di bawah `root`. Hari ini = TODAY."""

    def __init__(self, root: Path, today: date = TODAY):
        self.root = Path(root)
        materi = self.root / "materi"
        for rel, text in MATERI.items():
            p = materi / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        emb = _QuietEmbedder(MockProvider(embed_dim=256))
        self.store = VectorStore(self.root / "vs.db", emb.embedding_id)
        refs, _ = discover_files(materi, "materi")     # bukan sync_folder: itu membersihkan cache konversi asli
        for ref in refs:
            index_document(read_document(ref), self.store, emb)
        self.tasks = TaskDB(self.root / "app.db", today_fn=lambda: today)
        self.ctx = ToolContext(retriever=Retriever(self.store, emb, min_score=0.1), store=self.store,
                               embedder=emb, tasks=self.tasks, notes_dir=self.root / "notes",
                               log_path=self.root / "tools.jsonl")
        self.reg = build_registry(self.ctx)
        self._base = None

    # --- seed
    def add_note(self, content: str, title: str, course: str | None = None) -> None:
        knowledge.add_note(self.ctx, {"content": content, "title": title, **({"course": course} if course else {})})

    # --- keadaan (dipakai pemeriksa)
    def notes(self) -> dict[str, str]:
        d = self.ctx.notes_dir
        return {p.name: p.read_text(encoding="utf-8") for p in sorted(d.glob("*.md"))} if d.exists() else {}

    def trash(self) -> list[dict]:
        return self.tasks.trash_list()

    def _state(self):
        return self.tasks.dump_state(), self.notes()

    def freeze(self) -> None:
        self._base = self._state()

    def changed(self) -> bool:
        return self._base is not None and self._state() != self._base

    def close(self) -> None:
        try:
            self.store.close()
        except Exception:      # noqa: BLE001
            pass


# ---------------------------------------------------------------- provider
class _QuizLLM:
    """Model palsu untuk make_quiz pada mode --mock: selalu mengembalikan JSON soal yang valid."""

    def chat(self, messages, tools=None):
        m = re.search(r"Jumlah soal:\s*(\d+)", messages[-1].content)
        n = int(m.group(1)) if m else 3
        qs = [{"question": f"Soal contoh nomor {i} tentang bentuk normal?",
               "options": ["Opsi satu", "Opsi dua", "Opsi tiga", "Opsi empat"], "answer": "B",
               "explanation": f"Penjelasan kunci rahasia nomor {i} dari materi normalisasi.",
               "source": "normalisasi.md"} for i in range(1, n + 1)]
        return ChatResponse(text=json.dumps({"questions": qs}, ensure_ascii=False), finish_reason="STOP")


def make_provider(model, sc=None):
    """(provider chat, provider tambahan atau None). Model asli: satu GeminiProvider."""
    import config  # noqa: F401  (memuat .env)
    from providers.gemini import GeminiProvider
    return GeminiProvider(model, api_key=os.getenv("GEMINI_API_KEY")), None


def make_mock_provider(model, sc=None):
    script = list(sc.script) if (sc is not None and model == "mock-ideal") else []
    return MockProvider(script=script, model=model), _QuizLLM()


make_mock_provider.per_scenario = True
make_provider.per_scenario = False


# ---------------------------------------------------------------- hasil
@dataclass
class Result:
    scenario: str
    status: str
    reason: str = ""
    requests: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    secs: float = 0.0


@dataclass
class ModelReport:
    model: str
    results: list[Result] = field(default_factory=list)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def ran(self) -> list[Result]:
        return [r for r in self.results if r.status in (PASS, FAIL)]

    @property
    def pass_rate(self) -> float | None:
        return self.count(PASS) / len(self.ran) if self.ran else None

    @property
    def avg_requests(self) -> float | None:
        return sum(r.requests for r in self.ran) / len(self.ran) if self.ran else None

    @property
    def tool_calls(self) -> int:
        return sum(r.tool_calls for r in self.results)

    @property
    def tool_errors(self) -> int:
        return sum(r.tool_errors for r in self.results)

    @property
    def tool_error_rate(self) -> float | None:
        return self.tool_errors / self.tool_calls if self.tool_calls else None

    @property
    def total_requests(self) -> int:
        return sum(r.requests for r in self.results)

    @property
    def avg_latency(self) -> float | None:
        secs = sum(r.secs for r in self.results)
        return secs / self.total_requests if self.total_requests else None


# ---------------------------------------------------------------- penilaian
def judge(sc: Scenario, env: Env, run: Run) -> str | None:
    """None bila lulus, selain itu alasan gagal. Urutan tool longgar."""
    names = [c.name for c in run.calls]
    seen = names or "tanpa tool"
    if sc.no_tools and names:
        return f"memanggil tool tanpa perlu: {names}"
    missing = [t for t in sc.expect if t not in names]
    if missing:
        return f"tidak memanggil {', '.join(missing)} (dapat: {seen})"
    if sc.expect_any and not any(t in names for t in sc.expect_any):
        return f"tidak memanggil satu pun dari {', '.join(sc.expect_any)} (dapat: {seen})"
    bad = [t for t in sc.forbid if t in names]
    if bad:
        return f"memanggil tool terlarang: {', '.join(bad)}"
    if sc.readonly:
        wrote = [n for n in names if n in WRITE_TOOLS]
        if wrote:
            return f"memanggil tool tulis pada permintaan baca: {wrote}"
    if (sc.readonly or sc.no_tools) and env.changed():
        return "data berubah padahal permintaan tidak mengubah apa pun"
    if run.answer.startswith("Aku mencapai batas langkah"):
        return "mencapai batas langkah agent"
    if run.answer.startswith("(model tidak memberi jawaban"):
        return "model tidak memberi jawaban akhir"
    return sc.check(env, run) if sc.check else None


def run_scenario(sc: Scenario, meter: Meter, workdir: Path) -> Result:
    env = Env(workdir)
    try:
        if sc.seed:
            sc.seed(env)
        env.freeze()
        env.ctx.llm_chat = meter.aux_chat
        calls: list[Call] = []
        orig = env.reg.execute

        def recording_execute(call):
            m = orig(call)
            calls.append(Call(call.name, dict(call.args), not m.is_error, m.content))
            return m

        env.reg.execute = recording_execute
        agent = Agent(env.reg, meter.chat, courses_fn=env.store.courses, today_fn=env.ctx.today,
                      notify=lambda s: None)
        req0, lat0, err0 = meter.mark()
        answer = agent.ask(sc.message)
        req1, lat1, err1 = meter.mark()
        errs = meter.errors[err0:err1]
        base = dict(scenario=sc.id, requests=req1 - req0, secs=lat1 - lat0, tool_calls=len(calls),
                    tool_errors=sum(1 for c in calls if not c.ok))
        if errs:
            e = errs[0]
            if isinstance(e, BudgetExhausted):
                return Result(status=SKIP, reason="anggaran --max-requests habis", **base)
            if isinstance(e, RateLimitError) and not e.retryable:
                return Result(status=SKIP, reason="kuota harian habis", **base)
            return Result(status=ERROR, reason=f"{type(e).__name__}: {str(e).splitlines()[0][:120]}", **base)
        why = judge(sc, env, Run(calls=calls, answer=answer, requests=req1 - req0))
        return Result(status=PASS if why is None else FAIL, reason=why or "", **base)
    finally:
        env.close()


def eval_model(model: str, scenarios: list[Scenario], provider_factory=make_provider,
               pacer_factory=Pacer, budget: Budget | None = None, progress=None) -> ModelReport:
    budget = budget or Budget()
    meter = Meter(pacer_factory(model), budget)
    report = ModelReport(model)
    halted: str | None = None
    prov = aux = None
    if not getattr(provider_factory, "per_scenario", False):
        prov, aux = provider_factory(model)
    with tempfile.TemporaryDirectory(prefix="ssb-eval-", ignore_cleanup_errors=True) as tmp:
        for i, sc in enumerate(scenarios):
            if halted:
                r = Result(sc.id, SKIP, halted)
            else:
                if getattr(provider_factory, "per_scenario", False):
                    prov, aux = provider_factory(model, sc)
                meter.provider, meter.aux = prov, aux
                r = run_scenario(sc, meter, Path(tmp) / f"{i:02d}")
                if r.status == SKIP:
                    halted = r.reason
            report.results.append(r)
            if progress:
                progress(model, r)
    return report


# ---------------------------------------------------------------- keluaran
def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x * 100:.0f}%"


def _num(x: float | None, nd: int = 1, unit: str = "") -> str:
    return "-" if x is None else f"{x:.{nd}f}{unit}"


def summary_rows(reports: list[ModelReport]) -> list[list[str]]:
    rows = []
    for r in sorted(reports, key=lambda r: (-(r.pass_rate or 0), r.avg_requests or 99)):
        rows.append([r.model, str(r.count(PASS)), str(r.count(FAIL)), str(r.count(ERROR)), str(r.count(SKIP)),
                     _pct(r.pass_rate), _num(r.avg_requests),
                     f"{r.tool_errors}/{r.tool_calls} ({_pct(r.tool_error_rate)})", str(r.total_requests),
                     _num(r.avg_latency, 1, " dtk")])
    return rows


SUMMARY_HEAD = ["Model", "Lulus", "Gagal", "Error", "Lewat", "Tingkat lulus", "Request/skenario",
                "Error tool", "Total request", "Latensi/request"]


def _cell(r: Result | None) -> str:
    return {PASS: "lulus", FAIL: "GAGAL", ERROR: "error", SKIP: "lewat", None: "-"}[r.status if r else None]


def format_text(reports: list[ModelReport], scenarios: list[Scenario]) -> str:
    out = []
    for r in reports:
        out.append(f"== {r.model}: lulus {r.count(PASS)}/{len(r.ran)} | {r.count(ERROR)} error, "
                   f"{r.count(SKIP)} lewat | {r.total_requests} request")
        for x in r.results:
            if x.status != PASS:
                out.append(f"   - {_cell(x)} [{x.scenario}]: {x.reason}")
    out.append("\nRingkasan:")
    rows = [SUMMARY_HEAD] + summary_rows(reports)
    w = [max(len(row[i]) for row in rows) for i in range(len(SUMMARY_HEAD))]
    for row in rows:
        out.append("  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(row)))
    return "\n".join(out)


def _esc(s: str) -> str:
    return s.replace("|", "\\|").replace("\n", " ")


def format_md(reports: list[ModelReport], scenarios: list[Scenario], mock: bool = False,
              budget: Budget | None = None, subset: bool | None = None) -> str:
    mode = "MOCK (offline, bukan hasil model sungguhan)" if mock else "model sungguhan"
    used = f" | request terpakai: {budget.used}" + (f" dari {budget.limit}" if budget.limit else "") if budget else ""
    L = [f"# Hasil evaluasi model - {date.today().isoformat()}", "",
         f"Skenario: {len(scenarios)} | mode: {mode}{used}"
         + ("" if subset is None else f" | subset tool: {'aktif' if subset else 'mati (semua tool)'}"), "",
         "Lulus/gagal dinilai dari isi DB/file dan argumen tool, bukan dari teks jawaban. "
         "Tingkat lulus = lulus / (lulus + gagal); `error` (gangguan API) dan `lewat` "
         "(anggaran/kuota habis) tidak dihitung.", "", "## Ringkasan", ""]
    L.append("| " + " | ".join(SUMMARY_HEAD) + " |")
    L.append("|" + "---|" * len(SUMMARY_HEAD))
    L += ["| " + " | ".join(_esc(c) for c in row) + " |" for row in summary_rows(reports)]
    L += ["", "## Skenario x model", "", "| Skenario | " + " | ".join(_esc(r.model) for r in reports) + " |",
          "|---|" + "---|" * len(reports)]
    for sc in scenarios:
        cells = []
        for r in reports:
            x = next((x for x in r.results if x.scenario == sc.id), None)
            cells.append(_cell(x))
        L.append(f"| {sc.id} | " + " | ".join(cells) + " |")
    bad = [(r.model, x) for r in reports for x in r.results if x.status != PASS]
    if bad:
        L += ["", "## Detail yang tidak lulus", ""]
        L += [f"- **{_esc(m)} / {x.scenario}** ({_cell(x)}): {_esc(x.reason)}" for m, x in bad]
    return "\n".join(L) + "\n"


def format_csv(reports: list[ModelReport], scenarios: list[Scenario]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["model", "scenario", "status", "reason", "requests", "tool_calls", "tool_errors", "secs"])
    for r in reports:
        for x in r.results:
            w.writerow([r.model, x.scenario, x.status, x.reason, x.requests, x.tool_calls, x.tool_errors,
                        f"{x.secs:.2f}"])
    return buf.getvalue()


def write_out(path: str, reports, scenarios, mock=False, budget=None, subset=None) -> None:
    ext = Path(path).suffix.lower()
    text = format_csv(reports, scenarios) if ext == ".csv" else format_md(reports, scenarios, mock, budget, subset)
    Path(path).write_text(text, encoding="utf-8", newline="")


# ---------------------------------------------------------------- uji serah-terima (failover)
HANDOFF_RESULTS = {"add_task": "Tugas ditambahkan: #7 [belum] Laporan praktikum jaringan | tenggat 2026-11-11"}


def handoff(models, provider_factory=make_provider, pacer_factory=Pacer, budget: Budget | None = None):
    """Riwayat tool call dari model A dikirim ke model B (yang terjadi saat failover)."""
    budget = budget or Budget()
    out = []
    with tempfile.TemporaryDirectory(prefix="ssb-eval-", ignore_cleanup_errors=True) as tmp:
        env = Env(Path(tmp))
        try:
            specs, system = env.reg.specs(), build_system_prompt(TODAY, env.store.courses())
        finally:
            env.close()
    provs = {m: provider_factory(m)[0] for m in models}
    pacers = {m: pacer_factory(m) for m in models}
    prompt = "Tambah tugas: laporan praktikum jaringan, deadline lusa"

    def send(m, msgs):
        budget.take()
        pacers[m].wait()
        return provs[m].chat(msgs, specs)

    for src in [m for m in models if m.startswith("gemini-3")]:
        msgs = [Message.system(system), Message.user(prompt)]
        try:
            r1 = send(src, msgs)
        except ProviderError as e:
            out.append((src, "-", f"sumber gagal: {str(e)[:100]}"))
            if isinstance(e, BudgetExhausted):
                return out
            continue
        if not r1.tool_calls:
            out.append((src, "-", "sumber tidak memanggil tool, dilewati"))
            continue
        msgs.append(r1.to_message())
        for c in r1.tool_calls:
            msgs.append(Message.tool(c, HANDOFF_RESULTS.get(c.name, "ok")))
        for dst in models:
            try:
                r2 = send(dst, msgs)
                out.append((src, dst, "OK" if (r2.text or r2.tool_calls) else "respons kosong"))
            except BudgetExhausted as e:
                out.append((src, dst, f"dilewati: {e}"))
                return out
            except ProviderError as e:
                out.append((src, dst, f"GAGAL {type(e).__name__}: {str(e)[:160]}"))
    return out


# ---------------------------------------------------------------- CLI
def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="eval_models.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("models_pos", nargs="*", metavar="MODEL", help="nama model (versi lama; boleh banyak)")
    ap.add_argument("--models", help="daftar model dipisah koma, mis. a,b")
    ap.add_argument("--mock", action="store_true", help="offline: MockProvider berskrip, 0 request")
    ap.add_argument("--max-requests", type=int, metavar="N", help="berhenti bila total request mencapai N")
    ap.add_argument("--out", metavar="FILE", help="tulis tabel hasil (.md atau .csv)")
    ap.add_argument("--only", help="id skenario dipisah koma (lihat --list)")
    ap.add_argument("--list", action="store_true", help="tampilkan daftar skenario lalu keluar")
    ap.add_argument("--handoff", action="store_true", help="jalankan juga uji serah-terima riwayat tool call")
    ap.add_argument("--no-subset", action="store_true",
                    help="matikan subset tool dinamis (kirim semua tool) selama run ini, untuk membandingkan")
    ap.add_argument("--no-handoff", action="store_true", help="(kompatibilitas) tidak berpengaruh; handoff mati kecuali --handoff")
    return ap


def main(argv=None, provider_factory=None, pacer_factory=None, out=print) -> int:
    ap = _parser()
    a = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if a.list:
        for sc in SCENARIOS:
            out(f"{sc.id:26} {sc.message}")
        return 0
    if a.out and Path(a.out).suffix.lower() not in (".md", ".csv"):
        ap.error("--out harus berakhiran .md atau .csv")
    if a.max_requests is not None and a.max_requests < 1:
        ap.error("--max-requests harus >= 1")
    scenarios = SCENARIOS
    if a.only:
        ids = [s.strip() for s in a.only.split(",") if s.strip()]
        unknown = [i for i in ids if i not in {s.id for s in SCENARIOS}]
        if unknown:
            ap.error(f"skenario tidak dikenal: {', '.join(unknown)} (lihat --list)")
        scenarios = [s for s in SCENARIOS if s.id in ids]
    models = [m.strip() for m in (a.models or "").split(",") if m.strip()] + a.models_pos
    if a.mock:
        models = models or MOCK_MODELS
        bad = [m for m in models if m not in MOCK_MODELS]
        if bad:
            ap.error(f"mode --mock hanya mengenal: {', '.join(MOCK_MODELS)}")
        provider_factory = provider_factory or make_mock_provider
        pacer_factory = pacer_factory or (lambda m: NullPacer())
    else:
        models = models or DEFAULT_MODELS
        provider_factory = provider_factory or make_provider
        pacer_factory = pacer_factory or Pacer
        import config  # noqa: F401  (memuat .env)
        if not os.getenv("GEMINI_API_KEY"):
            ap.error("GEMINI_API_KEY belum diisi di .env (atau pakai --mock untuk uji offline)")

    import config
    old_subset = getattr(config, "TOOL_SUBSET", True)
    if a.no_subset:
        config.TOOL_SUBSET = False
    subset = bool(getattr(config, "TOOL_SUBSET", True))
    budget = Budget(a.max_requests)
    reports: list[ModelReport] = []

    def progress(model, r):
        mark = _cell(r)
        out(f"   [{mark:5}] {r.scenario}" + (f": {r.reason}" if r.status != PASS else ""))

    out(f"Subset tool: {'aktif' if subset else 'mati (semua tool dikirim)'}")
    try:
        for m in models:
            out(f"== {m}")
            reports.append(eval_model(m, scenarios, provider_factory, pacer_factory, budget, progress))
        out("\n" + format_text(reports, scenarios))
        if a.handoff and not a.mock:
            out("\nSerah-terima riwayat tool call (sumber -> tujuan):")
            for src, dst, status in handoff(models, provider_factory, pacer_factory, budget):
                out(f"  {src} -> {dst}: {status}")
    finally:
        config.TOOL_SUBSET = old_subset
    if a.out:
        write_out(a.out, reports, scenarios, mock=a.mock, budget=budget, subset=subset)
        out(f"\nTabel hasil ditulis ke {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
