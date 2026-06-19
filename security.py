"""
security.py — MULA GO Security Module (The Guard)

Activation Algorithm: Multi-Layer Key Derivation
=================================================

Alur derivasi secret (SAMA persis di keygen admin & di app):

  1. NORMALIZE  : Uppercase HWID, hapus dash/spasi
  2. XOR PEPPER : Setiap byte HWID di-XOR dengan byte SALT (looping)
                  → menghasilkan 'pepper' yang unik per device
  3. PBKDF2     : PBKDF2-HMAC-SHA512
                    password = HWID_normalized + SALT
                    salt     = pepper (hasil XOR)
                    iter     = 150.000 rounds (brute-force costly)
                    dklen    = 32 bytes
  4. HMAC-MIX   : HMAC-SHA256(key=derived_bytes, msg=DOMAIN_TAG + HWID_bytes)
                    domain tag = b"MULAGO_V1_ACTIVATION"
  5. BASE32     : 20 byte pertama → base32 → TOTP secret
  6. TOTP       : pyotp.TOTP(secret, interval=30) → 6-digit code

Mengapa sulit dibajak:
  - PBKDF2 150k iterasi = brute-force mahal secara komputasi
  - Pepper di-XOR dari HWID sendiri → salt PBKDF2 beda tiap device
  - HMAC layer ke-2 dengan domain separator mencegah length-extension attack
  - Output TOTP berubah tiap 30 detik → replay attack tidak berlaku
"""

import hashlib
import hmac
import base64
import subprocess
import socket
import pyotp

# ─── Konstanta ────────────────────────────────────────────────────────────────
_SALT            = "MULA00990"
_DOMAIN_TAG      = b"MULAGO_V1_ACTIVATION"
_PBKDF2_ITER     = 150_000
_TOTP_INTERVAL   = 30   # detik per window
_TOTP_WINDOW     = 1    # toleransi 1 window (clock drift ~30 detik)


# ─── Hardware ID ──────────────────────────────────────────────────────────────

def get_hardware_id() -> str:
    """
    Ambil Hardware ID unik dari sistem Windows (UUID via WMI).
    Fallback ke motherboard serial, lalu hostname hash.
    """
    # Prioritas 1: System UUID (paling unik)
    try:
        out = subprocess.check_output(
            ["wmic", "csproduct", "get", "UUID"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
        for line in out.strip().splitlines():
            line = line.strip()
            if line and line.upper() != "UUID" and len(line) > 8:
                return line.upper()
    except Exception:
        pass

    # Prioritas 2: Motherboard Serial
    try:
        out = subprocess.check_output(
            ["wmic", "baseboard", "get", "SerialNumber"],
            stderr=subprocess.DEVNULL, timeout=5
        ).decode("utf-8", errors="ignore")
        for line in out.strip().splitlines():
            line = line.strip()
            if line and line.upper() not in ("SERIALNUMBER", "") and len(line) > 4:
                return f"MB-{line.upper()}"
    except Exception:
        pass

    # Prioritas 3: Hash dari hostname (last resort)
    digest = hashlib.sha256(socket.gethostname().encode()).hexdigest()[:16].upper()
    return f"HOST-{digest}"


# ─── Core Algorithm ───────────────────────────────────────────────────────────

def _normalize_hwid(hwid: str) -> str:
    """Standarisasi: uppercase, hapus dash dan spasi."""
    return hwid.upper().replace("-", "").replace(" ", "").strip()


def _derive_totp_secret(hwid: str) -> str:
    """
    Turunkan TOTP secret dari Hardware ID menggunakan algoritma multi-layer.

    Proses identik digunakan di sisi app (verify) dan sisi admin (keygen).
    """
    # ── Layer 1: Normalisasi ──────────────────────────────────────────────────
    norm_hwid  = _normalize_hwid(hwid)
    hwid_bytes = norm_hwid.encode("utf-8")
    salt_bytes = _SALT.encode("utf-8")

    # ── Layer 2: XOR Pepper ───────────────────────────────────────────────────
    # Setiap byte HWID di-XOR dengan byte SALT yang berputar (cyclic)
    # Hasil = pepper yang benar-benar unik per device
    pepper = bytes(
        hwid_bytes[i] ^ salt_bytes[i % len(salt_bytes)]
        for i in range(len(hwid_bytes))
    )

    # ── Layer 3: PBKDF2-HMAC-SHA512 ──────────────────────────────────────────
    # password = HWID_normalized + SALT  (input utama)
    # salt     = pepper (XOR result, unik per device)
    # iter     = 150.000 (mahal untuk brute-force)
    password = (norm_hwid + _SALT).encode("utf-8")
    derived = hashlib.pbkdf2_hmac(
        hash_name = "sha512",
        password  = password,
        salt      = pepper,
        iterations= _PBKDF2_ITER,
        dklen     = 32,  # 256-bit output
    )

    # ── Layer 4: HMAC-SHA256 Final Mix ────────────────────────────────────────
    # key = derived bytes dari PBKDF2
    # msg = DOMAIN_TAG + HWID_bytes (domain separation mencegah cross-context attack)
    final = hmac.new(
        key       = derived,
        msg       = _DOMAIN_TAG + hwid_bytes,
        digestmod = hashlib.sha256,
    ).digest()

    # ── Layer 5: Base32 Encoding ─────────────────────────────────────────────
    # Ambil 20 byte pertama (160-bit) → base32 untuk PyOTP
    return base64.b32encode(final[:20]).decode("utf-8")


# ─── TOTP Verification ────────────────────────────────────────────────────────

def verify_totp(hwid: str, code: str) -> bool:
    """
    Verifikasi kode 6-digit terhadap Hardware ID.

    Args:
        hwid : Hardware ID dari device user.
        code : 6-digit kode yang diinput user.

    Returns:
        True jika valid (termasuk toleransi clock drift ±1 window).
    """
    if not code or len(str(code)) != 6 or not str(code).isdigit():
        return False
    secret = _derive_totp_secret(hwid)
    totp   = pyotp.TOTP(secret, interval=_TOTP_INTERVAL)
    return totp.verify(str(code), valid_window=_TOTP_WINDOW)


# ─── Admin Only — DEBUG / KEYGEN (jangan expose di production .exe) ───────────

def generate_current_code(hwid: str) -> str:
    """Generate kode aktif saat ini. Gunakan di keygen.py sisi admin saja."""
    secret = _derive_totp_secret(hwid)
    return pyotp.TOTP(secret, interval=_TOTP_INTERVAL).now()
