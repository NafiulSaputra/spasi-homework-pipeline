"""
app/db.py

Koneksi PostgreSQL. DB_HOST bisa berupa:
- hostname/IP biasa (dev lokal via docker-compose, atau VM worker lewat
  public IP Cloud SQL yang sudah di-whitelist), ATAU
- path unix socket Cloud Run ("/cloudsql/<connection_name>") -- psycopg2
  otomatis mendeteksi unix socket ketika host diawali '/'.
"""

import os
from contextlib import contextmanager

import psycopg2


def get_db_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "spasi_db"),
        user=os.getenv("DB_USER", "spasi_app"),
        password=os.getenv("DB_PASS"),
    )


@contextmanager
def db_cursor():
    """Context manager: commit otomatis kalau sukses, rollback kalau
    exception, selalu tutup koneksi. Dipakai di semua query api.py/worker.py
    supaya tidak ada koneksi yang menggantung.
    """
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        yield conn, cur
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
