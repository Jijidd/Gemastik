"""
Cari & Gabungkan Beberapa Komponen Bersambung Terbesar (Multi-Koridor)
================================================================================
Input : screening_seluruh_jaksel.csv (hasil screening Flow Segment Data PENUH,
        kolom edge_idx + valid_match), jaksel_topology_v2.csv (from/to/distance)
Output: graf_bersambung_terbesar.csv (edge_idx target polling),
        + laporan kompleksitas PER KOMPONEN dan gabungan.

PERUBAHAN PENTING dari versi 1-komponen sebelumnya: cakupan riil TomTom di
Jaksel (11.9% dari screening penuh) ternyata tersebar jadi 291 komponen kecil
terpisah -- komponen TERBESAR SENDIRIAN cuma 15 node/16 edge (~979m total),
terlalu kecil utk representasi "complex urban network". Solusi: gabungkan
N komponen teratas -- masing2 TETAP bersambung secara internal, tapi ANTAR
komponen tidak saling terhubung (beberapa 'pulau' jaringan jalan terpisah).
Ini realistis & jujur mengikuti pola cakupan TomTom yg memang tersebar,
BUKAN dipaksakan jadi 1 jaringan menyatu yg sebenarnya tidak ada di data.

GRANULARITAS TIDAK DIUBAH -- tetap level segmen topologi v2, cuma DISARING.
"""

import pandas as pd
import numpy as np
import networkx as nx
import pyproj

# =========================================================
# 0. KONFIGURASI
# =========================================================
CAKUPAN_CSV = "screening_seluruh_jaksel.csv"   # hasil screening Flow Segment Data
                                                  # (BUKAN tile lagi -- tile terbukti
                                                  # tidak valid jadi proxy cakupan)
TOPOLOGY_V2_CSV = "jaksel_topology_v2.csv"
NODE_MAPPING_V2_CSV = "jaksel_node_mapping_v2.csv"

OUTPUT_EDGE_LIST_CSV = "graf_bersambung_terbesar.csv"
OUTPUT_POLLING_POINTS_CSV = "polling_points_graf_terbesar.csv"

# Jumlah komponen bersambung TERBESAR yg digabungkan jadi target polling.
# Naikkan angka ini kalau masih ingin cakupan lebih luas (lebih banyak edge),
# turunkan kalau ingin lebih hemat kuota. 15 dipilih sbg titik awal yg
# menghasilkan skala puluhan-ratusan edge (lihat hasil cetak utk angka pasti).
N_KOMPONEN_DIGABUNG = 15


def main():
    cakupan_df = pd.read_csv(CAKUPAN_CSV)
    topo_df = pd.read_csv(TOPOLOGY_V2_CSV)
    node_df = pd.read_csv(NODE_MAPPING_V2_CSV).set_index("index")

    assert len(cakupan_df) == len(topo_df), (
        "Jumlah baris cakupan vs topologi tidak sama -- pastikan file cocok!"
    )

    # --- 1. Filter ke edge yang tercakup TomTom saja ---
    edge_tercakup = set(cakupan_df.loc[cakupan_df["valid_match"] == True, "edge_idx"])
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
    print(f"\nTop {min(N_KOMPONEN_DIGABUNG, len(komponen_list))} komponen terbesar (berdasar jumlah NODE):")
    for i, komp in enumerate(komponen_list[:N_KOMPONEN_DIGABUNG]):
        subgraph = G.subgraph(komp)
        print(f"  #{i+1}: {len(komp)} node, {subgraph.number_of_edges()} pasangan edge")

    if len(komponen_list) == 0:
        raise RuntimeError("Tidak ada komponen bersambung sama sekali -- cek data cakupan.")

    # --- 4. GABUNGKAN N komponen teratas (TIDAK saling terhubung satu sama
    # lain, tapi masing2 bersambung secara internal -- direpresentasikan sbg
    # beberapa 'pulau' jaringan jalan terpisah, bukan 1 jaringan menyatu.
    # Ini realistis krn cakupan TomTom di Jaksel memang tersebar terpisah-
    # pisah (291 komponen terpisah dari screening penuh), bukan 1 blok utuh.
    komponen_terpilih = komponen_list[:N_KOMPONEN_DIGABUNG]
    semua_node_terpilih = set()
    for komp in komponen_terpilih:
        semua_node_terpilih |= komp
    subgraph_gabungan = G.subgraph(semua_node_terpilih)

    edge_idx_terpilih = []
    edge_idx_ke_komponen_id = {}   # BARU -- disimpan supaya poll_tomtom_flow.py tinggal baca,
                                     # tidak perlu membangun ulang graf/komponen dari nol
    for komp_id, komp in enumerate(komponen_terpilih):
        subgraph_komp = G.subgraph(komp)
        for u, v in subgraph_komp.edges():
            key = frozenset([u, v])
            for eidx in pasangan_ke_edge_idx[key]:
                edge_idx_terpilih.append(eidx)
                edge_idx_ke_komponen_id[eidx] = komp_id

    print(f"\n{'='*60}\nGABUNGAN {len(komponen_terpilih)} KOMPONEN TERBESAR\n{'='*60}")
    print(f"PENTING: ini {len(komponen_terpilih)} 'pulau' jaringan jalan TERPISAH")
    print(f"(tidak saling terhubung satu sama lain), BUKAN 1 jaringan menyatu --")
    print(f"realistis mengikuti pola cakupan TomTom yg memang tersebar di Jaksel.")
    print(f"\nJumlah node total          : {len(semua_node_terpilih)}")
    print(f"Jumlah edge_idx total (arah): {len(edge_idx_terpilih)}")

    # --- 5. Laporan kompleksitas percabangan PER KOMPONEN + gabungan ---
    print(f"\n--- Kompleksitas percabangan per komponen ---")
    total_titik_cabang, total_ujung_buntu = 0, 0
    semua_derajat = []
    for i, komp in enumerate(komponen_terpilih):
        sub = G.subgraph(komp)
        derajat = dict(sub.degree())
        d_values = list(derajat.values())
        semua_derajat.extend(d_values)
        n_cabang = sum(1 for d in d_values if d >= 3)
        n_buntu = sum(1 for d in d_values if d == 1)
        total_titik_cabang += n_cabang
        total_ujung_buntu += n_buntu
        print(f"  Komponen #{i+1}: {len(komp)} node, derajat maks={max(d_values)}, "
              f"titik cabang={n_cabang} ({100*n_cabang/len(komp):.0f}%)")

    print(f"\n--- Ringkasan gabungan ---")
    print(f"Rata-rata derajat node (gabungan) : {np.mean(semua_derajat):.2f}")
    print(f"Total titik percabangan (derajat>=3): {total_titik_cabang} "
          f"({100*total_titik_cabang/len(semua_node_terpilih):.1f}% dari total node)")
    print(f"Total jalan buntu (derajat==1)      : {total_ujung_buntu}")

    if total_titik_cabang / len(semua_node_terpilih) < 0.05:
        print(f"\nPERINGATAN: proporsi titik percabangan gabungan masih rendah --")
        print(f"pertimbangkan naikkan N_KOMPONEN_DIGABUNG utk cakupan lebih luas.")
    else:
        print(f"\nOK: proporsi titik percabangan gabungan cukup signifikan.")

    # --- 6. Simpan daftar edge_idx target, LENGKAP DENGAN component_id ---
    hasil_edge_df = pd.DataFrame({
        "edge_idx": sorted(edge_idx_terpilih),
    })
    hasil_edge_df["component_id"] = hasil_edge_df["edge_idx"].map(edge_idx_ke_komponen_id)
    hasil_edge_df.to_csv(OUTPUT_EDGE_LIST_CSV, index=False)
    print(f"\nTersimpan: {OUTPUT_EDGE_LIST_CSV} ({len(hasil_edge_df)} edge_idx, "
          f"dgn kolom component_id -- 0..{len(komponen_terpilih)-1})")

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
            "component_id": edge_idx_ke_komponen_id[edge_idx],   # BARU
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