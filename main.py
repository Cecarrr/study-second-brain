"""CLI Study Second Brain.  python main.py

Perintah: /today (ringkasan tugas) /tasks (daftar tugas) /notes (daftar catatan), ketiganya tanpa
model; /ingest /usage /reset /help /exit
"""
import logging
import sys

import config
import dashboard
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
from tools import sources, trash
from tools.registry import build_registry
from tools.summary import build_daily_summary

log = logging.getLogger(__name__)

HELP = ("Perintah: /today /tasks /notes /ingest /source [N] /undo /mode simple|normal /dashboard /privacy "
        "/trash /restore N /purge /usage /reset /help /exit. "
        "Selain itu, tulis saja seperti biasa.")


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
    agent = Agent(reg, chat_fn, courses_fn=store.courses, today_fn=ctx.today, notify=print)
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
