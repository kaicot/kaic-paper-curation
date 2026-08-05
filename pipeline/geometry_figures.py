"""Deterministic geometry-only figure manifest publication."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from pathlib import Path


def _atomic_write_json(path: Path, value: object) -> None:
    _ = path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    _ = temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    os.replace(temporary, path)


SCHEMA = "geometry-figures-v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def publish_geometry_manifest(
    pdf_path: str | Path,
    slug_dir: str | Path,
    figures: list[Mapping[str, object]],
) -> dict[str, object]:
    """Publish an exact manifest, including an empty row set."""
    source = Path(pdf_path).resolve()
    paper_dir = Path(slug_dir).resolve()
    figure_dir = paper_dir / "figures"
    _ = figure_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, object]] = []
    for figure in figures:
        name = str(figure.get("name", "")).strip()
        if not name.isdigit():
            continue
        image = figure_dir / f"fig{name}.png"
        if not image.is_file() or image.stat().st_size <= 0:
            continue
        page_value = figure.get("page", 0)
        page = (
            page_value
            if isinstance(page_value, int) and not isinstance(page_value, bool)
            else int(page_value)
            if isinstance(page_value, str) and page_value.isdigit()
            else 0
        )
        rows.append(
            {
                "caption": str(figure.get("caption", "")),
                "page": page,
                "path": image.relative_to(paper_dir).as_posix(),
                "sha256": _sha256(image),
            }
        )
    rows.sort(
        key=lambda row: (
            str(row["path"]),
            row["page"] if isinstance(row["page"], int) else 0,
            str(row["caption"]),
            str(row["sha256"]),
        )
    )
    manifest: dict[str, object] = {
        "rows": rows,
        "schema": SCHEMA,
        "source_pdf_sha256": _sha256(source),
    }
    _atomic_write_json(figure_dir / "manifest-v1.json", manifest)
    return manifest
