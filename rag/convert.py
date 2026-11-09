"""Konversi format Office lama (.ppt, .doc) ke format modern lewat LibreOffice (headless).

Hasil konversi disimpan di config.CONVERT_CACHE_DIR dengan nama dari sha256 file sumber,
jadi file yang tidak berubah tidak dikonversi ulang. Folder cache SENGAJA di luar
data/materi supaya salinan hasil konversi tidak ikut ter-index sebagai dokumen kedua.

Tiap konversi memakai profil LibreOffice sementara (-env:UserInstallation), supaya tidak
bentrok bila LibreOffice sedang terbuka (tanpa ini perintah headless sering diam-diam gagal).
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import config

log = logging.getLogger(__name__)

_FALLBACK_PATHS = (
    r"C:\Program Files\LibreOffice\program\soffice.exe",
    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
)


class ConvertError(Exception):
    """Konversi gagal; pesannya ditampilkan di laporan /ingest."""


def find_soffice() -> str | None:
    """Lokasi LibreOffice: SOFFICE_PATH di .env, lalu PATH, lalu lokasi umum Windows/macOS."""
    if config.SOFFICE_PATH and Path(config.SOFFICE_PATH).exists():
        return config.SOFFICE_PATH
    for name in ("soffice", "libreoffice"):
        found = shutil.which(name)
        if found:
            return found
    for cand in _FALLBACK_PATHS:
        if Path(cand).exists():
            return cand
    return None


def convert_legacy(src: Path, sha256: str, target_ext: str) -> Path:
    """Konversi `src` (.ppt/.doc) ke `target_ext` (.pptx/.docx); kembalikan path di cache.

    Raise ConvertError bila LibreOffice tidak ada, time out, atau tidak menghasilkan file."""
    cache = Path(config.CONVERT_CACHE_DIR)
    cache.mkdir(parents=True, exist_ok=True)
    out = cache / f"{sha256[:16]}{target_ext}"
    if out.exists() and out.stat().st_size > 0:
        return out

    soffice = find_soffice()
    if not soffice:
        raise ConvertError(f"LibreOffice tidak ditemukan, {src.suffix} tidak bisa dikonversi "
                           f"(pasang LibreOffice atau simpan ulang sebagai {target_ext})")

    with tempfile.TemporaryDirectory(prefix="ssb-lo-") as tmp_s:
        tmp = Path(tmp_s)
        work = tmp / f"src{src.suffix.lower()}"      # nama ASCII sederhana: aman untuk spasi/Unicode
        shutil.copyfile(src, work)
        cmd = [soffice, f"-env:UserInstallation={(tmp / 'profile').as_uri()}", "--headless",
               "--norestore", "--convert-to", target_ext.lstrip("."), "--outdir", str(tmp / "out"),
               str(work)]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=config.CONVERT_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise ConvertError(f"konversi {src.name} melewati batas {config.CONVERT_TIMEOUT_S} detik")
        except OSError as e:
            raise ConvertError(f"LibreOffice tidak bisa dijalankan: {e}")

        produced = tmp / "out" / f"src{target_ext}"
        if not produced.exists() or produced.stat().st_size == 0:
            tail = ((proc.stderr or "").strip() or (proc.stdout or "").strip()).splitlines()
            detail = tail[-1][:160] if tail else "tanpa pesan"
            raise ConvertError(f"LibreOffice gagal mengonversi {src.name} "
                               f"(kode {proc.returncode}: {detail})")
        part = out.with_name(out.name + ".part")
        shutil.move(str(produced), str(part))
        os.replace(part, out)                        # atomik: cache tidak pernah berisi file separuh
    log.info("Dikonversi %s -> %s", src.name, out.name)
    return out


# ---------------------------------------------------------------- pembersihan cache
_CACHE_NAME = re.compile(r"^[0-9a-f]{16}\.(pptx|docx)$")      # pola nama yang dibuat convert_legacy


def current_keys(root) -> set[str]:
    """Kunci cache (sha256[:16]) dari SEMUA file .ppt/.doc yang ada sekarang di `root`.
    Dihitung dari disk, bukan dari hasil discover_files, supaya cache tidak terhapus hanya karena
    LibreOffice sedang tidak ditemukan."""
    keys = set()
    for p in Path(root).rglob("*"):
        if p.is_file() and p.suffix.lower() in config.LEGACY_CONVERT and not p.name.startswith((".", "~$")):
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for blk in iter(lambda: f.read(1 << 20), b""):
                    h.update(blk)
            keys.add(h.hexdigest()[:16])
    return keys


def prune_cache(keep: set[str], part_max_age_s: int = 3600) -> tuple[int, int]:
    """Hapus hasil konversi yang sumbernya sudah dihapus/berubah (kuncinya tidak ada di `keep`),
    plus sisa *.part yang lama. File dengan nama lain tidak disentuh. -> (jumlah file, byte)."""
    cache = Path(config.CONVERT_CACHE_DIR)
    if not cache.is_dir():
        return 0, 0
    n = size = 0
    for f in cache.iterdir():
        if not f.is_file():
            continue
        if f.name.endswith(".part"):
            stale = time.time() - f.stat().st_mtime > part_max_age_s
        else:
            stale = bool(_CACHE_NAME.match(f.name)) and f.stem not in keep
        if not stale:
            continue
        try:
            sz = f.stat().st_size
            f.unlink()
        except OSError:
            continue
        n, size = n + 1, size + sz
    if n:
        log.info("Cache konversi dibersihkan: %d file, %.1f MB", n, size / 1e6)
    return n, size
