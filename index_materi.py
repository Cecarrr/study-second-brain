"""Indeks materi dan catatan ke vector store.

  python index_materi.py              sinkron: hanya file baru/berubah/dihapus
  python index_materi.py --rebuild    kosongkan indeks, indeks ulang semua (cache dipakai)
  python index_materi.py --stats      tampilkan isi indeks dan pemakaian request
  python index_materi.py --mock       embedding palsu offline (0 request)
"""
import argparse
import sys

import config
from providers.base import ProviderError
from rag.embedder import Embedder  # noqa: F401  (memastikan modul termuat)
from rag.indexer import SyncReport, sync_folder
from rag.stack import open_stack
from rag.store import EmbeddingMismatchError
from usage import tracker


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()

    try:
        store, embedder, _ = open_stack(mock=args.mock, rebuild=args.rebuild)
    except (ProviderError, EmbeddingMismatchError) as e:
        sys.exit(f"Gagal membuka indeks: {e}")

    if args.stats:
        s = store.stats()
        print(f"Embedding : {s['embedding_id']} (dimensi {s['dim']})")
        print(f"Dokumen   : {s['documents']} | total chunk: {s['chunks']}")
        for c, n in s["chunks_per_course"].items():
            print(f"  {c}: {n} chunk")
        print()
        print(tracker.report())
        return

    def progress(done, total):
        print(f"  embedding {done}/{total} teks")

    report = SyncReport()
    try:
        for root, st in ((config.MATERI_DIR, "materi"), (config.NOTES_DIR, "catatan")):
            print(f"Memindai {root} ...")
            sync_folder(root, st, store, embedder, report, progress)
    except ProviderError as e:
        print("\n".join(report.lines()))
        sys.exit(f"\nBerhenti: {e}\nHasil embedding yang sudah selesai tersimpan di cache; "
                 "jalankan perintah yang sama lagi untuk melanjutkan.")
    print("\n".join(report.lines()))
    print()
    print(tracker.report())


if __name__ == "__main__":
    main()
