"""
app/whatsapp_otp.py

Kirim OTP via WhatsApp API. Endpoint default berformat Fonnte -- ganti
lewat WHATSAPP_API_URL sesuai provider yang dipakai.

Mode mock (SPASI_MOCK_WHATSAPP=1): OTP tidak dikirim, hanya dicetak ke
log server. Khusus dev/demo/CI. Variabel ini TIDAK PERNAH diset di
Terraform, jadi production selalu mengirim OTP sungguhan.
"""

import os
import requests


def is_mock_mode() -> bool:
    return os.getenv("SPASI_MOCK_WHATSAPP", "0") == "1"


def _mask(phone_number: str) -> str:
    return phone_number[:4] + "***" if len(phone_number) > 4 else "***"


def send_otp_whatsapp(phone_number: str, otp: str) -> bool:
    if is_mock_mode():
        print(f"[MOCK WHATSAPP] OTP untuk {_mask(phone_number)}: {otp}")
        return True

    api_key = os.getenv("WHATSAPP_API_KEY")
    provider_url = os.getenv("WHATSAPP_API_URL", "https://api.fonnte.com/send")
    pesan = f"Kode OTP SPASI kamu: {otp}. Berlaku 5 menit. Jangan bagikan kode ini ke siapa pun."

    try:
        resp = requests.post(
            provider_url,
            headers={"Authorization": api_key},
            data={"target": phone_number, "message": pesan},
            timeout=10,
        )
        return resp.status_code == 200
    except requests.RequestException as e:
        print(f"[WHATSAPP OTP] Gagal kirim ke {_mask(phone_number)}: {e}")
        return False
