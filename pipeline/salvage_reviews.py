#!/usr/bin/env python3
"""Salvage review.md files broken by claude-sonnet-5's tool-use behaviour.

Root cause: WRITE_REVIEW_MODEL was switched to `claude-sonnet-5`, which invokes
the `emit_review` tool but crams the whole XML-tagged review into the single
`essence` field (and leaks a trailing `</invoke>`), leaving every other schema
field empty. write_review then renders essence=<blob>, empty Motivation/…/How,
and default 3/3/3/3/3 scores.

The full, correct content survives in the paper's `.llm_cache/*.json` (the raw
tool `result`). This script re-parses that blob (via run_update_force.
_salvage_review_data) and rebuilds review.md — body sections + the frontmatter
`essence`/`scores` — preserving the (correct) topic frontmatter and the
`## Related Papers` block. Idempotent; backs the original up to
`review.md.broken.bak` once.

Usage:
  python salvage_reviews.py --all            # fix every affected paper
  python salvage_reviews.py --slug 10726_... # one or more slugs
  python salvage_reviews.py --all --dry-run  # report only
"""
import argparse
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_update_force import _salvage_review_data  # noqa: E402

PAPERS_DIR = os.path.join(os.path.dirname(HERE), "docs", "papers")

SECTIONS = """## Essence

{fig_essence}{essence}

## Motivation

- **Known**: {known}
- **Gap**: {gap}
- **Why**: {why}
- **Approach**: {approach}

## Achievement

{fig_achievement}{achievement}

## How

{fig_how}{how}

## Originality

{originality}

## Limitation & Further Study

{limitation}

## Evaluation

- Novelty: {novelty}/5
- Technical Soundness: {technical}/5
- Significance: {significance}/5
- Clarity: {clarity}/5
- Overall: {overall}/5

**총평**: {verdict}
"""


_NARR = ("known", "gap", "why", "approach", "achievement", "how",
         "originality", "limitation", "verdict")


def load_cache_data(slug_dir):
    """Return the most complete salvaged review dict across the paper's LLM
    cache files (broken-blob caches are re-parsed; clean caches pass through)."""
    cache_dir = os.path.join(slug_dir, ".llm_cache")
    if not os.path.isdir(cache_dir):
        return None
    best, best_score = None, -1
    for fn in os.listdir(cache_dir):
        if not fn.endswith(".json"):
            continue
        try:
            res = json.load(open(os.path.join(cache_dir, fn), encoding="utf-8")).get("result")
        except Exception:
            continue
        if not (isinstance(res, dict) and (res.get("essence") or "").strip()):
            continue
        data = _salvage_review_data(res)
        score = sum(1 for k in _NARR if (data.get(k) or "").strip())
        if score > best_score:
            best, best_score = data, score
    return best


def fig_captions(slug_dir):
    p = os.path.join(slug_dir, ".pc_figs.json")
    if not os.path.exists(p):
        return {}
    try:
        return {int(f["name"]): (f.get("caption") or "").strip()
                for f in json.load(open(p, encoding="utf-8"))}
    except Exception:
        return {}


def fig_block(n, slug_dir, caps):
    try:
        n = int(n)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    for ext in ("webp", "png"):
        rel = "figures/fig%d.%s" % (n, ext)
        if os.path.exists(os.path.join(slug_dir, rel)):
            cap = caps.get(n) or ("Figure %d" % n)
            return "![Figure %d](%s)\n\n*%s*\n\n" % (n, rel, cap)
    return ""


def build_body(data, slug_dir):
    caps = fig_captions(slug_dir)

    def s(key):
        return (data.get(key) or "").strip()

    def i(key):
        try:
            return int(data.get(key))
        except (TypeError, ValueError):
            return 3

    return SECTIONS.format(
        fig_essence=fig_block(data.get("fig_essence", 0), slug_dir, caps),
        essence=s("essence"),
        known=s("known"), gap=s("gap"), why=s("why"), approach=s("approach"),
        fig_achievement=fig_block(data.get("fig_achievement", 0), slug_dir, caps),
        achievement=s("achievement"),
        fig_how=fig_block(data.get("fig_how", 0), slug_dir, caps),
        how=s("how"), originality=s("originality"), limitation=s("limitation"),
        novelty=i("novelty"), technical=i("technical"),
        significance=i("significance"), clarity=i("clarity"), overall=i("overall"),
        verdict=s("verdict"),
    ).strip() + "\n"


def fix_frontmatter(head, data):
    """Fix the malformed `essence` value + score fields inside the frontmatter."""
    essence = (data.get("essence") or "").strip()
    if essence:
        head = re.sub(r'(?m)^essence:\s*.*$', "essence: " + json.dumps(essence, ensure_ascii=False), head)
    for key in ("novelty", "technical", "significance", "clarity", "overall"):
        try:
            v = int(data.get(key))
        except (TypeError, ValueError):
            continue
        head = re.sub(r'(?m)^(  %s:\s*)\d+\s*$' % key, r'\g<1>%d' % v, head)
    try:
        head = re.sub(r'(?m)^(score:\s*)\d+\s*$', r'\g<1>%d' % int(data.get("overall")), head)
    except (TypeError, ValueError):
        pass
    return head


def blob_from_review(content):
    """Fallback when no cache exists: the malformed body dumps the whole tagged
    blob into the ## Essence section — recover it from there."""
    m = re.search(r'## Essence\s*\n(.*?)\n## Motivation', content, re.S)
    if not m:
        return None
    ess = re.sub(r'^\s*!\[[^\]]*\]\([^)]*\)\s*\n+\*[^*]*\*\s*\n*', '', m.group(1), flags=re.S)
    if "</essence>" in ess or "<known>" in ess:
        return _salvage_review_data({"essence": ess.strip()})
    return None


_LEAK_RE = re.compile(
    r'</(?:essence|known|gap|why|approach|achievement|how|originality|'
    r'limitation|verdict)>|<parameter name=|</invoke>')


def body_is_broken(content):
    idx = content.find("## Essence")
    body = content[idx:] if idx >= 0 else content
    if _LEAK_RE.search(body):
        return True
    m = re.search(r'(?m)^-\s*\*\*Known\*\*:[ \t]*(.*)$', body)
    return bool(m and not m.group(1).strip())


def salvage(slug_dir, dry_run=False):
    review = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review):
        return "no-review"
    content = open(review, encoding="utf-8").read()
    fm_broken = bool(re.search(r'(?m)^essence:\s*".*</essence>', content))
    bd_broken = body_is_broken(content)
    if not fm_broken and not bd_broken:
        return "clean"

    data = load_cache_data(slug_dir) or blob_from_review(content)
    if not data:
        return "no-cache"

    if "## Essence" not in content:
        return "no-essence-header"
    head = content[:content.index("## Essence")]
    tail = ""
    if "## Related Papers" in content:
        tail = content[content.index("## Related Papers"):]

    head = fix_frontmatter(head, data)
    body = build_body(data, slug_dir) if bd_broken else \
        content[content.index("## Essence"):(content.index("## Related Papers")
                                             if tail else len(content))].strip() + "\n"

    new = head.rstrip() + "\n\n" + body.strip() + "\n"
    if tail:
        new += "\n" + tail.strip() + "\n"

    if dry_run:
        return "WOULD-FIX(%s%s)" % ("body," if bd_broken else "", "fm" if fm_broken else "")

    bak = review + ".broken.bak"
    if not os.path.exists(bak):
        os.rename(review, bak)
    open(review, "w", encoding="utf-8").write(new)
    return "FIXED(%s%s)" % ("body," if bd_broken else "", "fm" if fm_broken else "")


def affected_slugs():
    out = []
    for name in sorted(os.listdir(PAPERS_DIR)):
        d = os.path.join(PAPERS_DIR, name)
        rv = os.path.join(d, "review.md")
        if not os.path.isdir(d) or not os.path.exists(rv):
            continue
        try:
            c = open(rv, encoding="utf-8").read()
        except Exception:
            continue
        if (re.search(r'(?m)^essence:\s*".*</essence>', c) or _LEAK_RE.search(c)
                or re.search(r'(?m)^-\s*\*\*Known\*\*:[ \t]*$', c)):
            out.append(name)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", action="append", default=[])
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    slugs = list(args.slug)
    if args.all:
        slugs = affected_slugs()
    if not slugs:
        print("no slugs (use --slug or --all)")
        return

    from collections import Counter
    tally = Counter()
    for s in slugs:
        d = s if os.path.isabs(s) else os.path.join(PAPERS_DIR, s)
        r = salvage(d, dry_run=args.dry_run)
        tally[r.split("(")[0]] += 1
        if r not in ("clean",) and not r.startswith("FIXED"):
            print("  %-14s %s" % (r, os.path.basename(d)))
    print("총 %d편:" % len(slugs), dict(tally))


if __name__ == "__main__":
    main()
