"""
Deteksi Cakupan TomTom Seluruh Jaksel via Vector Flow Tiles (CEPAT)
================================================================================
Alih-alih poll SETIAP segmen satu-satu (5700+ request, 3 hari), tutup seluruh
bbox Jaksel dengan tile (puluhan-ratusan request saja), kumpulkan SEMUA
geometri jalan yang muncul di tile (= jalan yang dipantau TomTom), lalu
cocokkan ke tiap segmen topologi v2 kita.

CATATAN PENTING: tile cuma dipakai utk DETEKSI CAKUPAN (ada/tidaknya jalan
dipantau), BUKAN utk ambil nilai speed -- makna 'traffic_level' di tile masih
ambigu (belum tentu km/h asli). Nilai speed sungguhan tetap diambil belakangan
via Flow Segment Data, tapi HANYA utk segmen yg sudah terbukti tercakup di
sini -- jumlahnya jauh lebih sedikit drpd 5700, jadi jauh lebih hemat kuota.

Selesai dalam SATU KALI JALAN (tidak perlu resumable multi-hari spt versi
per-titik sebelumnya), krn jumlah request jauh di bawah kuota harian.
"""

import os
import time
import math
import requests
import pandas as pd
import numpy as np
import pyproj
import mapbox_vector_tile
from shapely.geometry import LineString
from shapely.strtree import STRtree

# =========================================================
# 0. KONFIGURASI
# =========================================================
API_KEY = os.environ.get("TOMTOM_API_KEY", "")
TOPOLOGY_V2_CSV = "jaksel_topology_v2.csv"
NODE_MAPPING_V2_CSV = "jaksel_node_mapping_v2.csv"

OUTPUT_CSV = "cakupan_seluruh_jaksel_via_tile.csv"

ZOOM_TILE = 15              # ~192 tile utk seluruh Jaksel -- ubah ke 14 kalau
                             # mau lebih cepat lagi (lebih kasar) atau 16 kalau
                             # mau lebih detail (lebih banyak tile, msh aman kuota)
MAX_MATCH_DISTANCE_M = 100.0  # sedikit lebih ketat drpd Flow Segment Data (100m)
                             # krn tile geometrinya representasi jalan langsung,
                             # bukan snapshot titik -- boleh disesuaikan
QPS_LIMIT = 4
MAX_RETRIES, RETRY_BACKOFF_BASE = 4, 2.0


# =========================================================
# 1. TILE MATH (sesuai perbaikan bug sebelumnya)
# =========================================================
def deg2num(lat_deg, lon_deg, zoom):
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def num2deg(xtile, ytile, zoom):
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg


def batas_tile(xtile, ytile, zoom):
    lat_max, lon_min = num2deg(xtile, ytile, zoom)
    lat_min, lon_max = num2deg(xtile + 1, ytile + 1, zoom)
    return lon_min, lat_min, lon_max, lat_max


def tile_local_ke_lonlat(x_local, y_local, extent, tile_bounds):
    lon_min, lat_min, lon_max, lat_max = tile_bounds
    lon = lon_min + (x_local / extent) * (lon_max - lon_min)
    lat = lat_max - (y_local / extent) * (lat_max - lat_min)
    return lon, lat


# =========================================================
# 2. AMBIL + DECODE SATU TILE -> LIST GEOMETRI JALAN (lon/lat)
# =========================================================
def ambil_geometri_tile(session, zoom, xtile, ytile, api_key):
    url = f"https://api.tomtom.com/traffic/map/4/tile/flow/absolute/{zoom}/{xtile}/{ytile}.pbf"
    params = {"key": api_key}

    for percobaan in range(MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=15)
        except requests.RequestException:
            if percobaan == MAX_RETRIES:
                return []
            time.sleep(RETRY_BACKOFF_BASE * (2 ** percobaan))
            continue

        if resp.status_code == 200:
            break
        if resp.status_code == 429:
            if percobaan == MAX_RETRIES:
                return []
            time.sleep(RETRY_BACKOFF_BASE * (2 ** percobaan))
            continue
        return []  # tile kosong/error lain -- anggap tidak ada jalan di sini
    else:
        return []

    try:
        decoded = mapbox_vector_tile.decode(resp.content)
    except Exception:
        return []

    tile_bounds = batas_tile(xtile, ytile, zoom)
    geoms = []
    for layer_name, layer_data in decoded.items():
        extent = layer_data.get("extent", 4096)
        for feat in layer_data.get("features", []):
            geom = feat.get("geometry", {})
            if geom.get("type") != "LineString":
                continue
            coords_local = geom.get("coordinates", [])
            coords_lonlat = [tile_local_ke_lonlat(x, y, extent, tile_bounds) for x, y in coords_local]
            if len(coords_lonlat) >= 2:
                geoms.append(LineString(coords_lonlat))
    return geoms


# =========================================================
# MAIN
# =========================================================
def main():
    if not API_KEY:
        raise RuntimeError("TOMTOM_API_KEY belum diset.")

    topo_df = pd.read_csv(TOPOLOGY_V2_CSV)
    node_df = pd.read_csv(NODE_MAPPING_V2_CSV).set_index("index")
    transformer = pyproj.Transformer.from_crs("EPSG:32748", "EPSG:4326", always_xy=True)

    # --- 1. Hitung bbox seluruh Jaksel dari node mapping v2 ---
    lons, lats = [], []
    for _, row in node_df.iterrows():
        lon, lat = transformer.transform(row["x_m"], row["y_m"])
        lons.append(lon); lats.append(lat)
    lon_min_bbox, lon_max_bbox = min(lons), max(lons)
    lat_min_bbox, lat_max_bbox = min(lats), max(lats)
    print(f"Bbox Jaksel (dari topologi v2): lon[{lon_min_bbox:.4f},{lon_max_bbox:.4f}] "
          f"lat[{lat_min_bbox:.4f},{lat_max_bbox:.4f}]")

    # --- 2. Tentukan rentang tile yang menutup bbox ini ---
    x_min, y_max = deg2num(lat_min_bbox, lon_min_bbox, ZOOM_TILE)
    x_max, y_min = deg2num(lat_max_bbox, lon_max_bbox, ZOOM_TILE)
    x_range = range(min(x_min, x_max), max(x_min, x_max) + 1)
    y_range = range(min(y_min, y_max), max(y_min, y_max) + 1)
    n_tile = len(x_range) * len(y_range)
    print(f"Zoom {ZOOM_TILE}: {len(x_range)} x {len(y_range)} = {n_tile} tile diperlukan.")

    if n_tile > 2000:
        print(f"PERINGATAN: {n_tile} tile agak banyak, pertimbangkan turunkan ZOOM_TILE.")

    # --- 3. Fetch + decode semua tile, kumpulkan geometri jalan ---
    session = requests.Session()
    delay = 1.0 / QPS_LIMIT
    semua_geometri_jalan = []

    t_mulai = time.time()
    i = 0
    for xt in x_range:
        for yt in y_range:
            geoms = ambil_geometri_tile(session, ZOOM_TILE, xt, yt, API_KEY)
            semua_geometri_jalan.extend(geoms)
            i += 1
            if i % 25 == 0:
                print(f"  progres tile: {i}/{n_tile} ({time.time()-t_mulai:.0f}s), "
                      f"total geometri terkumpul: {len(semua_geometri_jalan)}")
            time.sleep(delay)

    print(f"\nSelesai fetch {n_tile} tile dalam {time.time()-t_mulai:.0f} detik.")
    print(f"Total geometri jalan (dari SEMUA tile) yang terkumpul: {len(semua_geometri_jalan)}")

    if len(semua_geometri_jalan) == 0:
        raise RuntimeError("Tidak ada geometri jalan sama sekali terkumpul -- cek API key/endpoint.")

    # --- 4. Bangun spatial index (STRtree) utk pencocokan cepat ---
    print("\nMembangun spatial index (STRtree) utk pencocokan cepat...")
    tree = STRtree(semua_geometri_jalan)

    # --- 5. Cocokkan SETIAP segmen topologi v2 ke geometri jalan terdekat ---
    print(f"Mencocokkan {len(topo_df)} segmen topologi v2 ke geometri hasil tile...")
    hasil_rows = []
    t_mulai_match = time.time()

    for edge_idx, row in topo_df.iterrows():
        p1 = node_df.loc[row["from"]]
        p2 = node_df.loc[row["to"]]
        lon1, lat1 = transformer.transform(p1["x_m"], p1["y_m"])
        lon2, lat2 = transformer.transform(p2["x_m"], p2["y_m"])
        titik_tengah_lon = (lon1 + lon2) / 2
        titik_tengah_lat = (lat1 + lat2) / 2

        from shapely.geometry import Point
        titik_tengah = Point(titik_tengah_lon, titik_tengah_lat)

        idx_terdekat = tree.nearest(titik_tengah)
        geom_terdekat = semua_geometri_jalan[idx_terdekat]
        jarak_deg = titik_tengah.distance(geom_terdekat)
        jarak_m = jarak_deg * 111000  # perkiraan kasar derajat->meter

        hasil_rows.append({
            "edge_idx": edge_idx,
            "requested_lat": titik_tengah_lat, "requested_lon": titik_tengah_lon,
            "jarak_ke_geometri_tile_terdekat_m": jarak_m,
            "tercakup_tomtom": jarak_m <= MAX_MATCH_DISTANCE_M,
        })

        if (edge_idx + 1) % 1000 == 0:
            print(f"  progres matching: {edge_idx+1}/{len(topo_df)} "
                  f"({time.time()-t_mulai_match:.0f}s)")

    hasil_df = pd.DataFrame(hasil_rows)
    hasil_df.to_csv(OUTPUT_CSV, index=False)

    n_tercakup = hasil_df["tercakup_tomtom"].sum()
    print(f"\n{'='*60}\nRINGKASAN AKHIR\n{'='*60}")
    print(f"Total segmen topologi v2       : {len(topo_df)}")
    print(f"Tercakup TomTom (jarak <= {MAX_MATCH_DISTANCE_M}m) : {n_tercakup} "
          f"({100*n_tercakup/len(topo_df):.1f}%)")
    print(f"Tidak tercakup                 : {len(topo_df)-n_tercakup} "
          f"({100*(len(topo_df)-n_tercakup)/len(topo_df):.1f}%)")
    print(f"\nTersimpan: {OUTPUT_CSV}")
    print(f"\nLANGKAH SELANJUTNYA: filter edge_idx dgn tercakup_tomtom=True dari file")
    print(f"ini, itulah himpunan segmen yg layak jadi kandidat polling Flow Segment")
    print(f"Data (fase 2, ambil nilai speed asli) -- jumlahnya jauh lebih kecil drpd")
    print(f"5700+ semula, jadi kuota fase 2 jauh lebih ringan.")


if __name__ == "__main__":
    main()
