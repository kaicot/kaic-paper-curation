#!/usr/bin/env python3
"""Resolve PDFs for papers the filename scan can't (ambiguous / short-title /
renamed) by going through the AUTHORITATIVE Zotero attachment via find_pdf.

scan_figures.py matches an index title against Zotero *filenames*, which fails
when several files match, the title is too short/generic, or the paper was
renamed. This tool instead fetches the Zotero collection items, matches each
target paper to its item by title, and calls run_update_force.find_pdf() — which
reads the item's actual attachment (Zotero children API) — to get the one true
PDF. It writes a manifest that reextract_figures.py can consume directly.

Targets default to every paper that still has a full-page figure (the old-algo
bug); pass --slugs to override. Collections come from config.json `zotero`.

Usage:
  PYTHONUTF8=1 python pipeline/resolve_stuck_pdfs.py            # full-page papers
  PYTHONUTF8=1 python pipeline/resolve_stuck_pdfs.py --slugs 042_...,913_...
  # then:  python pipeline/reextract_figures.py --manifest pipeline/_logs/stuck_resolved_manifest.json --no-resume
"""
import json, os, sys, glob, re, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import run_update_force as ruf
import config_loader as cl
from config_loader import PAPERS_DIR

PAPERS = str(PAPERS_DIR)
OUT = os.path.join(os.path.dirname(__file__), "_logs", "stuck_resolved_manifest.json")
FIGREF = re.compile(r"figures/fig\d+\.(png|webp)")
_FP = (1600, 1950, 2150, 2650)  # full-page webp dims at 3x (letter/A4)


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _has_fullpage(slug):
    from PIL import Image
    for f in glob.glob(os.path.join(PAPERS, slug, "figures", "*.webp")):
        try:
            w, h = Image.open(f).size
            if _FP[0] <= w <= _FP[1] and _FP[2] <= h <= _FP[3]:
                return True
        except Exception:
            pass
    return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slugs", help="comma-separated slugs (default: full-page papers)")
    ap.add_argument("--out", default=OUT)
    args = ap.parse_args()

    idx = {p["slug"]: p for p in json.load(open(os.path.join(PAPERS, "_papers_index.json"), encoding="utf-8"))}
    if args.slugs:
        targets = [s.strip() for s in args.slugs.split(",") if s.strip() in idx]
    else:
        targets = [s for s in idx if _has_fullpage(s)]
    print(f"targets: {len(targets)}", flush=True)

    # Fetch every configured collection -> title -> item map (+ 40-char prefix).
    exact, prefix = {}, {}
    for topic in cl.get_collections():
        try:
            ck = cl.get_collection_key(topic)
            if not ck:
                continue
            n = 0
            for it in ruf.fetch_zotero_items(ck):
                k = _norm(it.get("title", ""))
                exact.setdefault(k, it)
                prefix.setdefault(k[:40], it)
                n += 1
            print(f"  collection {topic}: {n} items", flush=True)
        except Exception as e:
            print(f"  collection {topic}: ERROR {str(e)[:80]}", flush=True)
    print(f"fetched {len(exact)} unique titles", flush=True)

    manifest, unresolved, methods = [], [], {}
    for s in targets:
        title = idx[s]["title"]
        it = exact.get(_norm(title)) or prefix.get(_norm(title)[:40])
        if not it:
            unresolved.append((s, "no_zotero_item"))
            continue
        try:
            path, method = ruf.find_pdf(it)
        except Exception as e:
            unresolved.append((s, f"find_pdf_error:{str(e)[:40]}"))
            continue
        methods[method] = methods.get(method, 0) + 1
        if path and os.path.exists(path) and method not in ("no_match", "fuzzy"):
            rv = os.path.join(PAPERS, s, "review.md")
            tier = "tier1" if (os.path.exists(rv) and FIGREF.search(open(rv, encoding="utf-8").read())) else "tier2"
            manifest.append({"slug": s, "tier": tier, "pdf": path, "title": title})
        else:
            unresolved.append((s, f"method={method}"))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    json.dump(manifest, open(args.out, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"\nRESOLVED: {len(manifest)}/{len(targets)}  (methods: {methods})")
    print(f"unresolved: {len(unresolved)}")
    for s, why in unresolved:
        print(f"  {s[:48]}: {why}")
    print(f"\nmanifest -> {args.out}")
    print(f"next: python pipeline/reextract_figures.py --manifest {args.out} --no-resume")


if __name__ == "__main__":
    main()
