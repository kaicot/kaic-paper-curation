"""test_relevance.py - 하이브리드 검색 관련성 필터 테스트.

검증 항목:
  1. SYNONYM 케이스: 키워드 모드는 동의어-only 논문을 탈락시킨다.
  2. hybrid 모드(STUB 의미 클라이언트 = 높은 점수)는 그 동의어 논문을 RESCUE 하고,
     키워드만 걸린 무관 논문은 DROP 한다.
  3. 예외를 던지는 클라이언트 → 우아한 폴백(크래시 없음): semantic_relevance 는
     {} 반환, _score_hybrid 는 False(키워드 폴백 신호), combined_score 는 키워드 유지.
  4. ANTHROPIC_API_KEY 가 있으면 작은 실제 Haiku 호출 한 번(스모크).

네트워크 검색(arXiv/S2/OpenAlex)은 절대 호출하지 않는다.

실행:
  PYTHONUTF8=1 /opt/homebrew/Caskroom/miniconda/base/envs/py312/bin/python \
      pipeline/tests/test_relevance.py
"""

import json
import os
import re
import sys
import tempfile

# pipeline 디렉토리를 path 에 추가 (search_papers / lib import 용).
_HERE = os.path.dirname(os.path.abspath(__file__))
_PIPELINE = os.path.dirname(_HERE)
if _PIPELINE not in sys.path:
    sys.path.insert(0, _PIPELINE)

import search_papers  # noqa: E402
from lib.relevance import (  # noqa: E402
    combined_score,
    expand_keywords,
    semantic_relevance,
)

THRESHOLD = 0.3  # search_papers --threshold 기본값

# 토픽: "remote control" — 동의어로 teleoperation/manipulation 등이 있다.
PRIMARY = ["remote control"]
SECONDARY = ["surgical"]

# 동의어-only 논문: 'remote control'/'surgical' 어느 것도 substring 으로 안 걸린다.
SYNONYM_PAPER = {
    "title": "Bilateral teleoperation of a robotic manipulator",
    "abstract": "We present a force-feedback scheme for teleoperation and remote "
                "manipulation of a slave arm by a human operator.",
}
# 키워드는 걸리지만(=primary 'remote control') 의미상 무관한 논문.
TV_REMOTE_PAPER = {
    "title": "Remote control of a television via infrared",
    "abstract": "A consumer-electronics infrared remote control for switching TV "
                "channels and adjusting volume.",
}


# ---------------------------------------------------------------------------
# Fake / stub Anthropic 클라이언트 (네트워크 없음)
# ---------------------------------------------------------------------------

class _Block:
    def __init__(self, text):
        self.text = text


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]


class _Messages:
    def __init__(self, handler):
        self._handler = handler

    def create(self, *, model, max_tokens, messages, **kw):
        prompt = messages[0]["content"]
        return _Resp(self._handler(prompt))


class StubClient:
    """handler(prompt) -> response_text 로 동작하는 가짜 클라이언트."""

    def __init__(self, handler):
        self.messages = _Messages(handler)

    def with_options(self, **kw):
        return self


class RaisingClient:
    """messages.create 가 항상 예외를 던지는 클라이언트."""

    class _M:
        def create(self, **kw):
            raise RuntimeError("simulated network failure")

    def __init__(self):
        self.messages = RaisingClient._M()

    def with_options(self, **kw):
        return self


def _semantic_handler(prompt):
    """프롬프트의 [idx] 블록을 읽어, 로보틱 teleoperation 논문엔 높은 점수,
    그 외엔 낮은 점수를 매기는 JSON 객체를 반환."""
    parts = re.split(r"\[(\d+)\]", prompt)
    scores = {}
    # parts = [pre, idx, block, idx, block, ...]
    for i in range(1, len(parts) - 1, 2):
        idx = parts[i]
        block = parts[i + 1].lower()
        if "teleop" in block or "manipulat" in block:
            scores[idx] = 95
        else:
            scores[idx] = 5
    return json.dumps(scores)


# ---------------------------------------------------------------------------
# 테스트
# ---------------------------------------------------------------------------

def test_keyword_mode_rejects_synonym():
    """키워드 모드: 동의어-only 논문은 임계값 미만 → 탈락."""
    syn = search_papers.score_relevance(SYNONYM_PAPER, PRIMARY, SECONDARY)
    tv = search_papers.score_relevance(TV_REMOTE_PAPER, PRIMARY, SECONDARY)
    assert syn < THRESHOLD, f"동의어 논문 키워드 점수 {syn} 가 임계값 미만이어야 함"
    assert tv >= THRESHOLD, f"TV 리모컨 논문 키워드 점수 {tv} 는 임계값 이상이어야 함"
    print(f"[1] keyword 모드: synonym={syn} (탈락), tv-remote={tv} (통과) OK")


def test_hybrid_rescues_synonym_and_drops_irrelevant():
    """hybrid(STUB): 동의어 논문 RESCUE, 키워드만 걸린 무관 논문 DROP."""
    papers = [dict(SYNONYM_PAPER), dict(TV_REMOTE_PAPER)]
    stub = StubClient(_semantic_handler)
    with tempfile.TemporaryDirectory() as cache_dir:
        ok = search_papers._score_hybrid(
            papers, "remote-control-test", PRIMARY, SECONDARY,
            client=stub, cache_dir=cache_dir,
        )
    assert ok is True, "hybrid 채점이 성공(True)해야 함"
    syn_score = papers[0]["relevance_score"]
    tv_score = papers[1]["relevance_score"]
    assert syn_score >= THRESHOLD, f"동의어 논문이 RESCUE 되어야 함 (got {syn_score})"
    assert tv_score < THRESHOLD, f"무관 논문이 DROP 되어야 함 (got {tv_score})"
    print(f"[2] hybrid 모드: synonym={syn_score} (RESCUE), "
          f"tv-remote={tv_score} (DROP) OK")


def test_semantic_relevance_scores_in_range():
    """STUB 의미 채점이 papers index → [0,1] dict 를 반환하는지."""
    papers = [dict(SYNONYM_PAPER), dict(TV_REMOTE_PAPER)]
    stub = StubClient(_semantic_handler)
    sem = semantic_relevance(papers, "remote control of robots", stub)
    assert set(sem.keys()) == {0, 1}, f"인덱스 키 {set(sem.keys())}"
    assert all(0.0 <= v <= 1.0 for v in sem.values()), sem
    assert sem[0] > sem[1], f"teleop 논문(0) 점수가 더 높아야 함: {sem}"
    print(f"[3] semantic_relevance(STUB): {sem} OK")


def test_combined_score_blend_and_passthrough():
    """blend 동작 + semantic None 패스스루."""
    # 키워드 0 + 의미 0.95 → 0.475 (RESCUE)
    assert abs(combined_score(0.0, 0.95) - 0.475) < 1e-9
    # 키워드 0.5 + 의미 0.05 → 0.275 (DROP, 임계 0.3 미만)
    assert combined_score(0.5, 0.05) < THRESHOLD
    # 의미 None → 키워드 점수 그대로 (폴백)
    assert combined_score(0.5, None) == 0.5
    assert combined_score(0.0, None) == 0.0
    print("[4] combined_score blend + None 패스스루 OK")


def test_raising_client_graceful_fallback():
    """예외 클라이언트 → 크래시 없이 우아한 폴백."""
    papers = [dict(SYNONYM_PAPER), dict(TV_REMOTE_PAPER)]
    rc = RaisingClient()

    # semantic_relevance 는 예외를 삼키고 {} 반환.
    sem = semantic_relevance(papers, "remote control", rc)
    assert sem == {}, f"예외 시 빈 dict 여야 함 (got {sem})"

    # expand_keywords 도 예외를 삼키고 [] 반환.
    exp = expand_keywords("t", PRIMARY, SECONDARY, rc, cache_dir=None)
    assert exp == [], f"예외 시 빈 리스트여야 함 (got {exp})"

    # _score_hybrid 는 False(키워드 폴백 신호) 반환, 점수는 세팅 안 됨.
    with tempfile.TemporaryDirectory() as cache_dir:
        ok = search_papers._score_hybrid(
            papers, "t", PRIMARY, SECONDARY, client=rc, cache_dir=cache_dir,
        )
    assert ok is False, "예외 시 _score_hybrid 는 False(폴백)여야 함"

    # 폴백 경로(키워드-only)가 정상 동작하는지.
    search_papers._score_keyword_only(papers, PRIMARY, SECONDARY)
    assert papers[1]["relevance_score"] >= THRESHOLD  # tv-remote 는 키워드로 통과
    print("[5] raising-client 우아한 폴백 (no crash) OK")


def test_keyword_mode_byte_identical():
    """SEARCH_RELEVANCE_MODE=keyword 면 _score_keyword_only 와 동일 점수."""
    papers = [dict(SYNONYM_PAPER), dict(TV_REMOTE_PAPER)]
    search_papers._score_keyword_only(papers, PRIMARY, SECONDARY)
    expected = [round(search_papers.score_relevance(p, PRIMARY, SECONDARY), 3)
                for p in (SYNONYM_PAPER, TV_REMOTE_PAPER)]
    assert [p["relevance_score"] for p in papers] == expected
    print("[6] keyword 모드 점수 = score_relevance (바이트 동일) OK")


def test_real_haiku_call_optional():
    """ANTHROPIC_API_KEY 가 있으면 작은 실제 Haiku 호출 한 번(스모크).

    실제 호출은 비결정적이고 망 변동이 있으므로, 반환 타입/범위만 확인하고
    실패는 (NOTE) 로만 남겨 테스트 스위트를 깨뜨리지 않는다.
    """
    key = os.environ.get("ANTHROPIC_API_KEY") or ""
    if not key:
        print("[7] (skip) ANTHROPIC_API_KEY 없음 — 실제 호출 생략")
        return
    try:
        from anthropic import Anthropic
        client = Anthropic(api_key=key, timeout=60.0, max_retries=1)
        papers = [
            dict(SYNONYM_PAPER),
            {"title": "A study of medieval poetry",
             "abstract": "Analysis of 14th-century verse forms."},
        ]
        sem = semantic_relevance(
            papers, "remote control / teleoperation of robotic systems", client)
        assert isinstance(sem, dict)
        assert all(0.0 <= v <= 1.0 for v in sem.values()), sem
        print(f"[7] 실제 Haiku 호출 OK: {sem}")
    except Exception as e:  # noqa: BLE001 — 스모크: 망 변동을 NOTE 로만
        print(f"[7] (NOTE) 실제 Haiku 호출 실패(비치명적): {str(e)[:120]}")


_TESTS = [
    test_keyword_mode_rejects_synonym,
    test_hybrid_rescues_synonym_and_drops_irrelevant,
    test_semantic_relevance_scores_in_range,
    test_combined_score_blend_and_passthrough,
    test_raising_client_graceful_fallback,
    test_keyword_mode_byte_identical,
    test_real_haiku_call_optional,
]


def main():
    failures = 0
    for t in _TESTS:
        try:
            t()
        except AssertionError as e:
            failures += 1
            print(f"FAIL {t.__name__}: {e}")
        except Exception as e:  # noqa: BLE001
            failures += 1
            print(f"ERROR {t.__name__}: {type(e).__name__}: {e}")
    print("-" * 60)
    if failures:
        print(f"RESULT: {failures}/{len(_TESTS)} FAILED")
        sys.exit(1)
    print(f"RESULT: all {len(_TESTS)} passed")


if __name__ == "__main__":
    main()
