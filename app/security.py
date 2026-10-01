"""
app/security.py

Fungsi keamanan inti SPASI:
- hash_with_pepper(): hash satu arah (NISN, kode aktivasi) -- TIDAK bisa
  dibalik, hanya untuk dibandingkan.
- encrypt_phone()/decrypt_phone(): enkripsi DUA ARAH (reversible) untuk
  nomor HP -- wajib reversible karena dipakai mengirim OTP, beda kasus
  dari NISN yang cuma perlu dibandingkan.
- generate_otp(): OTP 6 digit pakai CSPRNG (secrets), bukan random biasa.
"""

import os
import hashlib
import secrets
import base64
import uuid

from cryptography.fernet import Fernet


def _get_pepper() -> str:
    pepper = os.getenv("NIS_HASH_PEPPER")
    if not pepper:
        raise RuntimeError("NIS_HASH_PEPPER belum diset di environment")
    return pepper


def hash_with_pepper(value: str) -> str:
    """Hash satu arah (SHA256 + pepper rahasia tingkat aplikasi).

    Dipakai untuk NISN dan kode aktivasi. NISN di beberapa sekolah hanya
    4 digit (kebijakan sekolah, tidak bisa diubah) sehingga hash polos
    saja rawan brute-force (ruang pencarian cuma ~10.000 kemungkinan).
    Pepper mencegah hash dicocokkan ke rainbow table publik tanpa tahu
    pepper-nya. Pertahanan UTAMA tetap di Layer 2 (kode aktivasi) +
    Layer 3 (OTP) + rate limiting -- bukan di kekuatan hash ini sendiri.
    """
    pepper = _get_pepper()
    return hashlib.sha256(f"{value}:{pepper}".encode("utf-8")).hexdigest()


# Alias semantik supaya pemanggilan di kode lain jelas maksudnya
hash_nisn = hash_with_pepper
hash_kode_aktivasi = hash_with_pepper


def normalize_name(nama: str) -> str:
    """Normalisasi nama untuk matching toleran typo (BUKAN untuk NISN,
    NISN wajib exact match)."""
    return " ".join(nama.strip().lower().split())


def _get_fernet() -> Fernet:
    key = os.getenv("PHONE_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("PHONE_ENCRYPTION_KEY belum diset di environment")
    # PHONE_ENCRYPTION_KEY disimpan sebagai string biasa (mis. di Secret
    # Manager); Fernet butuh key 32-byte urlsafe-base64 -> derive dulu.
    derived = hashlib.sha256(key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(derived))


def encrypt_phone(phone_number: str) -> str:
    """Enkripsi nomor HP (REVERSIBLE). Dipegang hanya oleh service yang
    perlu mengirim OTP -- jangan didekripsi di luar alur OTP."""
    return _get_fernet().encrypt(phone_number.encode("utf-8")).decode("utf-8")


def decrypt_phone(encrypted_phone: str) -> str:
    return _get_fernet().decrypt(encrypted_phone.encode("utf-8")).decode("utf-8")


def generate_otp() -> str:
    """OTP 6 digit pakai secrets (CSPRNG), bukan random/randint biasa."""
    return f"{secrets.randbelow(1_000_000):06d}"


def generate_submission_attempt_id() -> str:
    """UUID untuk idempotency key -- digenerate SEKALI di sisi klien saat
    tombol 'Kirim' diklik, stabil walau request di-retry jaringan."""
    return str(uuid.uuid4())
