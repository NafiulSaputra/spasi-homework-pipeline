"""
tests/test_geofence.py

Unit test untuk app/geofence.py — fokus pada boundary case radius,
karena ini bagian yang paling rawan salah (lihat Known Limitation:
akurasi GPS smartphone ~5-20m bisa membuat submission di tepi radius
salah klasifikasi).

Jalankan: python -m unittest discover -s tests
(tidak butuh pytest — pakai unittest dari standard library)
"""

import math
import sys
import os
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.geofence import (
    SchoolArea,
    haversine_distance_meter,
    evaluate_submission_location,
)


SCHOOL = SchoolArea(
    school_id="SCH-TEST",
    nama_sekolah="Sekolah Uji",
    latitude=-6.200000,
    longitude=106.816666,
    radius_meter=150,
)


def offset_coordinate(lat, lon, distance_meter, bearing_degree=0):
    """Helper: geser titik GPS sejauh distance_meter dari (lat, lon)."""
    R = 6371000
    bearing = math.radians(bearing_degree)
    lat1 = math.radians(lat)
    lon1 = math.radians(lon)

    lat2 = math.asin(
        math.sin(lat1) * math.cos(distance_meter / R)
        + math.cos(lat1) * math.sin(distance_meter / R) * math.cos(bearing)
    )
    lon2 = lon1 + math.atan2(
        math.sin(bearing) * math.sin(distance_meter / R) * math.cos(lat1),
        math.cos(distance_meter / R) - math.sin(lat1) * math.sin(lat2),
    )
    return math.degrees(lat2), math.degrees(lon2)


class TestHaversineDistance(unittest.TestCase):

    def test_jarak_titik_sama_adalah_nol(self):
        d = haversine_distance_meter(-6.2, 106.816666, -6.2, 106.816666)
        self.assertAlmostEqual(d, 0, delta=0.01)

    def test_jarak_100m_sesuai_toleransi(self):
        lat2, lon2 = offset_coordinate(-6.2, 106.816666, 100)
        d = haversine_distance_meter(-6.2, 106.816666, lat2, lon2)
        self.assertAlmostEqual(d, 100, delta=1)


class TestEvaluateSubmissionLocation(unittest.TestCase):

    def test_pr_dalam_radius_sekolah_ditandai_pelanggaran(self):
        # 50m dari pusat sekolah, radius 150m -> di dalam
        lat2, lon2 = offset_coordinate(SCHOOL.latitude, SCHOOL.longitude, 50)
        hasil = evaluate_submission_location(lat2, lon2, SCHOOL, flag_if_inside=True)
        self.assertEqual(hasil["status_lokasi"], "di_area_sekolah")
        self.assertTrue(hasil["is_violation"])

    def test_pr_di_luar_radius_sekolah_tidak_pelanggaran(self):
        # 5000m dari sekolah -> jelas di luar
        lat2, lon2 = offset_coordinate(SCHOOL.latitude, SCHOOL.longitude, 5000)
        hasil = evaluate_submission_location(lat2, lon2, SCHOOL, flag_if_inside=True)
        self.assertEqual(hasil["status_lokasi"], "di_luar_sekolah")
        self.assertFalse(hasil["is_violation"])

    def test_boundary_tepat_di_tepi_radius(self):
        # Tepat di radius_meter (150m) -> is_inside pakai '<=', jadi masih dianggap dalam
        lat2, lon2 = offset_coordinate(SCHOOL.latitude, SCHOOL.longitude, 150)
        hasil = evaluate_submission_location(lat2, lon2, SCHOOL, flag_if_inside=True)
        self.assertEqual(hasil["status_lokasi"], "di_area_sekolah")

    def test_boundary_sedikit_di_luar_radius(self):
        # 151m -> harus sudah dianggap di luar
        lat2, lon2 = offset_coordinate(SCHOOL.latitude, SCHOOL.longitude, 151)
        hasil = evaluate_submission_location(lat2, lon2, SCHOOL, flag_if_inside=True)
        self.assertEqual(hasil["status_lokasi"], "di_luar_sekolah")

    def test_school_none_raise_error(self):
        with self.assertRaises(ValueError):
            evaluate_submission_location(-6.2, 106.8, None)

    def test_flag_if_inside_false_untuk_presensi(self):
        # Mode presensi: di dalam radius justru valid (bukan pelanggaran)
        lat2, lon2 = offset_coordinate(SCHOOL.latitude, SCHOOL.longitude, 50)
        hasil = evaluate_submission_location(lat2, lon2, SCHOOL, flag_if_inside=False)
        self.assertFalse(hasil["is_violation"])


if __name__ == "__main__":
    unittest.main()
