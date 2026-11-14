"""CLI Study Second Brain.  python main.py

Perintah: /today (ringkasan tugas) /tasks (daftar tugas) /notes (daftar catatan), ketiganya tanpa
model; /ingest /usage /reset /help /exit
"""
import logging
import sys

import config
import dashboard
import schedule_import
import keypool
import prefs
import privacy
from agent import Agent
from db.database import TaskDB
from providers.base import ProviderError, ToolCall
from rag.indexer import SyncReport, sync_folder
from rag.stack import open_stack
from rag.store import EmbeddingMismatchError
from tools.common import ToolContext
from rag import importers
from tools import sources, trash
from tools.registry import build_registry
from tools.summary import build_daily_summary

log = logging.getLogger(__name__)

HELP = ("Perintah: /today /tasks /notes /jadwal [hari] /import-jadwal FILE /import SUMBER /ingest /source [N] /undo /mode simple|normal /dashboard /privacy "
        "/trash /restore N /purge /usage /reset /help /exit. "
        "Selain itu, tulis saja seperti biasa.")


def do_import(ctx, store, embedder, argline: str) -> str:
    """/import SUMBER [--course NAMA] [--title JUDUL] [--no-index] | --check (tanpa LLM)."""
    args = importers.parse_args(argline)
    if args.check:
        return importers.check_deps_text()
    if not args.source:
        return importers.USAGE
    try:
        results = importers.run_import(args)
    except importers.ImportFailed as e:
        return f"Impor gagal: {e}"
    report = "\n".join(["Hasil impor:"] + [r.line() for r in results])
    if not args.no_index and any(r.dest for r in results):
        print(report)
        ingest(store, embedder)                         # memakai jalur indeks biasa (cache + batas kuota)
        return "Selesai. Coba tanyakan isinya, atau /source untuk melihat sumbernya."
    return report + "\nIndeks belum diperbarui; jalankan /ingest."


def import_jadwal(ctx, reg, path: str) -> str:
    """/import-jadwal FILE.csv|.ics (tanpa LLM). Tanpa argumen: tampilkan format CSV."""
    from pathlib import Path
    if not path:
        return "Pemakaian: /import-jadwal FILE.csv atau FILE.ics\nContoh isi CSV:\n" + schedule_import.TEMPLATE
    p = Path(path)
    if not p.is_file() or p.suffix.lower() not in (".csv", ".ics"):
        return f"File '{path}' tidak ditemukan atau bukan .csv/.ics."
    before = reg.journal.snapshot(ctx)
    report = schedule_import.import_text(ctx, p.read_text(encoding="utf-8-sig", errors="replace"), p.suffix.lower()[1:])
    if reg.journal.snapshot(ctx) != before:
        reg.journal.push(f"import-jadwal {p.name}", before)          # bisa dibatalkan dengan /undo
    return report


def ingest(store, embedder):
    rep = SyncReport()
    try:
        for root, st in ((config.MATERI_DIR, "materi"), (config.NOTES_DIR, "catatan")):
            sync_folder(root, st, store, embedder, rep, lambda d, t: print(f"  embedding {d}/{t}"))
    except ProviderError as e:
        print("\n".join(rep.lines()))
        print(f"Berhenti: {str(e).splitlines()[0]}\nProgres tersimpan di cache; jalankan /ingest lagi nanti.")
        return
    print("\n".join(rep.lines()))


def main():
    config.setup_logging()
    try:
        store, embedder, retriever = open_stack()
    except (ProviderError, EmbeddingMismatchError, ValueError) as e:
        sys.exit(f"Gagal membuka indeks: {e}")
    ctx = ToolContext(retriever=retriever, store=store, embedder=embedder, tasks=TaskDB())
    reg = build_registry(ctx)
    chat_fn = ((lambda m, t=None: keypool.chat(privacy.redact_messages(m), t))
               if privacy.mode() == "strict" else keypool.chat)
    agent = Agent(reg, chat_fn, courses_fn=ctx.known_courses, today_fn=ctx.today, notify=print)
    agent.style = prefs.load().get("mode", "normal")
    print(f"Study Second Brain | indeks: {len(store)} chunk. {HELP}")
    if config.SUMMARY_ON_START:
        try:
            print("\n" + build_daily_summary(ctx.tasks))
        except Exception as e:  # noqa: BLE001  ringkasan tidak boleh menggagalkan CLI
            log.exception("Ringkasan harian gagal")
            print(f"\n(Ringkasan harian gagal dibuat: {type(e).__name__}: {e})")
    while True:
        try:
            text = input("\nkamu> ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if not text:
            continue
        cmd = text.lower()
        if cmd in ("/exit", "/quit"):
            break
        try:
            if cmd == "/help":
                print(HELP)
            elif cmd == "/reset":
                agent.reset()
                print("Riwayat percakapan dikosongkan.")
            elif cmd == "/usage":
                print(keypool.usage_report())
            elif cmd == "/today":
                print(build_daily_summary(ctx.tasks))
            elif cmd == "/tasks":
                print(reg.execute(ToolCall(name="list_tasks", args={})).content)
            elif cmd == "/notes":
                print(reg.execute(ToolCall(name="list_notes", args={})).content)
            elif cmd == "/ingest":
                ingest(store, embedder)
            elif cmd.startswith("/source"):
                print(sources.show_source(ctx, text[7:]))
            elif cmd == "/undo":
                print(reg.journal.undo(ctx))
            elif cmd == "/dashboard":
                print(dashboard.build_dashboard(ctx))
            elif cmd == "/privacy":
                print(f"Mode privasi: {privacy.mode()} (off | logs | strict; ubah PRIVACY_MODE di config.py). "
                      "logs = samarkan data sensitif di log; strict = juga di pesan ke model chat.")
            elif cmd.startswith("/mode"):
                arg = cmd[5:].strip()
                if arg in ("simple", "normal"):
                    agent.style = arg
                    prefs.save(mode=arg)
                    print(f"Mode jawaban: {arg}.")
                else:
                    print(f"Mode sekarang: {agent.style}. Pakai: /mode simple  atau  /mode normal")
            elif cmd == "/import" or cmd.startswith("/import "):
                print(do_import(ctx, store, embedder, text[7:].strip()))
            elif cmd.startswith("/jadwal"):
                print(reg.execute(ToolCall(name="list_schedule", args={"day": text[7:].strip() or "minggu ini"})).content)
            elif cmd.startswith("/import-jadwal"):
                print(import_jadwal(ctx, reg, text[14:].strip().strip('"')))
            elif cmd == "/trash":
                print(trash.list_trash(ctx))
            elif cmd.startswith("/restore"):
                arg = text.split(maxsplit=1)[1].strip().lstrip("#") if " " in text else ""
                print(trash.restore(ctx, int(arg)) if arg.isdigit() else "Pemakaian: /restore NOMOR (lihat /trash)")
            elif cmd == "/purge":
                if input("Buang SEMUA isi tempat sampah secara permanen? (ketik ya): ").strip().lower() == "ya":
                    print(trash.purge(ctx))
                else:
                    print("Dibatalkan.")
            else:
                print("\nasisten> " + agent.ask(text))
        except KeyboardInterrupt:
            print("\n(dibatalkan)")
        except Exception as e:  # noqa: BLE001  satu perintah gagal tidak boleh menutup CLI
            log.exception("Perintah gagal: %s", text[:80])
            print(f"\nMaaf, terjadi kesalahan: {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
