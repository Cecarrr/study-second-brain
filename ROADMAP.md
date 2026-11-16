# Project Roadmap & Architecture - Study Second Brain

Dokumen ini mendokumentasikan arsitektur, modul, serta fitur lengkap yang telah diimplementasikan dalam **Study Second Brain** (Fitur A sampai G).

---

## 1. Arsitektur & Fitur yang Telah Diimplementasikan

Sistem dirancang sebagai asisten belajar berbasis AI dan *Second Brain* lokal dengan pendekatan *Retrieval-Augmented Generation (RAG)*. Berikut adalah rincian modul dan implementasi fitur A hingga G:

### A. Backup, Restore, & Schema Versioning (`backup.py`, `db/database.py`)
- **Schema Versioning:** Tabel `schema_metadata(version INTEGER)` melacak versi skema database (saat ini Versi 1).
- **Backup & Restore:** Fungsi `create_backup()` mengemas database SQLite, direktori vektor RAG, dan manifes `meta.json` (checksum SHA-256) ke dalam arsip `.zip`. Fungsi `restore_backup()` memulihkan data setelah memvalidasi kompatibilitas versi skema secara aman.

### B. Perintah Diagnostik `/doctor` (`doctor.py`)
- **System Health Check:** Memeriksa keberadaan `.env`, konfigurasi `pool.toml`, ketersediaan API key, integritas database SQLite (`PRAGMA integrity_check;`), serta status penyimpanan RAG dan deteksi file yatim.

### C. Peningkatan Aksesibilitas Impor (`rag/importers.py`)
- **Multi-source Ingestion:** Mendukung pengunduhan dan parsing konten dari tautan publik Google Docs/Sheets serta pemindaian folder Markdown (Obsidian / Notion vault) secara rekursif.

### D. Penulisan Ulang Query & Penyaring Sumber (`rag/retriever.py`)
- **Hybrid & Filtered Retrieval:** Mendukung penyaringan hasil retrieval berdasarkan jenis sumber (`source_type`) serta penanganan kueri bilingual.

### E. Spaced Repetition & Automated Flashcards (`tools/quiz.py`, `db/database.py`)
- **Spaced Repetition (Algoritma SM-2):** Tabel `flashcards` dan logika penjadwalan kartu berbasis interval pengulangan berjarak berdasarkan penilaian kualitas ulasan pengguna.

### F. Sinkronisasi Kalender (`tools/calendar_sync.py`)
- **iCalendar Exporter:** Mengonversi data jadwal kuliah (`schedule`) dan tenggat tugas (`tasks`) menjadi format standar `.ics` untuk sinkronisasi kalender di *smartphone*.

### G. Persistent Memory & User Preferences (`db/database.py`)
- **Cross-session Memory & Prefs:** Tabel `conversation_memory` untuk riwayat percakapan lintas sesi dan `user_preferences` untuk menyimpan preferensi personal belajar pengguna.

---

## 2. Rencana Pengembangan Selanjutnya (Future UI & Advanced Features)
- **Antarmuka Web / Dashboard:** Pengembangan antarmuka interaktif berbasis web setelah seluruh fondasi backend stabil.
- **Advanced OCR & Table Extraction:** Peningkatan lebih lanjut pada ekstraksi tabel kompleks dan teks gambar slide.
