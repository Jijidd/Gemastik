"""
Pemilihan Titik Representatif per Edge — Persiapan Polling Flow Segment Data
================================================================================
Input : jaksel_arteri_roads.geojson (hasil export_jaksel_roads.py, geometri edge)
        jaksel_node_mapping.csv     (index <-> osmid, hasil export_topology_and_boundary.py)
        jaksel_topology.csv         (from, to, distance -- format ala PeMS)

Output: polling_points.csv
        Kolom: edge_id, from_idx, to_idx, lat, lon, highway_tag, distance_m

Logika: untuk tiap edge di jaksel_topology.csv, cari geometri LineString-nya di
jaksel_arteri_roads.geojson (join lewat u,v osmid <-> from_idx,to_idx), lalu ambil
titik di 50% panjang garis (midpoint sepanjang kurva, BUKAN rata-rata koordinat
titik ujung -- penting untuk jalan yang melengkung).

edge_id di sini SAMA dengan urutan baris di jaksel_topology.csv, supaya nanti hasil
polling bisa langsung dikaitkan balik ke topologi graf untuk pembentukan tensor.
"""

import pandas as pd
import geopandas as gpd
from pathlib import Path

# =========================================================
# KONFIGURASI PATH -- SESUAIKAN
# =========================================================
ROADS_GEOJSON = "jaksel_arteri_roads.geojson"
NODE_MAPPING_CSV = "jaksel_node_mapping.csv"
TOPOLOGY_CSV = "jaksel_topology.csv"
OUTPUT_CSV = "polling_points.csv"


def bangun_titik_polling(roads_geojson_path, node_mapping_path, topology_path, output_path):
    print("Memuat data...")
    edges_gdf = gpd.read_file(roads_geojson_path)
    node_mapping = pd.read_csv(node_mapping_path)
    topology = pd.read_csv(topology_path)

    print(f"  jumlah edge geometri (GeoJSON) : {len(edges_gdf)}")
    print(f"  jumlah node mapping            : {len(node_mapping)}")
    print(f"  jumlah edge topologi (from/to) : {len(topology)}")

    # --- Mapping osmid -> index (kebalikan dari node_mapping.csv) ---
    osmid_to_index = dict(zip(node_mapping['osmid'], node_mapping['index']))

    # --- Pastikan kolom u/v di edges_gdf ada; kalau nama beda, sesuaikan di sini ---
    kolom_u = 'u' if 'u' in edges_gdf.columns else None
    kolom_v = 'v' if 'v' in edges_gdf.columns else None
    if kolom_u is None or kolom_v is None:
        raise ValueError(
            f"Kolom 'u'/'v' tidak ditemukan di {roads_geojson_path}. "
            f"Kolom yang tersedia: {list(edges_gdf.columns)}. "
            f"Sesuaikan nama kolom u/v di script ini secara manual."
        )

    edges_gdf['from_idx'] = edges_gdf[kolom_u].map(osmid_to_index)
    edges_gdf['to_idx'] = edges_gdf[kolom_v].map(osmid_to_index)

    n_unmapped = edges_gdf['from_idx'].isna().sum() + edges_gdf['to_idx'].isna().sum()
    if n_unmapped > 0:
        print(f"\nPERINGATAN: {n_unmapped} referensi node di edges_gdf tidak ketemu "
              f"di node_mapping.csv. Baris terkait akan dilewati.")

    edges_gdf = edges_gdf.dropna(subset=['from_idx', 'to_idx']).copy()
    edges_gdf['from_idx'] = edges_gdf['from_idx'].astype(int)
    edges_gdf['to_idx'] = edges_gdf['to_idx'].astype(int)

    # --- Join topology (from,to,distance) dengan geometri edge (from_idx,to_idx) ---
    # Kalau ada multi-edge (u,v) duplikat, ambil yang pertama saja (asumsi sudah
    # di-drop-duplicate saat pembuatan jaksel_topology.csv sebelumnya).
    edges_gdf_dedup = edges_gdf.drop_duplicates(subset=['from_idx', 'to_idx'], keep='first')

    merged = topology.merge(
        edges_gdf_dedup[['from_idx', 'to_idx', 'geometry', 'highway']],
        left_on=['from', 'to'],
        right_on=['from_idx', 'to_idx'],
        how='left',
    )

    n_geometri_hilang = merged['geometry'].isna().sum()
    if n_geometri_hilang > 0:
        print(f"\nPERINGATAN: {n_geometri_hilang} dari {len(merged)} edge topologi "
              f"tidak ketemu geometrinya di GeoJSON ({100*n_geometri_hilang/len(merged):.1f}%). "
              f"Kemungkinan penyebab: edge terbuang saat konsolidasi persimpangan, atau "
              f"mismatch arah (u,v) vs (from,to). Baris ini akan dilewati untuk polling.")

    merged = merged.dropna(subset=['geometry']).copy()

    # --- Ambil titik midpoint SEPANJANG KURVA (bukan rata-rata titik ujung) ---
    def ambil_midpoint(geom):
        titik = geom.interpolate(0.5, normalized=True)
        return titik.y, titik.x  # (lat, lon)

    lat_lon = merged['geometry'].apply(ambil_midpoint)
    merged['lat'] = lat_lon.apply(lambda x: x[0])
    merged['lon'] = lat_lon.apply(lambda x: x[1])

    # --- edge_id = posisi baris di topology.csv ASLI (bukan setelah dropna) ---
    # supaya konsisten dipakai untuk join balik ke adjacency matrix nanti.
    merged = merged.reset_index().rename(columns={'index': 'edge_id'})

    hasil = merged[['edge_id', 'from', 'to', 'lat', 'lon', 'highway', 'distance']].rename(
        columns={'from': 'from_idx', 'to': 'to_idx', 'highway': 'highway_tag', 'distance': 'distance_m'}
    )

    hasil.to_csv(output_path, index=False)

    print(f"\nTersimpan: {output_path} ({len(hasil)} titik polling)")
    print(f"\nCakupan: {len(hasil)} dari {len(topology)} edge topologi "
          f"({100*len(hasil)/len(topology):.1f}%) berhasil dapat titik polling.")
    print(f"\nContoh 5 baris pertama:")
    print(hasil.head())

    return hasil


if __name__ == "__main__":
    bangun_titik_polling(ROADS_GEOJSON, NODE_MAPPING_CSV, TOPOLOGY_CSV, OUTPUT_CSV)