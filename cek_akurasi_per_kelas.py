"""
Cek Akurasi Flow Segment Data per Kelas Jalan (FRC0-3)
================================================================================
Untuk tiap kelas jalan (motorway=FRC0, trunk=FRC1, primary=FRC2, secondary=FRC3),
cari satu rantai segmen BERSAMBUNG dalam kelas itu saja, lalu poll Flow Segment
Data utk tiap segmen dalam rantai, bandingkan titik yg diminta vs titik yg
benar-benar dicocokkan TomTom (match_distance_m), laporkan akurasi per kelas.

PRASYARAT: jaksel_topology_v2.csv TIDAK menyimpan kolom FRC per edge -- info
itu cuma ada di file JSON TomTom asli (representative file di folder
TomTom_Aug1_4). Script ini join ulang topologi v2 ke JSON tsb lewat
jaksel_segmentid_to_edge_v2.csv utk dapat FRC tiap edge.

Kalau struktur file kalian berbeda dari asumsi ini, sesuaikan bagian
'muat_frc_per_edge()' di bawah.
"""

import json
import glob
import math
import time
import os
from pathlib import Path
from collections import defaultdict

import numpy as np
import pandas as pd
import requests
import pyproj

# =========================================================
# 0. KONFIGURASI
# =========================================================
TOPOLOGY_V2_CSV = "jaksel_topology_v2.csv"
NODE_MAPPING_V2_CSV = "jaksel_node_mapping_v2.csv"
SEGMENTID_TO_EDGE_CSV = "jaksel_segmentid_to_edge_v2.csv"
TOMTOM_FOLDER = "TomTom_Aug1_4"   # dipakai cuma utk ambil 1 file representatif (FRC statis)

API_KEY = os.environ.get("TOMTOM_API_KEY", "")

# Kelas jalan yg dicek, dari SECONDARY ke atas (FRC3 -> FRC0), sesuai urutan diminta
KELAS_DICEK = [
    (3, "secondary"),
    (2, "primary"),
    (1, "trunk"),
    (0, "motorway"),
]

N_SEGMEN_PER_KELAS = 5     # panjang rantai bersambung yg dicari tiap kelas
MAX_MATCH_DISTANCE_M = 100.0
QPS_LIMIT = 4
STYLE, ZOOM, UNIT = "absolute", 15, "kmph"
MAX_RETRIES, RETRY_BACKOFF_BASE = 4, 2.0

OUTPUT_DIR = "hasil_cek_akurasi_per_kelas"


# =========================================================
# 1. MUAT FRC PER EDGE (join topologi v2 <-> JSON TomTom asli)
# =========================================================
def muat_frc_per_edge():
    files = sorted(glob.glob(str(Path(TOMTOM_FOLDER) / "*.json")))
    if not files:
        raise FileNotFoundError(f"Tidak ada file .json di '{TOMTOM_FOLDER}'.")
    representative_file = files[0]
    print(f"File representatif utk ambil FRC: {representative_file}")

    with open(representative_file) as f:
        data = json.load(f)
    segmentid_to_frc = {s["segmentId"]: s.get("frc") for s in data["network"]["segmentResults"]}

    seg_to_edge = pd.read_csv(SEGMENTID_TO_EDGE_CSV)
    seg_to_edge["frc"] = seg_to_edge["segmentId"].map(segmentid_to_frc)

    n_tak_dikenal = seg_to_edge["frc"].isna().sum()
    if n_tak_dikenal > 0:
        print(f"PERINGATAN: {n_tak_dikenal} edge tidak ketemu FRC-nya (segmentId "
              f"tidak ada di file representatif), akan diabaikan.")

    edge_to_frc = dict(zip(seg_to_edge["edge_idx"], seg_to_edge["frc"]))
    return edge_to_frc


# =========================================================
# 2. CARI RANTAI BERSAMBUNG DI DALAM SATU KELAS JALAN SAJA
# =========================================================
def cari_rantai_dalam_kelas(topo_df, edge_to_frc, frc_target, n_segmen):
    """
    Filter edge yg FRC-nya == frc_target, lalu cari rantai bersambung
    (edge i berbagi node 'to' dgn edge j yg 'from'-nya sama) SESAMA kelas itu.
    Coba dari beberapa titik awal kalau yg pertama tidak cukup panjang.
    """
    idx_kelas_ini = [i for i in topo_df.index if edge_to_frc.get(i) == frc_target]
    if len(idx_kelas_ini) == 0:
        return None, f"Tidak ada edge dgn FRC={frc_target} sama sekali di topologi."

    subset = topo_df.loc[idx_kelas_ini]
    edge_by_from = defaultdict(list)
    for idx, row in subset.iterrows():
        edge_by_from[row["from"]].append(idx)

    rantai_terbaik = []
    for start_idx in idx_kelas_ini:
        chain = [start_idx]
        current_to = topo_df.loc[start_idx, "to"]
        while len(chain) < n_segmen:
            kandidat = [e for e in edge_by_from.get(current_to, []) if e not in chain]
            if not kandidat:
                break
            next_idx = kandidat[0]
            chain.append(next_idx)
            current_to = topo_df.loc[next_idx, "to"]
        if len(chain) > len(rantai_terbaik):
            rantai_terbaik = chain
        if len(rantai_terbaik) >= n_segmen:
            break

    if len(rantai_terbaik) < 2:
        return None, (f"Cuma ditemukan {len(idx_kelas_ini)} edge FRC={frc_target}, "
                       f"tidak cukup membentuk rantai bersambung (>=2).")

    catatan = None
    if len(rantai_terbaik) < n_segmen:
        catatan = (f"Rantai terpanjang yg ditemukan cuma {len(rantai_terbaik)} segmen "
                   f"(target {n_segmen}) -- jaringan kelas ini terbatas di area studi.")

    return rantai_terbaik, catatan


# =========================================================
# 3. POLLING FLOW SEGMENT DATA (dipakai ulang polanya dari poll_tomtom_flow.py)
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
# MAIN
# =========================================================
def main():
    if not API_KEY:
        raise RuntimeError("TOMTOM_API_KEY belum diset.")

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    topo_df = pd.read_csv(TOPOLOGY_V2_CSV)
    node_df = pd.read_csv(NODE_MAPPING_V2_CSV).set_index("index")
    edge_to_frc = muat_frc_per_edge()

    transformer = pyproj.Transformer.from_crs("EPSG:32748", "EPSG:4326", always_xy=True)

    session = requests.Session()
    delay = 1.0 / QPS_LIMIT

    ringkasan_semua_kelas = []

    for frc_num, frc_nama in KELAS_DICEK:
        print(f"\n{'='*60}\nKELAS: {frc_nama.upper()} (FRC{frc_num})\n{'='*60}")

        rantai, catatan = cari_rantai_dalam_kelas(topo_df, edge_to_frc, frc_num, N_SEGMEN_PER_KELAS)
        if catatan:
            print(f"CATATAN: {catatan}")
        if rantai is None:
            ringkasan_semua_kelas.append({
                "frc": frc_num, "kelas": frc_nama, "n_segmen_diuji": 0,
                "n_valid": 0, "persen_valid": 0.0, "jarak_rata2_m": None,
                "catatan": catatan,
            })
            continue

        print(f"Rantai segmen (index topologi v2): {rantai}")

        hasil_rows = []
        for edge_idx in rantai:
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
            time.sleep(delay)

        hasil_df = pd.DataFrame(hasil_rows)
        out_path = Path(OUTPUT_DIR) / f"hasil_{frc_nama}.csv"
        hasil_df.to_csv(out_path, index=False)

        n_valid = hasil_df["valid_match"].sum() if "valid_match" in hasil_df else 0
        n_total = len(hasil_df)
        jarak_valid = hasil_df.loc[hasil_df["valid_match"] == True, "match_distance_m"]

        print(f"Hasil: {n_valid}/{n_total} valid ({100*n_valid/max(n_total,1):.1f}%)")
        if len(jarak_valid) > 0:
            print(f"Jarak rata-rata (yg valid): {jarak_valid.mean():.1f} m")
        print(f"Tersimpan: {out_path}")

        ringkasan_semua_kelas.append({
            "frc": frc_num, "kelas": frc_nama, "n_segmen_diuji": n_total,
            "n_valid": int(n_valid), "persen_valid": 100*n_valid/max(n_total,1),
            "jarak_rata2_m": jarak_valid.mean() if len(jarak_valid) > 0 else None,
            "catatan": catatan,
        })

    ringkasan_df = pd.DataFrame(ringkasan_semua_kelas)
    ringkasan_df.to_csv(Path(OUTPUT_DIR) / "ringkasan_semua_kelas.csv", index=False)

    print(f"\n{'='*60}\nRINGKASAN PERBANDINGAN ANTAR KELAS\n{'='*60}")
    print(ringkasan_df.to_string(index=False))
    print(f"\nTersimpan: {OUTPUT_DIR}/ringkasan_semua_kelas.csv")


if __name__ == "__main__":
    main()
