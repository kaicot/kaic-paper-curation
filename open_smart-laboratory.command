#!/bin/bash
# Serve docs/ at http://localhost:8000 (/api/embed Gemini proxy + 로컬 키 즉석주입) → smart-laboratory 열기.
# 8000 에 서버가 떠 있어도 실제 응답하는지 curl 헬스체크 — 응답 없는 좀비면 kill 후 재기동.
SUBPATH="smart-laboratory/"
cd "$(dirname "$0")" 2>/dev/null
[ -f pipeline/serve_local.py ] || cd "/Users/jehyunlee/Documents/내노트북/paper-curation" || exit 1
alive() { curl -fsS -m 5 -o /dev/null "http://localhost:8000/"; }

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
