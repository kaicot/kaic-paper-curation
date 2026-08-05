"""Stdlib-only integrity boundary for the explicit local SPECTER2 cache."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from pipeline.schemas.codex_schema import JsonObject


SCHEMA = "specter2-local-cache-v1"
MANIFEST_NAME = "specter2-provenance.json"
PROFILE = "specter2-proximity-cls-v1"


class Specter2CacheUnavailable(RuntimeError):
    """The local cache is missing, malformed, incomplete, or tampered."""


def _canonical(value: JsonObject) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def manifest_sha256(value: JsonObject) -> str:
    payload: JsonObject = {
        key: item for key, item in value.items() if key != "manifest_sha256"
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def verify_cache(cache_root: Path, *, verify_files: bool = True) -> JsonObject:
    """Validate canonical provenance and optionally every regular cache file."""
    manifest_path = cache_root / MANIFEST_NAME
    try:
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise Specter2CacheUnavailable("specter2 manifest is missing")
        raw = manifest_path.read_bytes()
        decoded = cast(object, json.loads(raw))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Specter2CacheUnavailable("specter2 manifest is unreadable") from error
    if not isinstance(decoded, dict):
        raise Specter2CacheUnavailable("specter2 manifest must be an object")
    value = cast(JsonObject, decoded)
    if raw != _canonical(value) + b"\n":
        raise Specter2CacheUnavailable("specter2 manifest is not canonical")
    if value.get("schema") != SCHEMA or value.get("schema_version") != 1:
        raise Specter2CacheUnavailable("specter2 manifest schema mismatch")
    if value.get("profile") != PROFILE:
        raise Specter2CacheUnavailable("specter2 profile mismatch")
    expected_manifest = value.get("manifest_sha256")
    if not isinstance(expected_manifest, str) or expected_manifest != manifest_sha256(value):
        raise Specter2CacheUnavailable("specter2 manifest checksum mismatch")
    files = value.get("files")
    if not isinstance(files, list) or not files:
        raise Specter2CacheUnavailable("specter2 manifest has no files")
    if verify_files:
        seen: set[str] = set()
        model_roots = (cache_root / "base", cache_root / "adapters" / "proximity")
        if any(
            path.is_symlink()
            for root in model_roots
            if root.exists()
            for path in root.rglob("*")
        ):
            raise Specter2CacheUnavailable("specter2 cache contains a symlink")
        for row in files:
            if not isinstance(row, dict):
                raise Specter2CacheUnavailable("specter2 file row is invalid")
            typed_row = cast(JsonObject, row)
            relative = typed_row.get("path")
            size = typed_row.get("size")
            digest = typed_row.get("sha256")
            if (
                not isinstance(relative, str)
                or not isinstance(size, int)
                or not isinstance(digest, str)
                or relative in seen
            ):
                raise Specter2CacheUnavailable("specter2 file row fields are invalid")
            if not (
                relative.startswith("base/")
                or relative.startswith("adapters/proximity/")
            ):
                raise Specter2CacheUnavailable("specter2 file escaped the model roots")
            path = (cache_root / relative).resolve()
            try:
                _ = path.relative_to(cache_root.resolve())
            except ValueError as error:
                raise Specter2CacheUnavailable("specter2 file escaped the cache") from error
            if (
                not path.is_file()
                or path.is_symlink()
                or path.stat().st_size != size
                or _sha256(path) != digest
            ):
                raise Specter2CacheUnavailable(f"specter2 file checksum mismatch: {relative}")
            seen.add(relative)
        actual = {
            path.relative_to(cache_root).as_posix()
            for root in model_roots
            if root.exists()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        if seen != actual:
            raise Specter2CacheUnavailable("specter2 manifest file set mismatch")
    return value


def embedding_identity(cache_root: Path, *, verify_files: bool = True) -> str:
    manifest = verify_cache(cache_root, verify_files=verify_files)
    digest = manifest.get("manifest_sha256")
    if not isinstance(digest, str):
        raise Specter2CacheUnavailable("specter2 manifest checksum is missing")
    return f"{PROFILE}:{digest}"
