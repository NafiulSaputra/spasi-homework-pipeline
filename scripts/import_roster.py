"""
scripts/import_roster.py

Import roster siswa dari CSV ke master_students, dengan hashing (pepper)
dan enkripsi (AES) yang BENAR -- menggantikan pendekatan lama yang pakai
md5() mentah langsung di SQL (tidak bisa membawa pepper rahasia maupun
enkripsi reversible untuk nomor HP).

Format CSV wajib punya kolom: nisn,nama,no_absen,kelas,no_hp

Pakai:
    python scripts/import_roster.py roster.csv SCH-001
"""

import sys
import csv
import secrets
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.security import hash_nisn, hash_kode_aktivasi, normalize_name, encrypt_phone
from app.db import db_cursor


def generate_kode_aktivasi() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def import_roster(csv_path: str, school_id: str):
    with open(csv_path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    kode_aktivasi_plain = {}  # nisn -> kode asli, untuk dicetak & dibagikan guru

    with db_cursor() as (conn, cur):
        for row in rows:
            nisn = row["nisn"].strip()
            kode = generate_kode_aktivasi()

            cur.execute(
                """
                INSERT INTO master_students
                    (nisn_hash, school_id, nama_asli, nama_normalized, no_absen,
                     kelas, no_hp_encrypted, kode_aktivasi_hash, kode_aktivasi_status)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'belum_dipakai')
                ON CONFLICT (nisn_hash) DO NOTHING
                """,
                (
                    hash_nisn(nisn), school_id, row["nama"], normalize_name(row["nama"]),
                    row["no_absen"], row["kelas"], encrypt_phone(row["no_hp"]),
                    hash_kode_aktivasi(kode),
                ),
            )
            # Hanya catat kode kalau baris benar-benar tersimpan. Kalau siswa
            # sudah ada (ON CONFLICT DO NOTHING), kode baru ini TIDAK berlaku
            # dan tidak boleh dibagikan ke siswa.
            if cur.rowcount == 1:
                kode_aktivasi_plain[nisn] = kode
            else:
                print(f"NISN {nisn} sudah ada di roster, dilewati (kode lama tetap berlaku).")

    print(f"\n{len(kode_aktivasi_plain)} dari {len(rows)} siswa diimpor ke sekolah {school_id}.")
    print("=== KODE AKTIVASI (bagikan ke siswa secara FISIK, JANGAN lewat chat grup) ===")
    for nisn, kode in kode_aktivasi_plain.items():
        print(f"NISN {nisn}: {kode}")
    return kode_aktivasi_plain


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Pakai: python scripts/import_roster.py roster.csv SCH-001")
        sys.exit(1)
    import_roster(sys.argv[1], sys.argv[2])
