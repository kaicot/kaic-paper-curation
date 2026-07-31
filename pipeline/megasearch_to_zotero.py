"""
scholar-megasearch corpus.json → paper-curation Zotero 등록 어댑터.

scholar-megasearch 가 생성한 corpus.json (+ pdfs/) 를 paper-curation 의
기존 `register_zotero.py` 입력 형식 (`{topic}/_search_results.json`) 으로
변환한다. 이미 리뷰한 논문 (`_papers_index.json`) 은 자동으로 걸러내고,
이미 받아둔 PDF 는 Zotero PDF 디렉토리에 pre-stage 하여 register_zotero 가
재다운로드하지 않도록 단축한다.

Usage:
  PYTHONUTF8=1 python pipeline/megasearch_to_zotero.py \
    --topic ai4s \
    --corpus literature_search/ai4s_2026-06-08/corpus.json \
    --pdfs-dir literature_search/ai4s_2026-06-08/pdfs \
    --min-sources 2

  # 변환만 (PDF pre-stage 없이) — register_zotero 가 PDF 를 직접 받게 둠
  PYTHONUTF8=1 python pipeline/megasearch_to_zotero.py \
    --topic bioml --corpus run/corpus.json

  # 변환 + 즉시 등록까지 한 번에
  PYTHONUTF8=1 python pipeline/megasearch_to_zotero.py \
    --topic ai4s --corpus run/corpus.json --register

다음 단계 (--register 미사용 시):
  PYTHONUTF8=1 python pipeline/register_zotero.py --topic ai4s
"""

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

PIPELINE_DIR = Path(__file__).resolve().parent
if str(PIPELINE_DIR) not in sys.path:
    sys.path.insert(0, str(PIPELINE_DIR))

from config_loader import get_topic_dir, get_zotero_dir, PROJECT_ROOT
from register_zotero import safe_filename, _run_register

_PAPERS_INDEX_PATH = PROJECT_ROOT / "docs" / "papers" / "_papers_index.json"


def _norm_doi(v):
    if not v:
        return ""
    v = str(v).strip().lower()
    v = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:)", "", v)
    return v


def _norm_arxiv(v):
    if not v:
        return ""
    v = str(v).strip()
    v = re.sub(r"^arxiv:", "", v, flags=re.IGNORECASE)
    v = v.rsplit("/", 1)[-1]
    v = re.sub(r"v\d+$", "", v)
    return v.lower()


def _norm_title_prefix(v, n=30):
    if not v:
        return ""
    return re.sub(r"[^a-z0-9]", "", str(v).lower())[:n]


def load_known_keys(topic):
    """`_papers_index.json` 에서 (doi_set, arxiv_set, title30_set) 반환.

    topic 필터는 하지 않는다 — 한 번 리뷰한 논문은 어느 토픽이든 재리뷰 안 함.
    """
    if not _PAPERS_INDEX_PATH.exists():
        return set(), set(), set()
    with open(_PAPERS_INDEX_PATH, encoding="utf-8") as f:
        idx = json.load(f)

    dois, arxiv_ids, title30s = set(), set(), set()
    for entry in idx:
        d = _norm_doi(entry.get("doi") or "")
        if d:
            dois.add(d)
        # _papers_index.json 에는 arxiv_id 필드가 없을 수 있다 — DOI 안에 박힌 거 시도
        ax = _norm_arxiv(entry.get("arxiv_id") or "")
        if not ax and d.startswith("10.48550/arxiv."):
            ax = d.split("10.48550/arxiv.", 1)[1]
        if ax:
            arxiv_ids.add(ax)
        t = _norm_title_prefix(entry.get("title") or "")
        if t:
            title30s.add(t)
    return dois, arxiv_ids, title30s


def is_known(rec, dois, arxiv_ids, title30s):
    """corpus 레코드가 이미 리뷰됐는지 — 셋 중 하나라도 매치되면 True."""
    d = _norm_doi(rec.get("doi") or "")
    if d and d in dois:
        return "doi", d
    ax = _norm_arxiv(rec.get("arxiv_id") or "")
    if ax and ax in arxiv_ids:
        return "arxiv", ax
    t = _norm_title_prefix(rec.get("title") or "")
    if t and t in title30s:
        return "title30", t
    return None


def to_search_record(rec):
    """corpus.json 레코드 → register_zotero.py 가 받는 paper dict.

    register_zotero.py 가 사용하는 필드 (title/doi/arxiv_id/pdf_url/url/authors/
    abstract/date) 를 채우고, scholar-megasearch 만의 메타 (sources, citations,
    venue) 는 `_megasearch_*` 접두사로 보존해 audit 추적에 쓴다.
    """
    year = rec.get("year")
    date = str(year) if year else ""
    paper = {
        "title": (rec.get("title") or "").strip(),
        "authors": rec.get("authors") or [],
        "date": date,
        "doi": rec.get("doi") or "",
        "arxiv_id": rec.get("arxiv_id") or "",
        "url": rec.get("url") or "",
        "pdf_url": rec.get("pdf_url") or "",
        "abstract": rec.get("abstract") or "",
    }
    if rec.get("venue"):
        paper["venue"] = rec["venue"]
    if rec.get("citations") is not None:
        paper["citations"] = rec["citations"]
    if rec.get("sources"):
        paper["_megasearch_sources"] = list(rec["sources"])
        paper["_megasearch_sources_count"] = len(rec["sources"])
    return paper


def _slug_for_corpus_idx(rec, i):
    """scholar-megasearch fetch_pdfs.py 의 slug() 와 동일."""
    base = rec.get("title") or rec.get("doi") or rec.get("arxiv_id") or f"paper{i}"
    s = re.sub(r"[^a-z0-9]+", "-", str(base).lower()).strip("-")
    return f"{i:02d}_{s[:60]}"


def prestage_pdfs(corpus, pdfs_dir, zotero_dir, mapping):
    """corpus 의 N번째 레코드와 매칭되는 PDF 를 Zotero 디렉토리로 복사.

    register_zotero.py 의 download_pdf 가 dest_path 의 존재 + 5KB 이상이면
    네트워크 호출 없이 그 경로를 반환한다 — 그래서 미리 정확한 이름으로 깔아둔다.

    mapping: {1-based corpus rank → True if prestaged}
    """
    if not pdfs_dir or not Path(pdfs_dir).exists():
        return 0, 0
    manifest_path = Path(pdfs_dir) / "manifest.json"
    if not manifest_path.exists():
        print(f"  [prestage] manifest.json 없음 — slug 추정으로 fallback", file=sys.stderr)

    zotero_dir = Path(zotero_dir)
    zotero_dir.mkdir(parents=True, exist_ok=True)

    # manifest 있으면 거기서 acquired 목록을 받고, 없으면 디렉토리 스캔
    acquired = {}  # corpus_rank (1-based) → source path
    if manifest_path.exists():
        with open(manifest_path, encoding="utf-8") as f:
            mlist = json.load(f)
        # manifest 항목은 fetch_pdfs.py 가 corpus 순서대로 enumerate 한다 — 즉 0-based idx + slug
        for m in mlist:
            if m.get("status") != "ok":
                continue
            # path 필드가 있으면 그걸 우선
            p = m.get("path") or ""
            if not p:
                # filename 만 들어있는 경우
                p = str(Path(pdfs_dir) / m.get("filename", ""))
            # rank 는 manifest 의 slug 앞 NN 에서 추출
            slug = Path(p).stem
            mnum = re.match(r"^(\d+)_", slug)
            if mnum:
                rank = int(mnum.group(1))
                acquired[rank] = p
    else:
        # fallback: pdfs/NN_*.pdf 직접 스캔
        for p in sorted(Path(pdfs_dir).glob("*.pdf")):
            mnum = re.match(r"^(\d+)_", p.name)
            if mnum:
                acquired[int(mnum.group(1))] = str(p)

    prestaged = 0
    total = 0
    for i, rec in enumerate(corpus, 1):
        total += 1
        src = acquired.get(i)
        if not src or not Path(src).exists():
            continue
        if Path(src).stat().st_size < 5120:
            continue
        title = rec.get("title") or ""
        if not title:
            continue
        dest = zotero_dir / (safe_filename(title) + ".pdf")
        if dest.exists() and dest.stat().st_size >= 5120:
            mapping[i] = "already_present"
            prestaged += 1
            continue
        try:
            shutil.copy2(src, dest)
            mapping[i] = "prestaged"
            prestaged += 1
        except OSError as e:
            print(f"  [prestage] {dest.name}: 복사 실패 — {e}", file=sys.stderr)
    return prestaged, total


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True, help="paper-curation topic alias (ai4s/scisci/bioml ...)")
    ap.add_argument("--corpus", required=True, help="scholar-megasearch corpus.json 경로")
    ap.add_argument("--pdfs-dir", default="",
                    help="scholar-megasearch 의 pdfs/ 디렉토리 (manifest.json 동봉). "
                         "지정 시 Zotero PDF 디렉토리로 pre-stage 하여 재다운로드 절약.")
    ap.add_argument("--min-sources", type=int, default=1,
                    help="N개 미만의 DB 가 surface 한 논문은 drop (기본 1 — 전체 통과)")
    ap.add_argument("--output", default="",
                    help="출력 _search_results.json 경로 (기본 {topic}/_search_results.json)")
    ap.add_argument("--no-skip-known", action="store_true",
                    help="_papers_index.json 의 기존 리뷰와 cross-dedup 하지 않음 (감사용)")
    ap.add_argument("--register", action="store_true",
                    help="변환 직후 register_zotero._run_register 를 바로 호출")
    ap.add_argument("--dry-run", action="store_true",
                    help="--register 와 함께 — 등록은 하지 않고 미리보기")
    args = ap.parse_args()

    # 1. corpus 로드
    corpus_path = Path(args.corpus)
    if not corpus_path.exists():
        sys.exit(f"corpus 파일 없음: {corpus_path}")
    with open(corpus_path, encoding="utf-8") as f:
        corpus = json.load(f)
    if not isinstance(corpus, list):
        sys.exit("corpus.json 은 list 여야 합니다 (merge_corpus.py 출력 형식)")
    print(f"[1/5] corpus 로드: {len(corpus)} 레코드 ← {corpus_path}")

    # 2. min-sources 필터
    if args.min_sources > 1:
        before = len(corpus)
        corpus = [r for r in corpus
                  if (r.get("sources_count") or len(r.get("sources") or [])) >= args.min_sources]
        print(f"[2/5] min-sources≥{args.min_sources} 필터: {before} → {len(corpus)}")
    else:
        print(f"[2/5] min-sources 필터 생략")

    # 3. _papers_index.json cross-dedup
    if args.no_skip_known:
        survivors = corpus
        skipped_known = []
        print(f"[3/5] 기존 리뷰 cross-dedup 생략 (--no-skip-known)")
    else:
        dois, arxiv_ids, title30s = load_known_keys(args.topic)
        print(f"[3/5] 기존 리뷰: doi={len(dois)} arxiv={len(arxiv_ids)} title30={len(title30s)}")
        survivors, skipped_known = [], []
        for rec in corpus:
            hit = is_known(rec, dois, arxiv_ids, title30s)
            if hit:
                skipped_known.append({"title": rec.get("title", ""),
                                       "match_kind": hit[0], "match_value": hit[1]})
            else:
                survivors.append(rec)
        print(f"      기존 리뷰와 매치 → skip: {len(skipped_known)}편, 신규 후보: {len(survivors)}편")

    # 4. PDF pre-stage (선택)
    prestage_map = {}
    if args.pdfs_dir:
        zotero_dir = get_zotero_dir() or str(get_topic_dir(args.topic) / "_pdfs")
        prestaged, total = prestage_pdfs(survivors, args.pdfs_dir, zotero_dir, prestage_map)
        print(f"[4/5] PDF pre-stage: {prestaged}/{total} → {zotero_dir}")
    else:
        print(f"[4/5] PDF pre-stage 생략 (--pdfs-dir 없음, register_zotero 가 직접 받음)")

    # 5. register_zotero 형식으로 변환
    papers = [to_search_record(r) for r in survivors]
    output_path = Path(args.output) if args.output \
        else get_topic_dir(args.topic) / "_search_results.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(papers, f, ensure_ascii=False, indent=2)
    print(f"[5/5] _search_results.json 저장: {len(papers)}편 → {output_path}")

    if skipped_known:
        skip_log = output_path.parent / "_megasearch_skipped_known.json"
        with open(skip_log, "w", encoding="utf-8") as f:
            json.dump(skipped_known, f, ensure_ascii=False, indent=2)
        print(f"      skip 로그: {skip_log}")

    print(f"\n어댑터 완료: corpus → _search_results.json")
    print(f"  다음: PYTHONUTF8=1 python pipeline/register_zotero.py --topic {args.topic}")

    if args.register:
        print(f"\n--register 옵션 → register_zotero._run_register 즉시 호출")
        result = _run_register(topic=args.topic, input_path=str(output_path),
                               dry_run=args.dry_run)
        if result:
            print(f"  registered={result.get('registered')} "
                  f"duplicates={result.get('duplicates')} "
                  f"pdf={result.get('pdf_success')}/{result.get('pdf_success', 0) + result.get('pdf_failed', 0)}")


if __name__ == "__main__":
    main()
