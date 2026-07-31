#!/usr/bin/env python3
"""Corpus-wide audit of review.md quality defects (read-only report).

Scans every docs/papers/*/review.md for the defect classes surfaced during the
claude-sonnet-5 incident and downstream fixes:

  leaked-tags   : raw schema/tool tags left in prose (</essence>, </known>,
                  <parameter name=, </invoke>) — sonnet-5 field-cramming residue
  placeholder   : <UNKNOWN>/<unknown> literals (render as invisible HTML tags)
  stray-tags    : other non-whitelisted <tag> tokens that render invisibly
                  (e.g. ML tokens <EOS>, or leaked field tags)
  empty-section : a narrative section (Known/Gap/Why/Approach/Achievement/How/
                  Originality/Limitation) or 총평 rendered empty
  default-scores: Evaluation all 3/5 (sonnet-5 empty-tool fallback signature)
  missing-figure: ![..](figures/figN.ext) whose file is absent on disk

For each affected paper it notes whether the content is RECOVERABLE from the
LLM cache (a cached response carrying real narrative) so a salvage/regen can be
targeted.

Usage: python audit_reviews.py [--json out.json] [--limit N]
"""
import argparse
import json
import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
PAPERS = os.path.join(os.path.dirname(HERE), "docs", "papers")

NARR = ["known", "gap", "why", "approach", "achievement", "how",
        "originality", "limitation", "verdict"]
LEAK_RE = re.compile(
    r'</(?:essence|known|gap|why|approach|achievement|how|originality|'
    r'limitation|verdict)>|<parameter name=|</invoke>')
PLACEHOLDER_RE = re.compile(r'<UNKNOWN>', re.I)
# HTML tags legitimately emitted/allowed in review prose
ALLOWED = {
    "a", "b", "br", "code", "em", "strong", "i", "u", "s", "sup", "sub", "img",
    "span", "table", "thead", "tbody", "tr", "td", "th", "ul", "ol", "li", "p",
    "h1", "h2", "h3", "h4", "h5", "blockquote", "figure", "figcaption", "div",
    "hr", "pre", "details", "summary", "small", "mark", "kbd", "abbr", "wbr",
}
STRAY_RE = re.compile(r'</?\s*([A-Za-z][A-Za-z0-9_-]*)(?:\s[^<>]*)?>')
# Describing-context around a token: when present we treat <foo> as legit
# subject matter (the paper defines/uses that token) rather than a leaked tag.
_TOKENISH = re.compile(
    r'토큰|태그|태깅|특수|구분|형식|형태|블록|플레이스홀더|시퀀스|출력|생성|설정|신호|'
    r'token|tag|placeholder|prompt|프롬프트|schema|스키마|special|boxed|sequence', re.I)


def strip_frontmatter(md):
    while md.startswith("---"):
        lines = md.split("\n")
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
        if end is None:
            break
        md = "\n".join(lines[end + 1:]).lstrip("\n")
    return md


def split_sections(body):
    out = {}
    for chunk in re.split(r'^##\s+', body, flags=re.M)[1:]:
        parts = chunk.split("\n", 1)
        out[parts[0].strip()] = (parts[1] if len(parts) > 1 else "").strip()
    return out


def cache_recoverable(slug_dir):
    cdir = os.path.join(slug_dir, ".llm_cache")
    if not os.path.isdir(cdir):
        return False
    for fn in os.listdir(cdir):
        if not fn.endswith(".json"):
            continue
        try:
            r = json.load(open(os.path.join(cdir, fn), encoding="utf-8")).get("result")
        except Exception:
            continue
        if not isinstance(r, dict):
            continue
        blob = " ".join(v for v in r.values() if isinstance(v, str))
        # real narrative present as a field or as a tagged/parameter blob
        if any((r.get(k) or "").strip() for k in ("known", "gap", "achievement", "how")):
            return True
        if any(t in blob for t in ("</known>", "<gap>", "<parameter name=", "</achievement>")):
            return True
    return False


def audit_one(slug_dir):
    review = os.path.join(slug_dir, "review.md")
    if not os.path.exists(review):
        return None
    md = open(review, encoding="utf-8").read()
    body = strip_frontmatter(md)
    issues = {}

    if LEAK_RE.search(body):
        issues["leaked-tags"] = LEAK_RE.findall(body)[:4]
    if PLACEHOLDER_RE.search(body):
        issues["placeholder"] = True
    _nocode = re.sub(r'```[\s\S]*?```', '', body)     # drop fenced code examples
    _nocode = re.sub(r'`[^`]*`', '', _nocode)         # drop inline code
    strays = []
    for _m in STRAY_RE.finditer(_nocode):
        _n = _m.group(1)
        if _n.lower() in ALLOWED:
            continue
        _win = _nocode[max(0, _m.start() - 45):_m.end() + 45]
        if _TOKENISH.search(_win):                    # described token → not a leak
            continue
        strays.append(_n)
    strays = sorted(set(strays))
    if strays:
        issues["stray-tags"] = strays[:6]

    secs = split_sections(body)
    empty = []
    mot = secs.get("Motivation", "")
    for label in ("Known", "Gap", "Why", "Approach"):
        m = re.search(r'\*\*' + label + r'\*\*\s*:\s*(.*)', mot)
        if not (m and m.group(1).strip()):
            empty.append(label)
    for sec in ("Achievement", "How", "Originality", "Limitation & Further Study"):
        b = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', secs.get(sec, ''))
        b = re.sub(r'\*[^*]+\*', '', b).strip()          # drop italic captions
        if len(b) < 10:
            empty.append(sec.split(" ")[0])
    vm = re.search(r'\*\*총평\*\*\s*:\s*(.*)', body)
    if not (vm and vm.group(1).strip()):
        empty.append("총평")
    if empty:
        issues["empty-section"] = empty

    sc = re.findall(r'(?:Novelty|Technical Soundness|Significance|Clarity|Overall)\s*:\s*(\d)/5', body)
    if len(sc) >= 5 and all(x == "3" for x in sc):
        issues["default-scores"] = True

    refs = re.findall(r'!\[[^\]]*\]\((figures/[^)]+)\)', body)
    missing = [r for r in refs if not os.path.exists(os.path.join(slug_dir, r))]
    if missing:
        issues["missing-figure"] = missing

    if not issues:
        return None
    return {"slug": os.path.basename(slug_dir),
            "issues": issues,
            "recoverable": cache_recoverable(slug_dir)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    results = []
    for name in sorted(os.listdir(PAPERS)):
        d = os.path.join(PAPERS, name)
        if not (os.path.isdir(d) and re.match(r'^\d{3,}_', name)):
            continue
        r = audit_one(d)
        if r:
            results.append(r)

    cat = Counter()
    rec = Counter()
    for r in results:
        for k in r["issues"]:
            cat[k] += 1
            rec[(k, r["recoverable"])] += 1
    print("=== corpus review.md audit ===")
    print("papers with >=1 issue: %d" % len(results))
    print("\nby category (recoverable-from-cache / not):")
    for k in sorted(cat, key=lambda x: -cat[x]):
        print("  %-16s %4d   (recoverable %d / no-cache %d)"
              % (k, cat[k], rec[(k, True)], rec[(k, False)]))
    print("\nsample (first %d):" % args.limit)
    for r in results[:args.limit]:
        tags = "; ".join("%s=%s" % (k, v if v is True else ",".join(map(str, v))[:40])
                         for k, v in r["issues"].items())
        print("  %-58s [%s] %s" % (r["slug"][:58], "REC" if r["recoverable"] else "---", tags))
    if args.json:
        json.dump(results, open(args.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print("\nwrote %s (%d papers)" % (args.json, len(results)))


if __name__ == "__main__":
    main()
