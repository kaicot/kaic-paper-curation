"""Deterministic static topic index for the local-safe profile."""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
from pathlib import Path
from typing import cast


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pipeline.config_loader import PAPERS_DIR, get_topic_dir  # noqa: E402


def _load_papers(topic: str) -> list[dict[str, object]]:
    index = Path(PAPERS_DIR) / "_papers_index.json"
    raw = cast(
        object,
        json.loads(index.read_text(encoding="utf-8")),
    )
    if not isinstance(raw, list):
        raise ValueError("paper index must be a list")
    papers: list[dict[str, object]] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            continue
        paper = cast(dict[str, object], item)
        classifications = paper.get("classifications")
        if not isinstance(classifications, dict):
            continue
        if topic not in classifications:
            continue
        papers.append(paper)
    return sorted(
        papers,
        key=lambda paper: (
            str(paper.get("date", "")),
            str(paper.get("title", "")),
            str(paper.get("slug", "")),
        ),
        reverse=True,
    )


def _card(topic: str, paper: dict[str, object]) -> str:
    title = html.escape(str(paper.get("title", "Untitled")))
    slug = html.escape(str(paper.get("slug", "")), quote=True)
    essence = html.escape(str(paper.get("essence", "")))
    classifications = cast(
        dict[str, object],
        paper.get("classifications", {}),
    )
    topic_classification = classifications.get(topic, {})
    category = (
        str(cast(dict[str, object], topic_classification).get("primary_category", ""))
        if isinstance(topic_classification, dict)
        else ""
    )
    return (
        '<article class="paper-card">'
        f'<h2><a href="../papers/{slug}/index.html">{title}</a></h2>'
        f'<p class="category">{html.escape(category)}</p>'
        f"<p>{essence}</p>"
        "</article>"
    )


def _run_topic_index(topic: str, *_args: object, **_kwargs: object) -> Path:
    papers = _load_papers(topic)
    cards = "\n".join(_card(topic, paper) for paper in papers)
    document = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(topic)} paper curation</title>
  <style>
    body {{ max-width: 72rem; margin: 2rem auto; padding: 0 1rem;
            font: 16px/1.6 system-ui, sans-serif; color: #172033; }}
    .paper-card {{ border: 1px solid #d8dee9; border-radius: .75rem;
                   padding: 1rem; margin: 1rem 0; }}
    h1, h2 {{ line-height: 1.25; }}
    a {{ color: #1856a0; }}
    .category {{ color: #526071; font-weight: 600; }}
  </style>
</head>
<body>
  <header><h1>{html.escape(topic)} Paper Curation</h1>
  <p>로컬 보존 아티팩트에서 생성된 정적 논문 목록입니다.</p></header>
  <main>{cards or '<p>등록된 논문이 없습니다.</p>'}</main>
</body>
</html>
"""
    output = Path(get_topic_dir(topic)) / "index.html"
    _ = output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".html.tmp")
    _ = temporary.write_text(document, encoding="utf-8")
    os.replace(temporary, output)
    return output


def get_topic() -> str:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("topic")
    return cast(str, parser.parse_args().topic)


def main() -> int:
    output = _run_topic_index(get_topic())
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
