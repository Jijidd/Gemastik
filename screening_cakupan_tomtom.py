"""
Screening Cakupan TomTom — Jalankan SEKALI sebelum menjadwalkan polling jangka panjang
================================================================================
Tujuan: menguji SEMUA titik di polling_points.csv satu kali (bukan rotasi grup),
menghitung jarak antara titik yang diminta vs titik yang benar-benar dicocokkan
TomTom, lalu memisahkan edge yang valid (match_distance_m <= ambang) dari yang
tidak (kemungkinan tidak punya cakupan traffic monitoring TomTom).

PENTING: script ini menghabiskan kuota SEBESAR jumlah baris di polling_points.csv
(1 request per titik). Untuk 688-9000 edge, ini bisa langsung menghabiskan
sebagian besar/seluruh kuota harian 2500 request -- jalankan ini SEKALI saja,
idealnya di awal hari kuota (biar kalaupun habis, langsung reset besok), dan
JANGAN dijadwalkan berulang lewat GitHub Actions (ini murni tools sekali pakai
untuk keputusan, bukan bagian dari siklus polling rutin).

Output:
  polling_points_VALID.csv   -> subset edge yang lolos ambang jarak, dipakai
                                 sebagai polling_points.csv pengganti untuk
                                 poll_tomtom_flow.py selanjutnya
  screening_report.csv       -> hasil lengkap semua titik (termasuk yang gagal),
                                 untuk diagnostik lebih lanjut
"""

import os
import time
import requests
import pandas as pd
from pathlib import Path

import poll_tomtom_flow as core  # reuse fungsi poll_satu_titik, haversine_m, dll.

# =========================================================
# KONFIGURASI
# =========================================================
API_KEY = os.environ.get("TOMTOM_API_KEY", "")
POLLING_POINTS_CSV = "polling_points.csv"
OUTPUT_VALID_CSV = "polling_points_VALID.csv"
OUTPUT_REPORT_CSV = "screening_report.csv"

QPS_LIMIT = 4
MAX_MATCH_DISTANCE_M = core.MAX_MATCH_DISTANCE_M  # konsisten dgn poll_tomtom_flow.py


def jalankan_screening():
    if not API_KEY:
        raise RuntimeError("TOMTOM_API_KEY belum diset di environment variable.")

    titik_df = pd.read_csv(POLLING_POINTS_CSV)
    n_total = len(titik_df)

    print(f"{'='*60}")
    print(f"SCREENING CAKUPAN TOMTOM")
    print(f"{'='*60}")
    print(f"Total titik akan diuji: {n_total}")
    print(f"Ambang jarak valid    : {MAX_MATCH_DISTANCE_M} meter")
    print(f"Estimasi kuota terpakai: {n_total} request (dari 2500/hari)")

    if n_total > 2500 * 0.9:
        print(f"\nPERINGATAN: jumlah titik ({n_total}) mendekati/melebihi kuota harian.")
        konfirmasi = input("Lanjutkan? (ketik 'ya' untuk lanjut): ")
        if konfirmasi.strip().lower() != "ya":
            print("Dibatalkan.")
            return

    session = requests.Session()
    delay = 1.0 / QPS_LIMIT
    hasil_rows = []

    waktu_mulai = time.time()
    for i, row in enumerate(titik_df.itertuples(index=False)):
        hasil = core.poll_satu_titik(session, row.lat, row.lon, API_KEY)
        rekaman = {
            "edge_id": row.edge_id,
            "requested_lat": row.lat,
            "requested_lon": row.lon,
            "highway_tag": row.highway_tag,
            **hasil,
        }
        hasil_rows.append(rekaman)

        if (i + 1) % 50 == 0:
            print(f"  progres: {i+1}/{n_total} titik diuji...")

        if i < n_total - 1:
            time.sleep(delay)

    durasi = time.time() - waktu_mulai
    print(f"\nSelesai dalam {durasi:.1f} detik ({durasi/60:.1f} menit)")

    hasil_df = pd.DataFrame(hasil_rows)
    hasil_df.to_csv(OUTPUT_REPORT_CSV, index=False)
    print(f"\nLaporan lengkap tersimpan: {OUTPUT_REPORT_CSV}")

    # --- Ringkasan ---
    n_error = hasil_df['error'].notna().sum()
    n_valid = (hasil_df['valid_match'] == True).sum()
    n_invalid = (hasil_df['valid_match'] == False).sum()

    print(f"\n{'='*60}")
    print(f"RINGKASAN SCREENING")
    print(f"{'='*60}")
    print(f"Total titik diuji         : {n_total}")
    print(f"Error request (HTTP/dll)  : {n_error} ({100*n_error/n_total:.1f}%)")
    print(f"Match VALID (<= {MAX_MATCH_DISTANCE_M}m) : {n_valid} ({100*n_valid/n_total:.1f}%)")
    print(f"Match TIDAK VALID (> {MAX_MATCH_DISTANCE_M}m): {n_invalid} ({100*n_invalid/n_total:.1f}%)")

    if n_valid > 0:
        print(f"\nStatistik match_distance_m untuk yang VALID:")
        print(hasil_df.loc[hasil_df['valid_match'] == True, 'match_distance_m'].describe())

    # --- Distribusi FRC pada edge yang valid vs tidak, untuk cek pola ---
    if 'frc' in hasil_df.columns:
        print(f"\nDistribusi FRC pada edge VALID:")
        print(hasil_df.loc[hasil_df['valid_match'] == True, 'frc'].value_counts())
        print(f"\nDistribusi highway_tag pada edge TIDAK VALID (kandidat dibuang):")
        print(hasil_df.loc[hasil_df['valid_match'] == False, 'highway_tag'].value_counts())

    # --- Simpan subset valid sebagai polling_points.csv pengganti ---
    edge_valid = hasil_df.loc[hasil_df['valid_match'] == True, 'edge_id']
    titik_valid = titik_df[titik_df['edge_id'].isin(edge_valid)].copy()
    titik_valid.to_csv(OUTPUT_VALID_CSV, index=False)

    print(f"\nTersimpan: {OUTPUT_VALID_CSV} ({len(titik_valid)} edge tervalidasi)")
    print(f"\n{'!'*60}")
    print(f"TINDAKAN SELANJUTNYA:")
    print(f"  1. Cek {OUTPUT_REPORT_CSV} untuk edge yang gagal/tidak valid --")
    print(f"     apakah ada pola (mis. semua secondary di area tertentu tidak")
    print(f"     tercakup TomTom)?")
    print(f"  2. Kalau hasil {OUTPUT_VALID_CSV} sudah oke, GANTI nama file ini")
    print(f"     jadi 'polling_points.csv' (timpa yang lama) sebelum menjalankan")
    print(f"     ulang perhitungan kuota (poll_tomtom_flow.py --rencana) dan")
    print(f"     menjadwalkan polling rutin lewat GitHub Actions.")
    print(f"  3. Jumlah edge yang berkurang otomatis MEMPERBAIKI rasio kuota vs")
    print(f"     cakupan yang sempat jadi masalah sebelumnya.")
    print(f"{'!'*60}")


if __name__ == "__main__":
    jalankan_screening()
