# Query Uji (v0, sesuai file contoh di data/materi)

Ganti query dan hasil yang diharapkan jika kamu mengganti materi contoh dengan materi aslimu.
Kolom terakhir diisi saat pengujian: OK / SALAH / catatan.

| # | Query | Tool yang diharapkan | Hasil yang diharapkan | Tujuan uji | Hasil |
|---|---|---|---|---|---|
| 1 | Apa bedanya 2NF dan 3NF? | search_knowledge | 2NF: ketergantungan parsial pada composite key. 3NF: ketergantungan transitif. Sumber: 01_normalisasi.md | RAG dasar (ID) | |
| 2 | Apa kompleksitas waktu merge sort dan quick sort pada kasus terburuk? | search_knowledge | Merge sort O(n log n), quick sort O(n^2). Sumber: 01_kompleksitas.txt | RAG, format .txt | |
| 3 | What data structure does BFS use and when should I use it? | search_knowledge | Queue; dipakai untuk shortest path di graph tanpa bobot. Sumber: 02_graph_traversal.md | RAG dasar (EN) | |
| 4 | Apa itu convoy effect pada penjadwalan proses? | search_knowledge | Proses pendek menunggu di belakang proses panjang pada FCFS. Sumber: 01_process_scheduling.md | Lintas bahasa (tanya ID, materi EN) | |
| 5 | Bagaimana cara mencegah starvation di priority scheduling? | search_knowledge | Aging: prioritas dinaikkan bertahap | Lintas bahasa | |
| 6 | Ada berapa host usable di subnet /27? | search_knowledge | 30. Sumber: praktikum_subnet.csv | RAG dari CSV | |
| 7 | Berapa bobot UAS mata kuliah Jaringan Komputer? | search_knowledge | 35%. Sumber: penilaian_mata_kuliah.csv | Fakta tepat dari CSV | |
| 8 | Catat: dosen basis data bilang UTS bab normalisasi dan SQL join, tanggal 12 Des | add_note + add_task | Catatan tersimpan dan ter-index; tugas/deadline 12 Des (tahun yang benar) | Dua tool dalam satu pesan, konversi tanggal | |
| 9 | Tadi aku catat apa soal UTS basis data? | search_knowledge | Menemukan catatan dari query 8 | Bukti add_note masuk RAG | |
| 10 | Tambah tugas: laporan praktikum jaringan, deadline Jumat depan | add_task | Tanggal sesuai kalender hari ini; atau agent bertanya jika ambigu | Konversi tanggal relatif | |
| 11 | Apa saja tugas yang belum selesai? | list_tasks | Daftar terurut deadline, memuat tugas dari query 8 dan 10 | list_tasks | |
| 12 | Tugas laporan praktikum jaringan sudah selesai | list_tasks lalu complete_task | Agent mencari id dulu, lalu menandai selesai | Rantai 2 tool, tidak mengarang id | |
| 13 | Kuis aku 3 soal pilihan ganda tentang normalisasi | make_quiz | 3 soal hanya dari 01_normalisasi.md, ada kunci jawaban | make_quiz memakai RAG | |
| 14 | Apa ibu kota Jepang? | (tanpa tool) | Dijawab langsung: Tokyo | Tidak memanggil tool tanpa perlu | |
| 15 | Jelaskan cara kerja protokol QUIC dari materi kuliah | search_knowledge | Mengaku tidak menemukan di materi, tidak mengarang | Tidak berhalusinasi | |

## Uji tambahan (opsional)
- Selesaikan tugas nomor 999 -> complete_task error, agent melapor dengan sopan, tidak crash.
- Pertanyaan yang sama dengan provider cadangan -> tool tetap terpilih dengan benar.
