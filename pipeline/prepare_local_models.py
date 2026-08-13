"""Explicit, auditable preparation of optional local model caches."""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, cast


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.lib.specter2_cache import (  # noqa: E402
    PROFILE,
    SCHEMA,
    Specter2CacheUnavailable,
    manifest_sha256,
    verify_cache,
)
from pipeline.schemas.codex_schema import JsonObject, JsonValue  # noqa: E402

DEFAULT_CACHE_ROOT = ROOT / ".cache"
BASE_REPOSITORY = "allenai/specter2_base"
ADAPTER_REPOSITORY = "allenai/specter2"

class SnapshotDownload(Protocol):
    def __call__(self, *, repo_id: str, revision: str, cache_dir: str) -> str: ...


class ModelPreparationError(RuntimeError):
    """An explicit model preparation could not publish a complete cache."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1_048_576), b""):
            digest.update(block)
    return digest.hexdigest()


def _file_manifest(root: Path) -> list[JsonObject]:
    rows: list[JsonObject] = []
    model_roots = (root / "base", root / "adapters" / "proximity")
    for model_root in model_roots:
        for path in sorted(item for item in model_root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            rows.append(
                {
                    "path": relative,
                    "sha256": _sha256(path),
                    "size": path.stat().st_size,
                }
            )
    return rows


def _resolved_revision(snapshot: Path) -> str:
    candidate = snapshot.name
    if len(candidate) >= 7 and all(character in "0123456789abcdef" for character in candidate.lower()):
        return candidate
    return "unresolved"


def _atomic_json(path: Path, value: JsonObject) -> None:
    encoded = (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    )
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            _ = handle.write(encoded)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def prepare_specter2(
    cache_root: Path = DEFAULT_CACHE_ROOT,
    *,
    snapshot_download: SnapshotDownload | None = None,
    force: bool = False,
) -> JsonObject:
    """Download SPECTER2 only from this explicit command and attest every file."""
    base_target = cache_root / "base"
    proximity_target = cache_root / "adapters" / "proximity"
    provenance_path = cache_root / "specter2-provenance.json"
    if not force and (base_target.exists() or proximity_target.exists() or provenance_path.exists()):
        try:
            return verify_cache(cache_root)
        except Specter2CacheUnavailable as error:
            raise ModelPreparationError(
                "specter2-cache-invalid: existing cache was preserved"
            ) from error
    if snapshot_download is None:
        try:
            snapshot_download = cast(
                SnapshotDownload,
                cast(
                    object,
                    getattr(
                        importlib.import_module("huggingface_hub"),
                        "snapshot_download",
                    ),
                ),
            )
        except ImportError as error:
            raise ModelPreparationError(
                "huggingface-hub is required for explicit model preparation"
            ) from error

    cache_root.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".specter2-prepare-",
        dir=cache_root.parent,
    ) as raw_temporary:
        temporary = Path(raw_temporary)
        base_snapshot = Path(
            snapshot_download(
                repo_id=BASE_REPOSITORY,
                revision="main",
                cache_dir=str(temporary / "hub"),
            )
        )
        adapter_snapshot = Path(
            snapshot_download(
                repo_id=ADAPTER_REPOSITORY,
                revision="main",
                cache_dir=str(temporary / "hub"),
            )
        )
        adapter_source = adapter_snapshot / "proximity"
        if not (base_snapshot / "config.json").is_file():
            raise ModelPreparationError("downloaded base snapshot lacks config.json")
        if not adapter_source.is_dir():
            # HF snapshot layout for the proximity adapter changed: the
            # adapter files live at the repo root, not under a proximity/ dir.
            adapter_source = adapter_snapshot
        if not (adapter_source / "adapter_config.json").is_file():
            raise ModelPreparationError(
                "downloaded adapter snapshot lacks proximity/adapter_config.json"
            )

        staged_base = temporary / "staged-base"
        staged_proximity = temporary / "staged-proximity"
        _ = shutil.copytree(base_snapshot, staged_base, symlinks=False)
        _ = shutil.copytree(adapter_source, staged_proximity, symlinks=False)

        if force:
            shutil.rmtree(base_target, ignore_errors=True)
            shutil.rmtree(proximity_target, ignore_errors=True)
        proximity_target.parent.mkdir(parents=True, exist_ok=True)
        cache_root.mkdir(parents=True, exist_ok=True)
        os.replace(staged_proximity, proximity_target)
        os.replace(staged_base, base_target)

        repositories: list[JsonValue] = [
            {
                "repo_id": BASE_REPOSITORY,
                "requested_revision": "main",
                "resolved_revision": _resolved_revision(base_snapshot),
            },
            {
                "repo_id": ADAPTER_REPOSITORY,
                "requested_revision": "main",
                "resolved_revision": _resolved_revision(adapter_snapshot),
            },
        ]
        files = cast(
            list[JsonValue],
            cast(object, _file_manifest(cache_root)),
        )
        provenance: JsonObject = {
            "files": files,
            "prepared_at_utc": datetime.now(timezone.utc).isoformat(),
            "profile": PROFILE,
            "repositories": repositories,
            "schema": SCHEMA,
            "schema_version": 1,
        }
        provenance["manifest_sha256"] = manifest_sha256(provenance)
        _atomic_json(provenance_path, provenance)
        return verify_cache(cache_root)


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare optional local-only models")
    _ = parser.add_argument("--specter2", action="store_true")
    _ = parser.add_argument("--cache-root", type=Path, default=DEFAULT_CACHE_ROOT)
    _ = parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    specter2 = cast(bool, args.specter2)
    cache_root = cast(Path, args.cache_root)
    force = cast(bool, args.force)
    if not specter2:
        parser.error("select --specter2")
    try:
        result = prepare_specter2(cache_root.resolve(), force=force)
    except (ModelPreparationError, OSError) as error:
        print(f"Local model preparation denied: {error}")
        return 2
    files = result.get("files")
    schema = result.get("schema")
    if not isinstance(files, list) or not isinstance(schema, str):
        print("Local model preparation denied: invalid result")
        return 2
    print(
        json.dumps(
            {
                "files": len(files),
                "result": "PASS",
                "schema": schema,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
