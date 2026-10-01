"""
tests/test_worker_pipeline.py

Menjalankan KODE ASLI worker.py (process_submission) dan scheduler.py
(refresh_semester_summary) dari ujung ke ujung, dengan:
- SQLite in-memory sebagai pengganti PostgreSQL
- antrean list Python sebagai pengganti Redis
- mode mock AI (atau stub Gemini yang sengaja gagal, untuk uji DLQ)

Yang diuji: geofence (arah PR), privasi GPS, deadline/penalti, timezone,
resubmission (is_latest_submission), idempotency, retry + DLQ, dan
agregasi rapor semester.

Tidak butuh Postgres/Redis/Gemini -- bisa jalan di laptop mana pun dan
di GitHub Actions. Test end-to-end dengan Postgres + Redis asli ada di
tests/integration/test_api_e2e.py.
"""

import os
import sys
import json
import types
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ["SPASI_MOCK_AI"] = "1"
os.environ.setdefault("NIS_HASH_PEPPER", "pepper-test")
os.environ.setdefault("PHONE_ENCRYPTION_KEY", "kunci-test")


def _ensure_importable(name, factory):
    """Pakai library asli kalau terpasang (mis. di CI); kalau tidak ada,
    pasang stub minimal supaya worker.py tetap bisa diimpor."""
    try:
        __import__(name)
    except ImportError:
        sys.modules[name] = factory()


def _stub_redis():
    m = types.ModuleType("redis")
    m.Redis = lambda *a, **k: None
    return m


def _stub_psycopg2():
    m = types.ModuleType("psycopg2")
    m.connect = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stub"))
    return m


def _stub_genai():
    if "google" not in sys.modules:
        try:
            import google  # noqa: F401
        except ImportError:
            sys.modules["google"] = types.ModuleType("google")
    m = types.ModuleType("google.generativeai")
    m.configure = lambda **k: None
    sys.modules["google"].generativeai = m
    return m


_ensure_importable("redis", _stub_redis)
_ensure_importable("psycopg2", _stub_psycopg2)
_ensure_importable("google.generativeai", _stub_genai)

import worker  # noqa: E402
import scheduler  # noqa: E402


# ------------------------------------------------------------------
# SQLite pengganti PostgreSQL (skema setara init.sql)
# ------------------------------------------------------------------

sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))
sqlite3.register_adapter(datetime, lambda d: d.isoformat(sep=" "))
sqlite3.register_adapter(bool, int)

SCHEMA = """
CREATE TABLE master_schools (school_id TEXT PRIMARY KEY, nama_sekolah TEXT,
  latitude NUMERIC, longitude NUMERIC, radius_meter NUMERIC);
CREATE TABLE master_students (nisn_hash TEXT UNIQUE NOT NULL, school_id TEXT,
  nama_asli TEXT, nama_normalized TEXT, no_absen TEXT, kelas TEXT,
  no_hp_encrypted TEXT, kode_aktivasi_hash TEXT, kode_aktivasi_status TEXT);
CREATE TABLE master_assignments (task_id TEXT PRIMARY KEY, school_id TEXT, judul TEXT,
  link_gdrive TEXT, deadline TIMESTAMP, rubrik_prompt TEXT, semester TEXT);
CREATE TABLE submissions (submission_attempt_id TEXT PRIMARY KEY, student_uuid TEXT,
  task_id TEXT, semester TEXT, is_latest_submission BOOLEAN DEFAULT TRUE,
  file_path TEXT, event_time TIMESTAMP, ingestion_time TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  status_lokasi TEXT, is_violation BOOLEAN, is_late BOOLEAN, penalty_applied NUMERIC,
  ai_score NUMERIC, ai_notes TEXT, final_score NUMERIC);
CREATE TABLE semester_summary (student_uuid TEXT, semester TEXT, rata_rata_score NUMERIC,
  jumlah_terlambat INTEGER, jumlah_pelanggaran_lokasi INTEGER, jumlah_tugas INTEGER,
  PRIMARY KEY (student_uuid, semester));
"""

SCHOOL_LAT, SCHOOL_LON = -6.200000, 106.816666
STUDENT = "hash-siswa-A"
TASK = "TUGAS-001"
DEADLINE = datetime(2026, 10, 1, 23, 59, 0)  # waktu lokal sekolah (WIB)


class FakeQueue:
    def __init__(self):
        self.lists = {}

    def rpush(self, key, value):
        self.lists.setdefault(key, []).append(value)

    def items(self, key):
        return [json.loads(v) for v in self.lists.get(key, [])]


class WorkerPipelineTest(unittest.TestCase):

    def setUp(self):
        self.conn = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES)
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT INTO master_schools VALUES (?,?,?,?,?)",
                          ("SCH-001", "Sekolah Uji", SCHOOL_LAT, SCHOOL_LON, 150))
        self.conn.execute(
            "INSERT INTO master_students (nisn_hash, school_id, nama_asli, kelas) VALUES (?,?,?,?)",
            (STUDENT, "SCH-001", "Siswa A", "X-1"))
        self.conn.execute(
            "INSERT INTO master_assignments VALUES (?,?,?,?,?,?,?)",
            (TASK, "SCH-001", "PR Matematika", "-", DEADLINE, "Nilai 0-100", "2026-Ganjil"))
        self.conn.commit()

        tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        tmp.write(b"fake-image-bytes")
        tmp.close()
        self.file_path = tmp.name

        self.queue = FakeQueue()
        conn = self.conn

        @contextmanager
        def sqlite_cursor():
            class Cur:
                def __init__(self):
                    self._c = conn.cursor()

                def execute(self, sql, params=()):
                    self._c.execute(sql.replace("%s", "?"), params)

                def fetchone(self):
                    return self._c.fetchone()

                def fetchall(self):
                    return self._c.fetchall()

            try:
                yield conn, Cur()
                conn.commit()
            except Exception:
                conn.rollback()
                raise

        self.patches = [
            mock.patch.object(worker, "db_cursor", sqlite_cursor),
            mock.patch.object(scheduler, "db_cursor", sqlite_cursor),
            mock.patch.object(worker, "queue", self.queue),
            mock.patch.object(worker.time, "sleep", lambda s: None),
            mock.patch.object(worker, "MOCK_AI", True),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.conn.close()
        os.remove(self.file_path)

    # -------- helper --------

    def payload(self, attempt_id, lat, lon, event_time):
        return {
            "submission_attempt_id": attempt_id,
            "student_uuid": STUDENT,
            "task_id": TASK,
            "file_path": self.file_path,
            "lat": lat,
            "lon": lon,
            "event_time": event_time,
        }

    def rows(self):
        cur = self.conn.execute(
            "SELECT submission_attempt_id, is_latest_submission, status_lokasi, "
            "is_violation, is_late, penalty_applied, ai_score FROM submissions "
            "ORDER BY ingestion_time, submission_attempt_id")
        return cur.fetchall()

    RUMAH = (-6.2400, 106.8500)    # ~5.6 km dari sekolah
    SEKOLAH = (-6.20005, 106.81670)  # ~6 m dari pusat sekolah

    # -------- test --------

    def test_submit_dari_rumah_tepat_waktu(self):
        worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-01T19:00:00"))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        _, latest, status, violation, late, penalty, score = rows[0]
        self.assertEqual(latest, 1)
        self.assertEqual(status, "di_luar_sekolah")
        self.assertEqual(violation, 0)
        self.assertEqual(late, 0)
        self.assertEqual(penalty, 0)
        self.assertEqual(score, 85)

    def test_submit_dari_sekolah_ditandai_pelanggaran_pr(self):
        worker.process_submission(self.payload("a1", *self.SEKOLAH, "2026-10-01T10:00:00"))
        _, _, status, violation, _, _, _ = self.rows()[0]
        self.assertEqual(status, "di_area_sekolah")
        self.assertEqual(violation, 1)

    def test_gps_mentah_tidak_pernah_tersimpan(self):
        worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-01T19:00:00"))
        dump = "\n".join(self.conn.iterdump())
        self.assertNotIn(str(self.RUMAH[0]), dump)
        self.assertNotIn(str(self.RUMAH[1]), dump)

    def test_terlambat_kena_penalti_tapi_tetap_diterima(self):
        worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-02T06:00:00"))
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][4], 1)   # is_late
        self.assertEqual(rows[0][5], 20)  # penalti 20%

    def test_timestamp_utc_dari_browser_dinormalkan_ke_wib(self):
        # 17:30Z = 00:30 WIB tanggal 2 -> lewat deadline 23:59 WIB tanggal 1
        worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-01T17:30:00.000Z"))
        self.assertEqual(self.rows()[0][4], 1)
        # 16:00Z = 23:00 WIB tanggal 1 -> belum lewat deadline
        self.conn.execute("DELETE FROM submissions")
        worker.process_submission(self.payload("a2", *self.RUMAH, "2026-10-01T16:00:00.000Z"))
        self.assertEqual(self.rows()[0][4], 0)

    def test_resubmission_hanya_yang_terakhir_aktif(self):
        worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-01T10:00:00"))
        worker.process_submission(self.payload("a2", *self.RUMAH, "2026-10-01T11:00:00"))
        latest = {r[0]: r[1] for r in self.rows()}
        self.assertEqual(latest, {"a1": 0, "a2": 1})

    def test_idempotent_payload_sama_diproses_dua_kali(self):
        p = self.payload("a1", *self.RUMAH, "2026-10-01T10:00:00")
        worker.process_submission(p)
        worker.process_submission(p)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][1], 1)  # tetap aktif, tidak hilang

    def test_ai_gagal_masuk_dlq_setelah_retry(self):
        class GagalTerus:
            calls = 0

            def upload_file(self, **k):
                GagalTerus.calls += 1
                raise ConnectionError("Gemini timeout")

            def delete_file(self, name):
                pass

        with mock.patch.object(worker, "MOCK_AI", False), \
             mock.patch.object(worker, "genai", GagalTerus()):
            worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-01T10:00:00"))

        self.assertEqual(GagalTerus.calls, worker.MAX_RETRY_AI)
        self.assertEqual(self.rows(), [])
        dlq = self.queue.items(worker.DLQ_KEY)
        self.assertEqual(len(dlq), 1)
        self.assertEqual(dlq[0]["submission_attempt_id"], "a1")
        self.assertIn("Gemini timeout", dlq[0]["_error"])

    def test_respons_ai_tidak_sesuai_skema_masuk_dlq(self):
        class Resp:
            text = '{"skor": 90}'  # field wajib lain hilang

        class Model:
            def __init__(self, **k):
                pass

            def generate_content(self, parts):
                return Resp()

        class GenaiSalahSkema:
            GenerativeModel = Model

            def upload_file(self, **k):
                return types.SimpleNamespace(name="f1")

            def delete_file(self, name):
                pass

        with mock.patch.object(worker, "MOCK_AI", False), \
             mock.patch.object(worker, "genai", GenaiSalahSkema()):
            worker.process_submission(self.payload("a1", *self.RUMAH, "2026-10-01T10:00:00"))

        self.assertEqual(self.rows(), [])
        self.assertIn("Field wajib hilang", self.queue.items(worker.DLQ_KEY)[0]["_error"])

    def test_tugas_tidak_dikenal_masuk_dlq(self):
        p = self.payload("a1", *self.RUMAH, "2026-10-01T10:00:00")
        p["task_id"] = "TUGAS-TIDAK-ADA"
        worker.process_submission(p)
        self.assertEqual(self.rows(), [])
        self.assertEqual(len(self.queue.items(worker.DLQ_KEY)), 1)

    def test_rekap_semester_pakai_submission_terbaru_dan_penalti(self):
        worker.process_submission(self.payload("a1", *self.SEKOLAH, "2026-10-01T10:00:00"))
        worker.process_submission(self.payload("a2", *self.RUMAH, "2026-10-02T06:00:00"))
        scheduler.refresh_semester_summary()

        row = self.conn.execute(
            "SELECT rata_rata_score, jumlah_terlambat, jumlah_pelanggaran_lokasi, jumlah_tugas "
            "FROM semester_summary WHERE student_uuid = ?", (STUDENT,)).fetchone()
        # Hanya a2 (terbaru) yang dihitung: 85 dikurangi penalti 20% = 68
        self.assertEqual(row, (68.0, 1, 0, 1))

        # final_score guru mengalahkan nilai AI
        self.conn.execute("UPDATE submissions SET final_score = 95 WHERE submission_attempt_id = 'a2'")
        self.conn.commit()
        scheduler.refresh_semester_summary()
        score = self.conn.execute("SELECT rata_rata_score FROM semester_summary").fetchone()[0]
        self.assertEqual(score, 95.0)


if __name__ == "__main__":
    unittest.main()
