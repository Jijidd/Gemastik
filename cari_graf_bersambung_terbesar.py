"""
Cari Graf Bersambung Terbesar dari Segmen Tercakup TomTom
================================================================================
Input : cakupan_seluruh_jaksel_via_tile.csv (hasil deteksi cakupan, kolom
        edge_idx + tercakup_tomtom), jaksel_topology_v2.csv (from/to/distance)
Output: graf_bersambung_terbesar.csv (edge_idx yg jadi target polling),
        + laporan kompleksitas (jumlah node, edge, titik percabangan) supaya
        bisa dinilai apakah komponen ini merepresentasikan "complex urban
        network" (banyak persimpangan/percabangan), bukan sekadar rantai lurus.

GRANULARITAS TIDAK DIUBAH -- tetap level segmen topologi v2 yg sama, cuma
DISARING ke subset yg (a) tercakup TomTom dan (b) membentuk satu komponen
graf yg saling terhubung.
"""

import pandas as pd
import numpy as np
import networkx as nx
import pyproj

# =========================================================
# 0. KONFIGURASI
# =========================================================
CAKUPAN_CSV = "cakupan_seluruh_jaksel_via_tile.csv"
TOPOLOGY_V2_CSV = "jaksel_topology_v2.csv"
NODE_MAPPING_V2_CSV = "jaksel_node_mapping_v2.csv"

OUTPUT_EDGE_LIST_CSV = "graf_bersambung_terbesar.csv"
OUTPUT_POLLING_POINTS_CSV = "polling_points_graf_terbesar.csv"


def main():
    cakupan_df = pd.read_csv(CAKUPAN_CSV)
    topo_df = pd.read_csv(TOPOLOGY_V2_CSV)
    node_df = pd.read_csv(NODE_MAPPING_V2_CSV).set_index("index")

    assert len(cakupan_df) == len(topo_df), (
        "Jumlah baris cakupan vs topologi tidak sama -- pastikan file cocok!"
    )

    # --- 1. Filter ke edge yang tercakup TomTom saja ---
    edge_tercakup = set(cakupan_df.loc[cakupan_df["tercakup_tomtom"] == True, "edge_idx"])
    print(f"Total edge topologi v2   : {len(topo_df)}")
    print(f"Edge tercakup TomTom     : {len(edge_tercakup)} "
          f"({100*len(edge_tercakup)/len(topo_df):.1f}%)")

    # --- 2. Bangun graf UNDIRECTED dari edge yang tercakup saja ---
    # Undirected krn yg kita cari adalah KETERHUBUNGAN fisik jaringan jalan
    # (relevan utk "complex urban network"), bukan reachability satu arah.
    # Tiap pasangan (from,to) dicatat -> edge_idx yg memetakan ke situ, supaya
    # edge dua-arah (A->B & B->A sbg baris terpisah) tetap terlacak keduanya.
    G = nx.Graph()
    pasangan_ke_edge_idx = {}

    for edge_idx in edge_tercakup:
        row = topo_df.loc[edge_idx]
        u, v = row["from"], row["to"]
        G.add_edge(u, v)
        key = frozenset([u, v])
        pasangan_ke_edge_idx.setdefault(key, []).append(edge_idx)

    print(f"\nGraf (undirected) dibangun: {G.number_of_nodes()} node, "
          f"{G.number_of_edges()} pasangan node unik (dari {len(edge_tercakup)} edge_idx)")

    # --- 3. Cari SELURUH komponen bersambung, urutkan dari terbesar ---
    komponen_list = sorted(nx.connected_components(G), key=len, reverse=True)
    print(f"\nJumlah komponen bersambung terpisah: {len(komponen_list)}")
    print(f"\nTop 5 komponen terbesar (berdasar jumlah NODE):")
    for i, komp in enumerate(komponen_list[:5]):
        subgraph = G.subgraph(komp)
        print(f"  #{i+1}: {len(komp)} node, {subgraph.number_of_edges()} pasangan edge")

    if len(komponen_list) == 0:
        raise RuntimeError("Tidak ada komponen bersambung sama sekali -- cek data cakupan.")

    komponen_terbesar = komponen_list[0]
    subgraph_terbesar = G.subgraph(komponen_terbesar)

    # --- 4. Kumpulkan SEMUA edge_idx (termasuk dua-arah) yg node-nya masuk komponen ini ---
    edge_idx_terpilih = []
    for u, v in subgraph_terbesar.edges():
        key = frozenset([u, v])
        edge_idx_terpilih.extend(pasangan_ke_edge_idx[key])

    print(f"\n{'='*60}\nKOMPONEN TERBESAR TERPILIH\n{'='*60}")
    print(f"Jumlah node (persimpangan) : {len(komponen_terbesar)}")
    print(f"Jumlah edge_idx (arah)     : {len(edge_idx_terpilih)}")

    # --- 5. Laporan kompleksitas percabangan (bukti "complex urban network") ---
    derajat = dict(subgraph_terbesar.degree())
    derajat_values = list(derajat.values())
    n_titik_cabang = sum(1 for d in derajat_values if d >= 3)  # persimpangan >2 arah
    n_ujung_buntu = sum(1 for d in derajat_values if d == 1)

    print(f"\n--- Kompleksitas percabangan ---")
    print(f"Rata-rata derajat node        : {np.mean(derajat_values):.2f}")
    print(f"Derajat maksimum (persimpangan tersibuk) : {max(derajat_values)}")
    print(f"Titik percabangan (derajat>=3): {n_titik_cabang} "
          f"({100*n_titik_cabang/len(komponen_terbesar):.1f}% dari node)")
    print(f"Jalan buntu (derajat==1)      : {n_ujung_buntu}")

    if n_titik_cabang / len(komponen_terbesar) < 0.05:
        print(f"\nPERINGATAN: cuma {100*n_titik_cabang/len(komponen_terbesar):.1f}% node yg jadi")
        print(f"titik percabangan -- komponen ini mendekati RANTAI LURUS, kurang")
        print(f"merepresentasikan 'complex urban network'. Pertimbangkan komponen")
        print(f"terbesar ke-2/ke-3 kalau strukturnya lebih bercabang (cek daftar di atas),")
        print(f"atau gabungkan beberapa komponen berdekatan.")
    else:
        print(f"\nOK: proporsi titik percabangan cukup signifikan, komponen ini punya")
        print(f"struktur bercabang (bukan sekadar rantai lurus).")

    # --- 6. Simpan daftar edge_idx target ---
    hasil_edge_df = pd.DataFrame({"edge_idx": sorted(edge_idx_terpilih)})
    hasil_edge_df.to_csv(OUTPUT_EDGE_LIST_CSV, index=False)
    print(f"\nTersimpan: {OUTPUT_EDGE_LIST_CSV} ({len(hasil_edge_df)} edge_idx)")

    # --- 7. Siapkan titik polling (format siap pakai utk Flow Segment Data) ---
    transformer = pyproj.Transformer.from_crs("EPSG:32748", "EPSG:4326", always_xy=True)
    polling_rows = []
    for edge_idx in sorted(edge_idx_terpilih):
        row = topo_df.loc[edge_idx]
        p1 = node_df.loc[row["from"]]
        p2 = node_df.loc[row["to"]]
        lon1, lat1 = transformer.transform(p1["x_m"], p1["y_m"])
        lon2, lat2 = transformer.transform(p2["x_m"], p2["y_m"])
        polling_rows.append({
            "edge_id": edge_idx,
            "lat": (lat1 + lat2) / 2,
            "lon": (lon1 + lon2) / 2,
            "highway_tag": "complex_corridor",  # placeholder, FRC per-edge tdk dilacak di tahap ini
            "distance_m": row["distance"],
        })

    polling_df = pd.DataFrame(polling_rows)
    polling_df.to_csv(OUTPUT_POLLING_POINTS_CSV, index=False)
    print(f"Tersimpan: {OUTPUT_POLLING_POINTS_CSV} (siap dipakai poll_tomtom_flow.py "
          f"dgn mengganti POLLING_POINTS_CSV ke file ini)")

    print(f"\nGRANULARITAS: tetap level segmen v2 (rata-rata panjang edge "
          f"{polling_df['distance_m'].mean():.1f}m) -- TIDAK diagregasi/digabung.")


if __name__ == "__main__":
    main()