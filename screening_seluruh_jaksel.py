"""
Screening Penuh Seluruh Ruas Jalan Jaksel (Topologi v2) -- RESUMABLE
================================================================================
Poll SETIAP edge di jaksel_topology_v2.csv sekali, catat apakah valid (match
dekat) atau tidak. Karena jumlah edge (~5700-6400) melebihi kuota harian
(2500), script ini RESUMABLE -- tiap kali dijalankan, dia cuma memproses edge
yang BELUM pernah discreening (dibaca dari hasil CSV yang sudah ada), lalu
berhenti begitu kuota harian ini habis. Jalankan berulang (lewat GitHub
Actions terjadwal harian) sampai seluruh edge selesai.

HASIL AKHIR: satu CSV besar berisi status valid/invalid + currentSpeed utk
SETIAP edge. Edge yang tidak valid nanti diperlakukan sbg NULL saat membangun
graf/tensor akhir (bukan dibuang dari topologi, bukan pula diisi paksa).
"""

import os
import time
import math
import glob
import json
from pathlib import Path

import requests
import pandas as pd
import numpy as np
import pyproj

# =========================================================
# 0. KONFIGURASI
# =========================================================
API_KEY = os.environ.get("TOMTOM_API_KEY", "")
TOPOLOGY_V2_CSV = "jaksel_topology_v2.csv"
NODE_MAPPING_V2_CSV = "jaksel_node_mapping_v2.csv"

OUTPUT_CSV = "screening_seluruh_jaksel.csv"

QUOTA_PER_RUN = 2400       # sedikit di bawah 2500, jaga buffer utk retry/error
QPS_LIMIT = 4
MAX_MATCH_DISTANCE_M = 100.0
STYLE, ZOOM, UNIT = "absolute", 15, "kmph"
MAX_RETRIES, RETRY_BACKOFF_BASE = 4, 2.0

# Skema kolom TETAP -- konsisten dgn pola self-healing di poll_tomtom_flow.py,
# supaya kalau nanti ada penambahan field, file lama otomatis dimigrasi,
# bukan malah korup spt insiden CSV schema-drift sebelumnya.
SKEMA_KOLOM = [
    "edge_idx", "requested_lat", "requested_lon",
    "frc_response", "currentSpeed", "freeFlowSpeed", "confidence",
    "matched_lat", "matched_lon", "match_distance_m", "valid_match", "error",
]


# =========================================================
# 1. FUNGSI POLLING (identik pola dgn cek_akurasi_per_kelas.py)
# =========================================================
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlambda/2)**2
    return 2*R*math.asin(math.sqrt(a))


def poll_satu_titik(session, lat, lon, api_key):
    url = f"https://api.tomtom.com/traffic/services/4/flowSegmentData/{STYLE}/{ZOOM}/json"
    params = {"key": api_key, "point": f"{lat},{lon}", "unit": UNIT}

    for percobaan in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=10)
        except requests.RequestException as e:
            if percobaan == MAX_RETRIES:
                return {"error": f"request_exception: {e}"}
            time.sleep(RETRY_BACKOFF_BASE * (2 ** percobaan))
            continue

        if resp.status_code == 200:
            data = resp.json().get("flowSegmentData", {})
            m_lat = data.get("coordinates", {}).get("coordinate", [{}])[0].get("latitude")
            m_lon = data.get("coordinates", {}).get("coordinate", [{}])[0].get("longitude")
            jarak = haversine_m(lat, lon, m_lat, m_lon) if m_lat is not None else None
            return {
                "frc_response": data.get("frc"),
                "currentSpeed": data.get("currentSpeed"),
                "freeFlowSpeed": data.get("freeFlowSpeed"),
                "confidence": data.get("confidence"),
                "matched_lat": m_lat, "matched_lon": m_lon,
                "match_distance_m": jarak,
                "valid_match": (jarak is not None and jarak <= MAX_MATCH_DISTANCE_M),
                "error": None,
            }

        if resp.status_code == 429:
            if percobaan == MAX_RETRIES:
                return {"error": "429_max_retries_exceeded"}
            time.sleep(RETRY_BACKOFF_BASE * (2 ** percobaan))
            continue

        return {"error": f"http_{resp.status_code}: {resp.text[:200]}"}

    return {"error": "unknown_failure"}


# =========================================================
# 2. SIMPAN HASIL DENGAN SKEMA SELF-HEALING (append aman)
# =========================================================
def simpan_hasil(hasil_df, output_path):
    output_path = Path(output_path)
    hasil_df = hasil_df.reindex(columns=SKEMA_KOLOM)

    if output_path.exists():
        header_lama = pd.read_csv(output_path, nrows=0).columns.tolist()
        if header_lama != SKEMA_KOLOM:
            print(f"Skema file lama beda, migrasi otomatis...")
            df_lama = pd.read_csv(output_path)
            df_lama = df_lama.reindex(columns=SKEMA_KOLOM)
            df_lama.to_csv(output_path, index=False)

    header_diperlukan = not output_path.exists()
    hasil_df.to_csv(output_path, mode="a", index=False, header=header_diperlukan)


# =========================================================
# MAIN
# =========================================================
def main():
    if not API_KEY:
        raise RuntimeError("TOMTOM_API_KEY belum diset.")

    topo_df = pd.read_csv(TOPOLOGY_V2_CSV)
    node_df = pd.read_csv(NODE_MAPPING_V2_CSV).set_index("index")
    n_total_edge = len(topo_df)

    print(f"Total edge di topologi v2: {n_total_edge}")

    # --- Cek edge mana yang SUDAH pernah discreening (baca dari CSV lama) ---
    if Path(OUTPUT_CSV).exists():
        existing_df = pd.read_csv(OUTPUT_CSV)
        edge_sudah_selesai = set(existing_df["edge_idx"].unique())
    else:
        edge_sudah_selesai = set()

    n_sudah_selesai = len(edge_sudah_selesai)
    print(f"Edge yang sudah discreening sebelumnya: {n_sudah_selesai}")

    edge_belum = [i for i in topo_df.index if i not in edge_sudah_selesai]
    print(f"Edge yang BELUM discreening: {len(edge_belum)}")

    if len(edge_belum) == 0:
        print(f"\n{'='*60}")
        print(f"SELESAI -- SEMUA {n_total_edge} EDGE SUDAH DISCREENING.")
        print(f"{'='*60}")
        print_ringkasan(OUTPUT_CSV, n_total_edge)
        return

    edge_batch_ini = edge_belum[:QUOTA_PER_RUN]
    print(f"\nMemproses {len(edge_batch_ini)} edge di run ini "
          f"(sisa setelah ini: {len(edge_belum) - len(edge_batch_ini)})")

    transformer = pyproj.Transformer.from_crs("EPSG:32748", "EPSG:4326", always_xy=True)
    session = requests.Session()
    delay = 1.0 / QPS_LIMIT

    hasil_rows = []
    t_mulai = time.time()

    for i, edge_idx in enumerate(edge_batch_ini):
        row = topo_df.loc[edge_idx]
        p1 = node_df.loc[row["from"]]
        p2 = node_df.loc[row["to"]]
        lon1, lat1 = transformer.transform(p1["x_m"], p1["y_m"])
        lon2, lat2 = transformer.transform(p2["x_m"], p2["y_m"])
        lat_mid, lon_mid = (lat1 + lat2) / 2, (lon1 + lon2) / 2

        hasil = poll_satu_titik(session, lat_mid, lon_mid, API_KEY)
        hasil_rows.append({
            "edge_idx": edge_idx,
            "requested_lat": lat_mid, "requested_lon": lon_mid,
            **hasil,
        })

        if (i + 1) % 200 == 0:
            elapsed = time.time() - t_mulai
            print(f"  progres: {i+1}/{len(edge_batch_ini)} ({elapsed:.0f}s)")

        if i < len(edge_batch_ini) - 1:
            time.sleep(delay)

    hasil_df = pd.DataFrame(hasil_rows)
    simpan_hasil(hasil_df, OUTPUT_CSV)

    n_valid_batch = hasil_df["valid_match"].sum()
    print(f"\nBatch selesai: {n_valid_batch}/{len(hasil_df)} valid di run ini.")
    print(f"Tersimpan (append): {OUTPUT_CSV}")

    sisa_setelah_ini = len(edge_belum) - len(edge_batch_ini)
    if sisa_setelah_ini > 0:
        print(f"\n{'!'*60}")
        print(f"BELUM SELESAI -- masih ada {sisa_setelah_ini} edge belum discreening.")
        print(f"Jalankan ulang script ini (otomatis lewat GitHub Actions terjadwal,")
        print(f"atau manual) utk lanjut dari sisa yg belum -- TIDAK akan mengulang")
        print(f"edge yang sudah selesai di run ini.")
        print(f"{'!'*60}")
    else:
        print(f"\nSELESAI -- seluruh {n_total_edge} edge sudah discreening di run ini.")
        print_ringkasan(OUTPUT_CSV, n_total_edge)


def print_ringkasan(output_csv, n_total_edge):
    df = pd.read_csv(output_csv)
    n_valid = df["valid_match"].sum()
    n_invalid = (~df["valid_match"].astype(bool)).sum()
    print(f"\n{'='*60}\nRINGKASAN AKHIR (SELURUH JAKSEL)\n{'='*60}")
    print(f"Total edge topologi     : {n_total_edge}")
    print(f"Total edge discreening  : {len(df)}")
    print(f"Valid (tercakup TomTom) : {n_valid} ({100*n_valid/len(df):.1f}%)")
    print(f"Tidak valid (jadi NULL) : {n_invalid} ({100*n_invalid/len(df):.1f}%)")
    print(f"\nEdge yg valid_match=False nanti diperlakukan sbg NULL saat membangun")
    print(f"tensor akhir -- BUKAN dibuang dari topologi, krn ruas jalannya tetap")
    print(f"ada scr fisik & tetap relevan sbg konteks spasial (message passing),")
    print(f"cuma nilai speed-nya yg tidak tersedia dr TomTom.")


if __name__ == "__main__":
    main()
