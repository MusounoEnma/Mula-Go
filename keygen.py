# -*- coding: utf-8 -*-
"""
keygen.py — MULA GO Admin Keygen Tool
======================================
Jalankan script ini di komputer ANDA (sisi admin/provider) untuk
menghasilkan kode aktivasi 6-digit bagi user.

CARA PENGGUNAAN:
  python keygen.py
  → masukkan Hardware ID yang diberikan user dari layar aktivasi app

DEPENDENSI:
  pip install pyotp

KEAMANAN:
  - Simpan file ini di tempat aman, JANGAN distribusikan ke user.
  - Kode yang dihasilkan hanya valid selama 30 detik (TOTP window).
  - Algoritma identik dengan yang ada di security.py dalam aplikasi.
"""
import io

import hashlib
import hmac
import base64
import time
import sys

# Force UTF-8 output on Windows so box characters print correctly
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

try:
    import pyotp
except ImportError:
    print("[ERROR] pyotp tidak terinstall. Jalankan: pip install pyotp")
    sys.exit(1)

# ══════════════════════════════════════════════════════════════════════════════
# KONFIGURASI — harus IDENTIK dengan security.py di dalam app
# ══════════════════════════════════════════════════════════════════════════════
_SALT        = "MULA00990"          # Salt rahasia (JANGAN ubah)
_DOMAIN_TAG  = b"MULAGO_V1_ACTIVATION"
_PBKDF2_ITER = 150_000              # Jumlah iterasi PBKDF2
_INTERVAL    = 30                   # Detik per TOTP window


# ══════════════════════════════════════════════════════════════════════════════
# ALGORITMA DERIVASI (harus identik dengan security.py)
# ══════════════════════════════════════════════════════════════════════════════

def _normalize(hwid: str) -> str:
    """Normalisasi: uppercase, hapus dash dan spasi."""
    return hwid.upper().replace("-", "").replace(" ", "").strip()


def _derive_secret(hwid: str) -> str:
    """
    Multi-layer key derivation:
      1. Normalisasi HWID
      2. XOR Pepper (HWID bytes XOR SALT bytes, cyclic)
      3. PBKDF2-HMAC-SHA512 (150.000 iterasi)
      4. HMAC-SHA256 final mix dengan domain tag
      5. Base32 encode 20 byte pertama
    """
    norm      = _normalize(hwid)
    h_bytes   = norm.encode("utf-8")
    s_bytes   = _SALT.encode("utf-8")

    # Layer 2: XOR Pepper
    pepper = bytes(h_bytes[i] ^ s_bytes[i % len(s_bytes)] for i in range(len(h_bytes)))

    # Layer 3: PBKDF2
    password = (norm + _SALT).encode("utf-8")
    derived  = hashlib.pbkdf2_hmac("sha512", password, pepper, _PBKDF2_ITER, dklen=32)

    # Layer 4: HMAC final mix
    final  = hmac.new(derived, _DOMAIN_TAG + h_bytes, hashlib.sha256).digest()

    # Layer 5: Base32
    return base64.b32encode(final[:20]).decode("utf-8")


def generate_codes(hwid: str) -> dict:
    """
    Hasilkan kode aktivasi untuk satu Hardware ID.

    Returns dict berisi:
      - secret       : TOTP secret (internal, jangan share)
      - current_code : kode 6-digit yang valid SEKARANG
      - next_code    : kode 6-digit window BERIKUTNYA
      - seconds_left : sisa detik sebelum current_code kadaluarsa
    """
    secret     = _derive_secret(hwid)
    totp       = pyotp.TOTP(secret, interval=_INTERVAL)
    now        = time.time()
    seconds_left = _INTERVAL - (int(now) % _INTERVAL)

    return {
        "secret"       : secret,
        "current_code" : totp.now(),
        "next_code"    : totp.at(now + _INTERVAL),
        "seconds_left" : seconds_left,
    }


# ══════════════════════════════════════════════════════════════════════════════
# TAMPILAN CLI
# ══════════════════════════════════════════════════════════════════════════════

def print_banner():
    print()
    print("  +================================================+")
    print("  |        MULA GO  --  Admin Keygen Tool         |")
    print("  |   Activation Code Generator   |   v1.0.0     |")
    print("  +================================================+")
    print()

def print_result(hwid: str, data: dict):
    bar = "-" * 50
    print(f"\n  {bar}")
    print(f"  Hardware ID    : {hwid}")
    print(f"  {bar}")
    print(f"  >> KODE AKTIVASI  : [ {data['current_code']} ] <<")
    print(f"     Kode Berikutnya: [ {data['next_code']} ]  (valid dalam {data['seconds_left']}s)")
    print(f"     Sisa Waktu      : {data['seconds_left']} detik")
    print(f"  {bar}")
    print()
    print("  [!] Berikan HANYA kode di atas kepada user.")
    print("  [!] Kode expired setelah window 30 detik.")
    print()


def interactive_mode():
    """Mode interaktif: input HWID → tampilkan kode."""
    print_banner()
    print("  Masukkan Hardware ID dari layar aktivasi user.")
    print("  Ketik 'q' untuk keluar.\n")

    while True:
        try:
            hwid_raw = input("  Hardware ID → ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\n  Keluar dari Keygen. Sampai jumpa!")
            break

        if hwid_raw.lower() in ("q", "quit", "exit"):
            print("  Keluar dari Keygen. Sampai jumpa!")
            break

        if not hwid_raw:
            print("  [!] Hardware ID tidak boleh kosong.\n")
            continue

        print(f"\n  Menghitung kode untuk: {hwid_raw}")
        print(f"  (PBKDF2 {_PBKDF2_ITER:,} iterasi — mohon tunggu ~1 detik...)")

        try:
            data = generate_codes(hwid_raw)
            print_result(hwid_raw, data)
        except Exception as e:
            print(f"\n  [ERROR] Gagal generate kode: {e}\n")


# ══════════════════════════════════════════════════════════════════════════════
# CONTOH PENGGUNAAN (jalankan langsung)
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Jika ada argumen CLI, gunakan itu sebagai HWID langsung
    if len(sys.argv) > 1:
        hwid_arg = " ".join(sys.argv[1:]).strip()
        print_banner()
        print(f"  Menghitung untuk HWID: {hwid_arg}")
        print(f"  (PBKDF2 {_PBKDF2_ITER:,} iterasi — mohon tunggu...)")
        data = generate_codes(hwid_arg)
        print_result(hwid_arg, data)
    else:
        # Mode interaktif
        interactive_mode()
