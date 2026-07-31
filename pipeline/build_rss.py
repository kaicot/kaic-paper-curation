"""
Atom 1.0 feed builder for paper-curation topics.

토픽의 _new_classification.json 에 배정된 논문을 papers/_papers_index.json 메타와
조인해 날짜 내림차순 최신 50편을 Atom 피드(docs/{topic}/feed.xml)로 출력한다.
review_to_html → build_topic_index 직후 run_update_force 가 체이닝한다.

Usage: PYTHONUTF8=1 python build_rss.py <topic>
  e.g. PYTHONUTF8=1 python build_rss.py humanoid
       PYTHONUTF8=1 python build_rss.py ai4s
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from xml.sax.saxutils import escape, quoteattr

from config_loader import PAPERS_DIR as _PAPERS_DIR, get_topic_dir
from lib.dateutil import normalize_date
from lib.atomic_io import atomic_write_text

PAPERS_DIR = str(_PAPERS_DIR)

# Canonical Cloudflare 도메인 (build_topic_index 의 OG 메타와 동일한 SITE 상수).
SITE = "https://paper-curation.jehyunlee.dev"
MAX_ENTRIES = 50


def _date_sort_key(date_str):
    """YYYY.MM / YYYY 문자열을 (year, month) 정수 튜플로. 내림차순 정렬용."""
    nd = normalize_date(date_str)
    m = re.match(r"^(\d{4})\.(\d{2})$", nd)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    m = re.match(r"^(\d{4})$", nd)
    if m:
        return (int(m.group(1)), 0)
    return (0, 0)


def _to_rfc3339(date_str, review_date=""):
    """논문 date(YYYY.MM/YYYY) 를 Atom updated 용 RFC3339 로. 없으면 review_date fallback."""
    nd = normalize_date(date_str)
    m = re.match(r"^(\d{4})\.(\d{2})$", nd)
    if m:
        return f"{m.group(1)}-{m.group(2)}-01T00:00:00Z"
    m = re.match(r"^(\d{4})$", nd)
    if m:
        return f"{m.group(1)}-01-01T00:00:00Z"
    # date 가 비었거나 파싱 불가 → 리뷰 작성일(YYYY-MM-DD) 사용
    if review_date:
        m = re.match(r"^(\d{4}-\d{2}-\d{2})", review_date)
        if m:
            return f"{m.group(1)}T00:00:00Z"
    return None


def _collect_topic_papers(topic, topic_dir, papers_index):
    """_new_classification.json 배정 슬러그를 papers_index 메타와 조인.

    반환: [{slug, title, url, updated, summary, category, authors}, ...] (미정렬)
    조인 실패(메타 없음)·날짜 파싱 실패 항목은 조용히 skip.
    """
    by_slug = {p["slug"]: p for p in papers_index}

    # 1순위: _new_classification.json assignments (slug + primary_category)
    cls_path = os.path.join(topic_dir, "_new_classification.json")
    slug_cats = {}  # slug -> primary_category
    if os.path.exists(cls_path):
        with open(cls_path, encoding="utf-8") as f:
            cls_data = json.load(f)
        for a in cls_data.get("assignments", []):
            slug_cats[a["slug"]] = a.get("primary_category", "")
    else:
        # Fallback: classification 파일 부재 시 papers_index 의 topics 멤버십으로 대체
        for p in papers_index:
            if topic in (p.get("topics") or []):
                cls = p.get("classifications", {}).get(topic, {})
                slug_cats[p["slug"]] = cls.get("primary_category", "")

    now_rfc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = []
    for slug, cat in slug_cats.items():
        p = by_slug.get(slug)
        if not p:
            continue  # 인덱스에 없는 슬러그 (렌더 전/삭제됨) — skip
        updated = _to_rfc3339(p.get("date", ""), p.get("review_date", "")) or now_rfc
        # primary_category: assignment 우선, 없으면 papers_index classifications fallback
        category = cat or p.get("classifications", {}).get(topic, {}).get("primary_category", "")
        entries.append({
            "slug": slug,
            "title": p.get("title", slug),
            "url": f"{SITE}/papers/{slug}/",
            "updated": updated,
            "sort_key": _date_sort_key(p.get("date", "")),
            "summary": p.get("essence", ""),
            "category": category,
            "authors": p.get("authors") or [],
        })
    return entries


def _build_atom(topic, entries):
    """entries 리스트를 Atom 1.0 XML 문자열로 직렬화 (모든 텍스트/속성 이스케이프)."""
    topic_url = f"{SITE}/{topic}/"
    feed_title = f"{topic} — Paper Curation"
    # 피드 updated: 최신 엔트리 시각, 없으면 빌드 시각
    feed_updated = entries[0]["updated"] if entries else \
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    lines = ['<?xml version="1.0" encoding="utf-8"?>']
    lines.append('<feed xmlns="http://www.w3.org/2005/Atom">')
    lines.append(f"  <title>{escape(feed_title)}</title>")
    lines.append(f'  <link href={quoteattr(topic_url)} rel="alternate" type="text/html"/>')
    lines.append(f'  <link href={quoteattr(topic_url + "feed.xml")} rel="self" type="application/atom+xml"/>')
    lines.append(f"  <id>{escape(topic_url)}</id>")
    lines.append(f"  <updated>{feed_updated}</updated>")
    lines.append("  <author><name>Jehyun Lee</name></author>")
    lines.append("  <generator>paper-curation build_rss.py</generator>")

    for e in entries:
        lines.append("  <entry>")
        lines.append(f"    <title>{escape(e['title'])}</title>")
        lines.append(f'    <link href={quoteattr(e["url"])} rel="alternate" type="text/html"/>')
        lines.append(f"    <id>{escape(e['url'])}</id>")
        lines.append(f"    <updated>{e['updated']}</updated>")
        for name in e["authors"]:
            if name:
                lines.append(f"    <author><name>{escape(str(name))}</name></author>")
        if e["category"]:
            lines.append(f'    <category term={quoteattr(e["category"])}/>')
        if e["summary"]:
            lines.append(f'    <summary type="text">{escape(e["summary"])}</summary>')
        lines.append("  </entry>")

    lines.append("</feed>")
    return "\n".join(lines) + "\n"


def build_rss(topic):
    topic_dir = str(get_topic_dir(topic))
    if not os.path.isdir(topic_dir):
        print(f"SKIP: topic dir not found: {topic_dir}")
        return None

    with open(os.path.join(PAPERS_DIR, "_papers_index.json"), encoding="utf-8") as f:
        papers_index = json.load(f)

    entries = _collect_topic_papers(topic, topic_dir, papers_index)
    # 날짜 내림차순 (동일 날짜는 슬러그 역순으로 안정 정렬)
    entries.sort(key=lambda e: (e["sort_key"], e["slug"]), reverse=True)
    entries = entries[:MAX_ENTRIES]

    xml = _build_atom(topic, entries)
    out_path = os.path.join(topic_dir, "feed.xml")
    atomic_write_text(out_path, xml)
    print(f"Written: {out_path} ({len(entries)} entries, {len(xml):,} chars)")
    return out_path


def main():
    parser = argparse.ArgumentParser(
        description="Build Atom 1.0 feed (feed.xml) for a topic")
    parser.add_argument("topic", nargs="?", default="ai4s",
                        help="Topic alias (e.g. humanoid, ai4s, scisci)")
    args = parser.parse_args()
    build_rss(args.topic)


if __name__ == "__main__":
    from _env_guard import force_py312
    force_py312()
    main()
