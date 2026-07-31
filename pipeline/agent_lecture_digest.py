#!/usr/bin/env python3
"""자율 강의 다이제스트 엔진 — Dashun Wang 커리큘럼의 한 강을 Deeper Research 로 처리.

새 규칙(미리 생성 + 예정시각엔 메일만):
  ① 미리(--build) 강별 산출물을 만들어 둔다:
     핵심 논문(내 코퍼스 우선) + 연결 그래프 1-hop 관련 논문 + 웹 검색(google_search)
     → Gemini 심화 리포트 → 대본(script) → Audio Overview(2인·전문가·학술·한국어, 30~50분)
     → 링크·관련연구·참고문헌·연구지도 갖춘 HTML → OUTDIR 저장(메일 없음, status=built).
  ② 예정시각엔(--due, launchd) 미리 만든 산출물을 불러와 **메일만** 발송(status=done).
     산출물이 없으면 그 자리에서 생성 후 발송(self-heal).

맥미니 launchd / 로컬:
    python pipeline/agent_lecture_digest.py --build --all               # 1~11강 미리 생성(대본·오디오·HTML)
    python pipeline/agent_lecture_digest.py --build --lecture 1         # 특정 강만 미리 생성
    python pipeline/agent_lecture_digest.py --due                       # 예정 강 메일 발송(미리 만든 것 사용)
    python pipeline/agent_lecture_digest.py --deliver --all \
        --recipients "a@b.com,c@d.com"                                  # 특정 수신자에게 재발송(status 미변경)
    python pipeline/agent_lecture_digest.py --build --lecture 1 --no-audio   # 오디오 생략(검증용)
"""
import argparse, json, os, re, sys, base64, urllib.request, urllib.error
import html as H
from pathlib import Path
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

try:
    sys.stdout.reconfigure(line_buffering=True)
    sys.stderr.reconfigure(line_buffering=True)
except Exception:
    pass

PIPE = Path(__file__).resolve().parent
sys.path.insert(0, str(PIPE))
from config_loader import PAPERS_DIR, DOCS_DIR, get_google_key  # noqa: E402
from generate_audio import (  # noqa: E402
    build_prompt, parse_turns, chunk_turns, chunk_paragraphs, tts_call, concat_pcm,
    speech_multi, speech_single, ROLES, TTS_PREFIX, SAMPLE_RATE, MAX_CHUNK_CHARS,
)
from google import genai  # noqa: E402
from google.genai import types  # noqa: E402
import lameenc  # noqa: E402
import usage_log  # noqa: E402

PAPERS = Path(PAPERS_DIR)
DOCS = Path(DOCS_DIR)
AGENT_DIR = Path(os.environ.get("PC_AGENT_DIR", str(Path.home() / "pc_agent" / "dashun_wang")))
LEDGER = AGENT_DIR / "curriculum.json"
OUTDIR = AGENT_DIR / "lectures"
REPORT_MODEL = "gemini-3.1-pro-preview"
TTS_WORKERS = 2
MATHJAX_HEAD = (
    "<script>window.MathJax={tex:{inlineMath:[['$','$'],['\\\\(','\\\\)']],"
    "displayMath:[['$$','$$'],['\\\\[','\\\\]']]},"
    "options:{skipHtmlTags:['script','noscript','style','textarea','pre','code']}};</script>\n"
    '<script src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-mml-chtml.js" async></script>'
)


def load_ledger():
    return json.loads(LEDGER.read_text(encoding="utf-8"))


def save_ledger(led):
    LEDGER.write_text(json.dumps(led, ensure_ascii=False, indent=2), encoding="utf-8")


# ── 코퍼스 인덱스 / 링크 ─────────────────────────────────────────────────────
_PIDX = None
def pidx():
    global _PIDX
    if _PIDX is None:
        d = json.loads((PAPERS / "_papers_index.json").read_text(encoding="utf-8"))
        _PIDX = {e["slug"]: e for e in d if e.get("slug")}
    return _PIDX


def _year(e):
    y = e.get("year")
    if isinstance(y, int) or (isinstance(y, str) and str(y).isdigit()):
        return int(y)
    dt = (e.get("date") or "")[:4]
    return int(dt) if dt.isdigit() else None


def paper_link(slug):
    e = pidx().get(slug, {})
    doi = (e.get("doi") or "").strip()
    if doi and doi.lower() not in ("n/a", "미제공", "-", ""):
        return "https://doi.org/" + doi.lstrip("https://doi.org/")
    title = e.get("title") or slug.split("_", 1)[-1].replace("_", " ")
    return "https://scholar.google.com/scholar?q=" + urllib.request.quote(title)


def _find_dir(slug):
    if (PAPERS / slug).is_dir():
        return PAPERS / slug
    pre = slug.split("_")[0] + "_"
    for d in PAPERS.iterdir():
        if d.is_dir() and d.name.startswith(pre):
            return d
    return None


def read_review(slug, cap=3000):
    d = _find_dir(slug)
    if not d or not (d / "review.md").exists():
        return pidx().get(slug, {}).get("essence", "") or ""
    t = (d / "review.md").read_text(encoding="utf-8")
    while t.startswith("---"):
        lines = t.split("\n")
        end = next((i for i, l in enumerate(lines[1:], 1) if l.strip() == "---"), None)
        if end is None:
            break
        t = "\n".join(lines[end + 1:]).lstrip("\n")
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    return t.strip()[:cap]


_CONN = None
def all_connections():
    global _CONN
    if _CONN is not None:
        return _CONN
    merged = {}
    for cp in DOCS.glob("*/_paper_connections.json"):
        try:
            conn = json.loads(cp.read_text(encoding="utf-8"))
        except Exception:
            continue
        for slug, edges in conn.items():
            b = merged.setdefault(slug, [])
            seen = {(e.get("slug"), e.get("relation")) for e in b}
            for e in edges if isinstance(edges, list) else []:
                k = (e.get("slug"), e.get("relation"))
                if k not in seen:
                    b.append(e); seen.add(k)
    _CONN = merged
    return merged


REL_KO = {"foundation": "기반", "extension": "후속·확장", "alternative": "대안",
          "application": "응용", "counterpoint": "반론·수정"}


def gather_evidence(lecture, related_cap=10):
    """반환: (evidence_text, core[list], related[list]). core/related 는 HTML 링크용 메타 포함."""
    idx = pidx()
    core = []
    for p in lecture["papers"]:
        e = idx.get(p["slug"], {})
        core.append({"slug": p["slug"], "title": p["title"], "year": p.get("year") or _year(e),
                     "link": paper_link(p["slug"]), "review": read_review(p["slug"], 3000)})
    core_slugs = {c["slug"] for c in core}

    # 연결 그래프 1-hop 확장 — 빈도 + 반론/대안 우선
    conns = all_connections()
    score = {}
    for c in core:
        for e in conns.get(c["slug"], []):
            rs = e.get("slug")
            if not rs or rs in core_slugs:
                continue
            boost = 1.0 + (0.6 if e.get("relation") in ("counterpoint", "alternative") else 0)
            cur = score.get(rs)
            if not cur or boost > cur["boost"]:
                score[rs] = {"boost": boost, "relation": e.get("relation", "related"), "reason": e.get("reason", "")}
            score[rs]["count"] = score[rs].get("count", 0) + 1
    ranked = sorted(score.items(), key=lambda kv: (kv[1]["count"] + kv[1]["boost"]), reverse=True)[:related_cap]
    related = []
    for rs, meta in ranked:
        e = idx.get(rs, {})
        related.append({"slug": rs, "title": e.get("title", rs.split("_", 1)[-1].replace("_", " ")),
                        "year": _year(e), "link": paper_link(rs), "relation": meta["relation"],
                        "reason": meta["reason"], "review": read_review(rs, 1800),
                        "essence": (e.get("essence") or "")[:700]})

    # LLM 근거 텍스트
    blocks = ["## 핵심 논문 (내 코퍼스 — 최우선 근거)"]
    for c in core:
        blocks.append(f"### ({c['year']}) {c['title']}\n{c['review'] or '(요약 없음)'}")
    if related:
        blocks.append("## 같이 보면 좋은 논문 (지정된 관련 논문 — 비교·대조·계보 분석의 최우선 대상)")
        for r in related:
            blocks.append(f"### [{REL_KO.get(r['relation'], r['relation'])}] ({r['year']}) {r['title']}\n{r['review'] or r['essence'] or '(요약 없음)'}\n(연결 사유: {r['reason']})")
    return "\n\n".join(blocks), core, related


# ── Deeper Research 리포트 합성 (코퍼스 우선 + 웹 검색) ──────────────────────
def extract_web_sources(resp):
    out, seen = [], set()
    try:
        gm = resp.candidates[0].grounding_metadata
        for ch in (gm.grounding_chunks or []):
            w = getattr(ch, "web", None)
            uri = getattr(w, "uri", None) if w else None
            if uri and uri not in seen:
                seen.add(uri)
                out.append((getattr(w, "title", "") or uri, uri))
    except Exception:
        pass
    return out


def prior_lectures_brief(led, current):
    """시리즈 연속성용: 이번 강 이전 강의들의 (번호·제목·핵심 주제) 한 줄 요약."""
    lines = []
    for L in sorted(led["lectures"], key=lambda x: x["lecture"]):
        if L["lecture"] >= current:
            continue
        theme = (L.get("objectives") or [""])[0]
        lines.append(f"  {L['lecture']}강 「{L['title']}」 — {theme}")
    return "\n".join(lines)


def synthesize_report(course, lecture, evidence, client, prior=""):
    obj = "\n".join(f"  {i+1}. {o}" for i, o in enumerate(lecture["objectives"]))
    plist = ", ".join(f"({p['year']}) {p['title']}" for p in lecture["papers"])
    cont = (("이 강의는 시리즈의 제%d강입니다. 앞서 다룬 강의:\n%s\n\n"
             "연속성 지침:\n"
             "- 도입부에서 시리즈 흐름 속 이번 강의의 위치를 한두 문장으로 짚고 들어갈 것.\n"
             "- 앞 강의의 개념·논문과 **실제로 관련될 때만** 자연스럽게 연결('앞서 N강에서 본 ~와 달리/처럼'). 억지 삽입 금지.\n"
             "- 비유·예시는 앞 강의와 겹치지 않게 새로운 것을 사용.\n\n") % (lecture["lecture"], prior)) if prior else ""
    prompt = (
        f"당신은 '{course}' 심화 강의를 집필하는 전문 연구자입니다. **제{lecture['lecture']}강: {lecture['title']}**.\n\n"
        f"오늘의 학습목표:\n{obj}\n\n다루는 핵심 논문: {plist}\n\n"
        f"{cont}"
        "이것은 **Deeper Research** 입니다 — 토픽 Deeper Research와 동일하게, **핵심 논문(시드)과 각 논문에 지정된 '같이 보면 좋은 논문'(연결 그래프: 기반·후속·대안·응용·반론)** 을 **최우선**으로 함께 동원해 비교·대조·계보를 분석하세요. 후대에 나온 관련 논문도 훌륭한 비교 대상입니다.\n\n"
        "작성 지침:\n"
        "- 학습목표 3개를 모두 충실히 충족.\n"
        "- 논문 단순 나열 금지. **연구 방법론·핵심 메시지 축**으로 엮고, 지정 관련 논문을 핵심 논문과 **직접 비교**: 무엇을 계승·확장했고 무엇을 반박·수정했는지, 방법론 차이는 무엇인지.\n"
        "- **계보·수정 서사 필수**: '당시엔 X로 여겨졌으나 후대 연구(관련 논문)에서 Y로 밝혀졌다', '이 결과는 이후 ~에 의해 반박·정교화됐다'를 지정 관련 논문 근거로 전개.\n"
        "- 인용: 논문은 (제목, 연도)로 본문에 명시. **웹 검색은 보조 수단** — 코퍼스(핵심+관련 논문)로 충분하면 웹을 남용하지 말고, 코퍼스에 없는 최신 맥락·외부 반응이 꼭 필요할 때만 google_search로 보강하고 [제목](URL)로 표기.\n"
        "- 전문가 청중, 학술적 톤. 마크다운(## 소제목, 단락). 각 논문의 문제의식·방법·핵심 결과(수치)·의의를 구체적으로. **최소 18,000자 이상**의 상세 강의(서둘러 마무리 금지).\n"
        "- 메타·머리말 없이 곧바로 강의 본문으로 시작.\n\n"
        f"=== 근거 (코퍼스) ===\n{evidence}"
    )
    cfg = types.GenerateContentConfig(
        temperature=0.6, max_output_tokens=40000,
        tools=[types.Tool(google_search=types.GoogleSearch())])
    resp = client.models.generate_content(model=REPORT_MODEL, contents=prompt, config=cfg)
    usage_log.record_gemini(resp, REPORT_MODEL)
    report = (resp.text or "").strip()
    return report, extract_web_sources(resp)


# ── 오디오 (2인·전문가·학술·한국어, 30분 이상·40분 목표·50분 이하) ───────────────
def make_audio(report_text, evidence, client, minutes=40, prior=""):
    lang, speakers = "ko", 2
    roles = ROLES[lang][speakers]
    labels = [r["label"] for r in roles]
    rolelines = "\n".join(f"- {r['label']}: {r['desc']}" for r in roles)
    source = report_text + (("\n\n---\n[상세 근거]\n" + evidence[:36000]) if evidence else "")
    # 단일 호출은 한국어에서 심하게 under-fill → 소스를 조각내 조각마다 심층 대화 생성 후 이어붙인다.
    # 30분 이상, 40분 목표, 50분 이하(메일 용량·청취 부담 균형).
    paras = [p for p in re.split(r"\n\s*\n", source) if p.strip()]
    n_seg = max(4, min(5, len(source) // 10000))   # ~4-5조각 → 30~45분 중심
    if len(paras) >= n_seg:
        per = -(-len(paras) // n_seg)
        segs = ["\n\n".join(paras[i:i + per]) for i in range(0, len(paras), per)]
    else:
        sz = max(1, -(-len(source) // n_seg))
        segs = [source[i:i + sz] for i in range(0, len(source), sz)]
    def gen_script_part(i, seg):
        pos = (("도입부: 아주 짧게 한두 마디로 시작(장황한 인사 금지)"
                + ("; 시리즈 흐름 속 이번 회차의 위치를 한 마디로 짚어도 좋다" if prior else "")) if i == 0
               else "마무리: 핵심 통찰을 종합하며 자연스럽게 끝맺음" if i == len(segs) - 1
               else "앞 대화에 자연스럽게 이어서 진행(재소개·재인사 금지)")
        cont = ("앞 회차와 실제로 연결될 때만 '앞서 N강에서 본 ~'처럼 자연스럽게 짚되, 억지 참조·재설명은 금지. " if prior else "")
        p = (f"당신은 전문가 청중용 학술 2인 팟캐스트 대본을 씁니다. 화자는 정확히 2명뿐:\n{rolelines}\n"
             f"각 발화는 '{labels[0]}:' 또는 '{labels[1]}:' 로 시작(콜론+공백). 이 부분은 전체 {len(segs)}개 중 {i + 1}번째. {pos}.\n"
             + cont +
             "아래 내용을 **약 3,400~4,000자, 6~8 turn**의 깊이 있는 한국어 대화로 작성. 전체 오디오는 30분 이상·40분 목표·50분 이하가 되도록 과도한 반복과 장황한 요약을 피한다. 방법·수치·연구 계보와 '당시엔 X로 여겨졌으나 후대엔 Y로 밝혀졌다'식 수정 서사를 구체적으로 풀되 청취자 눈높이 비유도 곁들일 것. 라벨 외 머리말·메타·프로그램명 금지.\n\n"
             f"내용:\n{seg}")
        r = client.models.generate_content(
            model=REPORT_MODEL, contents=p,
            config=types.GenerateContentConfig(temperature=0.8, max_output_tokens=16384))
        usage_log.record_gemini(r, REPORT_MODEL)
        return i, (r.text or "").strip()

    scripts = [""] * len(segs)
    workers = min(4, len(segs))
    print(f"    대본 병렬 생성 {len(segs)} parts · workers={workers}", flush=True)
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(gen_script_part, i, seg): i for i, seg in enumerate(segs)}
        for fut in as_completed(futs):
            i, t = fut.result()
            scripts[i] = t
            print(f"    대본 {i + 1}/{len(segs)} · {len(t):,}자", flush=True)
    script = "\n".join(s for s in scripts if s)
    if not script:
        raise RuntimeError("대본 생성 실패")
    turns = parse_turns(script, labels)
    max_script_chars = 25_000                       # 경험상 50분 내외 상한(64kbps 첨부 안전선)
    if turns and len(script) > max_script_chars:
        reserve = turns[-2:] if len(turns) > 2 else []
        reserve_chars = sum(len(a) + len(b) + 3 for a, b in reserve)
        kept, used = [], 0
        for lab, txt in turns[:-len(reserve)] if reserve else turns:
            add = len(lab) + len(txt) + 3
            if kept and used + add + reserve_chars > max_script_chars:
                break
            kept.append((lab, txt)); used += add
        if reserve:
            kept.extend(reserve)
        turns = kept
        script = "\n".join(f"{lab}: {txt}" for lab, txt in turns)
        print(f"    대본 길이 상한 적용 · {len(script):,}자", flush=True)
    if turns:
        chunks = chunk_turns(turns, MAX_CHUNK_CHARS); cfg = speech_multi(roles); prefix = TTS_PREFIX[lang]
        get = lambda i: prefix + chunks[i]
    else:
        chunks = chunk_paragraphs(script, MAX_CHUNK_CHARS); cfg = speech_single(roles[0]["voice"]); get = lambda i: chunks[i]
    print(f"    TTS {len(chunks)} chunks ...", flush=True)
    parts = [None] * len(chunks)
    with ThreadPoolExecutor(max_workers=TTS_WORKERS) as ex:
        futs = {ex.submit(tts_call, client, get(i), cfg): i for i in range(len(chunks))}
        done = 0
        for fut in as_completed(futs):
            parts[futs[fut]] = fut.result(); done += 1
            print(f"      [{done}/{len(chunks)}]", flush=True)
    pcm = concat_pcm([p for p in parts if p])
    enc = lameenc.Encoder(); enc.set_bit_rate(64); enc.set_in_sample_rate(SAMPLE_RATE); enc.set_channels(1); enc.set_quality(2)
    mp3 = enc.encode(pcm) + enc.flush()
    return mp3, script, len(pcm) / 2 / SAMPLE_RATE / 60


# ── HTML (링크·관련연구·참고문헌 정상) ───────────────────────────────────────
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")


def md_to_html(md):
    md = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md)          # 이미지 마크업 제거
    out, para = [], []

    def flush():
        if not para:
            return
        t = H.escape("\n".join(para)).replace("\n", "<br>")
        t = _LINK_RE.sub(lambda m: f'<a href="{m.group(2)}" target="_blank" rel="noopener">{m.group(1)}</a>', t)
        t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
        out.append(f"<p>{t}</p>")
        para.clear()

    for line in md.split("\n"):
        s = line.strip()
        if not s:
            flush(); continue
        m = re.match(r"(#{1,3})\s+(.*)", s)
        if m:
            flush()
            lvl = "h2" if len(m.group(1)) <= 2 else "h3"
            out.append(f"<{lvl}>{H.escape(m.group(2))}</{lvl}>")
        else:
            para.append(line)
    flush()
    return "\n".join(out)


def gather_figures(core, per_paper=2, total_cap=10, max_bytes=300 * 1024):
    """core 논문 폴더의 figures/fig*.webp 를 캡션과 함께 data-URI 로 수집(HTML 자체포함용)."""
    figs = []
    for c in core:
        d = _find_dir(c["slug"])
        if not d or not (d / "figures").is_dir():
            continue
        caps = {}
        rm = d / "review.md"
        if rm.exists():
            md = rm.read_text(encoding="utf-8")
            for m in re.finditer(r"!\[.*?\]\(figures/(fig\d+)[^)]*\)\s*\n+\*(.+?)\*", md):
                caps[m.group(1)] = m.group(2).strip()
        def _fnum(p):
            mm = re.search(r"(\d+)", p.stem)
            return int(mm.group(1)) if mm else 0
        taken = 0
        for w in sorted((d / "figures").glob("fig*.webp"), key=_fnum):
            if taken >= per_paper:
                break
            data = w.read_bytes()
            if len(data) > max_bytes:
                continue
            cap = re.sub(r"\s+", " ", caps.get(w.stem, "")).strip()
            if re.fullmatch(r"(?:FIG|Fig|Figure)\.?\s*\d+[.\-:]?", cap or ""):
                cap = ""
            if len(cap) > 110:
                cap = cap[:108] + "…"
            figs.append({"uri": "data:image/webp;base64," + base64.b64encode(data).decode(),
                         "caption": cap, "title": c["title"], "link": c["link"]})
            taken += 1
        if len(figs) >= total_cap:
            break
    return figs[:total_cap]


def _fig_block(f):
    lab = H.escape(f["title"])
    cap = f' — {H.escape(f["caption"])}' if f["caption"] else ""
    return (f'<figure class="paper-fig"><img src="{f["uri"]}" alt="{lab}" loading="lazy">'
            f'<figcaption><a href="{H.escape(f["link"])}" target="_blank" rel="noopener">{lab}</a>{cap}</figcaption></figure>')


def _interleave(body_html, figs):
    """리포트 본문의 h2 섹션 경계마다 그림을 하나씩 끼워넣어 글·수식 벽을 분절."""
    if not figs:
        return body_html
    parts = re.split(r"(?=<h2)", body_html)
    if len(parts) < 2:
        return '<div class="fig-gallery">' + "".join(_fig_block(f) for f in figs) + "</div>" + body_html
    out, fi = [], 0
    for part in parts:
        out.append(part)
        if fi < len(figs) and part.strip():
            out.append(_fig_block(figs[fi]))
            fi += 1
    if fi < len(figs):
        out.append('<div class="fig-gallery">' + "".join(_fig_block(f) for f in figs[fi:]) + "</div>")
    return "".join(out)


def _node_label(p):
    """3~5단어짜리 노드 라벨. LLM 호출 없이 제목/essence 키워드에서 축약."""
    title = (p.get("title") or "").lower()
    essence = (pidx().get(p.get("slug", ""), {}).get("essence") or "")
    rules = [
        (("emerg", "collective"), "위기 집단 반응"),
        (("mobility", "link"), "이동성 링크 예측"),
        (("connections", "human dynamics"), "동역학-네트워크 연결"),
        (("emergency response",), "재난 대응 네트워크"),
        (("big data",), "빅데이터 통계물리"),
        (("impact", "mobility"), "이동성이 네트워크 형성"),
        (("dominant attributes",), "지배 속성 커뮤니티"),
        (("citation", "impact"), "인용 영향력 예측"),
        (("poisson",), "인기도 동역학 모델"),
        (("scientific impact",), "과학 영향력 정량화"),
        (("failure",), "실패와 경력 경로"),
        (("team",), "팀 규모와 혁신"),
        (("creativity",), "창의성 확산"),
        (("artificial intelligence",), "AI 과학 발견"),
        (("large language",), "LLM 과학 발견"),
    ]
    for keys, lab in rules:
        if all(k in title for k in keys):
            return lab
    src = re.sub(r"본 (?:논문|연구|박사학위 논문)은\s*", "", essence).strip()
    src = re.sub(r"[,.;:()\\[\\]“”\"']", " ", src)
    toks = re.findall(r"[가-힣A-Za-z0-9+-]+", src)
    stop = {"활용하여", "분석한다", "제시한다", "규명한다", "보여준다", "논문은", "연구는", "통해"}
    toks = [t for t in toks if t not in stop and len(t) > 1][:5]
    return " ".join(toks[:5]) if toks else " ".join(re.findall(r"[A-Za-z0-9+-]+", p.get("title", ""))[:5])


def make_connection_map(core, related):
    """모바일 세로 스크롤용 인터랙티브 논문 연결지도."""
    if not core:
        return ""
    conns = all_connections()
    rel_by_core = {c["slug"]: [] for c in core}
    placed = set()
    for r in related:
        for c in core:
            edges = conns.get(c["slug"], [])
            if any(e.get("slug") == r["slug"] for e in edges):
                rel_by_core[c["slug"]].append(r)
                placed.add(r["slug"])
                break
    for i, r in enumerate([r for r in related if r["slug"] not in placed]):
        rel_by_core[core[i % len(core)]["slug"]].append(r)

    def core_node(c, idx):
        yr = H.escape(str(c.get("year") or ""))
        lab = H.escape(_node_label(c))
        ttl = H.escape(c["title"])
        link = H.escape(c["link"])
        branches = "".join(branch_node(r) for r in rel_by_core.get(c["slug"], []))
        return (f'<div class="flow-item">'
                f'<details class="flow-node core-node" open><summary>'
                f'<span class="flow-index">{idx}</span><span class="flow-year">{yr}</span>'
                f'<span class="flow-label">{lab}</span><span class="flow-title">{ttl}</span>'
                f'</summary><div class="flow-detail"><a href="{link}" target="_blank" rel="noopener">논문 링크 열기</a></div></details>'
                f'<div class="flow-branches">{branches}</div></div>')

    def branch_node(r):
        yr = H.escape(str(r.get("year") or ""))
        lab = H.escape(_node_label(r))
        ttl = H.escape(r["title"])
        link = H.escape(r["link"])
        rel = H.escape(REL_KO.get(r.get("relation"), r.get("relation", "related")))
        reason = H.escape((r.get("reason") or "")[:180])
        return (f'<details class="flow-node branch-node"><summary>'
                f'<span class="branch-rel">{rel}</span><span class="flow-year mini">{yr}</span>'
                f'<span class="flow-label">{lab}</span></summary>'
                f'<div class="flow-detail"><b>{ttl}</b><br>{reason}<br><a href="{link}" target="_blank" rel="noopener">관련 논문 열기</a></div></details>')

    items = "".join(core_node(c, i + 1) for i, c in enumerate(core))
    return (f'<section class="connection-map"><h2>🧭 논문 연결 지도</h2>'
            f'<div class="flow-wrap">{items}</div></section>')


def make_html(course, lecture, report_md, core, related, web_sources, total):
    obj = "".join(f"<li>{H.escape(o)}</li>" for o in lecture["objectives"])
    core_html = "".join(
        f'<li><a href="{H.escape(c["link"])}" target="_blank" rel="noopener">({c["year"]}) {H.escape(c["title"])}</a></li>'
        for c in core)
    rel_html = "".join(
        f'<li><span class="rel">{H.escape(REL_KO.get(r["relation"], r["relation"]))}</span> '
        f'<a href="{H.escape(r["link"])}" target="_blank" rel="noopener">({r["year"]}) {H.escape(r["title"])}</a>'
        f'<div class="reason">{H.escape(r["reason"])}</div></li>'
        for r in related) or "<li>(없음)</li>"
    web_html = "".join(
        f'<li><a href="{H.escape(u)}" target="_blank" rel="noopener">🌐 {H.escape(t[:90])}</a></li>'
        for t, u in web_sources)
    web_block = f'<div class="card"><h3>🌐 웹 참고문헌</h3><ol>{web_html}</ol></div>' if web_html else ""
    conn_map = make_connection_map(core, related)
    body = _interleave(md_to_html(report_md), gather_figures(core))
    return f"""<!DOCTYPE html><html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>제{lecture['lecture']}강 — {H.escape(lecture['title'])}</title>
<style>
body{{font-family:'KoPub Dotum',-apple-system,'Noto Sans KR',sans-serif;line-height:1.85;color:#222;max-width:860px;margin:0 auto;padding:2rem 1.4rem;background:#fbfbfd}}
.hero{{background:linear-gradient(135deg,#1a0d2a,#3a1a5c 55%,#6b21a8);color:#fff;border-radius:16px;padding:1.9rem 1.7rem;margin-bottom:1.6rem}}
.hero .kicker{{opacity:.82;font-size:.85rem;font-weight:600}}
.hero h1{{font-size:1.4rem;margin:.35rem 0 .25rem}}
.card{{background:#fff;border:1px solid #eee;border-radius:12px;padding:1.1rem 1.35rem;margin-bottom:1.3rem}}
.card h3{{font-size:.95rem;color:#6B21A8;margin-bottom:.55rem}}
.card ul,.card ol{{margin:0 0 0 1.15rem}} .card li{{margin:.35rem 0}}
.rel{{display:inline-block;font-size:.72rem;font-weight:700;color:#6B21A8;background:#f3e8ff;border-radius:999px;padding:.05rem .5rem;margin-right:.35rem}}
.reason{{font-size:.85rem;color:#666;margin:.15rem 0 .2rem}}
h2{{font-size:1.14rem;color:#6B21A8;margin:1.7rem 0 .6rem;border-bottom:1px solid #eee;padding-bottom:.3rem}}
h3{{font-size:1.02rem;margin:1.1rem 0 .4rem}}
p{{margin:.6rem 0}} a{{color:#7c3aed}}
.foot{{color:#999;font-size:.82rem;border-top:1px solid #eee;margin-top:2rem;padding-top:1rem}}
.paper-fig{{margin:1.3rem 0;text-align:center}}
.paper-fig img{{max-width:min(100%,560px);border:1px solid #e6e6e6;border-radius:10px;box-shadow:0 1px 6px rgba(0,0,0,.07)}}
.paper-fig figcaption{{font-size:.8rem;color:#777;margin-top:.45rem;font-style:italic;line-height:1.5}}
.fig-gallery{{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:1.1rem;margin:1.3rem 0}}
.fig-gallery .paper-fig{{margin:0}}
.connection-map{{margin:2.4rem 0 1.2rem}}
.flow-wrap{{position:relative;margin:1.2rem 0 0;padding-left:1.2rem}}
.flow-wrap:before{{content:"";position:absolute;left:2.05rem;top:.4rem;bottom:.4rem;width:3px;background:linear-gradient(#7c3aed,#f97316);border-radius:999px;opacity:.35}}
.flow-item{{position:relative;margin:0 0 1.25rem 0;padding-left:2.1rem}}
.flow-index{{position:absolute;left:-2.35rem;top:.72rem;width:1.7rem;height:1.7rem;border-radius:50%;background:#7c3aed;color:#fff;display:inline-flex;align-items:center;justify-content:center;font-size:.8rem;font-weight:800;box-shadow:0 0 0 5px #fbfbfd}}
.flow-node{{border-radius:16px;background:#fff;border:1px solid #eadcff;box-shadow:0 1px 8px rgba(76,29,149,.06);overflow:visible}}
.flow-node summary{{list-style:none;cursor:pointer;position:relative;padding:1.15rem 1rem .85rem 1rem;display:block}}
.flow-node summary::-webkit-details-marker{{display:none}}
.flow-year{{position:absolute;top:-.72rem;left:.85rem;background:#111827;color:#fff;border-radius:999px;padding:.12rem .58rem;font-weight:900;font-size:.88rem;letter-spacing:.01em;box-shadow:0 2px 8px rgba(0,0,0,.16)}}
.flow-year.mini{{position:static;display:inline-block;margin-left:.35rem;background:#6b7280;font-size:.74rem;vertical-align:middle}}
.flow-label{{display:block;font-size:1.02rem;font-weight:900;color:#1f2937;line-height:1.35;margin-top:.15rem}}
.flow-title{{display:block;color:#6b7280;font-size:.78rem;line-height:1.35;margin-top:.2rem}}
.flow-detail{{border-top:1px solid #f2e8ff;padding:.65rem 1rem .9rem;color:#555;font-size:.83rem;line-height:1.55}}
.flow-detail a{{font-weight:700}}
.flow-branches{{margin:.55rem 0 0 1rem;padding-left:1rem;border-left:2px dashed #ddd}}
.branch-node{{margin:.55rem 0;border-color:#eee;background:#fffdf7;box-shadow:none}}
.branch-node summary{{padding:.7rem .85rem}}
.branch-node .flow-label{{font-size:.9rem;font-weight:800;color:#4b5563;margin-top:.35rem}}
.branch-rel{{display:inline-block;background:#f3e8ff;color:#6b21a8;font-size:.7rem;font-weight:800;border-radius:999px;padding:.08rem .48rem}}
@media (max-width:620px){{body{{padding:1.1rem .75rem}}.flow-wrap{{padding-left:.6rem}}.flow-wrap:before{{left:1.35rem}}.flow-item{{padding-left:1.55rem}}.flow-index{{left:-1.8rem}}.flow-title{{font-size:.74rem}}}}
</style>
{MATHJAX_HEAD}
</head><body>
<div class="hero"><div class="kicker">{H.escape(course)} · 제{lecture['lecture']}강 / {total}</div>
<h1>{H.escape(lecture['title'])}</h1><div style="opacity:.85;font-size:.9rem">{H.escape(lecture['scheduled_at'])}</div></div>
<div class="card"><h3>🎯 오늘의 학습목표</h3><ul>{obj}</ul></div>
<div class="card"><h3>📄 다루는 논문 ({len(core)}편, 내 코퍼스)</h3><ul>{core_html}</ul></div>
<div class="card"><h3>🔗 함께 보는 관련 연구 ({len(related)}편)</h3><ul>{rel_html}</ul></div>
{body}
{web_block}
{conn_map}
<div class="foot">Dashun Wang 연구 흐름 따라잡기 · Deeper Research 자동 다이제스트 · Paper Curation</div>
</body></html>"""


def make_progress_image(led, current):
    """전체 커리큘럼 진도 로드맵 PNG(현재 강 하이라이트). Pillow만 사용."""
    from PIL import Image, ImageDraw, ImageFont
    import io
    fcands = ["/System/Library/Fonts/AppleSDGothicNeo.ttc",
              "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
              "/System/Library/Fonts/Supplemental/Arial Unicode.ttf"]
    fp = next((p for p in fcands if os.path.exists(p)), None)

    def F(sz):
        return ImageFont.truetype(fp, sz) if fp else ImageFont.load_default()

    lecs = sorted(led["lectures"], key=lambda x: x["lecture"])
    total = led.get("total", len(lecs))
    n = len(lecs)
    S, W, padx, head_h, row_h = 2, 1000, 34, 150, 66     # S=2x 슈퍼샘플 → 선명
    Hpx = head_h + n * row_h + 26
    img = Image.new("RGB", (W * S, Hpx * S), "white")
    d = ImageDraw.Draw(img)
    DONE, CUR, CUR_BG = (46, 158, 91), (232, 89, 12), (255, 243, 230)
    UP, TXT, MUTE = (173, 181, 189), (33, 37, 41), (134, 142, 150)
    BAR_BG, LINE = (233, 236, 239), (238, 238, 238)

    def rrect(box, r, **kw):
        d.rounded_rectangle([c * S for c in box], radius=r * S, **kw)

    def circle(cx, cy, r, **kw):
        d.ellipse([(cx - r) * S, (cy - r) * S, (cx + r) * S, (cy + r) * S], **kw)

    def text(x, y, s, sz, fill, bold=False, anchor="la"):
        d.text((x * S, y * S), s, font=F(sz * S), fill=fill, anchor=anchor,
               stroke_width=(S if bold else 0), stroke_fill=fill)

    def tw(s, sz):
        return d.textlength(s, font=F(sz * S)) / S

    def clip(s, sz, maxw):
        if tw(s, sz) <= maxw:
            return s
        while s and tw(s + "…", sz) > maxw:
            s = s[:-1]
        return s + "…"

    text(padx, 30, clip(led.get("course", "커리큘럼"), 30, W - 2 * padx), 30, TXT, bold=True)
    text(padx, 74, f"전체 진도  {current} / {total}강", 21, CUR, bold=True)
    by, bh = 116, 16
    rrect((padx, by, W - padx, by + bh), bh // 2, fill=BAR_BG)
    fillw = padx + (W - 2 * padx) * max(0.0, min(1.0, current / total))
    if fillw > padx + bh:
        rrect((padx, by, fillw, by + bh), bh // 2, fill=CUR)

    for i, L in enumerate(lecs):
        num = L["lecture"]
        cy = head_h + i * row_h + row_h // 2
        state = "cur" if num == current else ("done" if L.get("status") == "done" else "up")
        col = {"done": DONE, "cur": CUR, "up": UP}[state]
        if state == "cur":
            rrect((padx - 8, cy - row_h // 2 + 5, W - padx + 8, cy + row_h // 2 - 5), 12,
                  fill=CUR_BG, outline=CUR, width=2 * S)
        else:
            d.line([(padx * S, (cy + row_h // 2 - 4) * S), ((W - padx) * S, (cy + row_h // 2 - 4) * S)],
                   fill=LINE, width=S)
        circle(padx + 22, cy, 19, fill=col)
        text(padx + 22, cy - 1, str(num), 19, (255, 255, 255), bold=True, anchor="mm")
        tcol = TXT if state != "up" else MUTE
        title = clip(f"제{num}강  {L['title']}", 22, W - (padx + 58) - 120)
        text(padx + 58, cy - 13, title, 22, tcol, bold=(state == "cur"))
        text(padx + 58, cy + 14, L.get("scheduled_at", "")[5:16], 13, MUTE)
        badge = {"cur": "오늘", "done": "완료", "up": "예정"}[state]
        text(W - padx, cy - 1, badge, 16, col, bold=(state == "cur"), anchor="rm")

    img = img.resize((W, Hpx), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# ── 이메일 (Resend, 다중 수신자) ─────────────────────────────────────────────
def send_email(recipients, subject, html_body, attachments):
    resend = os.environ.get("RESEND_API_KEY", "")
    if not resend:
        raise RuntimeError("RESEND_API_KEY 없음")
    lk = {}
    p = DOCS / "_local_keys.json"
    if p.exists():
        lk = json.loads(p.read_text(encoding="utf-8"))
    att = []
    for it in attachments:
        a = {"filename": it[0], "content": base64.b64encode(it[1]).decode()}
        if len(it) > 2 and it[2]:                       # 인라인 이미지(cid)
            a["content_id"] = it[2]
            ext = it[0].rsplit(".", 1)[-1].lower()
            a["content_type"] = {"png": "image/png", "jpg": "image/jpeg",
                                  "jpeg": "image/jpeg", "gif": "image/gif"}.get(ext, "application/octet-stream")
        att.append(a)
    payload = {
        "from": lk.get("audio_from") or "Paper Curation <onboarding@resend.dev>",
        "to": recipients, "subject": subject, "html": html_body,
        "attachments": att,
    }
    if lk.get("audio_reply_to"):
        payload["reply_to"] = lk["audio_reply_to"]
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + resend, "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (paper-curation lecture agent)"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            return r.status, r.read().decode("utf-8", "replace")[:300]
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")[:300]


# ── 오케스트레이션 ───────────────────────────────────────────────────────────


def artifacts_ready(L, args):
    """OUTDIR 에 이 강의 산출물이 이미 준비돼 있는지(구버전 artifacts 포함)."""
    n = L["lecture"]
    if not (OUTDIR / f"lecture_{n:02d}.html").exists():
        return False
    if not args.no_audio and not (OUTDIR / f"lecture_{n:02d}.mp3").exists():
        return False
    return True


def build_lecture(L, led, args):
    """미리 생성: 리포트→대본→오디오→HTML→연구지도 를 만들어 OUTDIR 에 저장. 메일 없음."""
    total, course = led["total"], led["course"]
    n = L["lecture"]
    print(f"[build] 제{n}강/{total} · {L['title']} · 핵심 {len(L['papers'])}편")
    client = genai.Client(api_key=get_google_key())

    prior = prior_lectures_brief(led, n)
    print("  1) 근거 수집 (핵심 + 연결 그래프)")
    evidence, core, related = gather_evidence(L)
    print(f"     근거 {len(evidence):,}자 · 관련논문 {len(related)}편")
    print("  2) Deeper Research 리포트 합성 (웹 검색 포함)")
    report, web_sources = synthesize_report(course, L, evidence, client, prior)
    if not report:
        raise RuntimeError("리포트 합성 실패")
    print(f"     리포트 {len(report):,}자 · 웹 출처 {len(web_sources)}개")

    OUTDIR.mkdir(parents=True, exist_ok=True)
    html = make_html(course, L, report, core, related, web_sources, total)
    import lecture_map
    html = lecture_map.assemble_report_html(L["lecture"], html, report)
    have_map = False
    try:
        _mp = OUTDIR / f"lecture_{n:02d}_map.png"
        lecture_map.build_map(n, str(_mp), report)
        have_map = _mp.exists()
        if have_map:
            print(f"     연구 지도 생성 ({_mp.stat().st_size / 1024:.0f}KB)")
    except Exception as e:
        print("     ⚠️ 연구 지도 이미지 생성 실패:", e)
    (OUTDIR / f"lecture_{n:02d}.html").write_text(html, encoding="utf-8")
    (OUTDIR / f"lecture_{n:02d}_report.md").write_text(report, encoding="utf-8")

    audio_mb = 0.0
    if not args.no_audio:
        print("  3) 오디오 생성 (30분 이상·40분 목표·최대 50분)")
        mp3, script, dur = make_audio(report, evidence, client, minutes=led.get("audio", {}).get("minutes", 40), prior=prior)
        (OUTDIR / f"lecture_{n:02d}.mp3").write_bytes(mp3)
        (OUTDIR / f"lecture_{n:02d}_script.txt").write_text(script, encoding="utf-8")
        audio_mb = round(len(mp3) / 1048576, 1)
        print(f"     오디오 {audio_mb}MB · ~{dur:.0f}분 · 대본 {len(script):,}자")

    L["build"] = {"related": len(related), "web_sources": len(web_sources),
                  "audio_mb": audio_mb, "map": have_map, "no_audio": bool(args.no_audio),
                  "built_at": datetime.now().strftime("%Y-%m-%d %H:%M")}
    if L.get("status") != "done":
        L["status"] = "built"
    save_ledger(led)
    print(f"[build] 제{n}강 미리 생성 완료 → {OUTDIR}")
    return True


def deliver_lecture(L, led, args):
    """미리 만든 산출물을 불러와 메일만 발송(새 규칙). 산출물이 없으면 먼저 생성."""
    total, course = led["total"], led["course"]
    n = L["lecture"]
    recipients = args.recipients or led["recipients"]
    html_path = OUTDIR / f"lecture_{n:02d}.html"
    mp3_path = OUTDIR / f"lecture_{n:02d}.mp3"
    map_path = OUTDIR / f"lecture_{n:02d}_map.png"
    if not html_path.exists() or (not args.no_audio and not mp3_path.exists()):
        print(f"[deliver] 제{n}강 산출물 없음 → 먼저 생성")
        build_lecture(L, led, args)

    b = L.get("build") or L.get("artifacts") or {}
    tag = re.sub(r"[^가-힣A-Za-z0-9]+", "_", L["title"])[:30]
    html = html_path.read_text(encoding="utf-8")
    attachments = [(f"제{n}강_{tag}.html", html.encode("utf-8"))]

    map_png_html = ""
    if map_path.exists():
        attachments.append(("연구지도.png", map_path.read_bytes(), "lecture-map"))
        map_png_html = (
            '<div style="margin:8px 0 16px"><img src="cid:lecture-map" alt="연구 지도" '
            'style="width:100%;max-width:760px;border:1px solid #eee;border-radius:12px">'
            '<p style="color:#777;font-size:.82rem;margin:6px 0 0">'
            '이번 강의가 짚는 논문의 흐름 · 첨부 HTML 상단에 마우스오버·클릭이 되는 인터랙티브 지도가 있습니다.</p></div>')

    if not args.no_audio and mp3_path.exists():
        mp3 = mp3_path.read_bytes()
        if len(mp3) < 28 * 1024 * 1024:            # Resend 40MB 한도(base64 ~1.37x) 안전선
            attachments.append((f"제{n}강.mp3", mp3))
            print(f"     오디오 첨부 {len(mp3)/1048576:.1f}MB")
        else:
            print(f"     ⚠️ {len(mp3)/1048576:.1f}MB — 이메일 한도 초과, 첨부 제외(OUTDIR 에 보관)")

    prog_img_html = ""
    try:
        prog = make_progress_image(led, n)
        attachments.append(("커리큘럼_진도.png", prog, "lecture-progress"))
        prog_img_html = ('<div style="margin:10px 0 16px"><img src="cid:lecture-progress" '
                         'alt="전체 진도" style="width:100%;max-width:640px;border:1px solid #eee;border-radius:10px"></div>')
    except Exception as e:
        print("     ⚠️ 진도 이미지 생성 실패:", e)

    print("  4) 메일 발송")
    related_n, web_n = b.get("related", 0), b.get("web_sources", 0)
    obj_html = "".join(f"<li>{H.escape(o)}</li>" for o in L["objectives"])
    body = (f"<p><b>{H.escape(course)} · 제{L['lecture']}강 / {total}</b></p><h2>{H.escape(L['title'])}</h2>"
            f"{prog_img_html}{map_png_html}"
            f"<p><b>🎯 오늘의 학습목표</b></p><ul>{obj_html}</ul>"
            f"<p>첨부: Deeper Research 리포트(HTML, 상단에 인터랙티브 연구지도) · 관련 연구 {related_n}편 · 웹 출처 {web_n}개"
            + ("" if args.no_audio else " + Audio Overview(MP3, 2인·전문가·학술)") + "</p>"
            f"<p style='color:#999;font-size:.85rem'>Paper Curation 자동 발송 · {L['scheduled_at']}</p>")
    st, resp = send_email(recipients, f"[제{L['lecture']}강/{total}] {L['title']}", body, attachments)
    ok = st == 200
    print(f"     이메일 {'성공' if ok else '실패'} (status {st}) {resp}  → {recipients}")

    # 수신자 override(--recipients) 나 --no-mark 는 테스트/재발송 → 예정 배포(status)를 건드리지 않는다.
    if ok and not args.no_audio and not args.recipients and not args.no_mark:
        L["status"] = "done"
        L["completed_at"] = datetime.now().strftime("%Y-%m-%d %H:%M")
        L["artifacts"] = {"related": related_n, "web_sources": web_n, "audio_mb": b.get("audio_mb", 0)}
        save_ledger(led)
        print(f"[deliver] 제{n}강 done 기록.")
    return ok


def process_lecture(L, led, args):
    if args.force or not artifacts_ready(L, args):
        build_lecture(L, led, args)
    if args.build:
        return True
    return deliver_lecture(L, led, args)


def select_lectures(led, args):
    lecs = sorted(led["lectures"], key=lambda x: x["lecture"])
    if args.all:
        return lecs
    if args.lecture:
        m = [L for L in lecs if L["lecture"] == args.lecture]
        if not m:
            sys.exit(f"제{args.lecture}강 없음")
        return m
    now = datetime.now()
    due = [L for L in lecs if L.get("status") != "done"
           and datetime.strptime(L["scheduled_at"], "%Y-%m-%d %H:%M") <= now]
    if not due:
        print("[agent] 지금 발화할 예정 강 없음.")
        return []
    return [min(due, key=lambda L: L["scheduled_at"])]


def main():
    ap = argparse.ArgumentParser(description="Dashun Wang 강의 다이제스트 엔진 (미리 생성 + 예정시각 메일)")
    ap.add_argument("--lecture", type=int, help="특정 강 번호")
    ap.add_argument("--due", action="store_true", help="지금 시각 기준 다음 예정 강 1개")
    ap.add_argument("--all", action="store_true", help="전체 강(1~N)")
    ap.add_argument("--build", action="store_true", help="미리 생성만(대본·오디오·HTML), 메일 없음")
    ap.add_argument("--deliver", action="store_true", help="미리 만든 산출물로 메일만 발송")
    ap.add_argument("--recipients", help="수신자 override(콤마 구분). 지정 시 status 미변경(재발송/테스트)")
    ap.add_argument("--no-mark", action="store_true", help="발송해도 done 표시 안 함")
    ap.add_argument("--force", action="store_true", help="산출물이 있어도 재생성")
    ap.add_argument("--no-audio", action="store_true", help="오디오 생략(검증용)")
    args = ap.parse_args()
    if not (args.lecture or args.due or args.all):
        ap.error("--lecture N / --due / --all 중 하나 필요")
    args.recipients = ([r.strip() for r in args.recipients.split(",") if r.strip()]
                       if args.recipients else None)
    led = load_ledger()
    targets = select_lectures(led, args)
    ok_all = True
    for L in targets:
        try:
            ok_all = process_lecture(L, led, args) and ok_all
        except SystemExit:
            raise
        except Exception as e:
            ok_all = False
            print(f"[error] 제{L.get('lecture')}강 처리 실패: {e!r} — 다음 강 계속", flush=True)
    return 0 if ok_all else 1


if __name__ == "__main__":
    from _env_guard import force_py312
    force_py312()
    sys.exit(main())