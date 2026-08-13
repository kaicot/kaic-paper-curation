"""Read-only local Zotero inspection for keyless setup guidance.

Reads the local Zotero data directory (zotero.sqlite + storage/) so an LLM
assistant can guide a user through setup without a Zotero API key. This tool
only reads; it never writes to the Zotero database.

Usage:
  python pipeline/tools/inspect_local_zotero.py [--data-dir PATH] [--json]
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))


def _default_data_dir() -> Path:
    home = Path.home()
    for candidate in (
        home / "Zotero",
        home / "Documents" / "Zotero",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Zotero",
    ):
        if (candidate / "zotero.sqlite").is_file():
            return candidate
    return home / "Zotero"


def inspect(data_dir: Path) -> dict[str, object]:
    data_dir = data_dir.resolve()
    db = data_dir / "zotero.sqlite"
    if not db.is_file():
        return {"found": False, "data_dir": str(data_dir), "error": "zotero.sqlite missing"}
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        rows = cur.execute(
            "SELECT collectionName, key, parentCollectionID FROM collections "
            "ORDER BY collectionName"
        ).fetchall()
        collections = [
            {
                "name": row["collectionName"],
                "key": row["key"],
                "parent": row["parentCollectionID"],
            }
            for row in rows
        ]
        # Per-collection PDF counts: Zotero stores attachments under
        # storage/<attachmentKey>/ regardless of collection, so count every
        # PDF under the whole storage tree (a good "synced" signal).
        storage = data_dir / "storage"
        all_pdfs = (
            sum(1 for p in storage.rglob("*.pdf") if p.is_file())
            if storage.is_dir()
            else 0
        )
        con.close()
        return {
            "found": True,
            "data_dir": str(data_dir),
            "storage_dir": str(storage),
            "collections": [
                {**c, "local_pdfs": all_pdfs}
                for c in collections
            ],
            "total_local_pdfs": all_pdfs,
            "note": "Zotero stores attachment PDFs under storage/<attachmentKey>/; "
                    "total_local_pdfs is the number of PDFs already synced to this PC. "
                    "If it is 0, run the Zotero app sync to download attachments.",
        }
    except Exception as error:  # noqa: BLE001 - report any read failure clearly
        return {
            "found": False,
            "data_dir": str(data_dir),
            "error": f"{type(error).__name__}: {error}",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only local Zotero inspection")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--json", action="store_true", help="JSON output")
    args = parser.parse_args()
    data_dir = args.data_dir or _default_data_dir()
    result = inspect(data_dir)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if not result["found"]:
            print(f"Zotero data directory not found at {data_dir}.")
            return 1
        print(f"Zotero data: {result['data_dir']}")
        print(f"Storage: {result['storage_dir']}")
        print(f"Total local PDFs: {result['total_local_pdfs']}")
        for collection in result["collections"]:
            marker = "OK" if result["total_local_pdfs"] else "--"
            print(
                f"  [{marker}] {collection['name']} "
                f"(key={collection['key']})"
            )
        print("\n[OK] = PDF already on this PC (ready for curation)")
        print("[--] = no local PDF (run Zotero sync first)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
