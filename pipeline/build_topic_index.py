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


def theme_accent(topic: str) -> str:
    return {
        "ai4s": "#D63423",
        "scisci": "#2374D6",
    }.get(topic, "#1856a0")


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
    raw_title = str(paper.get("title", "Untitled"))
    raw_essence = str(paper.get("essence", ""))
    title = html.escape(raw_title)
    slug = html.escape(str(paper.get("slug", "")), quote=True)
    essence = html.escape(raw_essence)
    date = html.escape(str(paper.get("date", "")))
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
        '<article class="paper-card" '
        f'data-search="{html.escape(f"{raw_title} {category} {raw_essence}", quote=True)}" '
        f'data-slug="{slug}">'
        '<div class="card-meta">'
        f'<span class="category">{html.escape(category)}</span>'
        f"<time>{date}</time>"
        "</div>"
        f'<h2><a href="../papers/{slug}/index.html">{title}</a></h2>'
        f'<p class="essence">{essence}</p>'
        f'<a class="card-link" href="../papers/{slug}/index.html">'
        "리뷰 읽기 <span aria-hidden=\"true\">→</span></a>"
        "</article>"
    )


def _run_topic_index(topic: str, *_args: object, **_kwargs: object) -> Path:
    papers = _load_papers(topic)
    cards = "\n".join(_card(topic, paper) for paper in papers)
    topic_json = json.dumps(topic, ensure_ascii=False).replace(
        "<",
        "\\u003c",
    )
    accent = theme_accent(topic)
    document = f"""<!doctype html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>{html.escape(topic)} paper curation</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #172033;
      --muted: #5a6678;
      --line: #d8dee9;
      --surface: #ffffff;
      --subtle: #f5f7fa;
      --accent: {accent};
      --accent-strong: #0d3f7d;
      --focus: #f5a623;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      background: var(--subtle);
      color: var(--ink);
      font: 16px/1.65 system-ui, -apple-system, BlinkMacSystemFont,
            "Segoe UI", sans-serif;
    }}
    a {{ color: var(--accent); }}
    a:hover {{ color: var(--accent-strong); }}
    a:focus-visible, button:focus-visible, input:focus-visible,
    select:focus-visible {{
      outline: 3px solid var(--focus);
      outline-offset: 3px;
    }}
    .skip-link {{
      position: absolute;
      left: 1rem;
      top: -5rem;
      z-index: 10;
      padding: .65rem 1rem;
      background: var(--ink);
      color: white;
    }}
    .skip-link:focus {{ top: 1rem; }}
    .shell {{
      width: min(74rem, calc(100% - 2rem));
      margin: 0 auto;
    }}
    .site-header {{
      padding: clamp(2.5rem, 8vw, 6rem) 0 2rem;
      border-bottom: 1px solid var(--line);
      background: var(--surface);
    }}
    .eyebrow {{
      margin: 0 0 .5rem;
      color: var(--accent);
      font-size: .78rem;
      font-weight: 800;
      letter-spacing: .12em;
      text-transform: uppercase;
    }}
    h1, h2 {{ line-height: 1.2; }}
    h1 {{
      max-width: 18ch;
      margin: 0;
      font-size: clamp(2.2rem, 7vw, 4.8rem);
      letter-spacing: -.04em;
    }}
    .lede {{
      max-width: 54rem;
      margin: 1rem 0 0;
      color: var(--muted);
      font-size: 1.08rem;
    }}
    main {{ padding: 2rem 0 5rem; }}
    .toolbar {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 1rem;
      align-items: end;
      margin-bottom: 1.5rem;
    }}
    label {{ display: block; font-weight: 750; }}
    input, select, button {{
      min-height: 2.8rem;
      border: 1px solid #aeb8c7;
      border-radius: .4rem;
      background: white;
      color: var(--ink);
      font: inherit;
    }}
    input, select {{ width: 100%; padding: .65rem .8rem; }}
    button {{
      padding: .65rem 1rem;
      border-color: var(--accent);
      background: var(--accent);
      color: white;
      cursor: pointer;
      font-weight: 750;
    }}
    button:hover {{ background: var(--accent-strong); }}
    button:disabled {{ cursor: wait; opacity: .62; }}
    .field-hint, .result-count, .answer-status {{
      color: var(--muted);
      font-size: .9rem;
    }}
    .field-hint {{ display: block; margin-top: .35rem; }}
    .paper-grid {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1rem;
    }}
    .paper-card {{
      display: flex;
      min-width: 0;
      flex-direction: column;
      padding: 1.25rem;
      border: 1px solid var(--line);
      border-radius: .65rem;
      background: var(--surface);
    }}
    .paper-card[hidden] {{ display: none; }}
    .paper-card h2 {{ margin: .7rem 0 .5rem; font-size: 1.25rem; }}
    .paper-card h2 a {{ color: var(--ink); text-decoration: none; }}
    .paper-card h2 a:hover {{ color: var(--accent); }}
    .card-meta {{
      display: flex;
      justify-content: space-between;
      gap: 1rem;
      color: var(--muted);
      font-size: .82rem;
    }}
    .category {{ color: var(--accent); font-weight: 750; }}
    .essence {{ flex: 1; margin: 0 0 1rem; color: var(--muted); }}
    .card-link {{ font-weight: 750; text-decoration: none; }}
    .answer-panel {{
      margin-top: 3rem;
      padding: clamp(1.25rem, 4vw, 2rem);
      border-top: 4px solid var(--accent);
      background: var(--surface);
      box-shadow: 0 12px 30px rgb(23 32 51 / 8%);
    }}
    .answer-panel h2 {{ margin-top: 0; font-size: 1.7rem; }}
    .answer-form {{
      display: grid;
      grid-template-columns: minmax(0, 1fr) 8rem auto;
      gap: .75rem;
      align-items: end;
    }}
    .answer-output {{
      margin-top: 1.25rem;
      padding-top: 1.25rem;
      border-top: 1px solid var(--line);
    }}
    .answer-output:empty {{ display: none; }}
    .answer-copy {{ max-width: 70ch; white-space: pre-wrap; }}
    .answer-copy a {{ font-weight: 750; }}
    .citation-list {{ padding-left: 1.25rem; }}
    .error {{ color: #9c1c1c; font-weight: 700; }}
    @media (max-width: 44rem) {{
      .toolbar, .answer-form, .paper-grid {{
        grid-template-columns: 1fr;
      }}
      .result-count {{ justify-self: start; }}
    }}
    @media (prefers-reduced-motion: no-preference) {{
      .paper-card {{ transition: border-color .16s ease, transform .16s ease; }}
      .paper-card:hover {{
        border-color: #9cabc0;
        transform: translateY(-2px);
      }}
    }}
  </style>
</head>
<body>
  <a class="skip-link" href="#content">본문으로 건너뛰기</a>
  <header class="site-header">
    <div class="shell">
      <p class="eyebrow">Local paper curation</p>
      <h1>{html.escape(topic)} Paper Curation</h1>
      <p class="lede">로컬 보존 아티팩트를 검색하고, 선택된 근거에
      연결된 답변을 같은 기기에서 확인합니다.</p>
    </div>
  </header>
  <main id="content" class="shell">
    <section aria-labelledby="papers-heading">
      <h2 id="papers-heading">논문 탐색</h2>
      <div class="toolbar">
        <div>
          <label for="paper-search">제목, 카테고리, 핵심 내용 검색</label>
          <input id="paper-search" type="search" autocomplete="off"
                 placeholder="예: scientific agents">
          <span class="field-hint">입력 즉시 현재 목록을 필터링합니다.</span>
        </div>
        <p id="result-count" class="result-count" aria-live="polite"></p>
      </div>
      <div id="paper-grid" class="paper-grid">
        {cards or '<p>등록된 논문이 없습니다.</p>'}
      </div>
    </section>

    <section class="answer-panel" aria-labelledby="local-answer-heading">
      <h2 id="local-answer-heading">로컬 근거 답변</h2>
      <p>현재 토픽의 BM25 인덱스와 저장된 리뷰만 근거로 사용합니다.
      비밀키나 계정 정보를 입력하지 마세요.</p>
      <form id="local-answer-form" class="answer-form">
        <div>
          <label for="local-answer-query">질문</label>
          <input id="local-answer-query" name="query" required
                 maxlength="2000" placeholder="이 연구들의 공통 한계는?">
        </div>
        <div>
          <label for="local-answer-length">답변 길이</label>
          <select id="local-answer-length" name="length">
            <option value="short">짧게</option>
            <option value="medium">보통</option>
            <option value="long">길게</option>
          </select>
        </div>
        <button id="local-answer-submit" type="submit">근거로 답하기</button>
      </form>
      <p id="local-answer-status" class="answer-status" role="status"
         aria-live="polite">Local server required — 답변 기능은
         로컬 서버에서 페이지를 열 때만 동작합니다.</p>
      <div id="local-answer-output" class="answer-output"
           aria-live="polite"></div>
    </section>
  </main>
  <script>
  (() => {{
    "use strict";
    const topic = {topic_json};
    const search = document.getElementById("paper-search");
    const cards = Array.from(document.querySelectorAll(".paper-card"));
    const resultCount = document.getElementById("result-count");
    const form = document.getElementById("local-answer-form");
    const query = document.getElementById("local-answer-query");
    const length = document.getElementById("local-answer-length");
    const submit = document.getElementById("local-answer-submit");
    const status = document.getElementById("local-answer-status");
    const output = document.getElementById("local-answer-output");
    const localRequired = "local server required — " +
      "pipeline/serve_local.py로 페이지를 열어 주세요.";
    const isLoopback = location.protocol === "http:" &&
      location.hostname === "127.0.0.1";
    if (isLoopback) {{
      status.textContent = "질문을 입력하면 로컬 근거로 답변합니다.";
    }} else {{
      status.textContent = localRequired;
      status.className = "answer-status error";
    }}

    const updateCards = () => {{
      const needle = search.value.trim().toLocaleLowerCase("ko");
      let visible = 0;
      for (const card of cards) {{
        const match = !needle ||
          card.dataset.search.toLocaleLowerCase("ko").includes(needle);
        card.hidden = !match;
        if (match) visible += 1;
      }}
      resultCount.textContent = `${{visible}} / ${{cards.length}}편`;
    }};
    search.addEventListener("input", updateCards);
    updateCards();

    const citationLink = (citation) => {{
      const link = document.createElement("a");
      link.href = `../papers/${{encodeURIComponent(citation.slug)}}/index.html`;
      link.textContent = `[ref:${{citation.ref}}]`;
      link.title = `${{citation.slug}} · ${{citation.section}}`;
      return link;
    }};

    const renderAnswer = (payload) => {{
      if (payload.schema !== "local-answer-response-v1" ||
          payload.schema_version !== 1 ||
          !Array.isArray(payload.citations) ||
          typeof payload.answer !== "string") {{
        throw new Error("invalid local answer response");
      }}
      const references = new Map(
        payload.citations.map((item) => [Number(item.ref), item])
      );
      const copy = document.createElement("p");
      copy.className = "answer-copy";
      for (const token of payload.answer.split(/(\\[ref:\\d+\\])/g)) {{
        const match = /^\\[ref:(\\d+)\\]$/.exec(token);
        if (!match) {{
          copy.append(document.createTextNode(token));
          continue;
        }}
        const citation = references.get(Number(match[1]));
        if (!citation) throw new Error("unresolved local citation");
        copy.append(citationLink(citation));
      }}
      const heading = document.createElement("h3");
      heading.textContent = "근거";
      const list = document.createElement("ol");
      list.className = "citation-list";
      for (const citation of payload.citations) {{
        const item = document.createElement("li");
        item.append(citationLink(citation));
        item.append(document.createTextNode(
          ` — ${{citation.slug}} · ${{citation.section}}`
        ));
        list.append(item);
      }}
      output.replaceChildren(copy, heading, list);
    }};

    form.addEventListener("submit", async (event) => {{
      event.preventDefault();
      output.replaceChildren();
      if (!isLoopback) {{
        status.textContent = localRequired;
        status.className = "answer-status error";
        return;
      }}
      submit.disabled = true;
      form.setAttribute("aria-busy", "true");
      status.className = "answer-status";
      status.textContent = "로컬 근거를 확인하고 있습니다…";
      try {{
        const sessionResponse = await fetch("/api/session", {{
          cache: "no-store",
          credentials: "same-origin",
        }});
        if (!sessionResponse.ok) throw new Error("local session unavailable");
        const session = await sessionResponse.json();
        const answerResponse = await fetch("/api/answer", {{
          method: "POST",
          credentials: "same-origin",
          headers: {{
            "Content-Type": "application/json",
            "X-CSRF-Token": session.csrf_token,
          }},
          body: JSON.stringify({{
            topic,
            query: query.value.trim(),
            length: length.value,
          }}),
        }});
        const payload = await answerResponse.json();
        if (!answerResponse.ok) {{
          const error = new Error(payload.status || "local answer failed");
          error.status = answerResponse.status;
          throw error;
        }}
        renderAnswer(payload);
        status.textContent = "로컬 근거 답변을 생성했습니다.";
      }} catch (error) {{
        const stopped = error instanceof TypeError ||
          String(error.message).includes("session");
        status.textContent = stopped
          ? localRequired
          : "로컬 답변을 생성하지 못했습니다. 잠시 후 다시 시도해 주세요.";
        status.className = "answer-status error";
      }} finally {{
        submit.disabled = false;
        form.removeAttribute("aria-busy");
      }}
    }});
  }})();
  </script>
</body>
</html>
"""
    output = Path(get_topic_dir(topic)) / "index.html"
    _ = output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(".html.tmp")
    _ = temporary.write_text(document, encoding="utf-8")
    os.replace(temporary, output)
    return output


def build_topic_index(topic: str) -> Path:
    return _run_topic_index(topic)


def get_topic() -> str:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("topic")
    return cast(str, parser.parse_args().topic)


def main() -> int:
    output = build_topic_index(get_topic())
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
