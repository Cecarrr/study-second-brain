"""Konfigurasi pusat Study Second Brain.

Semua angka yang perlu disetel (model, limit, ukuran chunk, top-k, ambang skor)
ada di sini, bukan di dalam kode. Nilai model bisa ditimpa lewat `.env`
tanpa menyentuh file ini.
"""
from __future__ import annotations

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int | None) -> int | None:
    raw = os.getenv(name, "").strip()
    return int(raw) if raw else default


# ---------------------------------------------------------------- Path
DATA_DIR = BASE_DIR / "data"
MATERI_DIR = DATA_DIR / "materi"          # materi kuliah (subfolder = mata kuliah)
NOTES_DIR = DATA_DIR / "notes"            # catatan hasil add_note
DB_PATH = DATA_DIR / "app.db"             # SQLite: tugas
VECTOR_STORE_PATH = DATA_DIR / "vector_store.db"  # dipakai rag/store.py
USAGE_PATH = DATA_DIR / "usage.json"      # penghitung request harian
LOG_DIR = BASE_DIR / "logs"


def ensure_dirs() -> None:
    for d in (DATA_DIR, MATERI_DIR, NOTES_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)


ensure_dirs()

# ---------------------------------------------------------------- API key
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
API_KEYS = {"gemini": GEMINI_API_KEY, "openrouter": OPENROUTER_API_KEY}

# ---------------------------------------------------------------- Rantai model
# Urutan = prioritas failover (bagian 5A masterfile).
# rpm/tpm/rpd: batas free tier per model. VERIFIKASI di dashboard AI Studio /
# halaman limit OpenRouter, angka ini cepat berubah.
# Nama model dibaca dari .env: CHAT_MODEL (utama), GEMINI_MODEL_FALLBACK
# (cadangan Gemini), OPENROUTER_MODEL (cadangan lintas provider).
PROVIDER_CHAIN: list[dict] = [
    {
        "provider": "gemini",
        "model": os.getenv("CHAT_MODEL", "gemini-3.1-flash-lite"),
        "rpm": 15, "tpm": 250_000, "rpd": 500,
    },
    {
        "provider": "gemini",
        "model": os.getenv("GEMINI_MODEL_FALLBACK", "gemini-3.5-flash-lite"),
        "rpm": 15, "tpm": 250_000, "rpd": 500,
    },
    {
        # Kosongkan OPENROUTER_MODEL di .env untuk menonaktifkan.
        "provider": "openrouter",
        "model": os.getenv("OPENROUTER_MODEL", ""),
        "rpm": 20, "tpm": None, "rpd": 50,
    },
]

# ---------------------------------------------------------------- Embedding
# Dipilih SEKALI. Vektor antar-model tidak kompatibel; tidak ikut failover.
EMBEDDING: dict = {
    "provider": "gemini",
    "model": os.getenv("EMBED_MODEL", "gemini-embedding-001"),
    "dim": _env_int("EMBED_DIM", 768),        # 768 = hemat; hapus angka ini untuk bawaan 3072
    "batch_size": 100,                        # teks per request embed
    "use_task_type": True,                    # RETRIEVAL_DOCUMENT / RETRIEVAL_QUERY
    "rpm": 100, "tpm": 30_000, "rpd": 1000,   # verifikasi di dashboard
}


def embedding_id() -> str:
    """ID model embedding yang disimpan di metadata store."""
    dim = EMBEDDING["dim"] or "default"
    return f"{EMBEDDING['provider']}:{EMBEDDING['model']}:{dim}"


# ---------------------------------------------------------------- Pengendalian kuota
RPM_SAFETY_RATIO = 0.8        # 15 RPM -> batas efektif 12
DAILY_DEV_BUDGET = 300        # target pengembangan per model (dari 500)
FAILOVER_RPD_RATIO = 0.9      # pindah provider bila RPD lokal >= 90% batas
QUOTA_TIMEZONE = "America/Los_Angeles"  # RPD Gemini reset ~tengah malam Pasifik
USAGE_RETENTION_DAYS = 14

RETRY_MAX_ATTEMPTS = 3        # per provider
RETRY_BASE_DELAY = 2.0        # detik; eksponensial: 2, 4, 8, ...
RETRY_MAX_DELAY = 60.0
RETRY_JITTER = 0.25           # tambahan acak 0-25%
REQUEST_TIMEOUT_S = 60

# ---------------------------------------------------------------- LLM
# Gemini 3 menyarankan temperature bawaan (1.0); suhu rendah bisa membuat
# function calling berputar. Isi angka hanya jika benar-benar perlu.
LLM_TEMPERATURE: float | None = None
LLM_MAX_OUTPUT_TOKENS: int | None = None

# ---------------------------------------------------------------- Agent
MAX_ITERATIONS = 6            # tool call per pesan pengguna
HISTORY_MAX_TURNS = 6         # giliran terakhir yang disimpan

# ---------------------------------------------------------------- RAG
# Ukuran chunk: konten baru <= CHUNK_SIZE; dengan overlap ukuran akhir
# paling besar sekitar CHUNK_SIZE + CHUNK_OVERLAP (~790 karakter).
CHUNK_SIZE = 700
CHUNK_OVERLAP = 90            # ~13%
CHUNK_MERGE_BELOW = 300       # segmen pendek (slide dll.) digabung sampai ~ini
CHUNK_DROP_BELOW = 30         # buang chunk dengan < 30 karakter non-spasi
TOP_K = 5
MIN_SCORE = 0.50              # awal; setel saat uji retrieval (hari 1, blok 5)

# Cadangan pencarian kata kunci (BM25) saat embedding query tidak tersedia (rag/bm25.py).
KEYWORD_FALLBACK = True       # False = perilaku lama: error layanan dikembalikan ke agent
KEYWORD_MIN_COVERAGE = 0.5    # porsi minimal kata query (setelah stopword dibuang) yang harus cocok
BM25_K1 = 1.5
BM25_B = 0.75
EMBED_FAIL_COOLDOWN_S = 120   # setelah embedding query gagal, langsung pakai kata kunci selama ini

# ---------------------------------------------------------------- Ingest
STRUCTURED_EXTENSIONS = {".md", ".txt", ".csv", ".pdf", ".docx", ".pptx", ".ipynb"}
TEXT_LIKE_EXTENSIONS = {
    ".py", ".ino", ".c", ".cpp", ".h", ".java", ".js", ".ts", ".json",
    ".yml", ".yaml", ".sql", ".sh", ".toml", ".ini", ".cfg", ".rst",
}
SUPPORTED_EXTENSIONS = STRUCTURED_EXTENSIONS | TEXT_LIKE_EXTENSIONS

# Format Office lama dikonversi otomatis lewat LibreOffice (rag/convert.py). Tanpa LibreOffice
# file ini dilewati dengan petunjuk "simpan ulang sebagai ...".
LEGACY_CONVERT = {".ppt": ".pptx", ".doc": ".docx"}
SOFFICE_PATH = os.getenv("SOFFICE_PATH", "").strip()   # kosong = cari otomatis
CONVERT_CACHE_DIR = DATA_DIR / "converted"             # di luar MATERI_DIR (jangan ter-index ganda)
CONVERT_TIMEOUT_S = 180

# Notebook Jupyter: sel markdown + sel kode (+ keluaran teks singkat); gambar diabaikan.
IPYNB_INCLUDE_OUTPUTS = True
IPYNB_OUTPUT_MAX_CHARS = 400

PDF_MIN_CHARS_PER_PAGE = 30   # di bawah ini dianggap PDF scan dan dilewati
MAX_CSV_ROWS = 5000

# ---------------------------------------------------------------- CLI
SUMMARY_ON_START = True       # tampilkan ringkasan tugas saat CLI dibuka (tanpa request model)
SUMMARY_DUE_DAYS = 3          # tugas dengan tenggat dalam N hari ke depan dianggap mendesak


# ---------------------------------------------------------------- Helper
def get_limits(provider: str, model: str) -> dict:
    """Batas rpm/tpm/rpd untuk provider+model, {} bila tidak dikenal."""
    for entry in PROVIDER_CHAIN:
        if entry["provider"] == provider and entry["model"] == model:
            return entry
    if EMBEDDING["provider"] == provider and EMBEDDING["model"] == model:
        return EMBEDDING
    return {}


def active_chain() -> list[dict]:
    """Entri rantai yang punya nama model dan API key."""
    return [e for e in PROVIDER_CHAIN if e["model"] and API_KEYS.get(e["provider"])]


def missing_keys() -> list[str]:
    problems = []
    if not GEMINI_API_KEY:
        problems.append("GEMINI_API_KEY belum diisi di .env")
    return problems


def setup_logging(level: int = logging.INFO) -> None:
    """Log ke logs/app.log (berputar) dan peringatan ke konsol."""
    ensure_dirs()
    root = logging.getLogger()
    if any(getattr(h, "_ssb", False) for h in root.handlers):
        return
    root.setLevel(level)
    fh = RotatingFileHandler(LOG_DIR / "app.log", maxBytes=1_000_000,
                             backupCount=3, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    fh._ssb = True  # type: ignore[attr-defined]
    root.addHandler(fh)
