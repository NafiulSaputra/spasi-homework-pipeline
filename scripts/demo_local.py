"""
scripts/demo_local.py — Demo end-to-end SPASI untuk direkam layar.

Prasyarat:
    docker compose up --build -d      (tunggu ~1 menit sampai semua menyala)
    pip install requests

Jalankan:
    python scripts/demo_local.py            # jeda 2 detik antar langkah
    python scripts/demo_local.py --cepat    # tanpa jeda

Skenario:
  1. Guru import roster -> kode aktivasi
  2. Guru menerbitkan 2 tugas (satu deadline minggu depan, satu sudah lewat)
  3. Siswa daftar 3-layer (kode salah ditolak dulu, lalu berhasil)
  4. Siswa submit: dari rumah (normal), dari sekolah + telat (pelanggaran),
     lalu revisi tugas pertama (resubmission)
  5. Worker menilai (AI mock) -> tampilkan isi tabel submissions
  6. Rekap semester untuk rapor + laporan CSV guru + cek DLQ
"""

import sys
import time
import uuid
import subprocess
from datetime import datetime, timedelta, timezone

import requests

API = "http://localhost:8080"
JEDA = 0 if "--cepat" in sys.argv else 2

NISN, NAMA, ABSEN = "8435", "Alfiansyah Ary Pratama", "3"
RUMAH = (-6.2400, 106.8500)      # ~5.7 km dari sekolah
SEKOLAH = (-6.20005, 106.81670)  # di dalam radius 150 m


def judul(teks):
    print(f"\n\033[1;34m=== {teks} ===\033[0m")
    time.sleep(JEDA)


def ok(teks):
    print(f"  \033[32m✔\033[0m {teks}")


def gagal(teks):
    print(f"  \033[31m✘ {teks}\033[0m")
    sys.exit(1)


def compose_exec(service, *cmd, stdin=None):
    r = subprocess.run(["docker", "compose", "exec", "-T", service, *cmd],
                       input=stdin, capture_output=True, text=True)
    if r.returncode != 0:
        gagal(f"docker compose exec {service} gagal:\n{r.stderr}")
    return r.stdout


def psql(sql):
    return compose_exec("db", "psql", "-U", "spasi_app", "-d", "spasi_db", "-c", sql)


def main():
    judul("0. Cek API menyala")
    try:
        requests.get(f"{API}/health", timeout=5).raise_for_status()
    except Exception:
        gagal("API belum menyala. Jalankan: docker compose up --build -d")
    ok("API sehat")

    judul("1. Guru import roster siswa (NISN di-hash, nomor HP dienkripsi)")
    out = compose_exec(
        "api", "python", "-m", "scripts.import_roster", "/dev/stdin", "SCH-001",
        stdin=f"nisn,nama,no_absen,kelas,no_hp\n{NISN},{NAMA},{ABSEN},X-1,081234567890\n",
    )
    kode = next((l.split(": ")[1].strip() for l in out.splitlines() if l.startswith(f"NISN {NISN}:")), None)
    if not kode:
        print(out)
        gagal("Kode aktivasi tidak ditemukan (roster mungkin sudah pernah diimport -- "
              "reset dengan: docker compose down -v)")
    ok(f"Kode aktivasi untuk dibagikan guru secara fisik: {kode}")
    print(psql("SELECT left(nisn_hash,16) AS nisn_hash, nama_asli, left(no_hp_encrypted,24) "
               "AS no_hp_encrypted FROM master_students;"))

    judul("2. Guru menerbitkan tugas")
    token_guru = compose_exec(
        "api", "python", "-c",
        "import api; print(api._issue_jwt('guru@sekolah.sch.id','guru',14))").strip()
    hg = {"Authorization": f"Bearer {token_guru}"}
    now = datetime.now()
    for task_id, judul_tugas, deadline in [
        ("PR-MTK-01", "PR Matematika Bab 1", now + timedelta(days=7)),
        ("PR-IPA-01", "PR IPA Bab 2", now - timedelta(days=1)),
    ]:
        r = requests.post(f"{API}/guru/tambah-tugas", headers=hg, data={
            "task_id": task_id, "school_id": "SCH-001", "judul": judul_tugas,
            "link_gdrive": "https://drive.google.com/contoh", "semester": "2026-Ganjil",
            "deadline": deadline.strftime("%Y-%m-%d %H:%M:%S"),
            "rubrik_prompt": "Nilai 0-100 berdasarkan kebenaran langkah"})
        r.raise_for_status()
        ok(f"{task_id} — deadline {deadline:%d %b %Y %H:%M}")

    judul("3a. Siswa daftar dengan kode aktivasi SALAH")
    ip = "demo-device-1"
    salah = "000000" if kode != "000000" else "111111"
    r = requests.post(f"{API}/auth/register/step1", data={
        "nama": NAMA, "no_absen": ABSEN, "nisn": NISN, "kode_aktivasi": salah, "ip_device": ip})
    ok(f"Ditolak ({r.status_code}): {r.json().get('detail')}")

    judul("3b. Siswa daftar: Layer 1 (data) + Layer 2 (kode aktivasi)")
    r = requests.post(f"{API}/auth/register/step1", data={
        "nama": NAMA.lower(), "no_absen": ABSEN, "nisn": NISN, "kode_aktivasi": kode, "ip_device": ip})
    r.raise_for_status()
    nisn_hash = r.json()["nisn_hash"]
    ok("Lolos (nama huruf kecil tetap cocok berkat normalisasi)")

    judul("3c. Layer 3: OTP WhatsApp (mode demo menampilkan OTP)")
    r = requests.post(f"{API}/auth/register/request-otp", data={"nisn_hash": nisn_hash, "ip_device": ip})
    r.raise_for_status()
    otp = r.json()["otp_debug"]
    ok(f"OTP terkirim: {otp}")
    r = requests.post(f"{API}/auth/register/verify-otp",
                      data={"nisn_hash": nisn_hash, "otp": otp, "ip_device": ip})
    r.raise_for_status()
    hs = {"Authorization": f"Bearer {r.json()['token']}"}
    ok("Akun dibuat, token login diterbitkan")

    r = requests.post(f"{API}/auth/register/step1", data={
        "nama": NAMA, "no_absen": ABSEN, "nisn": NISN, "kode_aktivasi": kode, "ip_device": ip})
    ok(f"Coba daftar ulang -> ditolak ({r.status_code}): {r.json().get('detail')}  [1 akun 1 siswa]")

    judul("4. Siswa mengirim tugas")
    gambar = b"\x89PNG\r\n\x1a\n-demo-jawaban-tulisan-tangan"

    def submit(task_id, lokasi, keterangan):
        r = requests.post(f"{API}/submit-tugas", headers=hs, data={
            "task_id": task_id, "lat": lokasi[0], "lon": lokasi[1],
            "event_time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "submission_attempt_id": str(uuid.uuid4())},
            files={"foto": ("jawaban.png", gambar, "image/png")})
        r.raise_for_status()
        ok(f"{task_id}: {keterangan} -> masuk antrean")
        time.sleep(1)

    submit("PR-MTK-01", RUMAH, "dikirim dari RUMAH, tepat waktu")
    submit("PR-IPA-01", SEKOLAH, "dikirim dari SEKOLAH, deadline sudah lewat")
    submit("PR-MTK-01", RUMAH, "REVISI tugas matematika (resubmission)")

    judul("5. Worker menilai (geofence -> privasi -> deadline -> AI -> upsert)")
    for _ in range(30):
        n = psql("SELECT count(*) FROM submissions;").split("\n")[2].strip()
        if n == "3":
            break
        time.sleep(1)
    else:
        gagal("Worker belum memproses 3 submission. Cek: docker compose logs worker")
    ok("Ketiga submission sudah dinilai")
    print(psql(
        "SELECT task_id, is_latest_submission AS terbaru, status_lokasi, is_violation AS pelanggaran, "
        "is_late AS telat, penalty_applied AS penalti, ai_score FROM submissions ORDER BY ingestion_time;"))
    ok("Koordinat GPS mentah tidak ada di tabel mana pun (hanya status_lokasi)")

    judul("6. Rekap semester untuk rapor (hanya revisi terakhir yang dihitung)")
    compose_exec("worker", "python", "-c", "import scheduler; scheduler.refresh_semester_summary()")
    print(psql("SELECT semester, rata_rata_score, jumlah_terlambat, jumlah_pelanggaran_lokasi, "
               "jumlah_tugas FROM semester_summary;"))

    judul("7. Laporan CSV untuk guru")
    r = requests.get(f"{API}/download-laporan", headers=hg)
    r.raise_for_status()
    print(r.text)

    judul("8. Dead Letter Queue")
    dlq = compose_exec("redis", "redis-cli", "LLEN", "tugas_dlq").strip()
    ok(f"Jumlah item gagal di DLQ: {dlq}")

    print("\n\033[1;32mDemo selesai. Semua langkah berhasil.\033[0m")
    print("Frontend siswa juga bisa dicoba manual di http://localhost:3000")


if __name__ == "__main__":
    main()
