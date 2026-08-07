"""Todo 23 — safe evidence path handling.

Ports the approval-bound guard's rooted-path / Win32 no-follow identity /
Cloud-tag / transcript behavior into a reusable pipeline library so the
release-evidence runners can bind every artifact beneath a held, identity
verified evidence root without reparse escape or overwrite.
"""
from __future__ import annotations

import ctypes
import ctypes.wintypes
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
OPEN_EXISTING = 3
INVALID_HANDLE_VALUE = ctypes.wintypes.HANDLE(-1).value
FILE_ID_INFO_CLASS = 0x12
FILE_ATTRIBUTE_TAG_INFO_CLASS = 9
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Microsoft Cloud placeholder tags (OneDrive): the only allowed reparse tags.
CLOUD_TAGS = {
    0x9000001A, 0x9000101A, 0x9000201A, 0x9000301A, 0x9000401A,
    0x9000501A, 0x9000601A, 0x9000701A, 0x9000801A, 0x9000901A,
    0x9000A01A, 0x9000B01A, 0x9000C01A, 0x9000D01A, 0x9000E01A,
    0x9000F01A,
}


class SafeEvidencePathError(RuntimeError):
    """The evidence path contract was violated."""


class _FileIdInfo(ctypes.Structure):
    _fields_ = [
        ("volume_serial_number", ctypes.c_ulonglong),
        ("file_id", ctypes.c_ubyte * 16),
    ]


class _FileAttributeTagInfo(ctypes.Structure):
    _fields_ = [
        ("file_attributes", ctypes.c_ulong),
        ("reparse_tag", ctypes.c_ulong),
    ]


@dataclass(frozen=True)
class PathIdentity:
    """Win32 identity of one path, captured through a no-follow handle."""

    final_path: str
    volume_serial_number: str
    file_id: str
    attributes: int
    reparse_tag: str | None


def lexical_rooted(root: Path, candidate: Path) -> Path:
    """Resolve candidate beneath root lexically; reject escape."""
    absolute_root = os.path.abspath(root)
    absolute = os.path.abspath(candidate)
    if os.path.commonpath((absolute_root, absolute)) != absolute_root:
        raise SafeEvidencePathError(f"path-escape: {candidate}")
    return Path(absolute)


def open_metadata_handle(path: Path) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        ctypes.wintypes.LPCWSTR, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
        ctypes.c_void_p, ctypes.wintypes.DWORD, ctypes.wintypes.DWORD,
        ctypes.wintypes.HANDLE,
    ]
    create_file.restype = ctypes.wintypes.HANDLE
    handle = create_file(
        str(path), 0, 0x00000001, None, OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS | FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle == INVALID_HANDLE_VALUE:
        raise SafeEvidencePathError(f"win32-open-metadata: {path}:{ctypes.get_last_error()}")
    return int(handle)


def close_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if not kernel32.CloseHandle(ctypes.wintypes.HANDLE(handle)):
        raise SafeEvidencePathError(f"win32-close: {ctypes.get_last_error()}")


def identity_from_handle(handle: int) -> PathIdentity:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    identity = _FileIdInfo()
    tag = _FileAttributeTagInfo()
    if not kernel32.GetFileInformationByHandleEx(
        ctypes.wintypes.HANDLE(handle), FILE_ID_INFO_CLASS,
        ctypes.byref(identity), ctypes.sizeof(identity),
    ):
        raise SafeEvidencePathError(f"win32-file-id: {ctypes.get_last_error()}")
    if not kernel32.GetFileInformationByHandleEx(
        ctypes.wintypes.HANDLE(handle), FILE_ATTRIBUTE_TAG_INFO_CLASS,
        ctypes.byref(tag), ctypes.sizeof(tag),
    ):
        raise SafeEvidencePathError(f"win32-file-tag: {ctypes.get_last_error()}")
    buffer = ctypes.create_unicode_buffer(32768)
    length = kernel32.GetFinalPathNameByHandleW(
        ctypes.wintypes.HANDLE(handle), buffer, len(buffer), 0,
    )
    if length == 0 or length >= len(buffer):
        raise SafeEvidencePathError(f"win32-final-path: {ctypes.get_last_error()}")
    final_path = buffer.value
    if final_path.startswith("\\\\?\\"):
        final_path = final_path[4:]
    file_id = "0x" + bytes(identity.file_id)[::-1].hex()
    reparse_tag = f"0x{tag.reparse_tag:08X}" if tag.file_attributes & FILE_ATTRIBUTE_REPARSE_POINT else None
    return PathIdentity(
        final_path,
        f"0x{identity.volume_serial_number & 0xFFFFFFFF:08X}",
        file_id,
        int(tag.file_attributes),
        reparse_tag,
    )


def path_identity(path: Path) -> PathIdentity:
    handle = open_metadata_handle(path)
    try:
        return identity_from_handle(handle)
    finally:
        close_handle(handle)


def assert_safe_identity(path: Path, identity: PathIdentity) -> None:
    """Reject junction/symlink/mount and any non-Cloud reparse tag."""
    if identity.reparse_tag is not None:
        tag_value = int(identity.reparse_tag, 16)
        if tag_value not in CLOUD_TAGS:
            raise SafeEvidencePathError(f"unsafe-reparse: {path}:{identity.reparse_tag}")
        if identity.attributes & FILE_ATTRIBUTE_REPARSE_POINT == 0:
            raise SafeEvidencePathError(f"reparse-attribute-mismatch: {path}")


def read_held_bytes(path: Path, expected: PathIdentity) -> bytes:
    identity = path_identity(path)
    if identity != expected:
        raise SafeEvidencePathError(f"identity-drift: {path}")
    with path.open("rb") as stream:
        return stream.read()


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_new(path: Path, payload: bytes) -> None:
    """Create-new write-through publish; never overwrite."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def canonical_json_bytes(value: object) -> bytes:
    import json
    import unicodedata

    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return unicodedata.normalize("NFC", text).encode("utf-8")
