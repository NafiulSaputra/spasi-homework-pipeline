"""
tests/test_security.py

Unit test untuk app/security.py -- fokus pada:
- hash_with_pepper() konsisten & deterministik (dipakai untuk matching NISN)
- encrypt_phone()/decrypt_phone() benar-benar roundtrip (reversible)
- OTP yang dihasilkan selalu 6 digit
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Set env var SEBELUM import app.security, supaya _get_pepper()/_get_fernet()
# tidak raise RuntimeError saat modul dites berdiri sendiri.
os.environ.setdefault("NIS_HASH_PEPPER", "pepper-rahasia-untuk-test")
os.environ.setdefault("PHONE_ENCRYPTION_KEY", "kunci-enkripsi-untuk-test")

from app.security import (
    hash_with_pepper,
    normalize_name,
    encrypt_phone,
    decrypt_phone,
    generate_otp,
    generate_submission_attempt_id,
)


class TestHashWithPepper(unittest.TestCase):

    def test_hash_konsisten_untuk_input_sama(self):
        self.assertEqual(hash_with_pepper("8435"), hash_with_pepper("8435"))

    def test_hash_beda_untuk_input_beda(self):
        self.assertNotEqual(hash_with_pepper("8435"), hash_with_pepper("8436"))

    def test_hash_bukan_md5_polos(self):
        import hashlib
        md5_polos = hashlib.md5("8435".encode()).hexdigest()
        self.assertNotEqual(hash_with_pepper("8435"), md5_polos)


class TestNormalizeName(unittest.TestCase):

    def test_normalisasi_spasi_dan_kapital(self):
        self.assertEqual(
            normalize_name("  Alfiansyah   Ary   Pratama  "),
            "alfiansyah ary pratama",
        )


class TestEncryptDecryptPhone(unittest.TestCase):

    def test_roundtrip_enkripsi_nomor_hp(self):
        nomor_asli = "081234567890"
        terenkripsi = encrypt_phone(nomor_asli)
        self.assertNotEqual(terenkripsi, nomor_asli)
        self.assertEqual(decrypt_phone(terenkripsi), nomor_asli)

    def test_enkripsi_berbeda_tiap_kali_dipanggil(self):
        # Fernet menambahkan random IV, jadi ciphertext harus beda
        # walau plaintext sama -- bukti ini bukan hash deterministik.
        a = encrypt_phone("081234567890")
        b = encrypt_phone("081234567890")
        self.assertNotEqual(a, b)
        self.assertEqual(decrypt_phone(a), decrypt_phone(b))


class TestGenerateOtp(unittest.TestCase):

    def test_otp_selalu_6_digit(self):
        for _ in range(50):
            otp = generate_otp()
            self.assertEqual(len(otp), 6)
            self.assertTrue(otp.isdigit())


class TestSubmissionAttemptId(unittest.TestCase):

    def test_selalu_unik(self):
        ids = {generate_submission_attempt_id() for _ in range(100)}
        self.assertEqual(len(ids), 100)


if __name__ == "__main__":
    unittest.main()
