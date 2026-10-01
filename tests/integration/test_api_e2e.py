"""
tests/integration/test_api_e2e.py

Test END-TO-END dengan PostgreSQL + Redis ASLI (bukan pengganti).
Dijalankan otomatis oleh GitHub Actions (lihat .github/workflows/ci-cd.yml,
job `test`, yang menyalakan service postgres & redis).

Alur yang diuji persis seperti pemakaian nyata:
  import roster -> daftar (3 layer) -> guru terbitkan tugas ->
  siswa lihat tugas -> submit -> worker menilai -> laporan guru

Gemini & WhatsApp memakai mode mock (SPASI_MOCK_AI / SPASI_MOCK_WHATSAPP)
supaya gratis dan deterministik.

Jalankan lokal (butuh Postgres & Redis menyala, mis. via docker compose):
    SPASI_INTEGRATION=1 python -m unittest discover -s tests/integration -v
"""

import os
import sys
import csv
import json
import uuid
import tempfile
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

if os.getenv("SPASI_INTEGRATION") != "1":
    raise unittest.SkipTest("Set SPASI_INTEGRATION=1 untuk menjalankan test integrasi")

os.environ.setdefault("SPASI_MOCK_AI", "1")
os.environ.setdefault("SPASI_MOCK_WHATSAPP", "1")
os.environ.setdefault("NIS_HASH_PEPPER", "pepper-integration")
os.environ.setdefault("PHONE_ENCRYPTION_KEY", "kunci-integration")
os.environ.setdefault("JWT_SECRET", "jwt-integration")

import psycopg2  # noqa: E402
import redis  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
import worker  # noqa: E402
from scripts.import_roster import import_roster  # noqa: E402
from app.db import get_db_connection  # noqa: E402

NISN = "8435"
NAMA = "Alfiansyah Ary Pratama"
NO_ABSEN = "3"
KELAS = "X-1"
TASK_ID = "TUGAS-E2E-001"
IP = "10.0.0.1"
RUMAH = (-6.2400, 106.8500)  # ~5.7 km dari titik sekolah seed (SCH-001)


def reset_database():
    conn = get_db_connection()
    conn.autocommit = True
    cur = conn.cursor()
    cur.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public;")
    with open(os.path.join(ROOT, "init.sql")) as f:
        cur.execute(f.read())
    cur.close()
    conn.close()


class ApiEndToEndTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        reset_database()
        cls.r = redis.Redis(
            host=os.getenv("REDIS_HOST", "127.0.0.1"),
            port=int(os.getenv("REDIS_PORT", "6379")),
            decode_responses=True,
        )
        cls.r.delete("tugas_masuk", worker.DLQ_KEY)

        with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, newline="") as f:
            w = csv.writer(f)
            w.writerow(["nisn", "nama", "no_absen", "kelas", "no_hp"])
            w.writerow([NISN, NAMA, NO_ABSEN, KELAS, "081234567890"])
            roster_path = f.name
        cls.kode_aktivasi = import_roster(roster_path, "SCH-001")[NISN]
        os.remove(roster_path)

        cls.client = TestClient(api.app)
        cls.guru_headers = {
            "Authorization": "Bearer " + api._issue_jwt("guru@sekolah.sch.id", "guru", 14)
        }

    # Urutan test penting (alur pendaftaran -> submit), jadi semuanya
    # dijalankan dari satu method berurutan dengan subTest per langkah.
    def test_alur_lengkap(self):
        c = self.client

        with self.subTest("health check"):
            self.assertEqual(c.get("/health").json(), {"status": "ok"})

        with self.subTest("layer 2: kode aktivasi salah ditolak"):
            r = c.post("/auth/register/step1", data={
                "nama": NAMA, "no_absen": NO_ABSEN, "nisn": NISN,
                "kode_aktivasi": "000000" if self.kode_aktivasi != "000000" else "111111",
                "ip_device": IP})
            self.assertEqual(r.status_code, 400)

        with self.subTest("layer 1+2: data benar + kode benar lolos (nama beda kapital tetap cocok)"):
            r = c.post("/auth/register/step1", data={
                "nama": "  alfiansyah   ARY pratama ", "no_absen": NO_ABSEN, "nisn": NISN,
                "kode_aktivasi": self.kode_aktivasi, "ip_device": IP})
            self.assertEqual(r.status_code, 200, r.text)
            nisn_hash = r.json()["nisn_hash"]

        with self.subTest("layer 3: OTP"):
            r = c.post("/auth/register/request-otp", data={"nisn_hash": nisn_hash, "ip_device": IP})
            self.assertEqual(r.status_code, 200, r.text)
            otp = r.json()["otp_debug"]

            r = c.post("/auth/register/verify-otp",
                       data={"nisn_hash": nisn_hash, "otp": "999999" if otp != "999999" else "888888",
                             "ip_device": IP})
            self.assertEqual(r.status_code, 400)

            r = c.post("/auth/register/verify-otp",
                       data={"nisn_hash": nisn_hash, "otp": otp, "ip_device": IP})
            self.assertEqual(r.status_code, 200, r.text)
            siswa_headers = {"Authorization": "Bearer " + r.json()["token"]}

        with self.subTest("1 akun 1 siswa: daftar ulang ditolak"):
            r = c.post("/auth/register/step1", data={
                "nama": NAMA, "no_absen": NO_ABSEN, "nisn": NISN,
                "kode_aktivasi": self.kode_aktivasi, "ip_device": "10.0.0.2"})
            self.assertEqual(r.status_code, 400)

        with self.subTest("login ulang via OTP"):
            r = c.post("/auth/login/request-otp", data={"nisn": NISN, "ip_device": IP})
            self.assertEqual(r.status_code, 200, r.text)
            r = c.post("/auth/login/verify-otp", data={
                "nisn_hash": nisn_hash, "otp": r.json()["otp_debug"], "ip_device": IP})
            self.assertEqual(r.status_code, 200, r.text)

        with self.subTest("guru menerbitkan tugas"):
            r = c.post("/guru/tambah-tugas", headers=self.guru_headers, data={
                "task_id": TASK_ID, "school_id": "SCH-001", "judul": "PR Matematika",
                "link_gdrive": "https://drive.google.com/x", "deadline": "2030-12-31 23:59:00",
                "rubrik_prompt": "Nilai kebenaran langkah 0-100", "semester": "2026-Ganjil"})
            self.assertEqual(r.status_code, 200, r.text)

        with self.subTest("siswa tidak bisa akses endpoint guru"):
            r = c.get("/download-laporan", headers=siswa_headers)
            self.assertEqual(r.status_code, 403)

        with self.subTest("siswa melihat daftar tugas"):
            r = c.get("/siswa/daftar-tugas", headers=siswa_headers)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn(TASK_ID, [t["task_id"] for t in r.json()["data"]])

        with self.subTest("file > 2MB ditolak"):
            r = c.post("/submit-tugas", headers=siswa_headers,
                       data={"task_id": TASK_ID, "lat": RUMAH[0], "lon": RUMAH[1],
                             "event_time": "2026-10-01T10:00:00.000Z",
                             "submission_attempt_id": str(uuid.uuid4())},
                       files={"foto": ("besar.jpg", b"x" * (2 * 1024 * 1024 + 1), "image/jpeg")})
            self.assertEqual(r.status_code, 413)

        with self.subTest("fake GPS ditolak"):
            r = c.post("/submit-tugas", headers=siswa_headers,
                       data={"task_id": TASK_ID, "lat": RUMAH[0], "lon": RUMAH[1],
                             "event_time": "2026-10-01T10:00:00.000Z",
                             "submission_attempt_id": str(uuid.uuid4()),
                             "mock_location_detected": "true"},
                       files={"foto": ("jawaban.jpg", b"img", "image/jpeg")})
            self.assertEqual(r.status_code, 403)

        attempt_id = str(uuid.uuid4())
        with self.subTest("submit tugas masuk antrean"):
            r = c.post("/submit-tugas", headers=siswa_headers,
                       data={"task_id": TASK_ID, "lat": RUMAH[0], "lon": RUMAH[1],
                             "event_time": "2026-10-01T10:00:00.000Z",
                             "submission_attempt_id": attempt_id},
                       files={"foto": ("jawaban.jpg", b"fake-image", "image/jpeg")})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(self.r.llen("tugas_masuk"), 1)

        with self.subTest("worker menilai dan menyimpan"):
            raw = self.r.lpop("tugas_masuk")
            payload = json.loads(raw)
            self.assertNotIn("foto", payload)  # antrean hanya berisi path, bukan file
            worker.process_submission(payload)

            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("""SELECT status_lokasi, is_violation, is_late, ai_score, is_latest_submission
                           FROM submissions WHERE submission_attempt_id = %s""", (attempt_id,))
            row = cur.fetchone()
            cur.close()
            conn.close()
            self.assertEqual(row, ("di_luar_sekolah", False, False, 85, True))

        with self.subTest("laporan guru berisi nilai siswa"):
            r = c.get("/download-laporan", headers=self.guru_headers)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn(NAMA, r.text)
            self.assertIn(TASK_ID, r.text)

    def test_rate_limit_registrasi(self):
        status_codes = []
        for _ in range(api.RATE_LIMIT_MAX_ATTEMPTS + 1):
            r = self.client.post("/auth/register/step1", data={
                "nama": "Bukan Siswa", "no_absen": "99", "nisn": "0000",
                "kode_aktivasi": "123456", "ip_device": "10.9.9.9"})
            status_codes.append(r.status_code)
        self.assertEqual(status_codes[-1], 429)


if __name__ == "__main__":
    unittest.main()
