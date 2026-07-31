"""
PDF ↔ review 매칭 감사 스크립트 (Phase 1b).

`find_pdf()`의 구 fuzzy 매칭이 야기한 오매칭(약 139편 추정)을 찾아낸다.

검사 축:
  1. TITLE  — index.title의 주요 키워드가 PDF 1페이지 텍스트에 나타나는가
  2. DOI    — index.doi가 PDF 1페이지에 나타나는가 (있을 때만)
  3. ARXIV  — index.title/doi/url에서 추출한 arXiv ID가 PDF 1페이지에 나타나는가 (있을 때만)
  4. REVIEW — review.md 본문 첫 1000자에 index.title 주요 단어가 포함되어 있는가
             (리뷰가 잘못된 PDF로부터 생성되었는지 확인)

Confidence:
  - high   : TITLE/DOI/ARXIV 중 명시 검사 2개 이상 실패 (거의 확실)
  - medium : 1개 실패
  - clean  : 통과 또는 PDF 없음(판정 불가)

Usage:
  PYTHONUTF8=1 python pipeline/audit_matching.py --topic ai4s
  PYTHONUTF8=1 python pipeline/audit_matching.py --topic ai4s --limit 50
  PYTHONUTF8=1 python pipeline/audit_matching.py --topic ai4s --slug 003_X

출력: docs/{topic}/_audit_report.json
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from config_loader import PROJECT_ROOT, get_topic_dir, get_zotero_dir

DOCS = PROJECT_ROOT / "docs"
PAPERS_DIR = DOCS / "papers"
INDEX_PATH = PAPERS_DIR / "_papers_index.json"
ZOTERO_DIR = get_zotero_dir()


# ── PDF text extraction (first page only) ────────────────────────────────────

def extract_first_page_text(pdf_path, max_chars=4000):
    try:
        import fitz  # PyMuPDF
    except ImportError:
        return ""
    try:
        doc = fitz.open(pdf_path)
        if len(doc) == 0:
            return ""
        text = doc[0].get_text() or ""
        doc.close()
        return text[:max_chars]
    except Exception:
        return ""


# ── PDF candidate search (mirrors the stricter find_pdf, read-only) ──────────

def candidate_pdfs_by_doi(doi):
    if not doi:
        return []
    doi_norm = re.sub(r"[^a-z0-9]", "", doi.lower())
    if len(doi_norm) < 10:
        return []
    out = []
    try:
        for fname in os.listdir(ZOTERO_DIR):
            if not fname.lower().endswith(".pdf"):
                continue
            fname_norm = re.sub(r"[^a-z0-9]", "", fname.lower())
            if doi_norm in fname_norm:
                out.append(os.path.join(ZOTERO_DIR, fname))
    except FileNotFoundError:
        pass
    return out


def candidate_pdfs_by_arxiv(arxiv_id):
    if not arxiv_id:
        return []
    out = []
    try:
        for fname in os.listdir(ZOTERO_DIR):
            if fname.lower().endswith(".pdf") and arxiv_id in fname:
                out.append(os.path.join(ZOTERO_DIR, fname))
    except FileNotFoundError:
        pass
    return out


def candidate_pdfs_by_title_fuzzy(title, min_coverage=0.8):
    """Return up to 3 best fuzzy candidates sorted by score (stricter than the
    legacy find_pdf). Used ONLY for audit reconnaissance — never for matching."""
    if not title:
        return []
    title_words = re.sub(r"[^a-z0-9\s]", "", title.lower()).split()
    key_words = [w for w in title_words if len(w) > 3][:8]
    if len(key_words) < 4:
        return []
    scored = []
    try:
        for fname in os.listdir(ZOTERO_DIR):
            if not fname.lower().endswith(".pdf"):
                continue
            fname_lower = fname.lower()
            score = sum(1 for w in key_words if w in fname_lower)
            coverage = score / max(1, len(key_words))
            if coverage >= min_coverage:
                scored.append((score, os.path.join(ZOTERO_DIR, fname)))
    except FileNotFoundError:
        return []
    scored.sort(reverse=True)
    return [p for _, p in scored[:3]]


def find_audit_pdf(entry):
    """Locate the PDF currently most likely to have been used for this review.
    Order mirrors the new strict find_pdf minus the Zotero API round-trip."""
    doi = (entry.get("doi") or "").strip()
    c = candidate_pdfs_by_doi(doi)
    if c:
        return c[0], "doi_filename", c

    arxiv_id = _extract_arxiv_id(entry)
    c = candidate_pdfs_by_arxiv(arxiv_id)
    if c:
        return c[0], "arxiv_filename", c

    c = candidate_pdfs_by_title_fuzzy(entry.get("title", ""))
    if c:
        return c[0], "fuzzy", c

    return None, "no_match", []


def _extract_arxiv_id(entry):
    for field in ("doi", "url", "title"):
        v = entry.get(field, "") or ""
        m = re.search(r"(\d{4}\.\d{4,5})", v)
        if m:
            return m.group(1)
    return None


# ── Checks ───────────────────────────────────────────────────────────────────

def check_title(index_title, pdf_text):
    """Return (passed, reason, coverage)."""
    if not pdf_text:
        return None, "no_pdf_text", 0.0
    words = re.sub(r"[^a-z0-9\s]", " ", index_title.lower()).split()
    key = [w for w in words if len(w) > 3][:8]
    if len(key) < 3:
        return None, "title_too_short", 0.0
    pdf_low = pdf_text.lower()
    hit = sum(1 for w in key if w in pdf_low)
    coverage = hit / len(key)
    # Strong title appearance — >= 80% of significant words visible on page 1
    if coverage >= 0.8:
        return True, "title_ok", coverage
    return False, "title_missing", coverage


def check_doi(index_doi, pdf_text):
    if not index_doi:
        return None, "no_doi_in_index", 0.0
    if not pdf_text:
        return None, "no_pdf_text", 0.0
    doi_norm = re.sub(r"[^a-z0-9]", "", index_doi.lower())
    pdf_norm = re.sub(r"[^a-z0-9]", "", pdf_text.lower())
    if doi_norm and doi_norm in pdf_norm:
        return True, "doi_ok", 1.0
    return False, "doi_absent", 0.0


def check_arxiv(entry, pdf_text):
    aid = _extract_arxiv_id(entry)
    if not aid:
        return None, "no_arxiv_in_entry", 0.0
    if not pdf_text:
        return None, "no_pdf_text", 0.0
    if aid in pdf_text:
        return True, "arxiv_ok", 1.0
    return False, "arxiv_absent", 0.0


def check_review(index_title, review_md_path):
    if not review_md_path.exists():
        return None, "no_review", 0.0
    try:
        text = review_md_path.read_text(encoding="utf-8")[:2000].lower()
    except Exception:
        return None, "review_read_error", 0.0
    words = re.sub(r"[^a-z0-9\s]", " ", index_title.lower()).split()
    key = [w for w in words if len(w) > 3][:8]
    if len(key) < 3:
        return None, "title_too_short", 0.0
    hit = sum(1 for w in key if w in text)
    coverage = hit / len(key)
    if coverage >= 0.7:
        return True, "review_ok", coverage
    return False, "review_title_missing", coverage


def build_text_md_dup_map(topic, index_entries):
    """Scan docs/papers/*/text.md and group slugs by SHA256.

    Two slugs sharing an identical text.md hash means the SAME PDF was
    used to generate both reviews — a certain mismatch. This is the
    authoritative high-confidence signal (Memory/pending_pdf_matching_bug).
    """
    topic_slugs = {e["slug"] for e in index_entries if topic in e.get("topics", [])}
    by_hash = defaultdict(list)
    for slug_dir in PAPERS_DIR.iterdir():
        if not slug_dir.is_dir():
            continue
        if slug_dir.name not in topic_slugs:
            continue
        txt = slug_dir / "text.md"
        if not txt.exists():
            continue
        try:
            h = hashlib.sha256(txt.read_bytes()).hexdigest()
        except Exception:
            continue
        by_hash[h].append(slug_dir.name)
    # Only keep hashes with >1 slug
    return {h: sorted(slugs) for h, slugs in by_hash.items() if len(slugs) > 1}


def classify(results):
    """Decide confidence based on the two strongest signals:

    - TITLE : index.title의 핵심 단어가 PDF 1페이지에 나타나는가
              (제목은 거의 항상 page 1에 있으므로 불일치 = 강한 증거)
    - REVIEW: index.title이 review.md 본문에 나타나는가
              (리뷰가 잘못된 PDF로 생성되면 index.title과 본문이 어긋남)

    DOI/ARXIV 부재는 해당 PDF가 page 1에 출력하지 않았을 수도 있으므로
    보조 증거로만 쓰고, 일치(True)하면 clean 판정을 보강한다.
    """
    t = results.get("title", {}).get("passed")
    r = results.get("review", {}).get("passed")
    d = results.get("doi", {}).get("passed")
    a = results.get("arxiv", {}).get("passed")

    # 명시적 메타데이터 일치는 '확실한 clean' 증거
    explicit_ok = (d is True) or (a is True)

    if t is False and r is False:
        return "high"            # 양쪽 주축 모두 실패 → 거의 확실한 오매칭
    if t is False or r is False:
        return "medium"          # 한쪽만 실패 → 확인 필요
    if t is True and r is True:
        return "clean"           # 양쪽 통과 (DOI/arxiv 부재는 무관)
    # 판정 불가 (PDF 없음 등)
    return "clean" if explicit_ok else "medium"


# ── Audit driver ─────────────────────────────────────────────────────────────

def audit_one(entry, topic):
    slug = entry["slug"]
    if topic not in entry.get("topics", []):
        return None
    index_title = entry.get("title", "")
    index_doi = entry.get("doi", "")

    pdf_path, pdf_method, candidates = find_audit_pdf(entry)
    pdf_text = extract_first_page_text(pdf_path) if pdf_path else ""

    results = {
        "title": dict(zip(("passed", "reason", "coverage"),
                          check_title(index_title, pdf_text))),
        "doi": dict(zip(("passed", "reason", "coverage"),
                        check_doi(index_doi, pdf_text))),
        "arxiv": dict(zip(("passed", "reason", "coverage"),
                          check_arxiv(entry, pdf_text))),
        "review": dict(zip(("passed", "reason", "coverage"),
                           check_review(index_title,
                                         PAPERS_DIR / slug / "review.md"))),
    }
    return {
        "slug": slug,
        "title": index_title[:120],
        "doi": index_doi,
        "pdf_path": pdf_path,
        "pdf_method": pdf_method,
        "candidates": candidates,
        "checks": results,
        "confidence": classify(results),
    }


def _run_audit(topic, *, limit=0, slug=None):
    """Programmatic entrypoint for audit_matching."""
    if not INDEX_PATH.exists():
        print(f"ERROR: {INDEX_PATH} missing", file=sys.stderr)
        sys.exit(2)
    index = json.loads(INDEX_PATH.read_text(encoding="utf-8"))

    if slug:
        entries = [e for e in index if e["slug"].startswith(slug)]
    else:
        entries = [e for e in index if topic in e.get("topics", [])]
    if limit:
        entries = entries[: limit]

    dup_map = build_text_md_dup_map(topic, index)
    slug_to_group = {}
    for h, slugs in dup_map.items():
        for s in slugs:
            slug_to_group[s] = {"hash": h[:12], "peers": [x for x in slugs if x != s]}
    print(f"[dup-scan] {len(dup_map)} duplicate text.md groups "
          f"covering {sum(len(v) for v in dup_map.values())} slugs")

    out = []
    buckets = {"high": 0, "medium": 0, "clean": 0, "skipped": 0}
    t0 = datetime.now()
    for i, entry in enumerate(entries, 1):
        res = audit_one(entry, topic)
        if res is None:
            buckets["skipped"] += 1
            continue
        dup_info = slug_to_group.get(res["slug"])
        if dup_info:
            res["text_md_duplicate"] = dup_info
            res["confidence"] = "high"
        out.append(res)
        buckets[res["confidence"]] += 1
        if i % 50 == 0 or i == len(entries):
            el = (datetime.now() - t0).total_seconds()
            print(f"  [{i}/{len(entries)}] high={buckets['high']} "
                  f"medium={buckets['medium']} clean={buckets['clean']} "
                  f"({el:.0f}s)", flush=True)

    report = {
        "topic": topic,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(out),
        "buckets": buckets,
        "duplicate_text_md_groups": {
            "count": len(dup_map),
            "affected_slug_count": sum(len(v) for v in dup_map.values()),
            "groups": dup_map,
        },
        "results": out,
    }
    out_path = get_topic_dir(topic) / "_audit_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    from lib.atomic_io import atomic_write_json
    atomic_write_json(out_path, report)
    print(f"\nAudit report: {out_path}")
    print(f"  high-confidence mismatch : {buckets['high']}")
    print(f"  medium-confidence suspect: {buckets['medium']}")
    print(f"  clean                    : {buckets['clean']}")
    return report


def main():
    ap = argparse.ArgumentParser(description="PDF↔review matching audit")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--slug", help="Audit a single slug (smoke test)")
    args = ap.parse_args()
    _run_audit(topic=args.topic, limit=args.limit, slug=args.slug)


if __name__ == "__main__":
    main()
