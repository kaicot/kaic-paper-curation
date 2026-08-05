"""
Obsidian MOC (Map of Content) 자동 생성.

_insights.json → MOC_Insights.md (교차 트렌드, 갭, 정책 시사점)
_papers_index.json → MOC_Categories.md (카테고리별 논문 wikilink 목록)

Usage:
  PYTHONUTF8=1 python pipeline/generate_moc.py --topic ai4s
  PYTHONUTF8=1 python pipeline/generate_moc.py --topic scisci
"""

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pipeline.config_loader import PAPERS_DIR as _PAPERS_DIR, get_topic_dir
from pipeline.lib.atomic_io import atomic_write_text

PAPERS_DIR = str(_PAPERS_DIR)



def _optional_json(path, expected_type):
    source = Path(path)
    if not source.exists():
        return expected_type()
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, expected_type):
        raise ValueError(f"invalid retained artifact: {source.name}")
    return value


def _retained_moc(topic, topic_dir):
    """Render deterministic retained evidence when legacy insights are absent."""
    summaries = _optional_json(Path(topic_dir) / "_category_summaries.json", list)
    connections = _optional_json(Path(topic_dir) / "_paper_connections.json", dict)
    timeline = _optional_json(Path(topic_dir) / "_timeline_narrative.json", dict)

    summary_by_category = {}
    slug_meta = {}
    for row in summaries:
        if not isinstance(row, dict):
            raise ValueError("invalid category summary row")
        category = str(row.get("category", "")).strip()
        if not category:
            continue
        summary_by_category[category] = row
        for paper in row.get("papers", []):
            if not isinstance(paper, dict):
                continue
            slug = str(paper.get("slug", "")).strip()
            if slug:
                slug_meta[slug] = {
                    "category": category,
                    "title": str(paper.get("title", "")).strip() or slug,
                }

    analyses = timeline.get("category_analyses", {})
    if not isinstance(analyses, dict):
        raise ValueError("invalid timeline category analyses")
    categories = sorted(set(summary_by_category) | set(analyses))
    lines = [
        "---",
        "schema: moc-insights-v1",
        "---",
        "",
        f"# Research Insights — {topic}",
        "",
        "*Source: moc-insights-v1 (deterministic retained-artifact fallback)*",
        (
            f"*Inputs: category_summaries={len(summary_by_category)}; "
            f"connection_records={sum(len(v) for v in connections.values() if isinstance(v, list))}; "
            f"timeline_categories={len(analyses)}*"
        ),
        "",
        "## Category Signals",
        "",
    ]
    if not categories:
        lines.append("No retained category signals are available.")
    for category in categories:
        summary = summary_by_category.get(category, {})
        analysis = analyses.get(category, {})
        if not isinstance(analysis, dict):
            analysis = {}
        papers = summary.get("papers", []) if isinstance(summary, dict) else []
        count = len(papers) if isinstance(papers, list) else 0
        description = ""
        if isinstance(summary, dict):
            description = str(
                summary.get("description_ko")
                or summary.get("description")
                or ""
            ).strip()
        lines.extend(["", f"### {category} ({count} papers)"])
        lines.append(description or "No retained category description is available.")
        current = str(analysis.get("current_state_summary", "")).strip()
        if current:
            lines.append(f"- Current state: {current}")
        themes = analysis.get("sub_themes", [])
        if isinstance(themes, list):
            sortable = [theme for theme in themes if isinstance(theme, dict)]
            for theme in sorted(sortable, key=lambda value: str(value.get("name", ""))):
                name = str(theme.get("name", "")).strip()
                if not name:
                    continue
                status = str(theme.get("status", "RETAINED")).strip() or "RETAINED"
                start = str(theme.get("start", "?")).strip() or "?"
                end = str(theme.get("end", "?")).strip() or "?"
                paper_count = int(theme.get("paper_count", 0))
                lines.append(
                    f"- {status}: {name} ({start}–{end}; {paper_count} papers)"
                )

    edge_groups = {}
    seen_edges = set()
    unmapped = 0
    for source in sorted(connections):
        records = connections[source]
        if not isinstance(records, list):
            raise ValueError("invalid connection records")
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("invalid connection row")
            target = str(record.get("slug", "")).strip()
            relation = str(record.get("relation", "related")).strip() or "related"
            reasons = record.get("reasons")
            if isinstance(reasons, list):
                reason = "; ".join(sorted(str(value) for value in reasons))
            else:
                reason = str(record.get("reason", "")).strip()
            key = (min(source, target), max(source, target), relation, reason)
            if not target or key in seen_edges:
                continue
            seen_edges.add(key)
            source_meta = slug_meta.get(source)
            target_meta = slug_meta.get(target)
            if (
                not source_meta
                or not target_meta
                or source_meta["category"] == target_meta["category"]
            ):
                unmapped += 1
                continue
            pair = tuple(sorted((source_meta["category"], target_meta["category"])))
            edge_groups.setdefault(pair, []).append(
                (source, target, relation, reason)
            )

    lines.extend(["", "## Retained Cross-category Connections", ""])
    ranked_pairs = sorted(
        edge_groups.items(),
        key=lambda item: (-len(item[1]), item[0][0], item[0][1]),
    )
    selected_pairs = ranked_pairs[:12]
    if not selected_pairs:
        lines.append("No mapped retained cross-category connections are available.")
    omitted_edges = 0
    for pair, edges in selected_pairs:
        ordered = sorted(edges)
        lines.extend(["", f"### {pair[0]} × {pair[1]} ({len(ordered)} retained links)"])
        for source, target, relation, reason in ordered[:5]:
            source_title = slug_meta[source]["title"]
            target_title = slug_meta[target]["title"]
            lines.append(
                f"- [[papers/{source}/review|{source_title}]] — {relation} → "
                f"[[papers/{target}/review|{target_title}]]"
                + (f": {reason}" if reason else "")
            )
        omitted_edges += max(0, len(ordered) - 5)
    omitted_pairs = max(0, len(ranked_pairs) - 12)
    lines.extend(
        [
            "",
            (
                f"*Omitted: unmapped_or_same_category={unmapped}; "
                f"category_pairs={omitted_pairs}; evidence_rows={omitted_edges}.*"
            ),
            "",
            "## Timeline Overview",
            "",
        ]
    )
    executive = str(timeline.get("executive_summary_ko", "")).strip()
    lines.append(executive or "No retained timeline overview is available.")
    text = "\n".join(lines).rstrip() + "\n"
    output = Path(topic_dir) / "MOC_Insights.md"
    atomic_write_text(output, text)
    return {
        "artifact": str(output),
        "renderer": "moc-insights-v1",
        "status": "ok",
    }


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def generate_moc_insights(topic, topic_dir):
    """Always render the safe retained-artifact MOC."""
    result = _retained_moc(topic, topic_dir)
    log(f"  Generated: {result['artifact']} ({result['renderer']})")
    return result


def generate_moc_categories(topic, topic_dir):
    """_papers_index.json → MOC_Categories.md"""
    with open(os.path.join(PAPERS_DIR, "_papers_index.json"), "r", encoding="utf-8") as f:
        all_papers = json.load(f)

    topic_papers = [p for p in all_papers if topic in p.get("topics", [])]

    # Group by primary_category
    cat_papers = defaultdict(list)
    for p in topic_papers:
        cls = p.get("classifications", {}).get(topic, {})
        pc = cls.get("primary_category", "Other")
        cat_papers[pc].append(p)

    lines = [
        f"# Categories — {topic}",
        f"",
        f"*{len(topic_papers)}편 | {len(cat_papers)} 카테고리*",
        f"",
    ]

    for cat_name in sorted(cat_papers.keys()):
        papers = sorted(cat_papers[cat_name], key=lambda x: -x.get("score", 0))
        lines.append(f"## {cat_name} ({len(papers)}편)")
        lines.append("")
        for p in papers:
            slug = p.get("slug", "")
            title = p.get("title", slug)[:80]
            score = p.get("score", 0)
            lines.append(f"- [[papers/{slug}/review|{title}]] — score: {score}")
        lines.append("")

    moc_path = os.path.join(topic_dir, "MOC_Categories.md")
    with open(moc_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    log(f"  Written: {moc_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate Obsidian MOC files")
    parser.add_argument("--topic", default="ai4s")
    args = parser.parse_args()

    topic = args.topic
    topic_dir = str(get_topic_dir(topic))

    log(f"Generating MOC for {topic}...")
    generate_moc_insights(topic, topic_dir)
    generate_moc_categories(topic, topic_dir)
    log("Done!")


if __name__ == "__main__":
    main()
