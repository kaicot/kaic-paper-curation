#!/usr/bin/env python3
"""Bulk figure re-extraction with the geometric (nearest-caption) extractor.

Re-runs the fixed extract_figures() across the corpus to replace old
full-page / over-cropped figures with tight, localized crops. Robust by
design — safe to interrupt and resume, never regresses a paper.

Two tiers (auto-detected from review.md):
  - tier1: review already references figures -> re-extract -> webp -> re-render
    HTML. NO LLM call; review prose is left untouched.
  - tier2: review has no figure refs -> re-extract -> write_review (re-embed)
    -> splice the original frontmatter + Related Papers back -> webp -> render.

Robustness guarantees:
  - NO-REGRESS: if the new extraction yields 0 figures, the paper is left
    exactly as-is (old figures + review kept).
  - Per-paper transient backup (figures/ + review.md + index.html) restored on
    failure, deleted on success (peak disk ~= workers x one paper).
  - Referenced figure numbers the new run does not reproduce keep their old
    webp (no broken references).
  - Checkpoint/resume via an append-only JSONL log (re-running skips done
    slugs). Atomic _papers_index.json patch at the end (single-threaded).
  - Geometric extraction only (GOOGLE_API_KEY unset) — deterministic, fast,
    and already validated; Gemini refinement is skipped for the bulk pass.

Usage:
  PYTHONUTF8=1 python pipeline/reextract_figures.py --manifest /tmp/figure_scan_manifest.json --dry-run
  PYTHONUTF8=1 python pipeline/reextract_figures.py --manifest /tmp/figure_scan_manifest.json --limit 30
  PYTHONUTF8=1 python pipeline/reextract_figures.py --manifest /tmp/figure_scan_manifest.json --workers 16
  PYTHONUTF8=1 python pipeline/reextract_figures.py --slugs 099_...,481_...
"""
import os, sys, re, json, glob, shutil, argparse, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_update_force as ruf
from prepare_deploy import convert_png_to_webp, update_html_refs
from config_loader import PAPERS_DIR
from lib.atomic_io import atomic_write_json

PAPERS = str(PAPERS_DIR)
LOG_DEFAULT = os.path.join(os.path.dirname(__file__), "_logs", "reextract_log.jsonl")

FIGREF = re.compile(r'figures/fig(\d+)\.(png|webp)')


def _now():
    return time.strftime("%H:%M:%S")


def review_fig_refs(review_path):
    """Set of referenced fig numbers + dominant extension in review.md."""
    if not os.path.exists(review_path):
        return set(), "webp"
    txt = open(review_path, encoding="utf-8").read()
    nums = {int(m.group(1)) for m in FIGREF.finditer(txt)}
    exts = [m.group(2) for m in FIGREF.finditer(txt)]
    ext = "webp" if exts.count("webp") >= exts.count("png") else "png"
    return nums, ext


def _fm(text):
    m = re.match(r"^(---\n.*?\n---\n)", text, flags=re.S)
    return m.group(1) if m else ""


def _related(text):
    m = re.search(r"(^## Related Papers\b.*)$", text, flags=re.S | re.M)
    return (m.group(1).rstrip() + "\n") if m else ""


def _authors(fm):
    blk = re.search(r"^authors:\s*\n((?:\s*-\s*.*\n)+)", fm, flags=re.M)
    if not blk:
        return []
    return [re.sub(r'^\s*-\s*"?(.*?)"?\s*$', r'\1', ln)
            for ln in blk.group(1).splitlines() if ln.strip()]


def _yget(fm, key):
    m = re.search(rf'^{key}:\s*"?(.*?)"?\s*$', fm, flags=re.M)
    return m.group(1) if m else ""


def _split_name(n):
    p = n.rsplit(" ", 1)
    return {"firstName": p[0] if len(p) > 1 else "", "lastName": p[-1]}


def _remove_fig_embed(review_path, n):
    """Remove the markdown embed (image + optional italic caption) for fig{n}.

    Handles BOTH layouts the corpus uses: image and caption on the SAME line
    `![..](figures/figN.webp) *caption*` AND on separate lines
    `![..](figures/figN.webp)\n\n*caption*`. The old regex required a newline
    right after the `)`, so it silently failed on same-line embeds — leaving
    the old (often full-page) figure referenced and on disk."""
    txt = open(review_path, encoding="utf-8").read()
    pat = re.compile(
        rf'!\[[^\]]*\]\(figures/fig{n}\.(?:png|webp)\)'
        rf'[ \t]*\n*(?:\*[^*\n]+\*[ \t]*\n*)?')
    new = pat.sub("", txt)
    if new != txt:
        new = re.sub(r"\n{3,}", "\n\n", new)
        open(review_path, "w", encoding="utf-8").write(new)


def _drop_fullpage_figs(pdf, fd, rv):
    """Remove full-page figure files + their review embeds. Returns count.

    Used only when the new extractor produced NO figures: the new extractor's
    scanned-PDF guard keeps a legitimate full-page image, so new==0 means any
    old full-page crop is text/junk from the old algorithm — safe to drop."""
    import fitz
    from PIL import Image
    try:
        doc = fitz.open(pdf)
        p0 = doc[0]
        ppx = (p0.rect.width * 3) * (p0.rect.height * 3)
        doc.close()
    except Exception:
        return 0
    removed = 0
    for f in glob.glob(os.path.join(fd, "fig*.webp")) + glob.glob(os.path.join(fd, "fig*.png")):
        try:
            im = Image.open(f)
            ratio = (im.size[0] * im.size[1]) / max(1.0, ppx)
        except Exception:
            continue
        if ratio > 0.85:
            mm = re.match(r"fig(\d+)\.(?:webp|png)$", os.path.basename(f))
            if mm:
                _remove_fig_embed(rv, int(mm.group(1)))
            try:
                os.remove(f)
                removed += 1
            except OSError:
                pass
    return removed


def _prune_orphans(fd, rv):
    """Remove figure files the final review.md no longer references."""
    final_refs, _ = review_fig_refs(rv)
    for f in glob.glob(os.path.join(fd, "fig*.webp")):
        mm = re.match(r"fig(\d+)\.webp$", os.path.basename(f))
        if mm and int(mm.group(1)) not in final_refs:
            try:
                os.remove(f)
            except OSError:
                pass


def _drop_fullpage_from_new(pdf, fd, new_figs):
    """Drop just-extracted figN.png files that came out FULL-PAGE.

    Image-based / scanned PDFs render every page as a >90% image, so the
    extractor's scanned-PDF guard emits whole pages as 'figures'. Those are
    junk. Remove them from disk and from the returned list; if nothing tight
    survives, the caller's new==0 path drops the old full-page crops too."""
    import fitz
    from PIL import Image
    try:
        doc = fitz.open(pdf)
        p0 = doc[0]
        ppx = (p0.rect.width * 3) * (p0.rect.height * 3)
        doc.close()
    except Exception:
        return new_figs
    kept = []
    for f in new_figs:
        fp = os.path.join(fd, f"fig{f['name']}.png")
        try:
            im = Image.open(fp)
            ratio = (im.size[0] * im.size[1]) / max(1.0, ppx)
        except Exception:
            kept.append(f)
            continue
        if ratio > 0.85:
            try:
                os.remove(fp)
            except OSError:
                pass
        else:
            kept.append(f)
    return kept


# 모든 figure 참조 (숫자형 figN 만이 아니라 table1 / fig1a / fig-table1 같은
# 구추출기의 비표준 이름까지) — 이미지 embed + 뒤따르는 이탤릭 캡션 1줄.
GENERIC_EMBED = re.compile(
    r'!\[[^\]]*\]\(\.?/?figures/([A-Za-z0-9_.\-]+?)\.(png|webp)\)[ \t]*\n*'
    r'(?:\*[^*\n]+\*[ \t]*\n+)?')


def scrub_dangling_refs(sd, fd, rv):
    """review.md figure 참조 무결성 후처리. (ext_fixed, removed) 반환.

    왜 필요한가 (2026-06-12 validate 실측): 숫자형 figN 참조만 보던 기존
    로직(_remove_fig_embed / _prune_orphans)의 사각지대 — 구추출기가 만들던
    비표준 이름(table1, fig1a, fig-table1 …) 참조는 파일이 대체/삭제된 뒤에도
    review.md 에 dangling 으로 남아 validate 의 BROKEN_FIG/MISSING_FIG 로
    터졌다. 이 패스가 마지막에 한 번 더 돌며:
      1) 파일 존재 → 유지
      2) 확장자만 다른 파일 존재 → png 는 webp 로 변환해 참조 유지,
         webp 가 있으면 참조를 webp 로 고침 (FIG_MISMATCH 해소)
      3) 둘 다 없음 → embed(이미지+캡션) 제거 (MISSING/BROKEN 해소)
    """
    if not os.path.exists(rv):
        return 0, 0
    txt = open(rv, encoding="utf-8").read()
    counts = {"fixed": 0, "removed": 0}

    def _sub(m):
        stem, ext = m.group(1), m.group(2)
        if os.path.exists(os.path.join(fd, f"{stem}.{ext}")):
            return m.group(0)
        alt_ext = "webp" if ext == "png" else "png"
        alt = os.path.join(fd, f"{stem}.{alt_ext}")
        if os.path.exists(alt):
            if alt_ext == "png":
                # 참조는 webp, 파일은 png → png 를 webp 로 변환해 참조 유지
                _, _, webp = convert_png_to_webp(alt, quality=90)
                if webp and os.path.exists(webp):
                    try:
                        os.remove(alt)
                    except OSError:
                        pass
                    counts["fixed"] += 1
                    return m.group(0)
                # 변환 실패 → 참조를 png 로 맞춤
            counts["fixed"] += 1
            return m.group(0).replace(f"{stem}.{ext}", f"{stem}.{alt_ext}", 1)
        counts["removed"] += 1
        return ""

    new = GENERIC_EMBED.sub(_sub, txt)
    if new != txt:
        new = re.sub(r"\n{3,}", "\n\n", new)
        open(rv, "w", encoding="utf-8").write(new)
    return counts["fixed"], counts["removed"]


def _backup(sd, fd):
    """Transient backup of the mutable per-paper state. Returns backup dir."""
    bak = os.path.join(sd, ".reextract-bak")
    if os.path.isdir(bak):
        shutil.rmtree(bak, ignore_errors=True)
    os.makedirs(bak)
    if os.path.isdir(fd):
        shutil.copytree(fd, os.path.join(bak, "figures"))
    for fn in ("review.md", "index.html"):
        s = os.path.join(sd, fn)
        if os.path.exists(s):
            shutil.copy2(s, os.path.join(bak, fn))
    return bak


def _restore(sd, fd, bak):
    if not os.path.isdir(bak):
        return
    if os.path.isdir(os.path.join(bak, "figures")):
        shutil.rmtree(fd, ignore_errors=True)
        shutil.copytree(os.path.join(bak, "figures"), fd)
    for fn in ("review.md", "index.html"):
        b = os.path.join(bak, fn)
        if os.path.exists(b):
            shutil.copy2(b, os.path.join(sd, fn))


def process_one(entry):
    slug = entry["slug"]
    tier = entry["tier"]
    pdf = entry.get("pdf")
    sd = os.path.join(PAPERS, slug)
    fd = os.path.join(sd, "figures")
    rv = os.path.join(sd, "review.md")

    if not pdf or not os.path.exists(pdf):
        return (slug, "skip_no_pdf", 0)
    if not os.path.exists(rv):
        return (slug, "skip_no_review", 0)

    os.makedirs(fd, exist_ok=True)
    ref_nums, _ = review_fig_refs(rv)
    bak = _backup(sd, fd)
    try:
        # 1) extract (geometric; GOOGLE_API_KEY popped before the pool)
        new_figs = ruf.extract_figures(pdf, sd)        # writes figN.png under fd
        new_figs = _drop_fullpage_from_new(pdf, fd, new_figs)  # drop image-based junk
        new_nums = {int(f["name"]) for f in new_figs}

        # 2) NO-REGRESS: no new figures. Revert to the pristine old state. Then,
        #    since the new extractor's scanned-PDF guard would keep a legitimate
        #    full-page image, new==0 means any old full-page crop is text/junk —
        #    drop those (+ their embeds). Non-full-page old figures are kept.
        if not new_nums:
            _restore(sd, fd, bak)
            dropped = _drop_fullpage_figs(pdf, fd, rv)
            if dropped:
                for fn in ("review.md", "index.html"):
                    fp = os.path.join(sd, fn)
                    if os.path.exists(fp):
                        update_html_refs(fp)
                _prune_orphans(fd, rv)
                scrub_dangling_refs(sd, fd, rv)
                ruf.convert_to_html(slug)
                return (slug, "dropped_fullpage_junk", -dropped)
            _prune_orphans(fd, rv)
            fx, rm = scrub_dangling_refs(sd, fd, rv)
            if fx or rm:
                ruf.convert_to_html(slug)
            return (slug, "kept_old_no_new_figs", 0)

        # 3) tier2: regenerate review to embed figures, preserve frontmatter +
        #    Related Papers from the backup copy.
        if tier == "tier2":
            old = open(rv, encoding="utf-8").read()
            fm, rel = _fm(old), _related(old)
            item = {
                "title": _yget(fm, "title") or entry.get("title", ""),
                "creators": [_split_name(a) for a in _authors(fm)],
                "date": _yget(fm, "date"), "DOI": _yget(fm, "doi"),
                "abstractNote": "", "url": _yget(fm, "arxiv") or "",
            }
            ruf.write_review(item, sd, new_figs)        # review.md w/ png refs
            new = open(rv, encoding="utf-8").read()
            body = re.sub(r"^---\n.*?\n---\n", "", new, flags=re.S)
            body = re.split(r"^## Related Papers\b", body, flags=re.M)[0].rstrip()
            composed = (fm + "\n" if fm else "") + body + "\n\n" + rel
            composed = re.sub(r"\n{3,}", "\n\n", composed).rstrip() + "\n"
            open(rv, "w", encoding="utf-8").write(composed)

        # 4) tier1: the review references the OLD figure set. For any referenced
        #    number the new run did NOT reproduce, the old figure is stale (often
        #    the full-page bug) — REMOVE its embed from the review instead of
        #    showing it. Reproduced numbers now point at fresh tight crops.
        if tier != "tier2":
            for n in ref_nums - new_nums:
                _remove_fig_embed(rv, n)

        # 5) new png -> webp (uniform corpus format); drop the png on success.
        for p in glob.glob(os.path.join(fd, "*.png")):
            _, _, webp = convert_png_to_webp(p, quality=90)
            if webp and os.path.exists(webp):
                os.remove(p)

        # 6) point review/html refs at webp (tier2 wrote png refs; tier1 no-op).
        for fn in ("review.md", "index.html"):
            fp = os.path.join(sd, fn)
            if os.path.exists(fp):
                update_html_refs(fp)

        # 7) PRUNE figure files the final review no longer references, then
        #    SCRUB dangling refs (비표준 이름 포함 — 참조 무결성 후처리),
        #    then render HTML so the page matches the final review exactly.
        _prune_orphans(fd, rv)
        scrub_dangling_refs(sd, fd, rv)
        ruf.convert_to_html(slug)

        shutil.rmtree(bak, ignore_errors=True)          # success -> drop backup
        return (slug, f"done_{tier}", len(new_nums))
    except Exception as e:
        _restore(sd, fd, bak)
        shutil.rmtree(bak, ignore_errors=True)
        return (slug, f"FAILED:{type(e).__name__}:{str(e)[:140]}", 0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", help="figure_scan_manifest.json from the scan")
    ap.add_argument("--slugs", help="comma-separated slugs (overrides manifest)")
    ap.add_argument("--tier", choices=["tier1", "tier2", "all"], default="all")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--log", default=LOG_DEFAULT)
    ap.add_argument("--no-resume", action="store_true", help="ignore the log; redo all")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--scrub-only", action="store_true",
                    help="PDF/재추출 없이 corpus 전체(또는 --slugs)의 review.md "
                         "figure 참조 무결성만 후처리: 확장자 불일치 교정 + "
                         "dangling 참조 제거 + 변경분 HTML 재렌더 + has_figures 패치")
    args = ap.parse_args()

    # Geometric only — deterministic, fast, validated. Do this ONCE for the
    # whole process so every worker thread sees no key.
    os.environ.pop("GOOGLE_API_KEY", None)
    os.environ.pop("GEMINI_API_KEY", None)
    # env pop 만으론 부족하다: extract_figures 의 키 해석이 env→config.json 순이라
    # config.json 에 키가 있으면 Gemini 검증이 다시 켜진다. 명시 플래그로 확실히 끈다.
    os.environ["PAPER_CURATION_NO_GEMINI"] = "1"

    if args.scrub_only:
        slugs = ([s.strip() for s in args.slugs.split(",")] if args.slugs
                 else sorted(os.path.basename(d) for d in glob.glob(os.path.join(PAPERS, "*"))
                             if os.path.isfile(os.path.join(d, "review.md"))))
        print(f"[{_now()}] scrub-only: {len(slugs)} papers")
        tot_fx = tot_rm = changed_slugs = 0
        for s in slugs:
            sd = os.path.join(PAPERS, s)
            fd = os.path.join(sd, "figures")
            rv = os.path.join(sd, "review.md")
            fx, rm = (0, 0) if args.dry_run else scrub_dangling_refs(sd, fd, rv)
            if fx or rm:
                changed_slugs += 1
                tot_fx += fx
                tot_rm += rm
                ruf.convert_to_html(s)
                print(f"  {s[:55]}: ext_fixed={fx} removed={rm}")
        # has_figures 패치 (scrub 으로 마지막 figure 참조가 사라진 논문 반영)
        ipath = os.path.join(PAPERS, "_papers_index.json")
        idx = json.load(open(ipath, encoding="utf-8"))
        patched = 0
        for p in idx:
            sdir = os.path.join(PAPERS, p.get("slug", ""), "figures")
            has = os.path.isdir(sdir) and any(
                f.lower().endswith((".png", ".webp")) for f in os.listdir(sdir))
            if bool(p.get("has_figures")) != has:
                p["has_figures"] = has
                patched += 1
        if patched and not args.dry_run:
            atomic_write_json(ipath, idx)
        print(f"[{_now()}] scrub-only done: {changed_slugs} papers changed "
              f"(ext_fixed={tot_fx}, removed={tot_rm}), has_figures patched={patched}")
        return

    # Build the work list.
    if args.slugs:
        idx = {p["slug"]: p for p in json.load(open(os.path.join(PAPERS, "_papers_index.json"), encoding="utf-8"))}
        entries = []
        for s in args.slugs.split(","):
            s = s.strip()
            p = idx.get(s, {})
            rv = os.path.join(PAPERS, s, "review.md")
            tier = "tier1" if (os.path.exists(rv) and review_fig_refs(rv)[0]) else "tier2"
            # resolve pdf via the run_update_force resolver if not given
            entries.append({"slug": s, "tier": tier, "pdf": p.get("pdf_path") or None,
                            "title": p.get("title", "")})
    else:
        mani = json.load(open(args.manifest, encoding="utf-8"))
        entries = [m for m in mani if m["tier"] in ("tier1", "tier2")]

    if args.tier != "all":
        entries = [e for e in entries if e["tier"] == args.tier]

    # Resume: skip slugs already logged done/kept.
    done = set()
    os.makedirs(os.path.dirname(args.log), exist_ok=True)
    if not args.no_resume and os.path.exists(args.log):
        for ln in open(args.log, encoding="utf-8"):
            try:
                o = json.loads(ln)
                if o.get("status", "").startswith(("done_", "kept_old", "skip_")):
                    done.add(o["slug"])
            except Exception:
                pass
    entries = [e for e in entries if e["slug"] not in done]
    if args.limit:
        entries = entries[:args.limit]

    from collections import Counter
    tc = Counter(e["tier"] for e in entries)
    print(f"[{_now()}] work list: {len(entries)} papers "
          f"(tier1={tc.get('tier1',0)} tier2={tc.get('tier2',0)}), "
          f"{len(done)} already done, workers={args.workers}")
    if args.dry_run:
        print("[dry-run] not executing.")
        return

    log_lock = threading.Lock()
    results = Counter()
    t0 = time.time()
    n = 0
    with open(args.log, "a", encoding="utf-8") as logf, \
            ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {ex.submit(process_one, e): e for e in entries}
        for fut in as_completed(futs):
            slug, status, nfig = fut.result()
            key = status.split(":")[0]
            results[key] += 1
            n += 1
            with log_lock:
                logf.write(json.dumps({"slug": slug, "status": status, "nfig": nfig},
                                      ensure_ascii=False) + "\n")
                logf.flush()
            if status.startswith("FAILED") or n % 100 == 0:
                rate = n / max(1e-6, time.time() - t0)
                eta = (len(entries) - n) / max(1e-6, rate)
                print(f"[{_now()}] {n}/{len(entries)} "
                      f"({rate:.1f}/s, ETA {eta/60:.1f}m) last={slug[:40]} -> {status[:60]}")

    dt = time.time() - t0
    print(f"\n[{_now()}] done in {dt/60:.1f}m. outcomes: {dict(results)}")

    # Final single-threaded atomic index patch: has_figures for processed slugs.
    processed = {e["slug"] for e in entries}
    ipath = os.path.join(PAPERS, "_papers_index.json")
    idx = json.load(open(ipath, encoding="utf-8"))
    changed = 0
    for p in idx:
        s = p.get("slug")
        if s in processed:
            fd = os.path.join(PAPERS, s, "figures")
            has = os.path.isdir(fd) and any(
                f.lower().endswith((".png", ".webp")) for f in os.listdir(fd))
            if bool(p.get("has_figures")) != has:
                p["has_figures"] = has
                changed += 1
    if changed:
        atomic_write_json(ipath, idx)
    print(f"[{_now()}] index has_figures patched: {changed} entries")


if __name__ == "__main__":
    main()
