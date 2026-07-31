#!/usr/bin/env python3
"""Duplicate-text.md detector + safe deduplicator.

같은 text.md(=같은 논문 본문)가 여러 슬러그로 등록된 경우를 찾아 *안전한 것만*
정리한다. 보수적 분류 — 잃으면 안 되는 케이스는 절대 자동 삭제하지 않는다.

분류 (각 dup 그룹에서):
  - ORPHAN     : topics 비고 title 비어 *그리고* 같은 논문의 다른 슬러그(같은
                 title-stem)가 토픽 배정돼 있음 → 보이지 않는 고아 중복. 안전 삭제.
  - DUP_SAME   : 두 슬러그가 같은 정규화 DOI → 같은 논문(제목 버전 차이). 본문
                 제목과 일치하는 쪽을 살리고 나머지 삭제.
  - MISMATCH   : 슬러그들의 제목이 서로 다르고 DOI 도 다름 → 서로 다른 논문이
                 같은 본문을 공유(= 한쪽이 엉뚱한 PDF). *삭제 금지* — 잘못된
                 쪽은 올바른 PDF 로 재리뷰해야 함. 플래그만.
  - REVIEW     : 그 외(개명 의심 등) → 사람이 판단. 플래그만.

기본 dry-run. --execute 로 ORPHAN + DUP_SAME 만 삭제. --rename-pairs 로
명시한 개명 쌍(keep:drop)을 추가로 삭제할 수 있다.

슬러그 삭제는 4곳을 정리한다: docs/papers/{slug}/ 디렉토리, _papers_index.json,
각 토픽 _new_classification.json assignments, 각 토픽 _paper_connections.json
(키 + target 양쪽).
"""
import argparse
import glob
import hashlib
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import PAPERS_DIR, DOCS_DIR
from lib.atomic_io import atomic_write_json

PAPERS = str(PAPERS_DIR)
DOCS = str(DOCS_DIR)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _norm_doi(d):
    d = (d or "").lower().strip()
    d = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", d)
    d = re.sub(r"^doi:\s*", "", d)
    d = re.sub(r"^arxiv:", "", d)
    return d if d not in ("", "n/a", "none") else ""


def _text_title(slug):
    p = os.path.join(PAPERS, slug, "text.md")
    if not os.path.exists(p):
        return ""
    txt = open(p, encoding="utf-8").read()[:2000]
    for ln in txt.splitlines():
        ln = ln.strip().lstrip("#").strip()
        if len(ln) > 15 and not ln.lower().startswith(
                ("abstract", "http", "arxiv", "doi", "published as", "proceedings",
                 "type ", "affecting")):
            return ln
    return ""


def _title_stem(title):
    return _norm(title)[:40]


def classify_groups(idx):
    by_slug = {p["slug"]: p for p in idx}
    # text.md sha → slugs
    groups = {}
    for d in glob.glob(os.path.join(PAPERS, "*", "text.md")):
        slug = os.path.basename(os.path.dirname(d))
        h = hashlib.sha256(open(d, "rb").read()).hexdigest()[:12]
        groups.setdefault(h, []).append(slug)
    dups = {h: s for h, s in groups.items() if len(s) > 1}

    # title-stem → slugs (고아가 정식본을 가졌는지 판단)
    stem_has_topic = {}
    for p in idx:
        stem = _title_stem(p.get("title", ""))
        if stem and p.get("topics"):
            stem_has_topic.setdefault(stem, []).append(p["slug"])
    # 빈 제목 고아의 stem 은 idx 제목으론 못 잡으니 slug 이름으로 보조 추정
    slugname_has_topic = {}
    for p in idx:
        # slug: NNN_Title... → 제목부 정규화 앞 40자
        sm = re.match(r"\d+_(.+)", p.get("slug", ""))
        stem2 = _norm(sm.group(1))[:40] if sm else ""
        if stem2 and p.get("topics"):
            slugname_has_topic.setdefault(stem2, []).append(p["slug"])

    results = []
    for h, slugs in sorted(dups.items()):
        ttitle = _text_title(slugs[0])
        members = []
        for s in slugs:
            p = by_slug.get(s, {})
            sm = re.match(r"\d+_(.+)", s)
            members.append({
                "slug": s,
                "title": p.get("title", ""),
                "topics": p.get("topics", []),
                "doi": _norm_doi(p.get("doi")),
                "slugstem": _norm(sm.group(1))[:40] if sm else "",
                "matches_text": bool(ttitle) and (
                    _norm(ttitle)[:30] in _norm(p.get("title", "")) or
                    _norm(p.get("title", ""))[:30] in _norm(ttitle)),
            })
        results.append({"hash": h, "text_title": ttitle, "members": members,
                        "verdict": _verdict(members, slugname_has_topic)})
    return results


def _verdict(members, slugname_has_topic):
    """그룹 분류 + (삭제 가능하면) keep/drop 슬러그."""
    empties = [m for m in members if not m["topics"] and not m["title"]]
    nonempty = [m for m in members if m["topics"] or m["title"]]

    # ORPHAN: 빈 슬러그(topics·title 모두 없음) — 같은 paper(slugstem)의
    # 토픽 배정본이 어딘가에 존재하면 *보이지 않는 고아* 라 전부 삭제한다.
    # 정식본은 다른 text.md 해시 그룹에 있을 수 있으므로(추출 미세차이) slugstem
    # 으로 찾는다. 정식본이 없으면(전부 고아) 1개만 남기고 삭제.
    if empties:
        drops = []
        stem_has_canon = False
        for m in empties:
            canon = [s for s in slugname_has_topic.get(m["slugstem"], [])
                     if s != m["slug"]]
            if canon:
                stem_has_canon = True
                drops.append(m["slug"])
        if drops:
            # 정식본이 따로 있으면 빈 고아 전부 삭제 (이 그룹의 non-empty 도 유지)
            keep = [m["slug"] for m in members if m["slug"] not in drops]
            # 안전장치: 삭제 후 이 paper(stem)가 토픽 배정본으로 살아있는지 보장됨
            # (slugname_has_topic 의 정식본은 절대 drops 에 안 들어감 — 빈 슬러그만 대상)
            return {"type": "ORPHAN", "keep": keep, "drop": drops}
        # 전부 고아인데 정식본이 없음 → 데이터 보존 위해 1개만 남김
        if not stem_has_canon and len(empties) == len(members) and len(members) > 1:
            keep1 = members[0]["slug"]
            return {"type": "ORPHAN", "keep": [keep1],
                    "drop": [m["slug"] for m in members if m["slug"] != keep1]}

    # DUP_SAME: 정확히 2개, 같은 DOI → 본문 일치하는 쪽 유지
    if len(members) == 2:
        a, b = members
        if a["doi"] and a["doi"] == b["doi"]:
            keep = a if a["matches_text"] else (b if b["matches_text"] else a)
            drop = b if keep is a else a
            return {"type": "DUP_SAME", "keep": [keep["slug"]], "drop": [drop["slug"]]}
        # 서로 다른 DOI(둘 다 있음) + 제목 불일치 → 다른 논문, 본문 공유 = 오매칭
        if a["doi"] and b["doi"] and a["doi"] != b["doi"]:
            return {"type": "MISMATCH", "keep": [], "drop": []}

    return {"type": "REVIEW", "keep": [], "drop": []}


def redirect_references(redirect, idx):
    """삭제된 슬러그를 가리키던 참조를 살아남은 정식본으로 리디렉트.

    redirect: {deleted_prefix: canonical_full_slug}. 삭제 슬러그는 이미
    _papers_index 에서 빠졌으므로 *링크 경로의 prefix* 로 매칭한다.

    처리 대상:
      1) 모든 review.md 의 wiki-link `[[papers/{deleted}/review]]` → 정식본.
         리디렉트 후 *자기참조*(논문이 자기 자신을 가리킴 — 개명 전 중복을
         가리키던 정식본에서 발생)는 그 Related 라인을 통째로 제거. 같은
         정식본을 두 번 가리키면 뒤엣것 제거(dedupe).
      2) _global_connections.json + 각 토픽 _paper_connections.json:
         키·target 의 삭제 슬러그를 정식본으로 치환, 자기연결 제거, dedupe.
      변경된 논문은 HTML 재렌더. 반환: 변경된 슬러그 set.
    """
    import re as _re
    canon_full = set(redirect.values())
    # prefix → canonical 정규식 (링크 경로 안의 전체 슬러그 세그먼트 치환)
    pats = [( _re.compile(rf"papers/{p}_[^/\]\"']+"), f"papers/{c}")
            for p, c in redirect.items()]
    changed = set()

    for rv in glob.glob(os.path.join(PAPERS, "*", "review.md")):
        own = os.path.basename(os.path.dirname(rv))
        txt = open(rv, encoding="utf-8").read()
        new = txt
        for rx, repl in pats:
            new = rx.sub(repl, new)
        if new == txt:
            continue
        # Related Papers 라인 단위로 자기참조 제거 + dedupe
        out_lines, seen = [], set()
        for ln in new.splitlines():
            mm = _re.search(r"\[\[papers/([^/\]]+)/review\]\]", ln)
            if mm:
                tgt = mm.group(1)
                if tgt == own:
                    continue  # 자기참조 제거
                if tgt in seen:
                    continue  # 같은 대상 중복 라인 제거
                seen.add(tgt)
            out_lines.append(ln)
        new = "\n".join(out_lines)
        if not new.endswith("\n"):
            new += "\n"
        open(rv, "w", encoding="utf-8").write(new)
        changed.add(own)

    # 연결 JSON 치환 (글로벌 + 토픽별)
    def _fix_conn(path):
        if not os.path.exists(path):
            return
        data = json.load(open(path, encoding="utf-8"))
        body = data.get("connections", data) if isinstance(data, dict) else data
        if not isinstance(body, dict):
            return
        full_map = {}  # deleted_full(있다면) → canonical; prefix 매칭으로 처리
        def remap(slug):
            for p, c in redirect.items():
                if slug.startswith(p + "_"):
                    return c
            return slug
        new_body = {}
        for k, lst in body.items():
            nk = remap(k)
            cur = new_body.setdefault(nk, [])
            seen_t = {c.get("slug") for c in cur}
            for c in (lst if isinstance(lst, list) else []):
                t = remap(c.get("slug", ""))
                if t == nk or t in seen_t:
                    continue  # 자기연결 / 중복 제거
                nc = dict(c); nc["slug"] = t
                cur.append(nc); seen_t.add(t)
        if isinstance(data, dict) and "connections" in data:
            data["connections"] = new_body
            atomic_write_json(path, data)
        else:
            atomic_write_json(path, new_body)

    _fix_conn(os.path.join(PAPERS, "_global_connections.json"))
    for td in glob.glob(os.path.join(DOCS, "*")):
        if os.path.isdir(td) and os.path.basename(td) != "papers":
            _fix_conn(os.path.join(td, "_paper_connections.json"))
    return changed


def delete_slugs(slugs, idx):
    """슬러그들을 안전 삭제. 갱신된 idx 반환."""
    drop = set(slugs)
    # 1) _papers_index.json
    new_idx = [p for p in idx if p["slug"] not in drop]
    # 2) 디렉토리
    for s in drop:
        d = os.path.join(PAPERS, s)
        if os.path.isdir(d):
            shutil.rmtree(d, ignore_errors=True)
    # 3) 토픽 분류 + 연결
    for topic_dir in glob.glob(os.path.join(DOCS, "*")):
        if not os.path.isdir(topic_dir) or os.path.basename(topic_dir) == "papers":
            continue
        cls = os.path.join(topic_dir, "_new_classification.json")
        if os.path.exists(cls):
            data = json.load(open(cls, encoding="utf-8"))
            assigns = data.get("assignments", [])
            new_assigns = [a for a in assigns if a.get("slug") not in drop]
            if len(new_assigns) != len(assigns):
                data["assignments"] = new_assigns
                atomic_write_json(cls, data)
        conn = os.path.join(topic_dir, "_paper_connections.json")
        if os.path.exists(conn):
            data = json.load(open(conn, encoding="utf-8"))
            body = data.get("connections", data) if isinstance(data, dict) else data
            changed = False
            for s in list(body.keys()):
                if s in drop:
                    del body[s]; changed = True
            for s, lst in body.items():
                if isinstance(lst, list):
                    filt = [c for c in lst if c.get("slug") not in drop]
                    if len(filt) != len(lst):
                        body[s] = filt; changed = True
            if changed:
                atomic_write_json(conn, data)
    return new_idx


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--execute", action="store_true",
                    help="실제 삭제 (기본 dry-run). ORPHAN + DUP_SAME 만 대상.")
    ap.add_argument("--rename-pairs", default="",
                    help="개명 쌍 추가 삭제: 'keep:drop,keep:drop' (예 590:586,239:240)")
    args = ap.parse_args()

    idx = json.load(open(os.path.join(PAPERS, "_papers_index.json"), encoding="utf-8"))
    results = classify_groups(idx)

    auto_drop, flagged = [], []
    for g in results:
        v = g["verdict"]
        tag = v["type"]
        line = f"[{g['hash']}] {tag}: {g['text_title'][:55]}"
        if tag in ("ORPHAN", "DUP_SAME") and v["drop"]:
            auto_drop += v["drop"]
            line += f"\n    KEEP {v['keep']}  DROP {v['drop']}"
        else:
            flagged.append(g)
            line += "  → 재리뷰/사람판단 (삭제 안 함)"
            for m in g["members"]:
                line += f"\n    {m['slug'][:46]:48s} match={'✓' if m['matches_text'] else '✗'} doi={m['doi'] or '-'}"
        print(line)

    # 리디렉트 맵: {삭제_prefix: 생존_정식본_full}. DUP_SAME + rename 만.
    # (ORPHAN 빈 고아는 사이트 콘텐츠 참조가 없어 제외)
    def _prefix(slug):
        m = re.match(r"(\d+)_", slug)
        return m.group(1) if m else slug

    def _full(prefix):
        return next((p["slug"] for p in idx if p["slug"].startswith(prefix + "_")), None)

    redirect = {}
    for g in results:
        v = g["verdict"]
        if v["type"] == "DUP_SAME" and v["keep"] and v["drop"]:
            for d in v["drop"]:
                redirect[_prefix(d)] = v["keep"][0]

    # 개명 쌍 추가
    rename_drop = []
    if args.rename_pairs:
        for pair in args.rename_pairs.split(","):
            keep, drop = pair.split(":")
            full = [p["slug"] for p in idx if p["slug"].startswith(drop + "_") or p["slug"] == drop]
            rename_drop += full
            kf = _full(keep)
            if kf:
                redirect[drop if "_" not in drop else _prefix(drop)] = kf
        print(f"\n[rename] 추가 삭제: {rename_drop}")

    all_drop = sorted(set(auto_drop) | set(rename_drop))
    print(f"\n=== 요약 ===")
    print(f"  자동 삭제 대상: {len(all_drop)} 슬러그 (ORPHAN+DUP_SAME+rename)")
    print(f"  플래그(삭제 안 함): {len(flagged)} 그룹 (MISMATCH/REVIEW — 재리뷰 필요)")

    if not args.execute:
        print("\n  DRY-RUN. --execute 로 자동 삭제 대상만 삭제.")
        return

    if all_drop:
        new_idx = delete_slugs(all_drop, idx)
        atomic_write_json(os.path.join(PAPERS, "_papers_index.json"), new_idx)
        print(f"\n  삭제 완료: {len(all_drop)} 슬러그.")
        # 삭제된 슬러그를 가리키던 참조를 생존 정식본으로 리디렉트 + 변경 HTML 재렌더
        if redirect:
            changed = redirect_references(redirect, new_idx)
            print(f"  참조 리디렉트: {len(changed)} 논문 review.md → 정식본")
            try:
                import contextlib, io
                import run_update_force as ruf
                for s in changed:
                    with contextlib.redirect_stdout(io.StringIO()):
                        ruf.convert_to_html(s)
                print(f"  HTML 재렌더: {len(changed)}")
            except Exception as e:
                print(f"  HTML 재렌더 일부 실패: {str(e)[:80]}")
        print(f"  → 토픽 인덱스 재빌드 필요 (build_topic_index 각 토픽).")


if __name__ == "__main__":
    main()
