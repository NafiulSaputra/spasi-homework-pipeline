"""
scheduler.py — Proses terjadwal SPASI (berjalan di worker VM, BUKAN Cloud Run)

Kenapa dipisah dari api.py: Cloud Run bisa scale ke nol (scheduler ikut
mati) atau berjalan multi-instance (sync jalan dobel). Proses tunggal
yang selalu hidup di VM worker adalah tempat yang tepat.

Tugas:
- Forward ETL tiap 5 menit : Tab "Master Soal" -> master_assignments
- Reverse ETL tiap 5 menit : submissions terbaru -> Tab "Nilai Masuk" per kelas
- Agregasi tiap 1 jam      : submissions -> semester_summary (bahan rapor)
"""

import os
import time

from dotenv import load_dotenv

from app.db import db_cursor

load_dotenv()

INTERVAL_SYNC_SECONDS = int(os.getenv("SYNC_INTERVAL_SECONDS", "300"))
INTERVAL_SUMMARY_SECONDS = int(os.getenv("SUMMARY_INTERVAL_SECONDS", "3600"))


def refresh_semester_summary():
    """Hitung ulang ringkasan rapor per siswa per semester.

    Pakai final_score kalau guru sudah mengisi; kalau belum, pakai
    ai_score setelah dikurangi penalti keterlambatan. Hanya submission
    terbaru per tugas yang dihitung (is_latest_submission = TRUE).
    """
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO semester_summary
                (student_uuid, semester, rata_rata_score, jumlah_terlambat,
                 jumlah_pelanggaran_lokasi, jumlah_tugas)
            SELECT
                student_uuid,
                semester,
                ROUND(AVG(COALESCE(final_score,
                      ai_score * (100 - COALESCE(penalty_applied, 0)) / 100.0)), 2),
                SUM(CASE WHEN is_late THEN 1 ELSE 0 END),
                SUM(CASE WHEN is_violation THEN 1 ELSE 0 END),
                COUNT(*)
            FROM submissions
            WHERE is_latest_submission = TRUE AND semester IS NOT NULL
            GROUP BY student_uuid, semester
            ON CONFLICT (student_uuid, semester) DO UPDATE SET
                rata_rata_score = EXCLUDED.rata_rata_score,
                jumlah_terlambat = EXCLUDED.jumlah_terlambat,
                jumlah_pelanggaran_lokasi = EXCLUDED.jumlah_pelanggaran_lokasi,
                jumlah_tugas = EXCLUDED.jumlah_tugas
            """
        )
    print("[Summary] semester_summary diperbarui")


def run_sheets_sync():
    if os.getenv("SHEETS_SYNC_ENABLED", "0") != "1":
        print("[Scheduler] SHEETS_SYNC_ENABLED belum diaktifkan, sync Google Sheets dilewati.")
        return

    from app.sheets_sync import sync_master_soal_from_sheet, sync_nilai_to_sheet

    with db_cursor() as (conn, cur):
        cur.execute("SELECT school_id FROM master_schools")
        schools = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT kelas FROM class_spreadsheet_mapping")
        classes = [r[0] for r in cur.fetchall()]

    for school_id in schools:
        spreadsheet_id = os.getenv(f"SPREADSHEET_ID_{school_id}", os.getenv("SPREADSHEET_ID"))
        if not spreadsheet_id:
            continue
        try:
            sync_master_soal_from_sheet(spreadsheet_id, school_id)
        except Exception as e:
            print(f"[Forward ETL] Gagal sync {school_id}: {e}")

    for kelas in classes:
        try:
            sync_nilai_to_sheet(kelas)
        except Exception as e:
            print(f"[Reverse ETL] Gagal sync kelas {kelas}: {e}")


def main():
    print("Scheduler SPASI menyala.")
    last_summary = 0.0
    while True:
        try:
            run_sheets_sync()
        except Exception as e:
            print(f"[Scheduler] Error sync: {e}")

        if time.time() - last_summary >= INTERVAL_SUMMARY_SECONDS:
            try:
                refresh_semester_summary()
                last_summary = time.time()
            except Exception as e:
                print(f"[Scheduler] Error summary: {e}")

        time.sleep(INTERVAL_SYNC_SECONDS)


if __name__ == "__main__":
    main()
