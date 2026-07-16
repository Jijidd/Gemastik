"""
Reset Baris ERROR di Hasil Screening -- utk Retry Selektif
================================================================================
Masalah: screening_seluruh_jaksel.py menganggap SEMUA edge_idx yang sudah ada
di CSV output sebagai "selesai discreening" -- termasuk yang gagal karena
error (mis. kuota habis / InsufficientFunds), bukan cuma yang benar-benar
berhasil dicoba.

Solusi: script ini MEMBUANG baris-baris yang error dari screening_seluruh_jaksel.csv
(disimpan dulu ke file backup log), supaya edge_idx itu dianggap "BELUM
discreening" oleh script utama -- jalankan screening_seluruh_jaksel.py lagi
setelahnya, otomatis HANYA memproses ulang edge yang errornya kemarin, TANPA
mengulang ribuan edge yang sudah berhasil valid.

JALANKAN INI SEKALI sebelum re-run screening_seluruh_jaksel.py, SETELAH kuota
TomTom tersedia lagi.
"""

import pandas as pd
from pathlib import Path
from datetime import datetime, timezone

OUTPUT_CSV = "screening_seluruh_jaksel.csv"
BACKUP_ERROR_LOG = "screening_error_dibuang.csv"   # simpan dulu utk arsip/debug


def main():
    if not Path(OUTPUT_CSV).exists():
        raise FileNotFoundError(f"'{OUTPUT_CSV}' tidak ditemukan.")

    df = pd.read_csv(OUTPUT_CSV)
    print(f"Total baris sebelum reset: {len(df)}")

    mask_error = df["error"].notna()
    n_error = mask_error.sum()
    n_sukses = (~mask_error).sum()

    print(f"Baris SUKSES (dipertahankan)  : {n_sukses}")
    print(f"Baris ERROR (akan direset)    : {n_error}")

    if n_error == 0:
        print("\nTidak ada baris error sama sekali -- tidak ada yang perlu direset.")
        return

    # --- Simpan baris error ke backup log (arsip, jaga2 perlu ditinjau) ---
    df_error = df[mask_error].copy()
    if Path(BACKUP_ERROR_LOG).exists():
        df_error_lama = pd.read_csv(BACKUP_ERROR_LOG)
        df_error = pd.concat([df_error_lama, df_error], ignore_index=True)
    df_error.to_csv(BACKUP_ERROR_LOG, index=False)
    print(f"\nBaris error disimpan ke arsip: {BACKUP_ERROR_LOG} ({len(df_error)} baris total)")

    # --- Contoh jenis error yang direset (utk konfirmasi visual) ---
    print(f"\nContoh pesan error yang direset:")
    for pesan in df.loc[mask_error, "error"].unique()[:3]:
        print(f"  - {pesan[:100]}")

    # --- Buang baris error dari file utama ---
    df_bersih = df[~mask_error].reset_index(drop=True)
    df_bersih.to_csv(OUTPUT_CSV, index=False)

    edge_idx_direset = sorted(df.loc[mask_error, "edge_idx"].tolist())
    print(f"\n{'='*60}")
    print(f"SELESAI")
    print(f"{'='*60}")
    print(f"'{OUTPUT_CSV}' sekarang berisi {len(df_bersih)} baris (hanya yg sukses).")
    print(f"Edge_idx yang akan di-RETRY di run berikutnya: {len(edge_idx_direset)}")
    print(f"Rentang: {min(edge_idx_direset)} - {max(edge_idx_direset)}")
    print(f"\nLANGKAH SELANJUTNYA: jalankan screening_seluruh_jaksel.py seperti biasa --")
    print(f"otomatis akan HANYA memproses {len(edge_idx_direset)} edge yang direset ini,")
    print(f"TIDAK mengulang {len(df_bersih)} edge yang sudah sukses.")


if __name__ == "__main__":
    main()
