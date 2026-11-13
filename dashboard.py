"""Dasbor pemakaian: python dashboard.py  atau  /dashboard di CLI. Tanpa request API."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import config


def _read_log(path: Path, days: int) -> list[dict]:
    rows, cutoff = [], (datetime.now() - timedelta(days=days)).isoformat()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("t", "") >= cutoff:
                rows.append(r)
    except OSError:
        pass
    return rows


def _p95(vals: list[int]) -> int:
    v = sorted(vals)
    return v[min(len(v) - 1, int(len(v) * 0.95))] if v else 0


def build_dashboard(ctx=None, log_path=None, days: int = 7, usage_report=None) -> str:
    rows = _read_log(Path(log_path or config.LOG_DIR / "tools.jsonl"), days)
    out = [f"== PANGGILAN TOOL ({days} hari terakhir, {len(rows)} panggilan) =="]
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[r["tool"]].append(r)
    if by:
        out.append(f"{'tool':<18}{'panggilan':>10}{'error':>9}{'rata2 ms':>10}{'p95 ms':>9}")
        for name, rs in sorted(by.items(), key=lambda kv: -len(kv[1])):
            errs = sum(1 for r in rs if not r.get("ok", True))
            ms = [int(r.get("ms", 0)) for r in rs]
            err = f"{errs} ({errs * 100 // len(rs)}%)"
            out.append(f"{name:<18}{len(rs):>10}{err:>9}{sum(ms) // len(ms):>10}{_p95(ms):>9}")
        top = Counter(r["out"][:70] for r in rows if not r.get("ok", True)).most_common(3)
        if top:
            out += ["", "Error tersering:"] + [f"  {n}x {msg}" for msg, n in top]
        per_day = Counter(r["t"][:10] for r in rows)
        out += ["", "Aktivitas harian:"]
        for i in range(days - 1, -1, -1):
            d = (date.today() - timedelta(days=i)).isoformat()
            n = per_day.get(d, 0)
            out.append(f"  {d} {'#' * min(n, 40):<40} {n}")
    else:
        out.append("(belum ada log; jalankan beberapa perintah dulu)")

    if ctx is not None:
        try:
            tasks = ctx.tasks.list("all")
            today = ctx.today().isoformat()
            pend = [t for t in tasks if t["status"] == "pending"]
            late = [t for t in pend if t["due_date"] and t["due_date"] < today]
            notes = len(list(ctx.notes_dir.glob("*.md"))) if ctx.notes_dir.exists() else 0
            st = ctx.store.stats()
            out += ["", "== DATA ==",
                    f"Tugas: {len(pend)} belum selesai ({len(late)} terlambat), "
                    f"{len(tasks) - len(pend)} selesai",
                    f"Catatan: {notes} | Tempat sampah: {len(ctx.tasks.trash_list())} item",
                    f"Jadwal: {len(ctx.tasks.sched_list())} sesi aktif ({len(ctx.tasks.sched_courses())} mata kuliah)",
                    f"Indeks: {st['chunks']} chunk, dokumen {st['documents']}"]
        except Exception as e:  # noqa: BLE001
            out.append(f"(data tidak terbaca: {type(e).__name__}: {e})")
    if usage_report is None:
        try:
            import keypool
            usage_report = keypool.usage_report
        except Exception:  # noqa: BLE001
            usage_report = None
    if usage_report:
        try:
            out += ["", "== KUOTA ==", usage_report()]
        except Exception as e:  # noqa: BLE001
            out.append(f"(kuota tidak terbaca: {type(e).__name__})")
    return "\n".join(out)


if __name__ == "__main__":
    print(build_dashboard())
