"""
app/storage.py

Penyimpanan file tugas siswa. Path internal, BUKAN Signed URL -- Worker
akses langsung via Service Account (GCS) atau disk lokal (dev), sesuai
keputusan arsitektur anti "bom waktu" saat antrean menumpuk mendekati
deadline (Signed URL berbatas waktu bisa expired sebelum Worker sempat
memprosesnya).
"""

import os
import shutil

LOCAL_FALLBACK_DIR = "local_uploads"


def _bucket_configured() -> bool:
    return bool(os.getenv("GCS_BUCKET_NAME"))


def save_submission_file(local_tmp_path: str, student_uuid: str, task_id: str,
                          submission_attempt_id: str, original_filename: str) -> str:
    """Simpan file ke path internal:
    {student_uuid}/{task_id}/{submission_attempt_id}{ext}
    -- path ini unik per percobaan submit, jadi TIDAK PERNAH collision
    antar siswa atau antar revisi (beda dari kode lama yang pakai nama
    file asli apa adanya dan bisa saling menimpa).
    """
    ext = os.path.splitext(original_filename)[1]
    dest_name = f"{student_uuid}/{task_id}/{submission_attempt_id}{ext}"

    if _bucket_configured():
        from google.cloud import storage
        client = storage.Client()
        bucket = client.bucket(os.getenv("GCS_BUCKET_NAME"))
        blob = bucket.blob(dest_name)
        blob.upload_from_filename(local_tmp_path)
        os.remove(local_tmp_path)
        return f"gs://{os.getenv('GCS_BUCKET_NAME')}/{dest_name}"

    # Fallback lokal untuk dev tanpa GCP (docker-compose)
    dest_path = os.path.join(LOCAL_FALLBACK_DIR, dest_name)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)
    shutil.move(local_tmp_path, dest_path)
    return dest_path


def read_submission_file(internal_path: str) -> bytes:
    """Baca file dari path internal untuk dikirim ke Gemini."""
    if internal_path.startswith("gs://"):
        from google.cloud import storage
        client = storage.Client()
        _, _, rest = internal_path.partition("gs://")
        bucket_name, _, blob_name = rest.partition("/")
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        return blob.download_as_bytes()

    with open(internal_path, "rb") as f:
        return f.read()
