# SQL JOIN dan Agregasi

## Jenis JOIN
- INNER JOIN: hanya baris yang cocok di kedua tabel.
- LEFT JOIN: semua baris tabel kiri, plus yang cocok dari tabel kanan. Jika tidak cocok, kolom kanan bernilai NULL.
- RIGHT JOIN: kebalikan LEFT JOIN.
- FULL OUTER JOIN: semua baris dari kedua tabel; yang tidak cocok diisi NULL.

Contoh: menampilkan semua mahasiswa beserta nilainya, termasuk yang belum punya nilai:
SELECT m.nama, n.nilai FROM mahasiswa m LEFT JOIN nilai n ON m.id = n.id_mahasiswa;

## GROUP BY dan HAVING
GROUP BY mengelompokkan baris untuk fungsi agregat (COUNT, SUM, AVG, MIN, MAX).
WHERE memfilter baris sebelum pengelompokan. HAVING memfilter hasil kelompok setelah agregasi.
Contoh: SELECT id_prodi, COUNT(*) FROM mahasiswa GROUP BY id_prodi HAVING COUNT(*) > 30;
