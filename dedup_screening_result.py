"""
Deduplikasi Kandidat Edge Berdasarkan Titik TomTom Sesungguhnya
=====================================================================
TEMUAN: dari 682 edge valid hasil screening, banyak yang ternyata nyasar
ke TITIK TomTom YANG SAMA (matched_lat/matched_lon identik) -- granularitas
topologi_v2 kita (~59m/edge) di beberapa lokasi lebih halus dari granularitas
internal TomTom Flow Segment Data, jadi beberapa edge kita "menempel" ke
segmen TomTom yang sama.

PENTING: dedup ini TIDAK BUTUH kuota API tambahan -- matched_lat/matched_lon
SUDAH ADA di hasil screening_seluruh_jaksel.py (screening kemarin SUDAH
menyimpannya). Jalankan skrip ini SEBELUM cari_graf_bersambung_terbesar.py,
supaya bin-packing bekerja di atas himpunan TITIK FISIK UNIK, bukan edge_idx
mentah yang banyak duplikatnya.
"""

import pandas as pd

# =========================================================
# 0. KONFIGURASI -- SESUAIKAN nama file hasil screening_seluruh_jaksel.py
# =========================================================
SCREENING_RESULT_CSV = "screening_seluruh_jaksel.csv"   # file MENTAH (5745 baris, campur valid/invalid -- OK, filter valid_match dilakukan di skrip ini)
# Kolom yg diharapkan ada (sesuai struktur poll_tomtom_flow.py juga):
#   edge_idx (atau edge_id), matched_lat, matched_lon, valid_match, dst.

OUTPUT_DEDUP_CSV = "screening_results_deduped.csv"

# =========================================================
# 1. LOAD & FILTER CUMA YANG VALID
# =========================================================
df = pd.read_csv(SCREENING_RESULT_CSV)
edge_col = "edge_idx" if "edge_idx" in df.columns else "edge_id"

df_valid = df[df["valid_match"] == True].copy()
print(f"Edge valid hasil screening: {len(df_valid)}")

# =========================================================
# 2. GROUPING BERDASARKAN TITIK TOMTOM SESUNGGUHNYA
# =========================================================
# Bulatkan ke 5 desimal (~1.1m presisi) supaya floating-point noise kecil
# tidak dianggap titik berbeda secara keliru.
df_valid["matched_lat_r"] = df_valid["matched_lat"].round(5)
df_valid["matched_lon_r"] = df_valid["matched_lon"].round(5)

grup_titik = df_valid.groupby(["matched_lat_r", "matched_lon_r"])[edge_col].apply(list)
grup_duplikat = grup_titik[grup_titik.apply(len) > 1]

print(f"\nJumlah titik TomTom unik           : {len(grup_titik)}")
print(f"Jumlah titik yg py >1 edge (duplikat): {len(grup_duplikat)}")
n_edge_terbuang = sum(len(v) - 1 for v in grup_duplikat)
print(f"Jumlah edge_idx REDUNDAN (akan dibuang): {n_edge_terbuang}")
print(f"Penghematan kuota per siklus polling  : {n_edge_terbuang}/{len(df_valid)} "
      f"({100*n_edge_terbuang/len(df_valid):.1f}%)")

# =========================================================
# 3. PILIH SATU EDGE_IDX PER TITIK UNIK
# =========================================================
# Strategi pemilihan representatif: ambil yg PALING KECIL edge_idx-nya
# (arbitrer tapi deterministik -- bisa diganti pertimbangan lain, misal
# prioritaskan yg jarak match paling dekat, kalau kolom itu ada).
edge_terpilih = []
for (lat_r, lon_r), grup in df_valid.groupby(["matched_lat_r", "matched_lon_r"]):
    grup_sorted = grup.sort_values("match_distance_m") if "match_distance_m" in grup.columns else grup
    edge_terpilih.append(grup_sorted.iloc[0][edge_col])

df_dedup = df_valid[df_valid[edge_col].isin(edge_terpilih)].drop(
    columns=["matched_lat_r", "matched_lon_r"]
)
df_dedup.to_csv(OUTPUT_DEDUP_CSV, index=False)

print(f"\nTersimpan: {OUTPUT_DEDUP_CSV}")
print(f"Edge valid SEBELUM dedup : {len(df_valid)}")
print(f"Edge valid SETELAH dedup : {len(df_dedup)}")
print(f"\nLANGKAH SELANJUTNYA: ganti input cari_graf_bersambung_terbesar.py")
print(f"dari hasil screening mentah -> {OUTPUT_DEDUP_CSV} ini, baru jalankan bin-packing.")