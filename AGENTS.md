# AGENTS.md

이 파일은 Codex(및 유사 에이전트)가 이 저장소에서 작업할 때 따를 지침이다.

## 🚫 원작과의 모든 교류 금지 (운영자 지시 2026-08-13)

- 이 저장소는 **kaicot 포크**다. 원작 저장소(`jehyunlee/paper-curation`, upstream)에
  PR·이슈·기여·푸시·동기화를 **절대 수행하지 않는다.**
- upstream remote 는 조회용으로만 남겨두고 fetch/pull/merge/push 는 하지 않는다.
- 설치·호출부호·스킬 이름은 반드시 `kaic-paper-curation` 을 사용한다
  (원작의 `paper-curation` 과 혼동 금지).

## 이 포크의 정체성

**왕초보가 Codex에게 말만 하면 논문 리뷰가 완성되는 로컬 전용 도구.**

- 유료 API 키 불필요 (생성은 Codex saved-auth = ChatGPT 로그인)
- 배포 없음 (Cloudflare Worker / wrangler / gh-pages 전부 제거됨 — 로컬 서버 열람)
- Zotero API 키 없이도 로컬 Zotero(`zotero.sqlite`) 감지로 시작 가능
- PDF/URL 로 논문을 주면 Zotero 컬렉션 자동 생성·등록·리뷰까지 처리
- LLM(Codex)이 설치·일상 사용을 step-by-step 으로 안내 (위자드)

## 사용자 경험 원칙

사용자는 **코드를 치지 않는다.** 대화만 한다.

- "여기에 kaic-paper-curation 설치해줘: https://github.com/kaicot/kaic-paper-curation"
- "이 논문 넣어줘" + PDF 드래그 또는 주소
- "새 논문 리뷰해줘" / "웹에서 보고 싶어" / "몇 편 리뷰됐어?"

LLM(에이전트)은 아래 순서를 따른다:

1. **설치 위자드** — 한 번에 하나씩 질문 (Codex 로그인 → Zotero 컬렉션 →
   토픽 alias → PDF 폴더). API 키가 없으면 `inspect_local_zotero.py` 로 로컬 감지.
2. **일상 사용 위자드** — 요청을 명령으로 매핑하고 실행 (SKILL.md <Wizard_Daily_Use>).
3. **결과 안내** — 결과 요약 + `http://localhost:8000/{topic}/` URL 안내.
   서버가 꺼져 있으면 자동으로 `serve_local.py` 를 실행한다.

## 설치 플로우 (Codex)

사용자가 "설치해줘" + 저장소 주소를 주면:

1. 클론: `git clone https://github.com/kaicot/kaic-paper-curation.git`
2. 의존성: Python 3.12 환경에서 `pip install -r requirements.txt`
3. 정보 수집 (한 번에 하나씩):
   - Zotero API 키 (`ZOTERO_API_KEY` 환경변수 또는 로컬 Zotero 감지)
   - 이메일
   - **Zotero 컬렉션 이름** — API/로컬 DB로 검증, 없으면 목록 제시
   - 토픽 alias — 영문 소문자·숫자·`-`·`_` 만
   - Zotero PDF 폴더 (`C:\Users\<이름>\Zotero` 보통)
4. `config.json` 생성 → `doctor.py` 검증 → 첫 파이프라인 실행
5. `~/.codex/skills/kaic-paper-curation/` 에 스킬 설치
   (`python pipeline/setup.py --install-skill`)
6. 마무리: "브라우저에서 `http://localhost:8000/{topic}/` 을 여세요" 안내

## 일상 사용 (LLM 위자드)

| 사용자 말 | LLM 행동 |
|---|---|
| "이 논문 넣어줘" + PDF | `add_paper_to_zotero.py --pdf <경로> --collection <이름>` → curation 자동 |
| "이 주소 논문 넣어줘" + URL | `add_paper_to_zotero.py --url <URL> --collection <이름>` (arXiv/DOI 메타 추출) |
| "새 논문 리뷰해줘" | `run_full.py --topic <토픽> --mode curate --source zotero` |
| "이번 주 논문 찾아줘" | `run_full.py --topic <토픽> --mode curate --source web --days 7` |
| "분류 다시 해줘" | `run_full.py --topic <토픽> --mode reclassify` |
| "타임라인 다시" | `run_full.py --topic <토픽> --mode retime` |
| "웹에서 보고 싶어" | `serve_local.py` 실행 확인 후 URL 안내 |
| "무슨 컬렉션 있어?" | `inspect_local_zotero.py --json` |
| "몇 편 리뷰됐어?" | `docs/papers/` review.md 개수 확인 후 요약 |

## 삭제 방법 (깔끔하게 제거하기)

사용자가 "이거 삭제하고 싶어", "지워줘" 라고 하면 아래를 진행한다.

### 1. 스킬 제거

```powershell
# 설치된 스킬 디렉토리 삭제
Remove-Item -LiteralPath "$env:USERPROFILE\.codex\skills\kaic-paper-curation" -Recurse -Force
```

### 2. config / 로컬 산출물 제거

```powershell
# 설정 파일 (API 키 포함 — 안전하게 삭제)
Remove-Item -LiteralPath "config.json" -Force

# 생성된 리뷰·인덱스·캐시 (원하면 백업 후 삭제)
Remove-Item -LiteralPath "docs\papers" -Recurse -Force
Remove-Item -LiteralPath "docs\dementia2025" -Recurse -Force   # 토픽별
Remove-Item -LiteralPath ".cache" -Recurse -Force
Remove-Item -LiteralPath "pipeline\_safe_update_state" -Recurse -Force
Remove-Item -LiteralPath "pipeline\_update_force_checkpoint.json" -Force
```

### 3. (선택) 저장소 자체 제거

```powershell
Remove-Item -LiteralPath "D:\OneDrive\AI\kaic-paper-curation" -Recurse -Force
```

> ⚠️ 삭제 전에 `docs/papers` 안에 리뷰 결과가 있으면 백업을 권장한다.
> Git 히스토리에는 남으므로 `git clone` 으로 언제든 코드는 복구 가능하다.

## Architecture (포크 기준)

### Central Data Store

- `docs/papers/_papers_index.json` — 논문 마스터 인덱스
- `docs/papers/{slug}/review.md` — 한국어 리뷰 (schema v1 frontmatter)
- `docs/papers/{slug}/index.html` — 리뷰 페이지
- `docs/papers/{slug}/figures/` — 추출 Figure
- `docs/{topic}/` — 토픽 페이지 (카테고리·검색·타임라인·Deep Research UI)

### 파이프라인 순서 (`run_full.py`)

| 단계 | 스크립트 | 역할 |
|---|---|---|
| 0 | `search_papers.py` / `register_zotero.py` / `sync_zotero.py` | 검색·등록·동기화 (source=web 일 때만) |
| 0.5 | `dedup_zotero.py` | Zotero 중복 제거 |
| 1 | `run_update_force.py` | PDF 파싱 → 리뷰 → HTML (핵심 배치) |
| 1.5 | `run_metrics.py` | 피인용·레퍼런스 (soft, 실패 무시) |
| 2 | `build_papers_index.py` | 마스터 인덱스 재생성 |
| 3 | `topic_modeling.py` / `classify_papers.py` | SPECTER2+HDBSCAN 분류 (LLM 없음) |
| 4 | `build_category_summaries.py` | 카테고리 요약 (Codex Luna) |
| 4.5 | `extract_insights.py` | 논문 연결 (Codex Luna) |
| 5 | `generate_timelines.py` | 타임라인 내러티브 (Codex Terra) |
| 5.5 | `generate_network.py` | D3 네트워크 |
| 6 | `validate_papers.py` | 검증 게이트 |
| 7 | `review_to_html.py` | 리뷰 → HTML |
| 8 | `build_topic_index.py` | 토픽 페이지 |
| 8.5 | `build_search_index.py` | BM25 검색 인덱스 |
| 9 | `cleanup.py` | 오래된 파일 정리 |

### 핵심 도구

| 도구 | 역할 |
|---|---|
| `pipeline/tools/add_paper_to_zotero.py` | PDF/URL → Zotero 등록 + curation |
| `pipeline/tools/inspect_local_zotero.py` | 로컬 Zotero 컬렉션/PDF 감지 |
| `pipeline/run_full.py` | 단일 진입점 오케스트레이터 |
| `pipeline/serve_local.py` | 로컬 서버 (localhost:8000) |
| `pipeline/doctor.py` | 환경 점검 |
| `pipeline/setup.py` | 설치·스킬 설치 |

## Python 환경

- **Python 3.12 단독** (py314 금지 — numba 호환성). `_env_guard` 가 자동 라우팅.
- Windows: 모든 명령에 `PYTHONUTF8=1` 접두사.
- 클러스터링(UMAP/HDBSCAN/SPECTER2)은 로컬에서 실행되며 `.cache/` 에 모델 캐시.

## 보안·정책

- 생성은 **Codex saved-auth 만**. 유료 API 키 fallback 영구 거부
  (`allow_paid_api: false`).
- `--llm-mode off` 는 결정론 단계만 (생성 거부 exit 3).
- 비밀 검사: `scripts/scan-secrets.py` (커밋 전 실행).
- config.json·.cache·.omo 는 gitignore 로 푸시 제외.
