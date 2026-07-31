"""Backfill a `license` field into each paper's review.md frontmatter.

Source of truth for the public-deploy license gate (figures / ND reviews).
Resolves each paper to a DOI/arXiv id (frontmatter → docs/_zotero_meta.json),
fetches license metadata from OpenAlex (batchable by DOI), normalizes it via
lib/license_util, and writes `license: "<class>"` into the schema-v1 frontmatter
so build_papers_index / build_topic_index / review_to_html can gate.

  PYTHONUTF8=1 python pipeline/backfill_licenses.py --topic humanoid
  PYTHONUTF8=1 python pipeline/backfill_licenses.py            # all papers
  PYTHONUTF8=1 python pipeline/backfill_licenses.py --dry-run --limit 100
  PYTHONUTF8=1 python pipeline/backfill_licenses.py --force

Resilient to malformed frontmatter (regex reader, not YAML). Resumable: papers
that already carry a non-empty `license` are skipped unless --force. arXiv
papers with no CC license on OpenAlex are recorded as `arxiv`; papers that
cannot be resolved to any id are recorded as `unknown` (both figure-gated in the
default strict policy).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request

PIPELINE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PIPELINE_DIR)
sys.path.insert(0, os.path.join(PIPELINE_DIR, "lib"))

from config_loader import (PAPERS_DIR as _PAPERS_DIR, DOCS_DIR,
                           get_papers_index_path, PROJECT_ROOT)
import license_util as L

PAPERS_DIR = str(_PAPERS_DIR)


def _norm_title(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _fm_block(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    return m.group(1) if m else ""


def _fm_get(block, key):
    m = re.search(rf"(?m)^{key}:[ \t]*(.*)$", block)
    return m.group(1).strip().strip('"').strip("'") if m else ""


def _contact_email():
    for key in ("OPENALEX_MAILTO", "UNPAYWALL_EMAIL", "PAPER_CURATION_EMAIL"):
        if os.environ.get(key):
            return os.environ[key].strip()
    try:
        cfg = json.load(open(os.path.join(str(PROJECT_ROOT), "config.json"), encoding="utf-8"))
        for k in ("email", "unpaywall_email", "contact_email"):
            if cfg.get(k):
                return str(cfg[k]).strip()
    except Exception:
        pass
    return "paper-curation@example.com"


def _load_zmeta():
    p = os.path.join(str(DOCS_DIR), "_zotero_meta.json")
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return {}


def _norm_doi(doi):
    if not doi:
        return ""
    d = str(doi).strip().lower()
    for pref in ("https://doi.org/", "http://doi.org/", "doi:"):
        if d.startswith(pref):
            d = d[len(pref):]
    d = d.strip().strip("/")
    if not d.startswith("10.") or any(c in d for c in "|, "):
        return ""
    return d


def _arxiv_id(*vals):
    for v in vals:
        m = re.search(r"(\d{4}\.\d{4,5})", str(v or ""))
        if m:
            return m.group(1)
    return ""


def _queryable_doi(block, zmeta):
    """DOI usable by OpenAlex: frontmatter doi/arxiv → zotero_meta by title."""
    d = _norm_doi(_fm_get(block, "doi"))
    if d:
        return d
    aid = _arxiv_id(_fm_get(block, "arxiv"), _fm_get(block, "doi"))
    if aid:
        return f"10.48550/arxiv.{aid}"
    zm = zmeta.get(_norm_title(_fm_get(block, "title")))
    if isinstance(zm, dict):
        d = _norm_doi(zm.get("doi"))
        if d:
            return d
        aid = _arxiv_id(zm.get("url"))
        if aid:
            return f"10.48550/arxiv.{aid}"
    return ""


def _extract_license(work):
    for loc in (work.get("best_oa_location"), work.get("primary_location")):
        if loc and loc.get("license"):
            return loc["license"]
    for loc in (work.get("locations") or []):
        if loc and loc.get("license"):
            return loc["license"]
    return ""


def _openalex_batch(dois, email):
    qs = urllib.parse.urlencode(
        {"filter": "doi:" + "|".join(dois), "per-page": str(min(len(dois), 200)),
         "select": "doi,primary_location,best_oa_location,locations", "mailto": email},
        safe="|:/.")
    url = "https://api.openalex.org/works?" + qs
    req = urllib.request.Request(url, headers={"User-Agent": f"paper-curation/license ({email})"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.load(resp)
    out = {}
    for w in data.get("results", []):
        d = _norm_doi(w.get("doi"))
        if d:
            out[d] = _extract_license(w)
    return out


def _set_frontmatter_license(text, value):
    m = re.match(r"^(---\n)(.*?)(\n---\n)", text, re.DOTALL)
    if not m:
        return text, False
    head, fm, tail = m.group(1), m.group(2), m.group(3)
    line = f'license: "{value}"'
    if re.search(r"(?m)^license:", fm):
        fm2 = re.sub(r"(?m)^license:.*$", line, fm, count=1)
    elif re.search(r"(?m)^doi:", fm):
        fm2 = re.sub(r"(?m)^(doi:.*)$", r"\1\n" + line, fm, count=1)
    else:
        fm2 = fm + "\n" + line
    return head + fm2 + tail + text[m.end():], True


def _topic_slugs(topic):
    idx = json.load(open(get_papers_index_path(), encoding="utf-8"))
    if not topic:
        return [e["slug"] for e in idx]
    return [e["slug"] for e in idx
            if topic in (e.get("topics") or []) or e.get("primary_topic") == topic]


def run(topic=None, limit=None, dry_run=False, force=False, batch=50):
    email = _contact_email()
    zmeta = _load_zmeta()
    slugs = _topic_slugs(topic)
    if limit:
        slugs = slugs[:limit]
    print(f"[backfill] {len(slugs)} papers (topic={topic or 'ALL'}, dry_run={dry_run}, "
          f"force={force}, zmeta={len(zmeta)}, mailto={email})")

    pending, unresolved = [], []   # pending: (slug, path, text, qdoi)
    have = 0
    for slug in slugs:
        path = os.path.join(PAPERS_DIR, slug, "review.md")
        if not os.path.exists(path):
            continue
        text = open(path, encoding="utf-8").read()
        block = _fm_block(text)
        if _fm_get(block, "license") and not force:
            have += 1
            continue
        qdoi = _queryable_doi(block, zmeta)
        (pending if qdoi else unresolved).append((slug, path, text, qdoi))
    print(f"[backfill] resolvable={len(pending)}  unresolved={len(unresolved)}  already-set={have}")

    doi_to_lic = {}
    uniq = sorted({p[3] for p in pending})
    for i in range(0, len(uniq), batch):
        chunk = uniq[i:i + batch]
        for attempt in (1, 2):
            try:
                doi_to_lic.update(_openalex_batch(chunk, email))
                break
            except Exception as e:
                print(f"  WARN: OpenAlex batch {i//batch} attempt {attempt} failed ({e})")
                time.sleep(5)
        if (i // batch) % 5 == 0:
            print(f"  fetched {min(i+batch,len(uniq))}/{len(uniq)} DOIs", flush=True)
        time.sleep(0.2)

    from collections import Counter
    tally = Counter()
    written = 0
    for group, is_pending in ((pending, True), (unresolved, False)):
        for slug, path, text, qdoi in group:
            if is_pending:
                cls = L.normalize(doi_to_lic.get(qdoi, ""))
                if cls == L.UNKNOWN and qdoi.startswith("10.48550/arxiv"):
                    cls = L.ARXIV
            else:
                zm = zmeta.get(_norm_title(_fm_get(_fm_block(text), "title")))
                url = zm.get("url", "") if isinstance(zm, dict) else ""
                cls = L.OPENREVIEW if "openreview" in url.lower() else L.UNKNOWN
            tally[cls] += 1
            if dry_run:
                continue
            new_text, ok = _set_frontmatter_license(text, cls)
            if ok and new_text != text:
                tmp = path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    f.write(new_text)
                os.replace(tmp, path)
                written += 1

    print("\n[backfill] license distribution:")
    for cls, n in tally.most_common():
        print(f"    {cls:14} {n:5}   (figure_ok={L.figure_public_ok(cls)}, deriv_ok={L.derivative_public_ok(cls)})")
    print(f"[backfill] frontmatter written: {written}{' (dry-run)' if dry_run else ''}")
    return tally


def main():
    ap = argparse.ArgumentParser(description="Backfill license into review.md frontmatter (OpenAlex)")
    ap.add_argument("--topic", default=None, help="limit to a topic (default: all papers)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="re-fetch even if license already set")
    args = ap.parse_args()
    run(topic=args.topic, limit=args.limit, dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    main()
