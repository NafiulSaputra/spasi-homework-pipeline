"""
worker.py — Stateless Worker SPASI

Urutan langkah per submission (urutan ini PENTING, lihat spesifikasi
final Bagian 3 — jangan dibalik):

1. GEOFENCE CHECK  — dijalankan SELAGI GPS mentah masih ada.
2. PRIVACY         — setelah geofence selesai, GPS mentah TIDAK pernah
                      ditulis ke database; hanya status_lokasi/is_violation.
3. TEMPORAL LOGIC  — bandingkan event_time vs deadline, set is_late.
4. AI GRADING      — Gemini dengan retry + DLQ kalau gagal permanen.
5. UPSERT          — idempotent, pakai submission_attempt_id sebagai PK.
"""

import os
import json
import time
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

import redis
import google.generativeai as genai
from dotenv import load_dotenv

from app.db import db_cursor
from app.storage import read_submission_file
from app.geofence import SchoolArea, evaluate_submission_location

load_dotenv()

if os.getenv("SPASI_MOCK_AI", "0") != "1":
    genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

queue = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    decode_responses=True,
)

MAX_RETRY_AI = 3
DLQ_KEY = "tugas_dlq"
SKEMA_WAJIB = {"skor", "catatan_evaluasi", "tingkat_keyakinan", "teks_terbaca"}
MIN_CONFIDENCE = 0.4
SCHOOL_TZ = ZoneInfo(os.getenv("SCHOOL_TIMEZONE", "Asia/Jakarta"))
# Mode mock untuk dev/demo/CI: tidak memanggil Gemini sungguhan (gratis,
# deterministik). TIDAK PERNAH diset di Terraform/production.
MOCK_AI = os.getenv("SPASI_MOCK_AI", "0") == "1"


def get_school_for_student(student_uuid: str):
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT sc.school_id, sc.nama_sekolah, sc.latitude, sc.longitude, sc.radius_meter
            FROM master_schools sc
            JOIN master_students ms ON ms.school_id = sc.school_id
            WHERE ms.nisn_hash = %s
            """,
            (student_uuid,),
        )
        row = cur.fetchone()
    if not row:
        return None
    return SchoolArea(
        school_id=row[0], nama_sekolah=row[1],
        latitude=float(row[2]), longitude=float(row[3]), radius_meter=float(row[4]),
    )


def get_assignment(task_id: str):
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT rubrik_prompt, judul, deadline, semester FROM master_assignments WHERE task_id = %s",
            (task_id,),
        )
        return cur.fetchone()


def parse_event_time(event_time: str) -> datetime:
    """Normalisasi event_time ke waktu lokal sekolah (naive).

    Browser mengirim ISO UTC ('2026-10-01T03:00:00.000Z'), sedangkan
    deadline di DB disimpan naive dalam waktu lokal sekolah (WIB).
    Membandingkan datetime aware vs naive langsung = TypeError, jadi
    semua dinormalkan ke waktu lokal sekolah tanpa tzinfo.
    """
    dt = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(SCHOOL_TZ).replace(tzinfo=None)
    return dt


def mock_gemini_result() -> dict:
    """Hasil AI palsu yang lolos skema -- hanya untuk dev/demo/CI."""
    return {
        "skor": 85,
        "catatan_evaluasi": "[MOCK] Jawaban lengkap dan runtut. (Mode demo, bukan penilaian AI sungguhan.)",
        "tingkat_keyakinan": 0.9,
        "teks_terbaca": True,
    }


def call_gemini_with_retry(file_bytes: bytes, rubrik_prompt: str) -> dict:
    prompt = f"""
Anda adalah asisten guru otomatis untuk menilai tugas (PR) siswa.
Rubrik Penilaian dari guru: {rubrik_prompt}

Analisis gambar/dokumen jawaban siswa ini. Balas HANYA dalam format JSON
sesuai skema berikut, TANPA teks tambahan, TANPA markdown fence:
{{
  "skor": <angka 0-100>,
  "catatan_evaluasi": "<string, maksimal 500 karakter>",
  "tingkat_keyakinan": <angka 0-1>,
  "teks_terbaca": <true/false>
}}
"""
    last_error = None
    for attempt in range(1, MAX_RETRY_AI + 1):
        tmp_path = None
        uploaded_file = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
                tmp.write(file_bytes)
                tmp_path = tmp.name

            uploaded_file = genai.upload_file(path=tmp_path, display_name="Jawaban Siswa")
            model = genai.GenerativeModel(model_name="gemini-1.5-flash")
            response = model.generate_content([uploaded_file, prompt])

            json_text = response.text.replace("```json", "").replace("```", "").strip()
            hasil = json.loads(json_text)

            if not SKEMA_WAJIB.issubset(hasil.keys()):
                raise ValueError(f"Field wajib hilang dari respons AI: {list(hasil.keys())}")
            if hasil["tingkat_keyakinan"] < MIN_CONFIDENCE:
                raise ValueError(f"Tingkat keyakinan AI terlalu rendah: {hasil['tingkat_keyakinan']}")

            return hasil

        except Exception as e:
            last_error = e
            print(f"[AI Grading] Percobaan {attempt}/{MAX_RETRY_AI} gagal: {e}")
            time.sleep(2 ** attempt)  # exponential backoff
        finally:
            if uploaded_file is not None:
                try:
                    genai.delete_file(uploaded_file.name)
                except Exception:
                    pass
            if tmp_path and os.path.exists(tmp_path):
                os.remove(tmp_path)

    raise RuntimeError(f"AI Grading gagal setelah {MAX_RETRY_AI}x percobaan: {last_error}")


def send_to_dlq(payload: dict, error_message: str):
    """Karantina ke DLQ -- berbeda dari kode lama yang membuang item gagal
    begitu saja (print lalu hilang). Item di sini tetap ada di Redis
    untuk ditinjau ulang guru/admin."""
    payload = dict(payload)
    payload["_error"] = error_message
    payload["_dlq_at"] = time.time()
    queue.rpush(DLQ_KEY, json.dumps(payload))
    print(f"[DLQ] Submission {payload.get('submission_attempt_id')} dikarantina: {error_message}")
    # TODO: sambungkan ke alerting nyata (email/Slack) ke guru/admin.


def process_submission(data: dict):
    submission_attempt_id = data["submission_attempt_id"]
    student_uuid = data["student_uuid"]
    task_id = data["task_id"]
    file_path = data["file_path"]
    lat, lon = data["lat"], data["lon"]
    event_time = data["event_time"]

    # ---------- 0. IDEMPOTENCY GUARD ----------
    # Cek di AWAL: kalau submission_attempt_id ini sudah pernah tersimpan
    # (mis. worker crash setelah commit lalu payload terambil ulang),
    # lewati seluruh proses -- termasuk tidak memanggil AI lagi (hemat biaya)
    # dan tidak mengutak-atik flag is_latest_submission baris yang sudah ada.
    with db_cursor() as (conn, cur):
        cur.execute(
            "SELECT 1 FROM submissions WHERE submission_attempt_id = %s",
            (submission_attempt_id,),
        )
        if cur.fetchone():
            print(f"[Idempotent] {submission_attempt_id} sudah diproses sebelumnya, dilewati.")
            return

    # ---------- 1. GEOFENCE CHECK (selagi GPS mentah masih ada) ----------
    school = get_school_for_student(student_uuid)
    if school is None:
        send_to_dlq(data, "School tidak ditemukan untuk siswa ini")
        return

    hasil_geofence = evaluate_submission_location(lat, lon, school, flag_if_inside=True)
    status_lokasi = hasil_geofence["status_lokasi"]
    is_violation = hasil_geofence["is_violation"]
    print(f"[Geofence] {submission_attempt_id}: {hasil_geofence['catatan']}")

    # ---------- 2. PRIVACY ----------
    # lat/lon mentah berhenti di sini -- TIDAK PERNAH ditulis ke database.
    # Hanya status_lokasi & is_violation (hasil turunan) yang dipertahankan.

    # ---------- 3. TEMPORAL LOGIC ----------
    assignment = get_assignment(task_id)
    if not assignment:
        send_to_dlq(data, f"task_id {task_id} tidak ditemukan di master_assignments")
        return
    rubrik_prompt, judul, deadline, semester = assignment

    try:
        event_dt = parse_event_time(event_time)
    except ValueError:
        send_to_dlq(data, f"event_time tidak valid: {event_time}")
        return

    is_late = bool(deadline) and event_dt > deadline
    penalty_applied = 20 if is_late else 0  # diskon 20% sesuai spesifikasi

    # ---------- 4. AI GRADING ----------
    try:
        file_bytes = read_submission_file(file_path)
        hasil_ai = mock_gemini_result() if MOCK_AI else call_gemini_with_retry(file_bytes, rubrik_prompt)
    except Exception as e:
        send_to_dlq(data, str(e))
        return

    # ---------- 5. Idempotent upsert ----------
    with db_cursor() as (conn, cur):
        # Tandai submission lama pada (student_uuid, task_id) jadi bukan yang terbaru
        cur.execute(
            """
            UPDATE submissions SET is_latest_submission = FALSE
            WHERE student_uuid = %s AND task_id = %s AND submission_attempt_id <> %s
            """,
            (student_uuid, task_id, submission_attempt_id),
        )
        # ON CONFLICT DO NOTHING menjamin idempotency kalau worker crash
        # lalu payload yang sama (submission_attempt_id sama) diproses ulang.
        cur.execute(
            """
            INSERT INTO submissions
                (submission_attempt_id, student_uuid, task_id, semester,
                 is_latest_submission, file_path, event_time, status_lokasi,
                 is_violation, is_late, penalty_applied, ai_score, ai_notes)
            VALUES (%s, %s, %s, %s, TRUE, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (submission_attempt_id) DO NOTHING
            """,
            (
                submission_attempt_id, student_uuid, task_id, semester,
                file_path, event_dt, status_lokasi, is_violation, is_late,
                penalty_applied, hasil_ai["skor"], hasil_ai["catatan_evaluasi"],
            ),
        )

    print(f"[Selesai] {submission_attempt_id}: skor={hasil_ai['skor']} "
          f"status={status_lokasi} violation={is_violation} late={is_late}")


def main():
    print("Worker SPASI menyala, memantau antrean Redis...")
    while True:
        raw = queue.lpop("tugas_masuk")
        if not raw:
            time.sleep(2)
            continue
        try:
            data = json.loads(raw)
            process_submission(data)
        except Exception as e:
            # Kegagalan tak terduga di luar yang sudah ditangani di atas --
            # tetap karantina ke DLQ, jangan sampai item hilang begitu saja
            # (beda dari kode lama: except -> print -> hilang permanen).
            print(f"[Worker Error] {e}")
            try:
                send_to_dlq(json.loads(raw), f"Unexpected error: {e}")
            except Exception:
                pass


if __name__ == "__main__":
    main()
