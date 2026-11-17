"""Windows DPAPI (CryptProtectData) through ctypes, for vault values too big for Credential Manager.

DPAPI binds the encrypted bytes to the current Windows user: another user or a stolen disk cannot read
them. Stdlib only (no pywin32). Used by creds.py for files in %LOCALAPPDATA%\\TwinAgent\\vault\\.
"""
from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

_ENTROPY = b"twinagent-vault-v1"   # extra salt so a generic DPAPI dump tool needs to know it too


class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _blob(data: bytes) -> tuple[_Blob, ctypes.Array]:
    buf = ctypes.create_string_buffer(data, len(data))
    return _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), buf


def _require_windows() -> None:
    if sys.platform != "win32":
        raise OSError("DPAPI vault files need Windows")


def protect(data: bytes) -> bytes:
    _require_windows()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _keep1 = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    CRYPTPROTECT_UI_FORBIDDEN = 0x1
    if not crypt32.CryptProtectData(ctypes.byref(src), None, ctypes.byref(ent), None, None,
                                    CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def unprotect(data: bytes) -> bytes:
    _require_windows()
    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    src, _keep1 = _blob(data)
    ent, _keep2 = _blob(_ENTROPY)
    out = _Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(src), None, ctypes.byref(ent), None, None, 0x1,
                                      ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed (wrong Windows user, or the file was changed)")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def write_file(path: Path, data: bytes) -> None:
    """Encrypt and write atomically (temp file, then replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(protect(data))
    os.replace(tmp, path)


def read_file(path: Path) -> bytes:
    return unprotect(path.read_bytes())
