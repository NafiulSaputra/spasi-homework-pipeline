"""
tests/test_import_roster.py

Memastikan import roster: (1) menyimpan NISN sebagai hash dan nomor HP
terenkripsi (bukan plaintext), (2) TIDAK mengeluarkan kode aktivasi untuk
siswa yang sudah ada di roster (kode itu tidak akan pernah berlaku).
"""

import os
import sys
import types
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("NIS_HASH_PEPPER", "pepper-test")
os.environ.setdefault("PHONE_ENCRYPTION_KEY", "kunci-test")

try:
    import psycopg2  # noqa: F401
except ImportError:
    sys.modules["psycopg2"] = types.ModuleType("psycopg2")

from scripts import import_roster as ir  # noqa: E402
from app.security import hash_nisn, hash_kode_aktivasi, decrypt_phone  # noqa: E402


class ImportRosterTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute(
            "CREATE TABLE master_students (nisn_hash TEXT UNIQUE NOT NULL, school_id TEXT, "
            "nama_asli TEXT, nama_normalized TEXT, no_absen TEXT, kelas TEXT, "
            "no_hp_encrypted TEXT, kode_aktivasi_hash TEXT, kode_aktivasi_status TEXT)")
        conn = self.conn

        @contextmanager
        def sqlite_cursor():
            class Cur:
                def __init__(self):
                    self._c = conn.cursor()

                def execute(self, sql, params=()):
                    self._c.execute(sql.replace("%s", "?"), params)

                @property
                def rowcount(self):
                    return self._c.rowcount

            yield conn, Cur()
            conn.commit()

        self.patch = mock.patch.object(ir, "db_cursor", sqlite_cursor)
        self.patch.start()

        f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="")
        f.write("nisn,nama,no_absen,kelas,no_hp\n"
                "8435,Siswa Kakak,1,XI-2,081200000001\n"
                "8436,Siswa Adik,2,X-1,081200000001\n")  # kakak-adik berbagi 1 nomor HP
        f.close()
        self.csv = f.name

    def tearDown(self):
        self.patch.stop()
        os.remove(self.csv)

    def test_hash_enkripsi_dan_nomor_hp_bersama(self):
        kode = ir.import_roster(self.csv, "SCH-001")
        self.assertEqual(set(kode), {"8435", "8436"})

        rows = self.conn.execute(
            "SELECT nisn_hash, nama_normalized, no_hp_encrypted, kode_aktivasi_hash "
            "FROM master_students ORDER BY no_absen").fetchall()
        self.assertEqual(rows[0][0], hash_nisn("8435"))
        self.assertEqual(rows[0][1], "siswa kakak")
        self.assertNotIn("081200000001", rows[0][2])
        self.assertEqual(decrypt_phone(rows[0][2]), "081200000001")
        self.assertEqual(decrypt_phone(rows[1][2]), "081200000001")
        self.assertEqual(rows[0][3], hash_kode_aktivasi(kode["8435"]))

    def test_import_ulang_tidak_mengeluarkan_kode_palsu(self):
        pertama = ir.import_roster(self.csv, "SCH-001")
        kedua = ir.import_roster(self.csv, "SCH-001")
        self.assertEqual(kedua, {})
        # kode dari import pertama tetap yang berlaku
        tersimpan = self.conn.execute(
            "SELECT kode_aktivasi_hash FROM master_students WHERE nisn_hash = ?",
            (hash_nisn("8435"),)).fetchone()[0]
        self.assertEqual(tersimpan, hash_kode_aktivasi(pertama["8435"]))


if __name__ == "__main__":
    unittest.main()
