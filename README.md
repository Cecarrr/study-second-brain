# Study Second Brain

**Study Second Brain** adalah asisten AI dan sistem *Second Brain* berbasis *Retrieval-Augmented Generation (RAG)* yang dirancang khusus untuk membantu proses belajar dan manajemen akademik (catatan, jadwal, tugas, kuis, dan materi referensi).

---

## 🚀 Perintah Developer

- **Menjalankan semua tes:** 
  ```bash
  py -3.13 -m pytest -o pythonpath=. tests
  ```
- **Menjalankan tes Keypool:** 
  ```bash
  py -3.13 -m pytest -o pythonpath=. keypool/tests
  ```
- **Menjalankan aplikasi utama:** 
  ```bash
  py -3.13 main.py
  ```

---

## 🏗️ Arsitektur & Modul

- **`rag/`**: Pipeline RAG (chunking, embedding, indexing, retrieval, BM25, storage, ingestion, importers).
- **`tools/`**: Modul domain spesifik (notes, jadwal, knowledge, quiz, tasks, summary, sources, trash, select, registry).
- **`keypool/`**: Sistem rotasi API key, pelacakan kuota, dan router (`pool.toml`, quota, router, pooled, openai_compat).
- **`providers/`**: Abstraksi provider LLM (Gemini dan Mock).
- **`db/`**: Penyimpanan data relasional SQLite (`db/database.py`).

---

## 🗺️ Rencana Pengembangan Selanjutnya (Roadmap)

Sesuai urutan prioritas pengembangan backend:
1. **Fondasi & Keandalan:** Perintah `/backup`, `/restore-backup` (dengan versioning skema database), dan perintah `/doctor` untuk diagnostik sistem, API key, dan kesehatan indeks RAG.
2. **Perluasan Impor Data:** Dukungan Google Docs/Slides/Sheets publik, folder Obsidian/Notion (Markdown), repo GitHub, ekstraksi tabel PDF, dan OCR teks pada gambar slide.
3. **Penyempurnaan RAG & Retrieval:** Penulisan ulang query (multi-query, *cross-language* ID/EN) dan penyaring jenis sumber dengan referensi halaman/slide.
4. **Fitur Belajar & Produktivitas:** *Spaced repetition* dan generator *flashcard* otomatis dari ringkasan materi, serta sinkronisasi jadwal/tugas ke Google Calendar / format `.ics`.
5. **Personalisasi:** *Persistent memory* untuk riwayat percakapan lintas sesi dan preferensi belajar personal.

*(Catatan: Antarmuka berbasis Web/UI akan dikembangkan setelah seluruh rangkaian fitur backend di atas selesai).*

---

## ⚙️ Konvensi & Lingkungan

- **Bahasa:** Bahasa Indonesia digunakan untuk perintah prompt, dokumentasi, pesan antarmuka, dan respons agen.
- **Environment:** Membutuhkan Python 3.13+, file konfigurasi `.env`, dan `pool.toml`.
