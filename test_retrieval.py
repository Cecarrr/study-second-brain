"""Uji retrieval memakai query di tests/queries.md (baris yang tool-nya search_knowledge).

  python test_retrieval.py                     semua query di tests/queries.md
  python test_retrieval.py -q "apa itu 3NF"    satu query bebas
  python test_retrieval.py --mock              offline (embedding palsu, kurang akurat)
  python test_retrieval.py -k 5 --course "Basis Data"

Penilaian otomatis hanya untuk baris yang kolom hasilnya memuat "Sumber: namafile"
(HIT bila file itu muncul di top-k dan skornya lolos MIN_SCORE) atau "tidak menemukan"
(OK bila tidak ada hasil yang lolos). Di akhir dicetak saran MIN_SCORE.
"""
import argparse
import re
import sys
from pathlib import Path

import config
from providers.base import ProviderError
from rag.stack import open_stack
from rag.store import EmbeddingMismatchError
from usage import tracker


def load_queries(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[0].isdigit():
            continue
        num, query, tool, expected = cells[0], cells[1], cells[2], cells[3]
        if "search_knowledge" not in tool:
            continue
        m = re.search(r"Sumber:\s*([\w\-./]+\.\w+)", expected)
        rows.append({"num": num, "query": query, "expected": expected,
                     "source": m.group(1) if m else None,
                     "negative": "tidak menemukan" in expected.lower()})
    return rows


def show(res, k):
    for i, h in enumerate(res.hits_all[:k], 1):
        ok = "ok" if h.score >= res.min_score else "x "
        loc = f" ({h.location})" if h.location else ""
        head = f" | {h.metadata.get('heading')}" if h.metadata.get("heading") else ""
        print(f"    {i}. [{ok}] {h.score:.3f}  {h.source}{loc}{head}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-q", "--query")
    ap.add_argument("-k", type=int, default=config.TOP_K)
    ap.add_argument("--course")
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--file", default=str(config.BASE_DIR / "tests" / "queries.md"))
    args = ap.parse_args()

    try:
        store, embedder, retriever = open_stack(mock=args.mock)
    except (ProviderError, EmbeddingMismatchError) as e:
        sys.exit(f"Gagal membuka indeks: {e}")
    if len(store) == 0:
        sys.exit("Indeks kosong. Jalankan dulu: python index_materi.py" + (" --mock" if args.mock else ""))

    def run(query):
        # ambil top-k tanpa ambang supaya skor rendah juga terlihat
        r = retriever.search(query, k=args.k, course=args.course, min_score=-1.0)
        r.hits_all = r.hits
        r.min_score = config.MIN_SCORE
        r.hits = [h for h in r.hits_all if h.score >= config.MIN_SCORE]
        return r

    try:
        if args.query:
            r = run(args.query)
            print(f"Query: {args.query}  (MIN_SCORE={config.MIN_SCORE})")
            show(r, args.k)
            print("\nYang akan dilihat LLM:\n" + r.format())
            return

        rows = load_queries(args.file)
        hits_ok = total = 0
        good_scores, bad_scores = [], []
        for row in rows:
            r = run(row["query"])
            print(f"#{row['num']} {row['query']}")
            show(r, args.k)
            if row["source"]:
                total += 1
                found = [h for h in r.hits_all if h.source.endswith(row["source"])]
                passed = [h for h in r.hits if h.source.endswith(row["source"])]
                if found:
                    good_scores.append(max(h.score for h in found))
                rank = next((i for i, h in enumerate(r.hits_all, 1)
                             if h.source.endswith(row["source"])), None)
                if passed:
                    hits_ok += 1
                    print(f"    -> HIT (sumber {row['source']} di peringkat {rank})")
                else:
                    print(f"    -> MISS (sumber {row['source']} "
                          f"{'peringkat ' + str(rank) + ' tapi di bawah MIN_SCORE' if rank else 'tidak ada di top-k'})")
            elif row["negative"]:
                best = r.hits_all[0].score if r.hits_all else 0.0
                bad_scores.append(best)
                print("    -> OK (tidak ada hasil lolos)" if not r.hits
                      else f"    -> SALAH: ada {len(r.hits)} hasil lolos MIN_SCORE (harusnya kosong)")
            print()

        print("=" * 60)
        if total:
            print(f"Hit rate (dengan sumber): {hits_ok}/{total}")
        if good_scores:
            print(f"Skor terendah untuk sumber yang benar : {min(good_scores):.3f}")
        if bad_scores:
            print(f"Skor tertinggi untuk query tak relevan: {max(bad_scores):.3f}")
        if good_scores and bad_scores:
            lo, hi = max(bad_scores), min(good_scores)
            if hi > lo:
                print(f"Saran MIN_SCORE: antara {lo:.2f} dan {hi:.2f} (mis. {(lo + hi) / 2:.2f})")
            else:
                print("Skor benar dan salah tumpang tindih: ambang tunggal tidak cukup; "
                      "perbaiki chunking/query dulu.")
        print()
        print(tracker.report())
    except ProviderError as e:
        sys.exit(f"Berhenti: {e}")


if __name__ == "__main__":
    main()
