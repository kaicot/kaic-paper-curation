"""Add a paper to Zotero from a PDF file or a URL, then run curation.

For absolute beginners: the LLM can take a PDF path or a paper URL from the
user, create (or reuse) a Zotero collection, register the item with the
attachment, and kick off the curation pipeline - all without the user ever
touching the Zotero app.

Usage:
  python pipeline/tools/add_paper_to_zotero.py --pdf path/to/paper.pdf --collection "My Papers" [--topic mypapers]
  python pipeline/tools/add_paper_to_zotero.py --url https://arxiv.org/abs/2401.00001 --collection "My Papers"
  python pipeline/tools/add_paper_to_zotero.py --pdf x.pdf --collection "My Papers" --no-run

Requires a Zotero API key with write permission (config.json or ZOTERO_API_KEY).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPOSITORY_ROOT))

from pipeline.config_loader import (  # noqa: E402
    _ssl_ctx,
    get_zotero_api_key,
    get_zotero_user_id,
    load_config,
)


def _api(endpoint: str, method: str = "GET", data: object | None = None) -> object:
    api_key = get_zotero_api_key()
    user_id = get_zotero_user_id()
    base = f"https://api.zotero.org/users/{user_id}/{endpoint}"
    body = json.dumps(data).encode("utf-8") if data is not None else None
    req = urllib.request.Request(
        base,
        data=body,
        method=method,
        headers={
            "Zotero-API-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "paper-curation-add/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60, context=_ssl_ctx) as resp:
            raw = resp.read()
            return json.loads(raw.decode("utf-8")) if raw else {}
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Zotero API error [{error.code}] {endpoint}: {detail[:300]}"
        ) from error


def list_collections() -> dict[str, str]:
    rows = _api("collections?limit=100&format=json")  # type: ignore[arg-type]
    result: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        data = row.get("data", {})
        name = data.get("name", "")
        key = row.get("key", "")
        if name and key:
            result[name] = key
    return result


def ensure_collection(name: str) -> str:
    existing = list_collections()
    if name in existing:
        return existing[name]
    result = _api("collections", method="POST", data=[{"name": name}])
    if isinstance(result, dict):
        success_map = result.get("successful") or result.get("success")
    else:
        success_map = None
    if success_map and success_map.get("0"):
        created = success_map["0"]
        key = created.get("key") if isinstance(created, dict) else None
        if key:
            return str(key)
    raise RuntimeError(f"Failed to create collection '{name}': {result}")


def _arxiv_meta(arxiv_id: str) -> dict[str, object]:
    url = f"https://export.arxiv.org/api/query?id_list={arxiv_id}&max_results=1"
    try:
        with urllib.request.urlopen(url, timeout=30, context=_ssl_ctx) as resp:
            xml = resp.read().decode("utf-8", errors="replace")
        # First <entry>'s title (skip the feed-level <title>).
        entry = re.search(r"<entry>.*?</entry>", xml, re.S)
        entry_xml = entry.group(0) if entry else xml
        title = re.search(r"<title>(.*?)</title>", entry_xml, re.S)
        summary = re.search(r"<summary>(.*?)</summary>", xml, re.S)
        authors = re.findall(r"<name>(.*?)</name>", entry_xml)
        return {
            "title": (title.group(1).strip() if title else "").replace("\n ", " "),
            "abstract": summary.group(1).strip() if summary else "",
            "authors": [a.strip() for a in authors],
            "arxiv_id": arxiv_id,
        }
    except Exception:
        return {"title": f"arXiv:{arxiv_id}", "authors": [], "arxiv_id": arxiv_id}


def _crossref_meta(doi: str) -> dict[str, object]:
    url = f"https://api.crossref.org/works/{urllib.parse.quote(doi)}"
    try:
        with urllib.request.urlopen(url, timeout=30, context=_ssl_ctx) as resp:
            msg = json.load(resp)["message"]
        authors = []
        for a in msg.get("author", []):
            name = " ".join(x for x in (a.get("given"), a.get("family")) if x)
            if name:
                authors.append(name)
        return {
            "title": (msg.get("title") or [""])[0],
            "abstract": msg.get("abstract", ""),
            "authors": authors,
            "doi": doi,
            "date": (msg.get("issued", {}).get("date-parts") or [[None]])[0][0],
            "journal": (msg.get("container-title") or [""])[0],
        }
    except Exception:
        return {"title": doi, "authors": [], "doi": doi}


def _pdf_meta(pdf_path: str) -> dict[str, object]:
    """Best-effort title extraction from a local PDF (first page text)."""
    title = Path(pdf_path).stem
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        if doc.page_count > 0:
            text = doc[0].get_text().strip()
            first_lines = [ln.strip() for ln in text.splitlines() if ln.strip()][:8]
            for line in first_lines:
                if 15 <= len(line) <= 250 and not re.match(r"^[\W\d_]+$", line):
                    title = line
                    break
        doc.close()
    except Exception:
        pass
    return {"title": title, "authors": [], "pdf_path": pdf_path}


def _guess_url_meta(url: str) -> dict[str, object]:
    arxiv = re.search(r"arxiv\.org/(?:abs|pdf)/(\d{4}\.\d{4,5})", url)
    if arxiv:
        return _arxiv_meta(arxiv.group(1))
    doi = re.search(r"doi\.org/(10\.\S+)", url) or re.search(r"\b(10\.\d{4,9}/[-._;()/:A-Z0-9]+)", url)
    if doi:
        return _crossref_meta(doi.group(1).rstrip(")"))
    return {"title": url, "authors": [], "url": url}


def _to_zotero_item(meta: dict[str, object], collection_key: str) -> dict[str, object]:
    creators = []
    for author in meta.get("authors", []):
        parts = str(author).split()
        if len(parts) >= 2:
            creators.append({
                "creatorType": "author",
                "firstName": " ".join(parts[:-1]),
                "lastName": parts[-1],
            })
        elif parts:
            creators.append({"creatorType": "author", "lastName": parts[0]})
    is_arxiv = bool(meta.get("arxiv_id"))
    item_type = "preprint" if is_arxiv else "journalArticle"
    item: dict[str, object] = {
        "itemType": item_type,
        "title": str(meta.get("title", "")),
        "creators": creators,
        "collections": [collection_key],
        "url": str(meta.get("url", "") or meta.get("pdf_url", "")),
        "abstractNote": str(meta.get("abstract", "")),
        "tags": [],
    }
    if meta.get("doi"):
        item["DOI"] = str(meta["doi"])
    if meta.get("date"):
        item["date"] = str(meta["date"])
    if meta.get("journal"):
        item["publicationTitle"] = str(meta["journal"])
    if meta.get("arxiv_id"):
        item["archiveID"] = f"arXiv:{meta['arxiv_id']}"
        item["repository"] = "arXiv"
        item["url"] = f"https://arxiv.org/abs/{meta['arxiv_id']}"
    return item


def register_paper(meta: dict[str, object], collection_key: str) -> str:
    """Create the Zotero item and attach the PDF (imported_file for local PDF)."""
    item = _to_zotero_item(meta, collection_key)
    created = _api("items", method="POST", data=[item])
    if isinstance(created, dict):
        success_map = created.get("successful") or created.get("success")
    else:
        success_map = None
    if not (success_map and success_map.get("0")):
        raise RuntimeError(f"Failed to create item: {created}")
    item_key = str(success_map["0"]["key"])

    pdf_path = meta.get("pdf_path")
    if pdf_path and os.path.isfile(str(pdf_path)):
        # Use linked_file (local path reference) so no cloud storage quota is
        # consumed. The curation pipeline's find_pdf() reads the same local
        # path (ZOTERO_DIR / storage / <key> / file), so this stays fully
        # functional for local curation.
        attachment_meta = {
            "itemType": "attachment",
            "linkMode": "linked_file",
            "title": os.path.basename(str(pdf_path)),
            "path": os.path.abspath(str(pdf_path)),
            "contentType": "application/pdf",
            "parentItem": item_key,
        }
        attachment_created = _api("items", method="POST", data=[attachment_meta])
        if isinstance(attachment_created, dict):
            attach_map = attachment_created.get("successful") or attachment_created.get("success")
        else:
            attach_map = None
        if not (attach_map and attach_map.get("0")):
            raise RuntimeError(f"Failed to create attachment: {attachment_created}")
    return item_key


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Add a paper to Zotero (PDF or URL) and optionally curate it"
    )
    _ = parser.add_argument("--pdf", help="Local PDF file path")
    _ = parser.add_argument("--url", help="Paper URL (arXiv/DOI/other)")
    _ = parser.add_argument("--collection", required=True, help="Zotero collection name (created if missing)")
    _ = parser.add_argument("--topic", help="Topic alias (defaults to sanitized collection name)")
    _ = parser.add_argument("--no-run", action="store_true", help="Register only, skip curation")
    args = parser.parse_args()

    if not args.pdf and not args.url:
        print("Provide --pdf or --url.", file=sys.stderr)
        return 2
    if args.pdf and not os.path.isfile(args.pdf):
        print(f"PDF not found: {args.pdf}", file=sys.stderr)
        return 2

    try:
        if args.pdf:
            meta = _pdf_meta(args.pdf)
        else:
            meta = _guess_url_meta(args.url)
        collection_key = ensure_collection(args.collection)
        item_key = register_paper(meta, collection_key)
        print(
            json.dumps(
                {
                    "status": "registered",
                    "collection": args.collection,
                    "collection_key": collection_key,
                    "zotero_item_key": item_key,
                    "title": meta.get("title"),
                    "pdf_attached": bool(meta.get("pdf_path")),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        if not args.no_run:
            topic = args.topic or re.sub(r"[^a-z0-9_-]", "", args.collection.lower())[:40]
            # Add to config.json collections if missing.
            cfg_path = _REPOSITORY_ROOT / "config.json"
            if cfg_path.is_file():
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                collections = cfg.setdefault("zotero", {}).setdefault("collections", {})
                if topic not in collections:
                    collections[topic] = args.collection
                    cfg_path.write_text(
                        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
            print(f"\nRunning curation for topic '{topic}' ...")
            import subprocess

            code = subprocess.call(
                [
                    sys.executable,
                    str(_REPOSITORY_ROOT / "pipeline" / "run_full.py"),
                    "--topic", topic,
                    "--mode", "curate",
                    "--source", "zotero",
                ]
            )
            print(f"Curation exit code: {code}")
            return code
        return 0
    except Exception as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
