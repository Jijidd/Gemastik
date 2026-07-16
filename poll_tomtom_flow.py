"""
Polling TomTom Flow Segment Data — dengan Rotasi Grup Sesuai Kuota
================================================================================
Dirancang untuk dipanggil BERULANG oleh scheduler eksternal (cron / GitHub
Actions / Cloud Scheduler) setiap interval tertentu (mis. tiap 15 menit) —
BUKAN untuk dijalankan sebagai loop tak berhenti di Colab (sesi Colab akan
terputus sebelum data terkumpul cukup lama untuk riset ini).

Setiap kali dipanggil, script ini:
  1. Baca state (grup mana yang giliran dipoll sekarang, dari file JSON kecil)
  2. Poll semua titik di grup itu (dengan rate limit & retry otomatis)
  3. Simpan hasil ke file CSV harian (append, bukan overwrite)
  4. Update state ke grup berikutnya untuk pemanggilan selanjutnya

Kalau jumlah edge <= kapasitas kuota harian untuk interval yang diinginkan,
otomatis TIDAK ADA rotasi (n_groups=1, semua edge dipoll tiap kali).
"""

import os
import json
import time
import math
import requests
import pandas as pd
from datetime import datetime, timezone
from pathlib import Path

# =========================================================
# KONFIGURASI
# =========================================================
API_KEY = os.environ.get("TOMTOM_API_KEY", "")  # JANGAN hardcode API key di kode!
POLLING_POINTS_CSV = "polling_points.csv"
OUTPUT_DIR = "polling_results"
STATE_FILE = "polling_state.json"

QUOTA_PER_DAY = 2500          # kuota gratis TomTom non-tile request/hari
QUOTA_SAFETY_MARGIN = 0.95    # pakai 95% dari kuota, sisakan buffer utk error/retry
INTERVAL_MINUTES = 15         # target interval polling (disesuaikan dgn hasil hitung di bawah)
QPS_LIMIT = 4                 # request/detik, sedikit di bawah batas free tier (~5) utk aman

ZOOM = 15        # zoom cukup tinggi supaya jalan FRC2/FRC3 (bukan cuma motorway) ikut terdeteksi
STYLE = "absolute"
UNIT = "kmph"

MAX_RETRIES = 4
RETRY_BACKOFF_BASE = 2.0  # detik, dikali 2^percobaan

# Ambang jarak (meter) antara titik yang diminta vs titik yang benar-benar
# dicocokkan TomTom. Kalau jaraknya melebihi ini, data dianggap TIDAK VALID
# untuk edge tersebut -- kemungkinan TomTom tidak punya cakupan traffic
# monitoring di ruas itu, dan mengembalikan segmen fallback yang jauh.
MAX_MATCH_DISTANCE_M = 150

# Skema kolom TETAP -- SATU-SATUNYA sumber kebenaran urutan/nama kolom.
# Kalau nanti perlu menambah field baru, tambahkan di sini, dan fungsi
# simpan_hasil_polling() di bawah akan otomatis memigrasi file lama yang
# skemanya belum punya kolom itu (diisi NaN untuk baris lama), BUKAN
# menyebabkan schema drift seperti yang sempat terjadi sebelumnya.
SKEMA_KOLOM = [
    "timestamp_utc", "edge_id", "requested_lat", "requested_lon", "highway_tag",
    "frc", "currentSpeed", "freeFlowSpeed", "currentTravelTime", "freeFlowTravelTime",
    "confidence", "roadClosure", "matched_lat", "matched_lon",
    "match_distance_m", "valid_match", "error",
]


def simpan_hasil_polling(hasil_df, output_path):
    """
    Simpan hasil polling ke CSV harian dengan APPEND yang aman terhadap
    perubahan skema. Kalau file sudah ada tapi headernya beda dari
    SKEMA_KOLOM saat ini (mis. kode di-update di tengah hari), file lama
    otomatis dimigrasi dulu (kolom baru diisi NaN untuk baris lama) sebelum
    baris baru ditambahkan -- supaya tidak pernah terjadi campur skema di
    satu file yang bikin pd.read_csv gagal parse.
    """
    output_path = Path(output_path)
    hasil_df = hasil_df.reindex(columns=SKEMA_KOLOM)

    if output_path.exists():
        header_lama = pd.read_csv(output_path, nrows=0).columns.tolist()
        if header_lama != SKEMA_KOLOM:
            print(f"PERINGATAN: skema file lama ({len(header_lama)} kolom) berbeda dari "
                  f"skema kode saat ini ({len(SKEMA_KOLOM)} kolom). Memigrasi file lama...")
            df_lama = pd.read_csv(output_path)
            df_lama = df_lama.reindex(columns=SKEMA_KOLOM)  # kolom baru otomatis jadi NaN
            df_lama.to_csv(output_path, index=False)  # tulis ulang dgn skema baru
            print(f"Migrasi selesai: {output_path} sekarang konsisten {len(SKEMA_KOLOM)} kolom.")

    header_diperlukan = not output_path.exists()
    hasil_df.to_csv(output_path, mode="a", index=False, header=header_diperlukan)
# =========================================================
# 1. PERHITUNGAN KELAYAKAN KUOTA -- jalankan ini DULU sebelum polling sungguhan
# =========================================================
def hitung_rencana_polling(n_edges, quota_per_day=QUOTA_PER_DAY,
                            interval_minutes=INTERVAL_MINUTES,
                            safety_margin=QUOTA_SAFETY_MARGIN):
    """
    Menghitung berapa grup rotasi dibutuhkan, dan interval efektif per-edge
    (seberapa sering satu edge yang SAMA benar-benar terpoll ulang).
    """
    quota_efektif = int(quota_per_day * safety_margin)
    polls_per_day = (24 * 60) / interval_minutes
    max_titik_per_poll = math.floor(quota_efektif / polls_per_day)

    if max_titik_per_poll >= n_edges:
        n_groups = 1
        titik_per_poll = n_edges
        interval_efektif_per_edge = interval_minutes
    else:
        n_groups = math.ceil(n_edges / max_titik_per_poll)
        titik_per_poll = max_titik_per_poll
        interval_efektif_per_edge = interval_minutes * n_groups

    request_terpakai_per_hari = titik_per_poll * polls_per_day

    hasil = {
        'n_edges': n_edges,
        'quota_per_day': quota_per_day,
        'quota_efektif_dgn_margin': quota_efektif,
        'interval_target_menit': interval_minutes,
        'polls_per_day': polls_per_day,
        'max_titik_per_poll': max_titik_per_poll,
        'n_groups': n_groups,
        'titik_per_poll_aktual': titik_per_poll,
        'interval_efektif_per_edge_menit': interval_efektif_per_edge,
        'request_terpakai_per_hari': request_terpakai_per_hari,
    }
    return hasil


def cetak_tabel_skenario(n_edges):
    """
    Bandingkan beberapa pilihan interval supaya bisa lihat trade-off
    sebelum memutuskan INTERVAL_MINUTES final.
    """
    print(f"{'='*75}")
    print(f"SKENARIO POLLING untuk {n_edges} edge (kuota {QUOTA_PER_DAY}/hari, "
          f"margin {int(QUOTA_SAFETY_MARGIN*100)}%)")
    print(f"{'='*75}")
    header = (f"{'Interval target':<18}{'n_groups':<10}{'Titik/poll':<12}"
              f"{'Interval efektif/edge':<24}{'Request/hari':<14}")
    print(header)
    print("-" * 75)

    for interval in [5, 10, 15, 20, 30, 45, 60]:
        r = hitung_rencana_polling(n_edges, interval_minutes=interval)
        interval_efektif_jam = r['interval_efektif_per_edge_menit'] / 60
        interval_efektif_str = (f"{r['interval_efektif_per_edge_menit']:.0f} mnt "
                                 f"({interval_efektif_jam:.1f} jam)")
        print(f"{str(interval)+' menit':<18}{r['n_groups']:<10}"
              f"{r['titik_per_poll_aktual']:<12}"
              f"{interval_efektif_str:<24}"
              f"{r['request_terpakai_per_hari']:<14.0f}")

    print(f"\nCatatan interpretasi:")
    print(f"  - 'Interval target' = seberapa sering SCRIPT dijalankan (oleh scheduler)")
    print(f"  - 'Interval efektif/edge' = seberapa sering SATU EDGE YANG SAMA benar-benar")
    print(f"    terpoll ulang (kalau n_groups > 1, edge dipoll bergantian per grup)")
    print(f"  - Kalau n_groups > 1, resolusi temporal per-edge otomatis lebih kasar dari")
    print(f"    interval target -- ini trade-off wajib kalau n_edges > kapasitas kuota.")


# =========================================================
# 2. FUNGSI POLLING SATU TITIK (dengan retry & backoff)
# =========================================================
def haversine_m(lat1, lon1, lat2, lon2):
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def poll_satu_titik(session, lat, lon, api_key, style=STYLE, zoom=ZOOM, unit=UNIT,
                     max_retries=MAX_RETRIES, timeout=10):
    url = f"https://api.tomtom.com/traffic/services/4/flowSegmentData/{style}/{zoom}/json"
    params = {
        "key": api_key,
        "point": f"{lat},{lon}",
        "unit": unit,
    }

    for percobaan in range(max_retries + 1):
        try:
            resp = session.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            if percobaan == max_retries:
                return {"error": f"request_exception: {e}"}
            time.sleep(RETRY_BACKOFF_BASE * (2 ** percobaan))
            continue

        if resp.status_code == 200:
            data = resp.json().get("flowSegmentData", {})
            matched_lat = data.get("coordinates", {}).get("coordinate", [{}])[0].get("latitude")
            matched_lon = data.get("coordinates", {}).get("coordinate", [{}])[0].get("longitude")

            jarak = None
            valid = None
            if matched_lat is not None and matched_lon is not None:
                jarak = haversine_m(lat, lon, matched_lat, matched_lon)
                valid = jarak <= MAX_MATCH_DISTANCE_M

            return {
                "frc": data.get("frc"),
                "currentSpeed": data.get("currentSpeed"),
                "freeFlowSpeed": data.get("freeFlowSpeed"),
                "currentTravelTime": data.get("currentTravelTime"),
                "freeFlowTravelTime": data.get("freeFlowTravelTime"),
                "confidence": data.get("confidence"),
                "roadClosure": data.get("roadClosure"),
                "matched_lat": matched_lat,
                "matched_lon": matched_lon,
                "match_distance_m": jarak,
                "valid_match": valid,
                "error": None,
            }

        if resp.status_code == 429:
            # Too many requests -- backoff eksponensial lalu coba lagi
            if percobaan == max_retries:
                return {"error": "429_max_retries_exceeded"}
            time.sleep(RETRY_BACKOFF_BASE * (2 ** percobaan))
            continue

        # Error lain (400/403/500/503/dst) -- tidak perlu retry, catat saja
        return {"error": f"http_{resp.status_code}: {resp.text[:200]}"}

    return {"error": "unknown_failure"}


# =========================================================
# 3. STATE MANAGEMENT -- lacak grup mana yang giliran dipoll
# =========================================================
def load_state(state_path, n_groups):
    if Path(state_path).exists():
        with open(state_path) as f:
            state = json.load(f)
        # Kalau n_groups berubah (mis. jumlah edge berubah), reset grup ke 0
        if state.get("n_groups") != n_groups:
            state = {"current_group": 0, "poll_count": 0, "n_groups": n_groups}
    else:
        state = {"current_group": 0, "poll_count": 0, "n_groups": n_groups}
    return state


def save_state(state_path, state):
    with open(state_path, "w") as f:
        json.dump(state, f, indent=2)


# =========================================================
# 4. SATU SIKLUS POLLING PENUH (dipanggil sekali per invocation scheduler)
# =========================================================
def jalankan_satu_siklus_polling():
    if not API_KEY:
        raise RuntimeError(
            "TOMTOM_API_KEY belum diset. Set environment variable TOMTOM_API_KEY "
            "sebelum menjalankan script ini (JANGAN hardcode API key di kode)."
        )

    Path(OUTPUT_DIR).mkdir(parents=True, exist_ok=True)

    titik_df = pd.read_csv(POLLING_POINTS_CSV)
    n_edges = len(titik_df)

    rencana = hitung_rencana_polling(n_edges)
    n_groups = rencana['n_groups']
    titik_per_poll = rencana['titik_per_poll_aktual']

    state = load_state(STATE_FILE, n_groups)
    grup_sekarang = state['current_group']

    # Bagi titik_df jadi n_groups bagian, ambil bagian yang giliran sekarang
    titik_df = titik_df.reset_index(drop=True)
    titik_df['grup'] = titik_df.index % n_groups
    subset = titik_df[titik_df['grup'] == grup_sekarang].copy()

    print(f"{'='*60}")
    print(f"SIKLUS POLLING -- {datetime.now(timezone.utc).isoformat()}")
    print(f"{'='*60}")
    print(f"Total edge      : {n_edges}")
    print(f"Total grup      : {n_groups}")
    print(f"Grup sekarang   : {grup_sekarang} ({len(subset)} titik)")
    print(f"Poll ke-        : {state['poll_count'] + 1}")

    hasil_rows = []
    session = requests.Session()
    delay_antar_request = 1.0 / QPS_LIMIT
    waktu_mulai = time.time()

    berhasil, gagal = 0, 0
    for i, row in enumerate(subset.itertuples(index=False)):
        hasil = poll_satu_titik(session, row.lat, row.lon, API_KEY)

        timestamp = datetime.now(timezone.utc).isoformat()
        rekaman = {
            "timestamp_utc": timestamp,
            "edge_id": row.edge_id,
            "requested_lat": row.lat,
            "requested_lon": row.lon,
            "highway_tag": getattr(row, "highway_tag", "unknown"),
            **hasil,
        }
        hasil_rows.append(rekaman)

        if hasil.get("error") is None:
            berhasil += 1
        else:
            gagal += 1

        # Rate limiting antar-request
        if i < len(subset) - 1:
            time.sleep(delay_antar_request)

    durasi = time.time() - waktu_mulai
    print(f"\nSelesai polling {len(subset)} titik dalam {durasi:.1f} detik")
    print(f"  Berhasil : {berhasil}")
    print(f"  Gagal    : {gagal}")

    # --- Simpan hasil ke file CSV harian, append mode ---
    tanggal_hari_ini = datetime.now(timezone.utc).strftime("%Y%m%d")
    output_path = Path(OUTPUT_DIR) / f"poll_{tanggal_hari_ini}.csv"

    hasil_df = pd.DataFrame(hasil_rows)
    simpan_hasil_polling(hasil_df, output_path)

    print(f"\nTersimpan (append) ke: {output_path}")

    # --- Update state untuk siklus berikutnya ---
    state['current_group'] = (grup_sekarang + 1) % n_groups
    state['poll_count'] += 1
    save_state(STATE_FILE, state)

    print(f"State diupdate -> grup berikutnya: {state['current_group']}, "
          f"total poll_count: {state['poll_count']}")

    if gagal > 0:
        print(f"\nPERINGATAN: {gagal} titik gagal dipoll. Cek kolom 'error' di "
              f"{output_path} untuk detail penyebab (429 = rate limit/kuota habis, "
              f"403 = API key bermasalah, dst).")


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--rencana":
        # Mode cek rencana kuota saja, tanpa polling sungguhan.
        # Jalankan: python poll_tomtom_flow.py --rencana
        titik_df = pd.read_csv(POLLING_POINTS_CSV)
        cetak_tabel_skenario(len(titik_df))
    else:
        # Mode polling sungguhan -- dipanggil oleh scheduler setiap interval.
        jalankan_satu_siklus_polling()
