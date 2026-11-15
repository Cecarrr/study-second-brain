"""Pemeriksaan keamanan konfigurasi dan repo.  python security_check.py   atau  /security di CLI.
Kode keluar 1 bila ada temuan GAGAL. Hanya membaca; tidak mengubah apa pun, tanpa request API."""
from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from datetime import datetime, timedelta
from pathlib import Path

import config
import privacy

ROOT = Path(config.BASE_DIR)


def _git(*args) -> str | None:
    try:
        p = subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=15)
        return p.stdout if p.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def _read_text(path: Path) -> tuple[str, str]:
    """Baca teks dengan deteksi BOM (PowerShell `>` menulis UTF-16). -> (teks, nama encoding)."""
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", errors="replace"), "utf-16"
    return raw.decode("utf-8-sig", errors="replace"), "utf-8"


def scan_logs(log_dir: Path) -> list[str]:
    """Nama file log yang masih memuat kunci API atau kredensial."""
    bad = []
    for f in sorted(Path(log_dir).glob("*")) if Path(log_dir).is_dir() else []:
        if f.suffix in (".log", ".jsonl") and f.is_file():
            text = f.read_bytes()[-2_000_000:].decode("utf-8", errors="replace")
            if {"KUNCI-API", "KREDENSIAL"} & set(privacy.redact(text)[1]):
                bad.append(f.name)
    return bad


def run_checks() -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    add = lambda lvl, msg: out.append((lvl, msg))  # noqa: E731

    tracked = _git("ls-files")
    if tracked is None:
        add("warn", "git tidak tersedia atau bukan repo: pemeriksaan berkas terlacak dilewati")
    else:
        bad = [f for f in tracked.splitlines() if f in (".env", "pool.toml") or f.startswith(("data/", "logs/", ".vscode/"))
               or f.endswith((".db", ".db-wal", ".db-shm"))]
        add("fail" if bad else "ok", ("berkas sensitif terlacak git: " + ", ".join(bad[:6])) if bad
            else "tidak ada .env/pool.toml/data/logs/*.db yang terlacak git")

    gi = (ROOT / ".gitignore")
    lines = {l.strip().rstrip("/") for l in gi.read_text(encoding="utf-8").splitlines()} if gi.exists() else set()
    missing = [p for p in (".env", "pool.toml", "data", "logs", "*.db") if p not in lines]
    add("fail" if missing else "ok", ("di .gitignore belum ada: " + ", ".join(missing)) if missing else ".gitignore menutup .env, pool.toml, data/, logs/, *.db")

    pool = ROOT / "pool.toml"
    if pool.exists():
        try:
            accs = tomllib.loads(pool.read_text(encoding="utf-8")).get("account", [])
            literal = [a.get("name", "?") for a in accs if a.get("key")]
            add("warn" if literal else "ok", ("pool.toml memuat key langsung (pakai key_env): " + ", ".join(literal)) if literal
                else "pool.toml memakai key_env (tidak ada key tertulis)")
        except (tomllib.TOMLDecodeError, OSError) as e:
            add("warn", f"pool.toml tidak terbaca: {e}")

    leaks = scan_logs(config.LOG_DIR)
    add("fail" if leaks else "ok", ("log memuat kunci/kredensial: " + ", ".join(leaks)) if leaks else "log bersih dari kunci API dan kredensial")

    mode = getattr(config, "PRIVACY_MODE", "logs")
    add("warn" if mode == "off" else "ok", f"PRIVACY_MODE={mode}" + (" (penyamaran mati)" if mode == "off" else ""))
    add("fail" if getattr(config, "IMPORT_ALLOW_PRIVATE", False) else "ok",
        "IMPORT_ALLOW_PRIVATE aktif: impor boleh mengakses alamat lokal (risiko SSRF)" if getattr(config, "IMPORT_ALLOW_PRIVATE", False)
        else "impor menolak alamat lokal/privat")
    add("ok", f"batas keamanan: aksi tulis/giliran={getattr(config, 'SECURITY_MAX_WRITES', 8)}, argumen={getattr(config, 'SECURITY_MAX_ARG_CHARS', 20000)} karakter, "
              f"keluaran tool={getattr(config, 'SECURITY_TOOL_OUTPUT_MAX', 12000)}, file={getattr(config, 'MAX_FILE_MB', 100)} MB")

    req = ROOT / "requirements.txt"
    if req.exists():
        text, enc = _read_text(req)
        if enc != "utf-8":
            add("warn", "requirements.txt berenkode UTF-16 (dari PowerShell `>`); simpan ulang sebagai UTF-8: "
                        "python -m pip freeze | Out-File -Encoding utf8 requirements.txt")
        loose = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#") and "==" not in l]
        add("warn" if loose else "ok", (f"{len(loose)} paket di requirements.txt belum dipin versinya; simpan versi terpasang: "
                                        "python -m pip freeze > requirements.lock.txt") if loose else "requirements.txt memakai versi terpin")

    env = ROOT / ".env"
    if env.exists() and sys.platform != "win32" and env.stat().st_mode & 0o077:
        add("warn", ".env dapat dibaca pengguna lain: chmod 600 .env")

    ev = Path(config.LOG_DIR) / "security.jsonl"
    if ev.exists():
        cutoff, counts = (datetime.now() - timedelta(days=7)).isoformat(), {}
        for line in ev.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue
            if r.get("t", "") >= cutoff:
                counts[r.get("event", "?")] = counts.get(r.get("event", "?"), 0) + 1
        add("ok", "kejadian keamanan 7 hari terakhir: " + (", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "tidak ada"))
    return out


def report() -> tuple[str, bool]:
    tag = {"ok": "[ok]   ", "warn": "[PERHATIAN]", "fail": "[GAGAL]"}
    res = run_checks()
    lines = [f"{tag[l]} {m}" for l, m in res]
    fails = sum(1 for l, _ in res if l == "fail")
    lines.append(f"\n{sum(1 for l, _ in res if l == 'ok')} aman, {sum(1 for l, _ in res if l == 'warn')} perhatian, {fails} gagal.")
    return "\n".join(lines), fails == 0


if __name__ == "__main__":
    text, ok = report()
    print(text)
    sys.exit(0 if ok else 1)
