#!/usr/bin/env python3
"""Phase-0 scan for bulk figure re-extraction. READ-ONLY.

Classifies every paper into the work plan consumed by reextract_figures.py:
  - tier1: review.md already references figures -> re-extract + swap to tight
    crops, no LLM call.
  - tier2: review.md has no figure refs -> re-extract + write_review re-embed.
  - skip_no_pdf / skip_ambiguous: PDF could not be resolved from the Zotero
    folder by a normalized 40-char title-prefix match.

Also flags figures that look full-page (the old-algorithm bug) by dimension.
Writes the manifest to pipeline/_logs/figure_scan_manifest.json.

Usage:
  PYTHONUTF8=1 python pipeline/scan_figures.py
"""
import os, re, json, glob, sys
from collections import Counter
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config_loader import load_config, PAPERS_DIR

PAPERS = str(PAPERS_DIR)
OUT = os.path.join(os.path.dirname(__file__), "_logs", "figure_scan_manifest.json")
FIGREF = re.compile(r"figures/fig\d+\.(?:png|webp)")


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def main():
    cfg = load_config()
    zdir = cfg.get("zotero", {}).get("pdf_dir", "")
    if not zdir or not os.path.isdir(zdir):
        print(f"Zotero pdf_dir not found: {zdir!r}")
        sys.exit(1)
    znorm = [(norm(os.path.basename(p)), p) for p in glob.glob(os.path.join(zdir, "*.pdf"))]
    print(f"Zotero PDFs: {len(znorm)}")

    def resolve(title, year="", author=""):
        key = norm(title)[:40]
        if len(key) < 20:
            return None
        hits = [p for nf, p in znorm if key in nf]
        if not hits:
            return None
        if len(hits) == 1:
            return hits[0]

        # ── 모호 해소 ────────────────────────────────────────────────
        def _size(p):
            try:
                return os.path.getsize(p)
            except OSError:
                return 0

        def base_key(path):
            # Drive 중복 사본("X.pdf"/"X 1.pdf")의 말미 카운터 제거 후 정규화
            b = os.path.basename(path)
            b = re.sub(r"\s+\d+(?=\.pdf$)", "", b, flags=re.I)
            return norm(b)

        def pick(cands):
            # 최대 크기 우선, 동률이면 접미사 없는 원본(짧은 이름) 우선
            return max(cands, key=lambda p: (_size(p), -len(os.path.basename(p))))

        # (a) 전부 같은 문서의 중복 사본이면 바로 선택
        if len({base_key(p) for p in hits}) == 1:
            return pick(hits)
        # (b) 진짜 충돌: 연도(_YEAR_) → 제1저자 성으로 좁힌다
        cand = hits
        if year:
            nar = [p for p in cand if f"_{year}_" in os.path.basename(p)]
            if nar:
                cand = nar
        if author:
            an = norm(author)
            if an:
                nar = [p for p in cand
                       if an in norm(os.path.basename(p).split("_")[0])]
                if nar:
                    cand = nar
        if len({base_key(p) for p in cand}) == 1:
            return pick(cand)
        return "AMBIGUOUS"

    idx = json.load(open(os.path.join(PAPERS, "_papers_index.json"), encoding="utf-8"))
    seen, mani = set(), []
    for p in idx:
        slug = p.get("slug")
        if not slug or slug in seen:
            continue
        seen.add(slug)
        rv = os.path.join(PAPERS, slug, "review.md")
        if not os.path.exists(rv):
            continue
        has_ref = bool(FIGREF.search(open(rv, encoding="utf-8").read()))
        fd = os.path.join(PAPERS, slug, "figures")
        state, fullpage = "no_figdir", False
        if os.path.isdir(fd):
            files = [f for f in os.listdir(fd) if f.lower().endswith((".png", ".webp", ".jpg"))]
            state = "has_figs" if files else "empty"
            for f in files:
                try:
                    w, h = Image.open(os.path.join(fd, f)).size
                    if 1600 <= w <= 1950 and 2150 <= h <= 2650 and 1.2 <= h / w <= 1.5:
                        fullpage = True
                except Exception:
                    pass
        _year = str(p.get("date", ""))[:4]
        _auth = ""
        _al = p.get("authors") or []
        if _al:
            _auth = str(_al[0]).split()[-1] if str(_al[0]).strip() else ""
        pdf = resolve(p.get("title", ""), year=_year, author=_auth)
        ok = bool(pdf) and pdf != "AMBIGUOUS"
        tier = ("tier1" if has_ref else "tier2") if ok else \
               ("skip_no_pdf" if pdf is None else "skip_ambiguous")
        mani.append({"slug": slug, "tier": tier, "has_ref": has_ref,
                     "fig_state": state, "fullpage": fullpage,
                     "pdf": pdf if ok else None, "title": p.get("title", "")})

    tc = Counter(m["tier"] for m in mani)
    fp = sum(1 for m in mani if m["fullpage"] and m["tier"] in ("tier1", "tier2"))
    print(f"papers: {len(mani)} | " + " ".join(f"{k}={tc.get(k,0)}" for k in
          ("tier1", "tier2", "skip_no_pdf", "skip_ambiguous")))
    print(f"full-page figures (re-extractable): {fp}")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(mani, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"manifest -> {OUT}")


if __name__ == "__main__":
    main()
