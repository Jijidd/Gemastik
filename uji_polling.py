"""
Uji Coba Vector Flow Tiles -- Skala Kecil (2-3 Segmen Bersambung)
================================================================================
TUJUAN: sebelum polling besar-besaran, uji dulu di beberapa segmen topologi v2
untuk cek (a) apakah zoom level yang dipilih cukup akurat menangkap geometri
segmen target, dan (b) apakah hasil decode tile bisa dicocokkan balik ke
segmen kita dengan baik.

CATATAN PENTING: Vector Flow Tiles adalah snapshot REAL-TIME (bukan agregasi
15 menit). Tidak perlu menunggu 15 menit per polling -- request langsung
dijawab dengan kondisi saat itu. Interval 15 menit nanti (kalau lanjut ke
polling rutin) murni pilihan JADWAL kita sendiri, bukan keterbatasan API.
"""

import math
import io
import requests
import pandas as pd
import numpy as np
from shapely.geometry import LineString
import mapbox_vector_tile

# =========================================================
# 0. KONFIGURASI
# =========================================================
import os
API_KEY = os.environ.get("TOMTOM_API_KEY", "")
TOPOLOGY_V2_CSV = "jaksel_topology_v2.csv"
NODE_MAPPING_V2_CSV = "jaksel_node_mapping_v2.csv"   # index -> x_m, y_m (EPSG:32748, lihat build_topology_v2.py)

N_SEGMEN_UJI = 3            # 2 atau 3 sesuai permintaan
ZOOM_CANDIDATES = [16, 17, 18, 19]  # dicoba berurutan, pilih yg pas


# =========================================================
# 1. PILIH N SEGMEN BERSAMBUNG DARI TOPOLOGI V2 (BFS sederhana)
# =========================================================
def pilih_segmen_bersambung(topo_df, n_segmen):
    """
    Ambil N edge yang membentuk rantai bersambung (edge i berbagi node
    dengan edge i+1), mulai dari edge pertama di topologi.
    """
    from collections import defaultdict
    edge_by_from = defaultdict(list)
    for idx, row in topo_df.iterrows():
        edge_by_from[row["from"]].append(idx)

    start_idx = 0
    chain = [start_idx]
    current_to = topo_df.loc[start_idx, "to"]

    while len(chain) < n_segmen:
        kandidat = [e for e in edge_by_from.get(current_to, []) if e not in chain]
        if not kandidat:
            break
        next_idx = kandidat[0]
        chain.append(next_idx)
        current_to = topo_df.loc[next_idx, "to"]

    return chain


# =========================================================
# 2. HITUNG TILE (z, x, y) YANG MENCAKUP GEOMETRI SEGMEN TARGET
# =========================================================
def deg2num(lat_deg, lon_deg, zoom):
    """Standar slippy-map: konversi lat/lon -> nomor tile (x, y) di suatu zoom."""
    lat_rad = math.radians(lat_deg)
    n = 2.0 ** zoom
    xtile = int((lon_deg + 180.0) / 360.0 * n)
    ytile = int((1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n)
    return xtile, ytile


def num2deg(xtile, ytile, zoom):
    """Kebalikan deg2num -- batas lat/lon dari suatu tile."""
    n = 2.0 ** zoom
    lon_deg = xtile / n * 360.0 - 180.0
    lat_rad = math.atan(math.sinh(math.pi * (1 - 2 * ytile / n)))
    lat_deg = math.degrees(lat_rad)
    return lat_deg, lon_deg


def tile_size_meter(lat_deg, zoom):
    """Estimasi lebar 1 tile dalam meter, pada suatu lintang."""
    return 40075016.686 * math.cos(math.radians(lat_deg)) / (2 ** zoom)


def pilih_zoom_optimal(lat_center, bbox_diagonal_m, zoom_candidates,
                        margin_factor=3.0):
    """
    Pilih zoom TERBESAR (paling detail) yang tile-nya MASIH cukup lebar
    (>= bbox_diagonal_m * margin_factor) supaya segmen target tidak terlalu
    dekat ke tepi tile (menghindari perlu banyak tile sekaligus utk kasus
    sederhana ini) TAPI tetap presisi.
    """
    hasil = []
    for z in zoom_candidates:
        lebar_tile_m = tile_size_meter(lat_center, z)
        cukup = lebar_tile_m >= bbox_diagonal_m * margin_factor
        hasil.append((z, lebar_tile_m, cukup))

    print(f"\n{'='*60}\nPEMILIHAN ZOOM LEVEL\n{'='*60}")
    print(f"Diagonal bbox segmen target: {bbox_diagonal_m:.1f} m")
    for z, lebar, cukup in hasil:
        status = "OK (margin cukup)" if cukup else "terlalu sempit"
        print(f"  zoom={z}: lebar tile ~{lebar:.0f} m -> {status}")

    kandidat_ok = [z for z, lebar, cukup in hasil if cukup]
    if not kandidat_ok:
        print("PERINGATAN: tidak ada zoom kandidat dgn margin cukup, pakai zoom TERBESAR "
              "(paling detail) dari daftar -- kemungkinan perlu fetch >1 tile.")
        return zoom_candidates[-1]

    zoom_terpilih = max(kandidat_ok)   # paling detail yg masih memenuhi margin
    print(f"\nZoom terpilih: {zoom_terpilih}")
    return zoom_terpilih


# =========================================================
# 3. AMBIL & DECODE VECTOR TILE
# =========================================================
def ambil_dan_decode_tile(zoom, xtile, ytile, api_key):
    url = f"https://api.tomtom.com/traffic/services/4/tile/flow/absolute/{zoom}/{xtile}/{ytile}.pbf"
    params = {"key": api_key}
    resp = requests.get(url, params=params, timeout=15)

    if resp.status_code != 200:
        raise RuntimeError(f"Gagal ambil tile: HTTP {resp.status_code} -- {resp.text[:200]}")

    decoded = mapbox_vector_tile.decode(resp.content)
    return decoded


def tile_local_ke_lonlat(x_local, y_local, extent, tile_bounds):
    """
    Konversi koordinat lokal tile (0..extent) ke lon/lat asli, menggunakan
    batas geografis tile (tile_bounds = (lon_min, lat_min, lon_max, lat_max)).
    """
    lon_min, lat_min, lon_max, lat_max = tile_bounds
    lon = lon_min + (x_local / extent) * (lon_max - lon_min)
    # Y tile biasanya terbalik (0 di atas = lat_max)
    lat = lat_max - (y_local / extent) * (lat_max - lat_min)
    return lon, lat


# =========================================================
# 4. COCOKKAN FITUR HASIL DECODE KE SEGMEN TARGET (nearest-line matching)
# =========================================================
def cocokkan_fitur_ke_segmen(decoded_tile, zoom, xtile, ytile, segmen_target_geoms,
                              threshold_m=30.0):
    """
    segmen_target_geoms: list of shapely LineString (lon/lat) segmen yang kita cari.
    Return: list hasil match (index segmen target, jarak, properti speed).
    """
    lat_max, lon_min = num2deg(xtile, ytile, zoom)
    lat_min, lon_max = num2deg(xtile + 1, ytile + 1, zoom)
    tile_bounds = (lon_min, lat_min, lon_max, lat_max)

    # Cari layer yang relevan -- nama layer bisa "Traffic flow" atau serupa,
    # ambil semua layer yg ada & cek isinya (tidak diasumsikan nama pasti).
    semua_fitur_geoms = []
    for layer_name, layer_data in decoded_tile.items():
        extent = layer_data.get("extent", 4096)
        for feat in layer_data.get("features", []):
            geom = feat.get("geometry", {})
            coords_local = geom.get("coordinates", [])
            if geom.get("type") != "LineString":
                continue
            coords_lonlat = [tile_local_ke_lonlat(x, y, extent, tile_bounds) for x, y in coords_local]
            if len(coords_lonlat) >= 2:
                semua_fitur_geoms.append({
                    "layer": layer_name,
                    "geometry": LineString(coords_lonlat),
                    "properties": feat.get("properties", {}),
                })

    print(f"\nJumlah fitur LineString hasil decode tile: {len(semua_fitur_geoms)}")
    if len(semua_fitur_geoms) > 0:
        print(f"Nama layer yang ditemukan: {set(f['layer'] for f in semua_fitur_geoms)}")
        print(f"Contoh properti fitur pertama: {semua_fitur_geoms[0]['properties']}")

    hasil_match = []
    for i, target_geom in enumerate(segmen_target_geoms):
        target_mid = target_geom.interpolate(0.5, normalized=True)
        jarak_terdekat = float("inf")
        fitur_terdekat = None
        for f in semua_fitur_geoms:
            jarak = target_mid.distance(f["geometry"]) * 111000  # derajat -> meter kasar
            if jarak < jarak_terdekat:
                jarak_terdekat = jarak
                fitur_terdekat = f
        valid = jarak_terdekat <= threshold_m
        hasil_match.append({
            "segmen_target_idx": i,
            "jarak_ke_fitur_terdekat_m": jarak_terdekat,
            "valid_match": valid,
            "properties": fitur_terdekat["properties"] if fitur_terdekat else None,
        })

    return hasil_match


# =========================================================
# MAIN -- UJI COBA
# =========================================================
def jalankan_uji_coba():
    if not API_KEY:
        raise RuntimeError("TOMTOM_API_KEY belum diset di environment variable.")

    topo_df = pd.read_csv(TOPOLOGY_V2_CSV)
    node_df = pd.read_csv(NODE_MAPPING_V2_CSV).set_index("index")

    chain_idx = pilih_segmen_bersambung(topo_df, N_SEGMEN_UJI)
    print(f"Segmen terpilih (index topologi v2): {chain_idx}")

    # Bangun geometri tiap segmen target (lon/lat) dari node mapping.
    # CATATAN: node_mapping_v2.csv menyimpan x_m/y_m dlm EPSG:32748 (proyeksi
    # metrik), BUKAN lon/lat langsung -- perlu dikonversi balik. Kalau file
    # kalian sudah berbeda skema (mis. sudah simpan lon/lat), sesuaikan bagian ini.
    import pyproj
    transformer = pyproj.Transformer.from_crs("EPSG:32748", "EPSG:4326", always_xy=True)

    segmen_geoms = []
    all_lonlat_points = []
    for idx in chain_idx:
        row = topo_df.loc[idx]
        p1 = node_df.loc[row["from"]]
        p2 = node_df.loc[row["to"]]
        lon1, lat1 = transformer.transform(p1["x_m"], p1["y_m"])
        lon2, lat2 = transformer.transform(p2["x_m"], p2["y_m"])
        segmen_geoms.append(LineString([(lon1, lat1), (lon2, lat2)]))
        all_lonlat_points.extend([(lon1, lat1), (lon2, lat2)])

    lons = [p[0] for p in all_lonlat_points]
    lats = [p[1] for p in all_lonlat_points]
    lat_center = sum(lats) / len(lats)
    lon_center = sum(lons) / len(lons)

    # Hitung diagonal bbox segmen target (estimasi kasar, meter)
    lat_range_m = (max(lats) - min(lats)) * 111000
    lon_range_m = (max(lons) - min(lons)) * 111000 * math.cos(math.radians(lat_center))
    bbox_diagonal_m = math.sqrt(lat_range_m**2 + lon_range_m**2)

    zoom_terpilih = pilih_zoom_optimal(lat_center, bbox_diagonal_m, ZOOM_CANDIDATES)
    xtile, ytile = deg2num(lat_center, lon_center, zoom_terpilih)
    print(f"\nTile terpilih: z={zoom_terpilih}, x={xtile}, y={ytile}")

    decoded = ambil_dan_decode_tile(zoom_terpilih, xtile, ytile, API_KEY)
    hasil_match = cocokkan_fitur_ke_segmen(decoded, zoom_terpilih, xtile, ytile, segmen_geoms)

    print(f"\n{'='*60}\nHASIL MATCHING\n{'='*60}")
    for h in hasil_match:
        status = "VALID" if h["valid_match"] else "TIDAK VALID (terlalu jauh)"
        print(f"Segmen idx {chain_idx[h['segmen_target_idx']]}: "
              f"jarak={h['jarak_ke_fitur_terdekat_m']:.1f}m [{status}]")
        print(f"  properti: {h['properties']}")

    n_valid = sum(1 for h in hasil_match if h["valid_match"])
    print(f"\nRingkasan: {n_valid}/{len(hasil_match)} segmen berhasil match dgn valid.")
    print("Kalau semua/mayoritas valid -> zoom level ini layak dipakai utk polling")
    print("skala lebih besar. Kalau banyak gagal -> naikkan zoom (lebih detail)")
    print("atau cek ulang proyeksi koordinat node_mapping_v2.csv.")


if __name__ == "__main__":
    jalankan_uji_coba()