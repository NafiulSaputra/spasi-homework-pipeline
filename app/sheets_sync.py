"""
app/sheets_sync.py

Forward ETL: Tab "Master Soal" (Google Sheets) -> tabel master_assignments.
Reverse ETL: tabel submissions (is_latest_submission=TRUE) -> Tab
"Nilai Masuk", per kelas (routing via class_spreadsheet_mapping).

HANYA menulis kolom ai_score/status_lokasi -- TIDAK PERNAH menimpa
kolom final_score yang sudah diedit manual guru.

Catatan jujur: update sel per baris (gspread update_cell) lebih lambat
dan lebih gampang kena rate limit dibanding batch update -- untuk skala
kecil (per kelas, puluhan-ratusan baris) ini cukup; kalau makin sering
timeout, ganti ke sheet.batch_update().
"""

import os
import time

import gspread
from google.oauth2.service_account import Credentials
from gspread.exceptions import APIError

from app.db import db_cursor

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
MAX_RETRY = 3


def _get_gspread_client():
    """Pakai file key service account kalau disediakan (dev lokal);
    kalau tidak, pakai identitas service account VM (production di GCP,
    tanpa file key yang bisa bocor). Spreadsheet guru harus di-share ke
    email service account tersebut sebagai Editor."""
    key_path = os.getenv("GOOGLE_SHEETS_SERVICE_ACCOUNT_KEY_PATH")
    if key_path:
        creds = Credentials.from_service_account_file(key_path, scopes=SCOPES)
    else:
        import google.auth
        creds, _ = google.auth.default(scopes=SCOPES)
    return gspread.authorize(creds)


def _with_retry(fn, *args, **kwargs):
    """Retry sederhana untuk tahan rate limit/gangguan API Google Sheets
    sesaat (sesuai requirement 'tahan banting terhadap rate limit')."""
    last_error = None
    for attempt in range(1, MAX_RETRY + 1):
        try:
            return fn(*args, **kwargs)
        except APIError as e:
            last_error = e
            print(f"[Sheets] Percobaan {attempt}/{MAX_RETRY} gagal: {e}")
            time.sleep(2 ** attempt)
    raise last_error


def sync_master_soal_from_sheet(spreadsheet_id: str, school_id: str):
    """Forward ETL: Tab 'Master Soal' -> master_assignments (upsert)."""
    gc = _get_gspread_client()
    sheet = _with_retry(lambda: gc.open_by_key(spreadsheet_id).worksheet("Master Soal"))
    rows = _with_retry(sheet.get_all_records)

    with db_cursor() as (conn, cur):
        for row in rows:
            cur.execute(
                """
                INSERT INTO master_assignments
                    (task_id, school_id, judul, link_gdrive, deadline, rubrik_prompt, semester)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (task_id) DO UPDATE SET
                    judul = EXCLUDED.judul,
                    link_gdrive = EXCLUDED.link_gdrive,
                    deadline = EXCLUDED.deadline,
                    rubrik_prompt = EXCLUDED.rubrik_prompt,
                    semester = EXCLUDED.semester,
                    updated_at = now()
                """,
                (
                    row.get("ID Soal"), school_id, row.get("Judul"),
                    row.get("Link GDrive Soal"), row.get("Tenggat Waktu"),
                    row.get("Rubrik Penilaian/Prompt AI"), row.get("Semester"),
                ),
            )
    print(f"[Forward ETL] {len(rows)} soal disinkronkan dari sheet {spreadsheet_id}")


def sync_nilai_to_sheet(kelas: str):
    """Reverse ETL: submissions aktif -> Tab 'Nilai Masuk' pada
    spreadsheet milik kelas tsb. TIDAK PERNAH menyentuh kolom final_score."""
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT spreadsheet_id FROM class_spreadsheet_mapping WHERE kelas = %s",
            (kelas,),
        )
        row = cur.fetchone()
        if not row:
            print(f"[Reverse ETL] Tidak ada spreadsheet mapping untuk kelas {kelas}")
            return
        spreadsheet_id = row[0]

        cur.execute(
            """
            SELECT ms.nama_asli, s.ai_score, s.status_lokasi
            FROM submissions s
            JOIN master_students ms ON s.student_uuid = ms.nisn_hash
            WHERE ms.kelas = %s AND s.is_latest_submission = TRUE
            """,
            (kelas,),
        )
        hasil = cur.fetchall()

    gc = _get_gspread_client()
    sheet = _with_retry(lambda: gc.open_by_key(spreadsheet_id).worksheet("Nilai Masuk"))

    header = _with_retry(sheet.row_values, 1)
    existing = _with_retry(sheet.get_all_records)

    try:
        col_ai_score = header.index("ai_score") + 1
        col_status = header.index("status_lokasi") + 1
        col_nama = header.index("Nama Siswa") + 1
    except ValueError:
        print("[Reverse ETL] Header sheet tidak sesuai (butuh kolom "
              "'Nama Siswa', 'ai_score', 'status_lokasi')")
        return

    nama_to_row = {r.get("Nama Siswa"): i + 2 for i, r in enumerate(existing)}

    disinkron = 0
    for nama, ai_score, status_lokasi in hasil:
        row_idx = nama_to_row.get(nama)
        if row_idx is None:
            continue  # siswa belum ada barisnya di sheet, lewati (bukan error fatal)
        _with_retry(sheet.update_cell, row_idx, col_ai_score, ai_score)
        _with_retry(sheet.update_cell, row_idx, col_status, status_lokasi)
        disinkron += 1

    print(f"[Reverse ETL] {disinkron}/{len(hasil)} nilai disinkronkan ke kelas {kelas}")
