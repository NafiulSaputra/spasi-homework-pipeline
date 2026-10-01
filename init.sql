-- init.sql — Skema SPASI final, konsisten dengan api.py & worker.py.
-- Data roster siswa TIDAK di-insert manual di sini (lihat known issue
-- versi lama: md5() mentah di SQL tidak bisa bawa pepper rahasia dan
-- tidak reversible untuk nomor HP). Import roster lewat
-- scripts/import_roster.py setelah tabel ini dibuat.

-- 1. Sekolah (mendukung multi-sekolah jika project berkembang)
CREATE TABLE master_schools (
  school_id       TEXT PRIMARY KEY,
  nama_sekolah    TEXT NOT NULL,
  latitude        NUMERIC NOT NULL,
  longitude       NUMERIC NOT NULL,
  radius_meter    NUMERIC NOT NULL DEFAULT 150
);

-- 2. Roster siswa — sumber kebenaran, diisi via scripts/import_roster.py
CREATE TABLE master_students (
  nisn_hash          TEXT UNIQUE NOT NULL,     -- hash_nisn() dari app/security.py
  school_id          TEXT REFERENCES master_schools(school_id),
  nama_asli          TEXT,
  nama_normalized    TEXT,                      -- normalize_name(): lowercase, trim
  no_absen           TEXT,
  kelas              TEXT,
  no_hp_encrypted    TEXT,                      -- encrypt_phone(): AES reversible,
                                                  -- BUKAN hash -- wajib reversible
                                                  -- karena dipakai kirim OTP.
                                                  -- TIDAK UNIQUE: kakak-adik boleh
                                                  -- berbagi 1 nomor HP ortu/wali.
  kode_aktivasi_hash        TEXT,
  kode_aktivasi_status      TEXT DEFAULT 'belum_dipakai'
);

-- 3. Akun aktif siswa (setelah lolos 3-layer auth)
CREATE TABLE student_accounts (
  student_uuid    TEXT PRIMARY KEY,             -- = nisn_hash
  session_token   TEXT,
  created_at      TIMESTAMP DEFAULT now()
);

-- 4. Audit trail percobaan registrasi/login
CREATE TABLE registration_attempts (
  id              SERIAL PRIMARY KEY,
  nisn_hash       TEXT,
  ip_device       TEXT,
  layer_gagal     TEXT,
  status          TEXT,
  attempted_at    TIMESTAMP DEFAULT now()
);

-- 5. Soal dari guru (forward ETL dari Sheets, lihat app/sheets_sync.py)
CREATE TABLE master_assignments (
  task_id         TEXT PRIMARY KEY,
  school_id       TEXT REFERENCES master_schools(school_id),
  judul           TEXT,
  link_gdrive     TEXT,
  deadline        TIMESTAMP,
  rubrik_prompt   TEXT,
  semester        TEXT,
  updated_at      TIMESTAMP DEFAULT now()
);

-- 6. Hasil submission (idempotent, multi-histori)
CREATE TABLE submissions (
  submission_attempt_id  UUID PRIMARY KEY,
  student_uuid            TEXT REFERENCES student_accounts(student_uuid),
  task_id                 TEXT REFERENCES master_assignments(task_id),
  semester                TEXT,
  is_latest_submission    BOOLEAN DEFAULT TRUE,
  file_path               TEXT,
  event_time              TIMESTAMP,
  ingestion_time          TIMESTAMP DEFAULT now(),
  status_lokasi           TEXT,
  is_violation            BOOLEAN,
  is_late                 BOOLEAN,
  penalty_applied         NUMERIC,
  ai_score                NUMERIC,
  ai_notes                TEXT,
  final_score             NUMERIC
);
CREATE INDEX idx_submissions_latest ON submissions (student_uuid, task_id) WHERE is_latest_submission = TRUE;

-- 7. Ringkasan per siswa per semester (untuk rapor)
CREATE TABLE semester_summary (
  student_uuid       TEXT,
  semester           TEXT,
  rata_rata_score    NUMERIC,
  jumlah_terlambat   INTEGER,
  jumlah_pelanggaran_lokasi INTEGER,
  jumlah_tugas       INTEGER,
  PRIMARY KEY (student_uuid, semester)
);

-- 8. Routing partisi Google Sheets per kelas
CREATE TABLE class_spreadsheet_mapping (
  kelas           TEXT PRIMARY KEY,
  spreadsheet_id  TEXT,
  guru_email      TEXT
);

-- Contoh seed data SEKOLAH (ganti dengan data asli sekolahmu).
-- Roster SISWA tidak di-seed di sini -- pakai scripts/import_roster.py.
INSERT INTO master_schools (school_id, nama_sekolah, latitude, longitude, radius_meter)
VALUES ('SCH-001', 'Nama Sekolah Kamu', -6.200000, 106.816666, 150);
