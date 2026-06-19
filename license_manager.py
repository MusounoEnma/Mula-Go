"""
license_manager.py — MULA GO License Manager

Handles reading and writing the encrypted license.dat file stored in
%APPDATA%\\MulaGo\\license.dat.

Encryption: Fernet (AES-128-CBC + HMAC-SHA256) from the `cryptography` package.
The encryption key is derived from the machine's Hardware ID so the license
is device-bound and cannot simply be copied to another machine.
"""

import os
import json
import hashlib
import base64
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

# ─── Paths ───────────────────────────────────────────────────────────────────
_APP_DATA_DIR = Path(os.environ.get("APPDATA", "~")).expanduser() / "MulaGo"
_LICENSE_FILE = _APP_DATA_DIR / "license.dat"

# ─── App version stamped into the license ────────────────────────────────────
APP_VERSION = "1.0.0"


def _derive_fernet_key(hwid: str) -> bytes:
    """Derive a valid 32-byte Fernet key from the Hardware ID."""
    raw = f"MULAGOLICENSEKEY_{hwid}".encode("utf-8")
    digest = hashlib.sha256(raw).digest()          # 32 bytes
    return base64.urlsafe_b64encode(digest)        # Fernet needs urlsafe-b64


def save_license(hwid: str) -> bool:
    """
    Encrypt and save the license payload to disk.

    Returns True on success, False on failure.
    """
    try:
        _APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = _derive_fernet_key(hwid)
        fernet = Fernet(key)
        payload = json.dumps({
            "hwid": hwid,
            "activated_at": datetime.now(timezone.utc).isoformat(),
            "version": APP_VERSION,
        }).encode("utf-8")
        encrypted = fernet.encrypt(payload)
        _LICENSE_FILE.write_bytes(encrypted)
        return True
    except Exception as e:
        print(f"[LicenseManager] Failed to save license: {e}")
        return False


def check_license(hwid: str) -> dict:
    """
    Attempt to read and decrypt the license file for the given HW ID.

    Returns:
        {"valid": True, "data": {...}}  on success
        {"valid": False, "reason": "..."}  on failure
    """
    if not _LICENSE_FILE.exists():
        return {"valid": False, "reason": "no_license_file"}

    try:
        key = _derive_fernet_key(hwid)
        fernet = Fernet(key)
        encrypted = _LICENSE_FILE.read_bytes()
        payload = json.loads(fernet.decrypt(encrypted).decode("utf-8"))

        # Basic sanity: does the stored HWID match this machine?
        if payload.get("hwid") != hwid:
            return {"valid": False, "reason": "hwid_mismatch"}

        return {"valid": True, "data": payload}

    except InvalidToken:
        return {"valid": False, "reason": "invalid_token"}
    except Exception as e:
        return {"valid": False, "reason": str(e)}


def revoke_license() -> bool:
    """Delete the license file (e.g., for reset/re-activation)."""
    try:
        if _LICENSE_FILE.exists():
            _LICENSE_FILE.unlink()
        return True
    except Exception as e:
        print(f"[LicenseManager] Failed to revoke license: {e}")
        return False
