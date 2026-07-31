#!/usr/bin/env python3
"""review.md frontmatter 의 `journal` 필드를 채운다 (Deep Research 저널 필터용).

DOI 가 있는 논문은 OpenAlex 로 게재 저널명을 조회해 채우고, DOI 가 없거나
arXiv DOI(10.48550)거나 조회 실패한 논문은 "preprint" 로 채운다. 멱등 —
이미 값이 있으면 --force 없이는 건드리지 않는다. docs/papers/ 는 gitignore 라
이 변경은 로컬/Cloudflare 콘텐츠에만 반영된다.

Usage:
  PYTHONUTF8=1 python pipeline/enrich_journals.py            # 전체 enrich
  PYTHONUTF8=1 python pipeline/enrich_journals.py --dry-run  # 조회 없이 분류 집계만
  PYTHONUTF8=1 python pipeline/enrich_journals.py --force    # 기존 journal 값도 갱신
"""
import argparse
import json
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
PAPERS_DIR = PIPELINE_DIR.parent / "docs" / "papers"
INDEX = PAPERS_DIR / "_papers_index.json"
OPENALEX = "https://api.openalex.org/works"
MAILTO = "jehyun.lee@gmail.com"

# 사내 프록시가 HTTPS 를 self-signed 로 가로채는 환경 대응 (config_loader 와 동일)
_ctx = ssl.create_default_context()
_ctx.check_hostname = False
_ctx.verify_mode = ssl.CERT_NONE


def _norm_doi(d):
    d = (d or "").strip()
    if d.startswith("http"):
        d = d.split("doi.org/", 1)[-1]
    return d


def fetch_venues(dois):
    """resolvable DOI → 게재 저널명 dict. OpenAlex 배치(50/req) + 폴리트 풀."""
    out = {}
    uniq = sorted({_norm_doi(d).lower() for d in dois if _norm_doi(d)})
    for i in range(0, len(uniq), 50):
        batch = uniq[i:i + 50]
        flt = "doi:" + "|".join(batch)
        url = (f"{OPENALEX}?per-page=50&mailto={MAILTO}&select=doi,primary_location"
               f"&filter=" + urllib.parse.quote(flt, safe=":|/."))
        req = urllib.request.Request(
            url, headers={"User-Agent": f"paper-curation/1.0 (mailto:{MAILTO})"})
        data = {"results": []}
        for attempt in range(4):
            try:
                data = json.load(urllib.request.urlopen(req, timeout=40, context=_ctx))
                break
            except Exception as e:  # noqa: BLE001
                if attempt == 3:
                    print(f"  WARN batch @{i}: {type(e).__name__} {e}")
                else:
                    time.sleep(2 * (attempt + 1))
        for w in data.get("results", []):
            doi = _norm_doi(w.get("doi")).lower()
            src = ((w.get("primary_location") or {}).get("source") or {})
            name = (src.get("display_name") or "").strip()
            if doi and name:
                out[doi] = name
        print(f"  OpenAlex {min(i + 50, len(uniq))}/{len(uniq)}  (누적 venue {len(out)})")
        time.sleep(0.2)
    return out


def set_frontmatter_journal(md_path, value, force):
    """review.md frontmatter 의 journal 라인을 value 로 설정. 변경 시 True."""
    text = md_path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    fm, rest = text[:end], text[end:]
    new_line = f'journal: "{value}"'
    m = re.search(r'(?m)^journal:\s*"?(.*?)"?\s*$', fm)
    if m:
        if m.group(1).strip() and not force:
            return False  # 이미 채워짐
        new_fm = fm[:m.start()] + new_line + fm[m.end():]
    else:
        new_fm = fm.rstrip("\n") + "\n" + new_line + "\n"
    new_text = new_fm + rest
    if new_text == text:
        return False
    md_path.write_text(new_text, encoding="utf-8")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="OpenAlex 조회/쓰기 없이 분류 집계만")
    ap.add_argument("--force", action="store_true", help="기존 journal 값도 덮어씀")
    args = ap.parse_args()

    idx = json.load(open(INDEX, encoding="utf-8"))
    dois = [p["doi"] for p in idx
            if str(p.get("doi", "")).strip() and "10.48550" not in p["doi"]]
    print(f"{len(idx)}편 중 OpenAlex 조회 대상 DOI {len(dois)}건")
    venues = {} if args.dry_run else fetch_venues(dois)

    venue_found = preprint = written = skipped = missing = 0
    for p in idx:
        md = PAPERS_DIR / p["slug"] / "review.md"
        if not md.exists():
            missing += 1
            continue
        doi = _norm_doi(p.get("doi", "")).lower()
        venue = venues.get(doi) if (doi and "10.48550" not in doi) else None
        if venue:
            venue_found += 1
        else:
            preprint += 1
        if args.dry_run:
            continue
        if set_frontmatter_journal(md, venue or "preprint", args.force):
            written += 1
        else:
            skipped += 1

    print(f"\n저널명 매칭: {venue_found}  ·  preprint 분류: {preprint}")
    if args.dry_run:
        print("(dry-run) 쓰기 생략")
    else:
        print(f"frontmatter 기록: {written}  ·  스킵(기존값): {skipped}  ·  review.md 없음: {missing}")


if __name__ == "__main__":
    main()
