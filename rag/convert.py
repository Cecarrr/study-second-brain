"""Konversi format Office lama (.ppt, .doc) ke format modern lewat LibreOffice (headless).

Hasil konversi disimpan di config.CONVERT_CACHE_DIR dengan nama dari sha256 file sumber,
jadi file yang tidak berubah tidak dikonversi ulang. Folder cache SENGAJA di luar
data/materi supaya salinan hasil konversi tidak ikut ter-index sebagai dokumen kedua.

Tiap konversi memakai profil LibreOffice sementara (-env:UserInstallation), supaya tidak
bentrok bila LibreOffice sedang terbuka (tanpa ini perintah headless sering diam-diam gagal).
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
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
