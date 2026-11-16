# Hasil evaluasi model - 2026-11-10

Skenario: 22 | mode: model sungguhan | request terpakai: 47 dari 100 | subset tool: aktif

Lulus/gagal dinilai dari isi DB/file dan argumen tool, bukan dari teks jawaban. Tingkat lulus = lulus / (lulus + gagal); `error` (gangguan API) dan `lewat` (anggaran/kuota habis) tidak dihitung.

## Ringkasan

| Model | Lulus | Gagal | Error | Lewat | Tingkat lulus | Request/skenario | Error tool | Total request | Latensi/request |
|---|---|---|---|---|---|---|---|---|---|
| gemini-3.5-flash-lite | 21 | 1 | 0 | 0 | 95% | 2.0 | 0/22 (0%) | 44 | 4.6 dtk |
| gemini-3.1-flash-lite | 0 | 0 | 3 | 19 | - (0/3) | - | 0/0 (-) | 3 | 6.3 dtk |

## Skenario x model

| Skenario | gemini-3.1-flash-lite | gemini-3.5-flash-lite |
|---|---|---|
| catat+tenggat | error | lulus |
| catat-tanpa-tenggat | error | lulus |
| tugas-jumat | error | lulus |
| tugas-lusa | lewat | lulus |
| tugas-tanpa-tanggal | lewat | lulus |
| tanggal-lampau | lewat | lulus |
| tanpa-duplikat | lewat | lulus |
| selesai-judul | lewat | lulus |
| selesai-ambigu | lewat | GAGAL |
| dikumpulkan-bukan-hapus | lewat | lulus |
| ubah-tenggat | lewat | lulus |
| ganti-judul | lewat | lulus |
| hapus-tugas | lewat | lulus |
| daftar-tugas | lewat | lulus |
| daftar-catatan | lewat | lulus |
| tambah-ke-catatan | lewat | lulus |
| hapus-catatan | lewat | lulus |
| materi-id | lewat | lulus |
| materi-en | lewat | lulus |
| kuis-tanpa-kunci | lewat | lulus |
| luar-materi-id | lewat | lulus |
| luar-materi-en | lewat | lulus |

## Detail yang tidak lulus

- **gemini-3.1-flash-lite / catat+tenggat** (error): ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand 
- **gemini-3.1-flash-lite / catat-tanpa-tenggat** (error): ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand 
- **gemini-3.1-flash-lite / tugas-jumat** (error): ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand 
- **gemini-3.1-flash-lite / tugas-lusa** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / tugas-tanpa-tanggal** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / tanggal-lampau** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / tanpa-duplikat** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / selesai-judul** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / selesai-ambigu** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / dikumpulkan-bukan-hapus** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / ubah-tenggat** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / ganti-judul** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / hapus-tugas** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / daftar-tugas** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / daftar-catatan** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / tambah-ke-catatan** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / hapus-catatan** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / materi-id** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / materi-en** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / kuis-tanpa-kunci** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / luar-materi-id** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.1-flash-lite / luar-materi-en** (lewat): 3 error beruntun (ServerError: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This); model dilewati
- **gemini-3.5-flash-lite / selesai-ambigu** (GAGAL): menyelesaikan tugas padahal ambigu: ['Kuis 1']
