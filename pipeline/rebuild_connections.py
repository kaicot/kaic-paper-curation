"""Rebuild the bidirectional, per-target-deduped connection view + re-render.

Connections are stored in two layers (see ``lib/connections.py``):

  * ``docs/papers/_global_connections.json`` — the **raw, directional** store
    (one ``source_topic``-tagged A→B edge per generated connection). This is the
    durable accumulator written by every ``extract_insights`` / ``topic_modeling``
    run through ``sync_topic_connections``.
  * ``docs/<topic>/_paper_connections.json`` — the **consumer view** every
    renderer reads: the *bidirectional + per-target-deduped* projection of the
    global store (each A→B implies B→A with the relation flipped, and a paper
    connected for several reasons appears once with all reasons listed).

Normal pipeline runs already produce this automatically — ``sync_topic_connections``
applies the transform on every run, and ``review_to_html`` renders each reason
with its own relation badge and turns every ``[NNN]`` reference into a link. So
**both paper-curation and paper-curio emit connections in exactly this shape going
forward.** This script is the reproducible, LLM-free way to:

  * re-apply the transform to the existing global store (no re-generation), and
  * re-render every affected paper page (and, by default, the D3 networks),

e.g. after editing the transform/render logic or to reproduce the state on demand.

Usage:
  PYTHONUTF8=1 python pipeline/rebuild_connections.py                  # all topics: data + pages + networks
  PYTHONUTF8=1 python pipeline/rebuild_connections.py --topics ai4s scisci
  PYTHONUTF8=1 python pipeline/rebuild_connections.py --no-render      # rebuild topic JSON only
  PYTHONUTF8=1 python pipeline/rebuild_connections.py --no-networks    # data + pages, skip networks
  PYTHONUTF8=1 python pipeline/rebuild_connections.py --dry-run        # report only, write nothing
"""

import argparse
import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from lib.connections import (              # noqa: E402
    load_global_connections, make_bidirectional_deduped, filter_for_topic,
    build_slug_resolver, load_index_slugs,
)

ROOT = os.path.dirname(_HERE)
DOCS = os.path.join(ROOT, "docs")
PAPERS = os.path.join(DOCS, "papers")
INDEX = os.path.join(PAPERS, "_papers_index.json")


def discover_topics():
    """Topics = docs/<dir> that currently hold a _paper_connections.json."""
    topics = []
    for d in sorted(os.listdir(DOCS)):
        if os.path.exists(os.path.join(DOCS, d, "_paper_connections.json")):
            topics.append(d)
    return topics


def topic_slug_map():
    """topic -> set(slugs) from _papers_index.json's per-paper ``topics``."""
    with open(INDEX, encoding="utf-8") as f:
        idx = json.load(f)
    m = {}
    for p in idx:
        for t in (p.get("topics") or []):
            m.setdefault(t, set()).add(p["slug"])
    return m


def rebuild_data(topics, dry_run=False):
    """Rebuild each topic's _paper_connections.json from the global store.

    Returns the set of source-key slugs (papers that have ≥1 connection) across
    all rebuilt topics — i.e. exactly the pages whose connection box can change.
    """
    # Self-heal: remap renumbered endpoints to current slugs, prune deleted/
    # ambiguous ones — so orphaned slug-titles disappear from every rebuilt view.
    resolve = build_slug_resolver(load_index_slugs())
    bidi = make_bidirectional_deduped(load_global_connections(), resolve=resolve)
    st = resolve.stats
    print(f"  normalized: {len(st['remapped'])} remapped (renumbered), "
          f"{len(st['pruned'])} pruned (deleted/ambiguous)")
    slug_map = topic_slug_map()
    source_keys = set()
    for topic in topics:
        slugs = slug_map.get(topic)
        if not slugs:
            print(f"  [skip] {topic}: no slugs in index")
            continue
        out = filter_for_topic(bidi, slugs)
        n_links = sum(len(v) for v in out.values())
        source_keys |= set(out)
        fp = os.path.join(DOCS, topic, "_paper_connections.json")
        if dry_run:
            print(f"  [dry-run] {topic}: would write {len(out)} papers, {n_links} links")
        else:
            tmp = fp + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(out, f, ensure_ascii=False, indent=2)
            os.replace(tmp, fp)
            print(f"  {topic}: {len(out)} papers, {n_links} links -> {fp}")
    return source_keys


def render_pages(source_keys, dry_run=False):
    """Re-render every page that is a connection source (has a review.md)."""
    slugs = sorted(s for s in source_keys
                   if os.path.exists(os.path.join(PAPERS, s, "review.md")))
    if dry_run:
        print(f"  [dry-run] would re-render {len(slugs)} pages")
        return
    from review_to_html import _run_review_to_html
    print(f"  re-rendering {len(slugs)} pages ...")
    _run_review_to_html(slugs=slugs)


def inject_related(topics, source_keys, dry_run=False):
    """Refresh only the "## Related Papers" markdown block in each review.md.

    This is the block paper-curio's reviewHtml.ts and Obsidian render (paper-
    curation's own web pages strip it and use the connections-box instead). We
    swap *only* that section so frontmatter and body stay byte-identical — never
    routed through inject_into_review, which would also rewrite the frontmatter.
    """
    import re as _re
    from inject_frontmatter import build_related_section

    # union of all topic connection dicts so a paper's full edge list is visible
    conns = {}
    for topic in topics:
        fp = os.path.join(DOCS, topic, "_paper_connections.json")
        if os.path.exists(fp):
            with open(fp, encoding="utf-8") as f:
                conns.update(json.load(f))

    slugs = sorted(s for s in source_keys
                   if os.path.exists(os.path.join(PAPERS, s, "review.md")))
    if dry_run:
        print(f"  [dry-run] would refresh ## Related Papers in {len(slugs)} review.md")
        return
    pat = _re.compile(r"\n## Related Papers\n[\s\S]*?(?=\n## |\Z)")
    changed = 0
    for s in slugs:
        md_path = os.path.join(PAPERS, s, "review.md")
        with open(md_path, encoding="utf-8") as f:
            content = f.read()
        section = build_related_section(s, conns)  # "\n## Related Papers\n...\n" or ""
        new = pat.sub("", content).rstrip()
        if section:
            new += "\n" + section
        new += "\n" if not new.endswith("\n") else ""
        if new != content:
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(new)
            changed += 1
    print(f"  refreshed ## Related Papers in {changed}/{len(slugs)} review.md")


def regen_networks(topics, dry_run=False):
    """Regenerate each topic's D3 network (undirected dedup of bidi edges)."""
    script = os.path.join(_HERE, "generate_network.py")
    for topic in topics:
        if dry_run:
            print(f"  [dry-run] would regenerate network: {topic}")
            continue
        r = subprocess.run([sys.executable, script, "--topic", topic],
                           capture_output=True, text=True)
        tail = (r.stdout or r.stderr).strip().splitlines()[-1:] or [""]
        print(f"  network {topic}: {'OK' if r.returncode == 0 else 'FAIL'} — {tail[0]}")


def main():
    ap = argparse.ArgumentParser(description="Rebuild bidirectional connection view + re-render")
    ap.add_argument("--topics", nargs="+", default=None,
                    help="Topic dirs to rebuild (default: all with _paper_connections.json)")
    ap.add_argument("--no-render", action="store_true", help="Skip page re-rendering")
    ap.add_argument("--no-inject", action="store_true",
                    help="Skip refreshing the ## Related Papers block in review.md (paper-curio/Obsidian)")
    ap.add_argument("--no-networks", action="store_true", help="Skip network regeneration")
    ap.add_argument("--dry-run", action="store_true", help="Report only; write nothing")
    args = ap.parse_args()

    topics = args.topics or discover_topics()
    print(f"Topics: {', '.join(topics)}")

    print("[1/4] Rebuilding topic connection files (bidirectional + deduped)...")
    source_keys = rebuild_data(topics, dry_run=args.dry_run)

    if args.no_render:
        print("[2/4] Page re-render: skipped (--no-render)")
    else:
        print("[2/4] Re-rendering paper pages (equal reason badges + linked [NNN] refs)...")
        render_pages(source_keys, dry_run=args.dry_run)

    if args.no_inject:
        print("[3/4] ## Related Papers refresh: skipped (--no-inject)")
    else:
        print("[3/4] Refreshing ## Related Papers in review.md (paper-curio/Obsidian)...")
        inject_related(topics, source_keys, dry_run=args.dry_run)

    if args.no_networks:
        print("[4/4] Network regeneration: skipped (--no-networks)")
    else:
        print("[4/4] Regenerating networks...")
        regen_networks(topics, dry_run=args.dry_run)

    print("DONE" + (" (dry-run)" if args.dry_run else ""))


if __name__ == "__main__":
    main()
