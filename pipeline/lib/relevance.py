"""relevance.py - 하이브리드(키워드 + 의미) 검색 관련성 필터 유틸.

search_papers.py 의 순수 substring 키워드 매칭(score_relevance)을 보완한다.

세 가지 공개 함수:
  - expand_keywords(topic, primary, secondary, client) -> list[str]
      Haiku 한 번 호출로 동의어/약어/유사 표현을 뽑아 키워드 recall 을 높인다.
      (cache_dir 가 주어지면 JSON 으로 캐시.) 실패 시 [] 반환(우아한 폴백).
  - semantic_relevance(papers, topic_description, client) -> dict[int, float]
      제목+초록을 배치로 Haiku 에게 보내 토픽 관련성 0~1 점수를 받는다.
      papers 리스트 인덱스 → 점수. 실패한 배치는 결과에서 빠진다(부분 결과 허용).
  - combined_score(keyword_score, semantic_score) -> float
      키워드 점수와 의미 점수를 blend. semantic_score 가 None 이면 키워드 점수 그대로.

모든 함수는 예외를 밖으로 던지지 않는다 — LLM/네트워크/파싱 실패는 내부에서
삼키고 빈 결과 또는 키워드-only 동작으로 폴백한다. 그래서 호출부는 안전하게
hybrid 를 시도했다가 결과가 비면 기존 키워드 경로로 떨어질 수 있다.

Anthropic 클라이언트는 호출부가 만들어 넘긴다(키 해석/재시도 정책은 호출부 책임).
이 모듈은 client.messages.create(...) 만 사용한다.
"""

import json
import re
import sys

# search_papers / extract_insights 와 동일한 분류용 저비용 모델.
DEFAULT_MODEL = "claude-haiku-4-5"


def _warn(msg):
    print(f"  경고: {msg}", file=sys.stderr)


def _extract_json(text, opener):
    """LLM 응답에서 첫 번째 JSON 객체('{') 또는 배열('[') 조각을 추출해 파싱.

    opener: '{' 또는 '['. 코드펜스/설명문이 섞여 와도 균형 잡힌 첫 블록만 잘라
    json.loads 한다. 실패하면 None.
    """
    closer = "}" if opener == "{" else "]"
    start = text.find(opener)
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        c = text[i]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except Exception:
                    return None
    return None


# ---------------------------------------------------------------------------
# (a) 키워드 확장
# ---------------------------------------------------------------------------

def _keywords_hash(primary, secondary):
    import hashlib
    blob = "".join(sorted(primary)) + "" + "".join(sorted(secondary))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def expand_keywords(topic, primary, secondary, client, *, cache_dir=None,
                    model=DEFAULT_MODEL, max_terms=15):
    """동의어/약어/유사 표현을 Haiku 한 번 호출로 확장. 실패 시 [].

    이미 primary/secondary 에 있는 용어(대소문자 무시)는 제외하고 새 용어만 반환.
    cache_dir 가 주어지면 <cache_dir>/_keyword_expansion.json 에 캐시하고,
    키워드 해시가 같으면 재사용(LLM 호출 0회).
    """
    primary = list(primary or [])
    secondary = list(secondary or [])
    existing = {k.lower() for k in primary + secondary}

    cache_path = None
    if cache_dir is not None:
        try:
            from pathlib import Path
            cache_path = Path(cache_dir) / "_keyword_expansion.json"
        except Exception:
            cache_path = None

    kw_hash = _keywords_hash(primary, secondary)

    # 1) 캐시 히트?
    if cache_path is not None:
        try:
            if cache_path.exists():
                cached = json.loads(cache_path.read_text(encoding="utf-8"))
                if cached.get("hash") == kw_hash and isinstance(cached.get("terms"), list):
                    return [t for t in cached["terms"] if t.lower() not in existing]
        except Exception as e:
            _warn(f"keyword 확장 캐시 읽기 실패: {str(e)[:80]}")

    if client is None:
        return []

    prompt = (
        "You expand a set of literature-search keywords with synonyms, acronyms, "
        "and closely related terms to improve recall when searching academic papers.\n\n"
        f"TOPIC: {topic}\n"
        f"PRIMARY KEYWORDS: {', '.join(primary) or '(none)'}\n"
        f"SECONDARY KEYWORDS: {', '.join(secondary) or '(none)'}\n\n"
        f"List up to {max_terms} additional search terms (synonyms, acronym/expansion "
        "pairs, common alternative phrasings) that a paper on this topic might use but "
        "that are NOT already in the lists above. Prefer concrete, high-precision terms.\n"
        "Output ONLY a JSON array of strings, e.g. [\"teleoperation\", \"remote manipulation\"]. "
        "No other text."
    )

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        text = resp.content[0].text
    except Exception as e:
        _warn(f"keyword 확장 LLM 호출 실패: {str(e)[:80]}")
        return []

    arr = _extract_json(text, "[")
    if not isinstance(arr, list):
        _warn("keyword 확장 응답 파싱 실패 (JSON array 아님)")
        return []

    seen = set()
    terms = []
    for item in arr:
        if not isinstance(item, str):
            continue
        t = item.strip()
        low = t.lower()
        if not t or low in existing or low in seen:
            continue
        seen.add(low)
        terms.append(t)

    # 2) 캐시 기록 (성공 시에만)
    if cache_path is not None and terms:
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps({"hash": kw_hash, "terms": terms}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            _warn(f"keyword 확장 캐시 쓰기 실패: {str(e)[:80]}")

    return terms


# ---------------------------------------------------------------------------
# (b) 의미 관련성 (배치 Haiku)
# ---------------------------------------------------------------------------

def _build_semantic_prompt(topic_description, batch, offset):
    """batch(=papers slice)를 글로벌 인덱스 라벨([offset], [offset+1], ...)로 포맷."""
    lines = []
    for j, p in enumerate(batch):
        gi = offset + j
        title = (p.get("title", "") or "")[:200].replace("\n", " ")
        abstract = (p.get("abstract", "") or "")[:600].replace("\n", " ")
        lines.append(f"[{gi}] Title: {title}\n    Abstract: {abstract or '(no abstract)'}")
    body = "\n".join(lines)
    return (
        "You are screening academic papers for relevance to a research topic.\n\n"
        f"TOPIC: {topic_description}\n\n"
        "For each paper below, output a relevance score from 0 to 100 where:\n"
        "  100 = squarely on-topic, central to the topic\n"
        "  50  = partially related / tangential\n"
        "  0   = unrelated to the topic\n\n"
        f"Papers:\n{body}\n\n"
        "Respond with ONLY a JSON object mapping each paper index (as a string) to its "
        "integer score, e.g. {\"0\": 90, \"1\": 15}. No other text."
    )


def _normalize_score(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f > 1.0:
        f = f / 100.0
    if f < 0.0:
        f = 0.0
    if f > 1.0:
        f = 1.0
    return f


def semantic_relevance(papers, topic_description, client, *, model=DEFAULT_MODEL,
                       batch_size=20):
    """papers 각각의 토픽 의미 관련성을 배치 Haiku 로 채점. dict[index]->score(0~1).

    papers 리스트의 위치(index)를 키로 쓴다. 점수를 받지 못한(파싱/호출 실패) 배치의
    인덱스는 결과에서 빠지므로, 호출부는 누락 인덱스를 "의미 신호 없음"으로 보고
    키워드 점수로 폴백할 수 있다. 어떤 경우에도 예외를 밖으로 던지지 않는다.
    """
    scores: dict = {}
    if client is None or not papers:
        return scores

    n = len(papers)
    for offset in range(0, n, batch_size):
        batch = papers[offset:offset + batch_size]
        prompt = _build_semantic_prompt(topic_description, batch, offset)
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = resp.content[0].text
        except Exception as e:
            _warn(f"semantic 관련성 LLM 호출 실패 (batch {offset}): {str(e)[:80]}")
            continue

        obj = _extract_json(text, "{")
        if not isinstance(obj, dict):
            _warn(f"semantic 관련성 응답 파싱 실패 (batch {offset})")
            continue

        for k, v in obj.items():
            try:
                idx = int(k)
            except (TypeError, ValueError):
                continue
            if idx < 0 or idx >= n:
                continue
            s = _normalize_score(v)
            if s is not None:
                scores[idx] = s

    return scores


# ---------------------------------------------------------------------------
# (c) 점수 blend
# ---------------------------------------------------------------------------

def combined_score(keyword_score, semantic_score, *, kw_weight=0.5, sem_weight=0.5):
    """키워드 점수 + 의미 점수 blend.

    semantic_score 가 None 이면(=해당 paper 의 의미 신호 없음) 키워드 점수를 그대로
    돌려준다 → 의미 채점 실패 시 기존 키워드-only 동작 보존.

    blend(기본 50/50)은 두 가지를 동시에 만족시킨다:
      * 키워드가 0 이라도 의미가 높으면 점수가 살아나 임계값(예: 0.3)을 넘는다 → RESCUE
      * 키워드는 약하게 걸렸지만(예: 0.5) 의미가 낮으면 점수가 임계값 밑으로 → DROP
    """
    if semantic_score is None:
        return keyword_score
    kw = max(0.0, min(1.0, float(keyword_score)))
    sem = max(0.0, min(1.0, float(semantic_score)))
    return kw_weight * kw + sem_weight * sem
