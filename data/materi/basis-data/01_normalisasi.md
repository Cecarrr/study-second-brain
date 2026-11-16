# Normalisasi Database

Normalisasi adalah proses menyusun tabel supaya data tidak redundan dan anomali (insert, update, delete) bisa dihindari.

## Bentuk Normal Pertama (1NF)
Syarat 1NF: setiap kolom berisi nilai atomik (satu nilai saja) dan tidak ada kelompok kolom yang berulang.
Contoh yang salah: kolom `hobi` berisi "membaca, futsal". Perbaikan: pisahkan menjadi satu baris per hobi.

## Bentuk Normal Kedua (2NF)
Syarat 2NF: sudah 1NF dan tidak ada ketergantungan parsial. Atribut non-kunci harus bergantung pada seluruh primary key, bukan hanya sebagian. Ini relevan untuk composite key.
Contoh: tabel (id_mahasiswa, kode_mk, nama_mk, nilai). Kolom nama_mk hanya bergantung pada kode_mk, jadi dipisah ke tabel mata_kuliah.

## Bentuk Normal Ketiga (3NF)
Syarat 3NF: sudah 2NF dan tidak ada ketergantungan transitif. Atribut non-kunci tidak boleh bergantung pada atribut non-kunci lain.
Contoh: tabel (id_mhs, kode_prodi, nama_prodi). nama_prodi bergantung pada kode_prodi, bukan pada id_mhs, jadi dipisah ke tabel prodi.

## BCNF
BCNF lebih ketat dari 3NF: setiap determinan harus berupa superkey.

## Anomali
- Insert anomaly: data tidak bisa ditambah karena data lain belum ada.
- Update anomaly: satu data berubah, tetapi harus diubah di banyak baris.
- Delete anomaly: menghapus satu baris ikut menghilangkan informasi lain yang penting.

Catatan dosen: soal normalisasi hampir pasti keluar di UTS. Biasanya diberi tabel awal dan diminta menormalkan sampai 3NF.
