"""
api.py — SPASI Backend API (FastAPI)

Ditulis ulang total mengikuti spesifikasi final:
- Auth 3-layer (data dasar + kode aktivasi + OTP WhatsApp)
- Session guru 14 hari idle timeout (sliding, via /auth/refresh)
- Object storage internal (bukan Signed URL untuk Worker)
- Geofence & privacy diproses di Worker, BUKAN di sini (lihat worker.py)
- Idempotency key (submission_attempt_id) stabil terhadap retry
- Forward/Reverse ETL Google Sheets lewat scheduler background
"""

import os
import io
import csv
import time
import tempfile
from datetime import datetime, timedelta

import jwt
import redis
import json
from fastapi import FastAPI, UploadFile, Form, File, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv
from google.oauth2 import id_token as google_id_token
from google.auth.transport import requests as google_requests

from app.security import (
    hash_nisn, hash_kode_aktivasi, normalize_name,
    decrypt_phone, generate_otp,
)
from app.db import db_cursor
from app.storage import save_submission_file
from app.whatsapp_otp import send_otp_whatsapp, is_mock_mode

load_dotenv()

app = FastAPI(title="SPASI Backend API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: ganti ke domain frontend asli sebelum production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

queue = redis.Redis(
    host=os.getenv("REDIS_HOST", "127.0.0.1"),
    port=int(os.getenv("REDIS_PORT", "6379")),
    decode_responses=True,
)

JWT_SECRET = os.getenv("JWT_SECRET", "ganti-ini-di-production")
JWT_GURU_IDLE_TIMEOUT_DAYS = int(os.getenv("JWT_GURU_IDLE_TIMEOUT_DAYS", "14"))
JWT_SISWA_EXPIRE_DAYS = 30
MAX_FILE_SIZE_BYTES = 2 * 1024 * 1024  # 2MB -- validasi GIGO preventer (defense in depth)
RATE_LIMIT_MAX_ATTEMPTS = 5
RATE_LIMIT_WINDOW_SECONDS = 600  # 10 menit
OTP_TTL_SECONDS = 300  # 5 menit
OTP_MAX_ATTEMPTS = 3



# ============================================================
# Penyimpanan OTP di Redis (bukan memori proses): Cloud Run bisa
# menjalankan beberapa instance, jadi OTP yang diminta di instance A
# harus bisa diverifikasi di instance B. TTL Redis = masa berlaku OTP.
# ============================================================

def _otp_key(nisn_hash: str) -> str:
    return f"otp:{nisn_hash}"


OTP_RESEND_COOLDOWN_SECONDS = 60


def _otp_cooldown_guard(nisn_hash: str):
    """Cegah spam permintaan OTP (setiap OTP = 1 pesan WhatsApp berbayar)."""
    sisa_ttl = queue.ttl(_otp_key(nisn_hash))
    if sisa_ttl and sisa_ttl > OTP_TTL_SECONDS - OTP_RESEND_COOLDOWN_SECONDS:
        raise HTTPException(429, "OTP baru saja dikirim. Tunggu 1 menit sebelum meminta lagi.")


def _otp_save(nisn_hash: str, otp: str):
    key = _otp_key(nisn_hash)
    queue.delete(key)
    queue.hset(key, mapping={"otp": otp, "attempts": 0})
    queue.expire(key, OTP_TTL_SECONDS)


def _otp_check(nisn_hash: str, otp: str) -> str:
    """Return 'ok', 'expired', 'too_many', atau 'wrong'. OTP dihapus
    setelah berhasil dipakai (sekali pakai)."""
    key = _otp_key(nisn_hash)
    data = queue.hgetall(key)
    if not data:
        return "expired"
    if int(data.get("attempts", 0)) >= OTP_MAX_ATTEMPTS:
        return "too_many"
    if otp != data.get("otp"):
        queue.hincrby(key, "attempts", 1)
        return "wrong"
    queue.delete(key)
    return "ok"


# ============================================================
# Helper umum
# ============================================================

def _issue_jwt(subject: str, role: str, expire_days: int) -> str:
    payload = {
        "sub": subject,
        "role": role,
        "exp": datetime.utcnow() + timedelta(days=expire_days),
        "iat": datetime.utcnow(),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm="HS256")


def _decode_jwt(token: str) -> dict:
    try:
        return jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "Sesi sudah berakhir, silakan login kembali.")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "Token tidak valid.")


def require_student(authorization: str = Header(...)) -> str:
    payload = _decode_jwt(authorization.replace("Bearer ", ""))
    if payload.get("role") != "siswa":
        raise HTTPException(403, "Akses ditolak.")
    return payload["sub"]  # student_uuid (= hash_nisn)


def require_guru(authorization: str = Header(...)) -> str:
    payload = _decode_jwt(authorization.replace("Bearer ", ""))
    if payload.get("role") != "guru":
        raise HTTPException(403, "Akses ditolak.")
    return payload["sub"]  # email guru


def _otp_response(pesan: str, otp: str, extra: dict = None) -> dict:
    """Respons standar setelah OTP dikirim. Field otp_debug HANYA muncul
    di mode mock (dev/demo/CI) supaya alur bisa diuji otomatis tanpa HP
    sungguhan. Di production (SPASI_MOCK_WHATSAPP tidak diset) field ini
    tidak pernah ada."""
    resp = {"status": "sukses", "pesan": pesan}
    if extra:
        resp.update(extra)
    if is_mock_mode():
        resp["otp_debug"] = otp
    return resp


def _rate_limit_check(nisn_hash: str, ip_device: str):
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT COUNT(*) FROM registration_attempts
            WHERE nisn_hash = %s AND ip_device = %s AND status = 'failed'
              AND attempted_at > now() - make_interval(secs => %s)
            """,
            (nisn_hash, ip_device, RATE_LIMIT_WINDOW_SECONDS),
        )
        count = cur.fetchone()[0]
    if count >= RATE_LIMIT_MAX_ATTEMPTS:
        raise HTTPException(429, "Terlalu banyak percobaan. Coba lagi nanti.")


def _log_attempt(nisn_hash: str, ip_device: str, layer_gagal, status: str):
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO registration_attempts (nisn_hash, ip_device, layer_gagal, status)
            VALUES (%s, %s, %s, %s)
            """,
            (nisn_hash, ip_device, layer_gagal, status),
        )


# ============================================================
# LAYER 1 + LAYER 2: Registrasi (data dasar + kode aktivasi)
# ============================================================

@app.post("/auth/register/step1")
async def register_step1(
    nama: str = Form(...),
    no_absen: str = Form(...),
    nisn: str = Form(...),
    kode_aktivasi: str = Form(...),
    ip_device: str = Form(...),
):
    nisn_hash = hash_nisn(nisn)
    _rate_limit_check(nisn_hash, ip_device)
    nama_normalized = normalize_name(nama)

    with db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT kode_aktivasi_hash, kode_aktivasi_status
            FROM master_students
            WHERE nisn_hash = %s AND nama_normalized = %s AND no_absen = %s
            """,
            (nisn_hash, nama_normalized, no_absen),
        )
        row = cur.fetchone()

        if not row:
            _log_attempt(nisn_hash, ip_device, "layer1", "failed")
            raise HTTPException(400, "Data tidak ditemukan, hubungi guru.")

        kode_hash_tersimpan, status_kode = row

        cur.execute("SELECT 1 FROM student_accounts WHERE student_uuid = %s", (nisn_hash,))
        if cur.fetchone():
            _log_attempt(nisn_hash, ip_device, "layer1", "failed")
            raise HTTPException(400, "Akun untuk NISN ini sudah terdaftar.")

    # LAYER 2 — kode aktivasi dari guru
    if hash_kode_aktivasi(kode_aktivasi) != kode_hash_tersimpan or status_kode != "belum_dipakai":
        _log_attempt(nisn_hash, ip_device, "layer2", "failed")
        raise HTTPException(400, "Kode aktivasi salah atau sudah digunakan.")

    _log_attempt(nisn_hash, ip_device, None, "success")
    return {"status": "sukses", "pesan": "Verifikasi awal berhasil, lanjut OTP.", "nisn_hash": nisn_hash}


# ============================================================
# LAYER 3: OTP (registrasi baru)
# ============================================================

@app.post("/auth/register/request-otp")
async def register_request_otp(nisn_hash: str = Form(...), ip_device: str = Form(...)):
    with db_cursor() as (conn, cur):
        cur.execute("SELECT no_hp_encrypted FROM master_students WHERE nisn_hash = %s", (nisn_hash,))
        row = cur.fetchone()

    if not row or not row[0]:
        raise HTTPException(400, "Nomor HP tidak ditemukan di roster.")

    phone = decrypt_phone(row[0])
    _otp_cooldown_guard(nisn_hash)
    otp = generate_otp()
    _otp_save(nisn_hash, otp)

    if not send_otp_whatsapp(phone, otp):
        raise HTTPException(500, "Gagal mengirim OTP, coba lagi.")

    return _otp_response("OTP terkirim ke HP terdaftar.", otp)


@app.post("/auth/register/verify-otp")
async def register_verify_otp(nisn_hash: str = Form(...), otp: str = Form(...), ip_device: str = Form(...)):
    hasil = _otp_check(nisn_hash, otp)
    if hasil == "too_many":
        raise HTTPException(429, "Terlalu banyak percobaan OTP, minta OTP baru.")
    if hasil != "ok":
        _log_attempt(nisn_hash, ip_device, "layer3", "failed")
        raise HTTPException(400, "OTP kedaluwarsa, minta OTP baru." if hasil == "expired" else "OTP salah.")

    with db_cursor() as (conn, cur):
        cur.execute(
            "INSERT INTO student_accounts (student_uuid) VALUES (%s) ON CONFLICT DO NOTHING",
            (nisn_hash,),
        )
        cur.execute(
            "UPDATE master_students SET kode_aktivasi_status = 'sudah_dipakai' WHERE nisn_hash = %s",
            (nisn_hash,),
        )

    _log_attempt(nisn_hash, ip_device, None, "success")

    token = _issue_jwt(nisn_hash, "siswa", JWT_SISWA_EXPIRE_DAYS)
    return {"status": "sukses", "token": token}


# ============================================================
# LOGIN (siswa yang sudah pernah terdaftar — OTP saja, tanpa kode aktivasi)
# ============================================================

@app.post("/auth/login/request-otp")
async def login_request_otp(nisn: str = Form(...), ip_device: str = Form(...)):
    nisn_hash = hash_nisn(nisn)
    _rate_limit_check(nisn_hash, ip_device)

    with db_cursor() as (conn, cur):
        cur.execute("SELECT 1 FROM student_accounts WHERE student_uuid = %s", (nisn_hash,))
        akun_ada = cur.fetchone()
        cur.execute("SELECT no_hp_encrypted FROM master_students WHERE nisn_hash = %s", (nisn_hash,))
        row = cur.fetchone()

    if not akun_ada or not row:
        _log_attempt(nisn_hash, ip_device, "layer1", "failed")
        raise HTTPException(400, "Akun tidak ditemukan, silakan daftar dulu.")

    phone = decrypt_phone(row[0])
    _otp_cooldown_guard(nisn_hash)
    otp = generate_otp()
    _otp_save(nisn_hash, otp)
    if not send_otp_whatsapp(phone, otp):
        raise HTTPException(500, "Gagal mengirim OTP, coba lagi.")

    return _otp_response("OTP terkirim.", otp, {"nisn_hash": nisn_hash})


@app.post("/auth/login/verify-otp")
async def login_verify_otp(nisn_hash: str = Form(...), otp: str = Form(...), ip_device: str = Form(...)):
    hasil = _otp_check(nisn_hash, otp)
    if hasil == "too_many":
        raise HTTPException(429, "Terlalu banyak percobaan OTP, minta OTP baru.")
    if hasil != "ok":
        _log_attempt(nisn_hash, ip_device, "layer3", "failed")
        raise HTTPException(400, "OTP salah atau kedaluwarsa.")

    _log_attempt(nisn_hash, ip_device, None, "success")
    token = _issue_jwt(nisn_hash, "siswa", JWT_SISWA_EXPIRE_DAYS)
    return {"status": "sukses", "token": token}


# ============================================================
# LOGIN GURU — Google OAuth, session 14 hari idle timeout (sliding)
# ============================================================

@app.post("/auth/guru/google-login")
async def guru_google_login(id_token_str: str = Form(...)):
    try:
        idinfo = google_id_token.verify_oauth2_token(
            id_token_str, google_requests.Request(), os.getenv("GOOGLE_OAUTH_CLIENT_ID")
        )
    except ValueError:
        raise HTTPException(401, "Token Google tidak valid.")

    email = idinfo["email"]
    # TODO: validasi domain email sekolah / whitelist guru sebelum izinkan akses.
    token = _issue_jwt(email, "guru", JWT_GURU_IDLE_TIMEOUT_DAYS)
    return {"status": "sukses", "token": token, "nama": idinfo.get("name"), "email": email}


@app.post("/auth/refresh")
async def refresh_token(authorization: str = Header(...)):
    """Dipanggil frontend tiap ada aktivitas -- mengimplementasikan
    sliding expiration 14 hari untuk guru tanpa re-login manual."""
    payload = _decode_jwt(authorization.replace("Bearer ", ""))
    expire_days = JWT_GURU_IDLE_TIMEOUT_DAYS if payload["role"] == "guru" else JWT_SISWA_EXPIRE_DAYS
    return {"token": _issue_jwt(payload["sub"], payload["role"], expire_days)}


# ============================================================
# SUBMIT TUGAS
# ============================================================

@app.post("/submit-tugas")
async def submit_tugas(
    task_id: str = Form(...),
    lat: float = Form(...),
    lon: float = Form(...),
    event_time: str = Form(...),              # ISO timestamp, digenerate klien saat tombol diklik
    submission_attempt_id: str = Form(...),    # UUID stabil, digenerate SEKALI di klien
    mock_location_detected: bool = Form(False),
    foto: UploadFile = File(...),
    student_uuid: str = Depends(require_student),
):
    # Defense-in-depth: validasi ulang di server meski klien sudah cek duluan.
    if mock_location_detected:
        raise HTTPException(403, "Fake GPS terdeteksi, submission ditolak.")

    contents = await foto.read()
    if len(contents) > MAX_FILE_SIZE_BYTES:
        raise HTTPException(413, "Ukuran file melebihi 2MB.")

    with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(foto.filename)[1]) as tmp:
        tmp.write(contents)
        tmp_path = tmp.name

    file_path = save_submission_file(tmp_path, student_uuid, task_id, submission_attempt_id, foto.filename)

    payload = {
        "submission_attempt_id": submission_attempt_id,
        "student_uuid": student_uuid,
        "task_id": task_id,
        "file_path": file_path,
        "lat": lat,
        "lon": lon,
        "event_time": event_time,
    }
    # Hanya teks/path internal yang masuk antrean -- TIDAK ADA file berat,
    # TIDAK ADA Signed URL (sesuai keputusan arsitektur).
    queue.rpush("tugas_masuk", json.dumps(payload))

    return {"status": "sukses", "pesan": "Tugas masuk antrean penilaian."}


@app.get("/siswa/daftar-tugas")
def daftar_tugas(student_uuid: str = Depends(require_student)):
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT ma.task_id, ma.judul, ma.link_gdrive, ma.deadline, ma.semester
            FROM master_assignments ma
            JOIN master_students ms ON ms.school_id = ma.school_id
            WHERE ms.nisn_hash = %s
            ORDER BY ma.deadline DESC
            """,
            (student_uuid,),
        )
        rows = cur.fetchall()

    return {
        "status": "sukses",
        "data": [
            {"task_id": r[0], "judul": r[1], "link_gdrive": r[2],
             "deadline": str(r[3]), "semester": r[4]}
            for r in rows
        ],
    }


# ============================================================
# ENDPOINT GURU
# ============================================================

@app.post("/guru/tambah-tugas")
async def tambah_tugas(
    task_id: str = Form(...),
    school_id: str = Form(...),
    judul: str = Form(...),
    link_gdrive: str = Form(...),
    deadline: str = Form(...),
    rubrik_prompt: str = Form(...),
    semester: str = Form(...),
    guru_email: str = Depends(require_guru),
):
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            INSERT INTO master_assignments
                (task_id, school_id, judul, link_gdrive, deadline, rubrik_prompt, semester)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (task_id) DO UPDATE SET
                judul = EXCLUDED.judul, link_gdrive = EXCLUDED.link_gdrive,
                deadline = EXCLUDED.deadline, rubrik_prompt = EXCLUDED.rubrik_prompt,
                semester = EXCLUDED.semester, updated_at = now()
            """,
            (task_id, school_id, judul, link_gdrive, deadline, rubrik_prompt, semester),
        )
    return {"status": "sukses", "pesan": f"Tugas {judul} berhasil diterbitkan."}


@app.get("/download-laporan")
def download_laporan(guru_email: str = Depends(require_guru)):
    """Satu endpoint saja (versi lama punya 2 route duplikat yang saling
    bentrok, salah satunya query kolom yang tidak ada di skema)."""
    with db_cursor() as (conn, cur):
        cur.execute(
            """
            SELECT ms.nama_asli, ms.kelas, s.task_id, s.ai_score, s.status_lokasi,
                   s.is_violation, s.is_late, s.final_score, s.ingestion_time
            FROM submissions s
            JOIN master_students ms ON s.student_uuid = ms.nisn_hash
            WHERE s.is_latest_submission = TRUE
            ORDER BY s.ingestion_time DESC
            """
        )
        rows = cur.fetchall()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Nama Siswa", "Kelas", "ID Tugas", "Nilai AI", "Status Lokasi",
                      "Pelanggaran Lokasi", "Terlambat", "Nilai Akhir", "Waktu Masuk"])
    writer.writerows(rows)
    output.seek(0)

    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=laporan_nilai_spasi.csv"},
    )


# ============================================================
# HEALTH CHECK
# Scheduler Google Sheets sengaja TIDAK berjalan di sini: Cloud Run bisa
# scale ke nol (scheduler mati) atau jalan multi-instance (sync dobel).
# Scheduler kini proses terpisah -> scheduler.py (jalan di worker VM).
# ============================================================

@app.get("/health")
def health():
    return {"status": "ok"}
