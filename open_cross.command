#!/bin/bash
# Cross-topic 통합 Deep/Deeper Research (로컬 전용) — 더블클릭 한 번에 열기.
# 전 토픽 검색 인덱스를 slug dedup 병합(빠름) → docs/_cross/ → serve_local(8000) → 브라우저.
# 8000 에 좀비 서버면 정리 후 재기동. 병합 실패해도 기존 _cross 페이지로 서빙 시도.
SUBPATH="_cross/"
cd "$(dirname "$0")" 2>/dev/null
[ -f pipeline/serve_local.py ] || cd "/Users/jehyunlee/Documents/내노트북/paper-curation" || exit 1
alive() { curl -fsS -m 5 -o /dev/null "http://localhost:8000/"; }

# 최신 상태로 통합 인덱스 재생성 (모든 토픽 slug dedup 병합; 실패해도 계속 진행).
echo "통합 Deep Research 인덱스 병합 중... (모든 토픽 — 수 초 소요)"
python3 pipeline/build_cross_index.py || echo "경고: 병합 실패 — 기존 _cross 페이지로 서빙합니다."

if lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1 && ! alive; then
  echo "포트 8000 응답 없음(좀비 서버) — 정리 후 재기동합니다."
  lsof -nP -tiTCP:8000 -sTCP:LISTEN | xargs kill -9 2>/dev/null
  sleep 1
fi
if ! lsof -nP -iTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then
  python3 pipeline/serve_local.py --port 8000 &
  SERVER_PID=$!
  for i in $(seq 1 20); do alive && break; sleep 0.5; done
fi
open "http://localhost:8000/${SUBPATH}"
if [ -n "${SERVER_PID:-}" ]; then wait "$SERVER_PID"; fi
