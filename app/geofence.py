"""
app/geofence.py

Modul geofence untuk Stateless Worker SPASI.

Konteks bisnis: tugas adalah PR (pekerjaan rumah), sehingga submission
yang berasal dari DALAM area sekolah ditandai sebagai potensi
pelanggaran (indikasi dikerjakan/dicontek di sekolah, bukan di rumah).
"""

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class SchoolArea:
    school_id: str
    nama_sekolah: str
    latitude: float
    longitude: float
    radius_meter: float


def haversine_distance_meter(lat1: float, lon1: float,
                              lat2: float, lon2: float) -> float:
    """Jarak dua koordinat GPS (meter) memakai formula Haversine."""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (math.sin(delta_phi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lambda / 2) ** 2)
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c


def evaluate_submission_location(
    student_lat: float,
    student_lon: float,
    school: SchoolArea,
    flag_if_inside: bool = True,
) -> dict:
    """Evaluasi lokasi submission terhadap area sekolah.

    flag_if_inside=True  (default, kasus PR): di dalam area sekolah = pelanggaran.
    flag_if_inside=False (kasus presensi lain jika dibutuhkan suatu saat):
                          di dalam area sekolah = valid.
    """
    if school is None:
        raise ValueError(
            "SchoolArea tidak boleh None — pastikan school_id siswa "
            "sudah terhubung ke master_schools"
        )

    distance = haversine_distance_meter(
        student_lat, student_lon, school.latitude, school.longitude
    )
    is_inside = distance <= school.radius_meter
    status_lokasi = "di_area_sekolah" if is_inside else "di_luar_sekolah"
    is_violation = is_inside if flag_if_inside else not is_inside

    if flag_if_inside:
        catatan = (
            f"Submission {distance:.0f}m dari {school.nama_sekolah} "
            f"(radius {school.radius_meter:.0f}m) — dalam area sekolah, "
            f"tandai untuk ditinjau guru (PR seharusnya di rumah)."
            if is_violation else
            f"Submission {distance:.0f}m dari {school.nama_sekolah} — "
            f"di luar area sekolah, sesuai aturan PR."
        )
    else:
        catatan = (
            f"Submission {distance:.0f}m dari {school.nama_sekolah} — "
            f"di luar area sekolah, mencurigakan untuk konteks presensi."
            if is_violation else
            f"Submission {distance:.0f}m dari {school.nama_sekolah} "
            f"(radius {school.radius_meter:.0f}m) — valid untuk presensi."
        )

    return {
        "status_lokasi": status_lokasi,
        "jarak_meter": round(distance, 1),
        "is_violation": is_violation,
        "catatan": catatan,
    }


def load_school_area_from_db(school_id: str, db_connection) -> Optional[SchoolArea]:
    """Contoh pola query ambil area sekolah dari PostgreSQL."""
    query = """
        SELECT school_id, nama_sekolah, latitude, longitude, radius_meter
        FROM master_schools WHERE school_id = %s
    """
    with db_connection.cursor() as cur:
        cur.execute(query, (school_id,))
        row = cur.fetchone()
    if row is None:
        return None
    return SchoolArea(
        school_id=row[0], nama_sekolah=row[1],
        latitude=float(row[2]), longitude=float(row[3]),
        radius_meter=float(row[4]),
    )
