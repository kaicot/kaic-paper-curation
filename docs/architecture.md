# kaic-paper-curation — Architecture & Internals (포크 기준)

이 문서는 **kaicot 포크**의 내부 구조를 설명한다. 원작(jehyunlee)의
배포·BYOK·dense 검색·PaperBanana·Audio 등은 이 포크에서 제거되었거나
선택 기능이다.

## 파이프라인 단계

### 1. 데이터 수집

- Zotero 컬렉션의 아이템을 Web API 로 조회
- `find_pdf()` 가 로컬 `C:\Users\<이름>\Zotero\storage\` 에서 PDF 매칭:
  1. Zotero children API (attachments URI / absolute path)
  2. DOI 가 파일명에 포함
  3. arXiv ID 가 파일명에 포함
  4. fuzzy 제목 매칭 (`--strict-pdf` 면 비활성)
- PDF 가 없으면 해당 논문은 건너뜀 (리뷰 불가)

### 2. 구조화 리뷰

- PDF → `text.md` + `figures/` 추출 (PyMuPDF, opendataloader 없으면 자동 fallback)
- Codex(Terra, long_form)가 6섹션 한국어 `review.md` 생성:
  Essence·Motivation·Achievement·How·Originality·Limitation·Evaluation
- `schema_version: v1` frontmatter 필수
- `.llm_cache` 로 동일 입력 재호출 방지

### 3. 토픽 모델링 + 분류

- SPECTER2(base + proximity adapter, [CLS] pooling) 로 논문 임베딩
- UMAP 5D 투영 → HDBSCAN 클러스터링 → c-TF-IDF 키워드
- Codex 가 서브토픽 작명·카테고리 그룹핑
- LLM 없이 결정론적으로 배정 (multi-class: primary + all_categories)
- 모델 캐시: `.cache/base` + `.cache/adapters/proximity`

### 4. 논문 연결 (같이 보면 좋은 논문)

- 임베딩 코사인 유사도로 후보 선별
- Codex(Luna, short_form)가 관계 유형 + 한국어 이유 1문장 생성
- `_paper_connections.json` 에 저장

### 5. 요약 + 타임라인 + 검색 인덱스

- 카테고리 요약 (`build_category_summaries.py`, Codex Luna)
- 타임라인 내러티브 (`generate_timelines.py`, Codex Terra)
- BM25 검색 인덱스 (`build_search_index.py --mode bm25`) — 로컬·키 없음

### 6. 인덱스 + 열람

- `review_to_html.py` → 논문별 `index.html`
- `build_topic_index.py` → 토픽 페이지 (카테고리 카드·검색·Deep Research UI)
- `build_rss.py` → Atom 피드
- `serve_local.py` → `http://localhost:8000/<토픽>/`

## Deep Research (로컬)

- 토픽 페이지 검색창 → BM25 검색 (`_search_index.json`)
- `/api/answer` (로컬 서버) 가 Codex(saved-auth)로 근거 답변 생성
- `[ref:N]` 인용 + 논문 링크
- 질의 임베딩·BYOK·dense·웹 검색 토글은 **이 포크에 없음** (원작 전용)

## 신뢰성 설계

- **트랜잭션 상태**: `pipeline/_safe_update_state/` 에 run·marker·lock 저장.
  OS 파일 잠금으로 동일 토픽 동시 실행 차단 (`TopicBusyError`).
- **재개**: `--resume` → 실패 단계부터. 완료된 단계 해시 재사용.
- **원자적 쓰기**: `lib/atomic_io.py` (tmp + os.replace).
- **검증 게이트**: `release_dry_run.validate_default_artifacts` 가
  리뷰·분류·연결·타임라인·HTML·BM25·RSS·MOC 전부 검증 후 `succeeded`.
- **파일 무결성**: `_papers_index.json` 은 text.md 의 **전체 sha256** 저장.

## 프로그래매틱 API

```python
from pipeline.api import (
    search, register, sync, dedup_zotero,
    curate, build_papers_index, topic_model, classify,
    category_summary, insights, timeline,
    network, search_index, topic_index,
    review_to_html, validate, audit_matching, fix_matching, cleanup,
)
```

## 핵심 도구

| 도구 | 역할 |
|---|---|
| `add_paper_to_zotero.py` | PDF/URL → Zotero 등록 (컬렉션 자동 생성, linked_file) |
| `inspect_local_zotero.py` | 로컬 Zotero DB 컬렉션/PDF 상태 읽기 |
| `run_full.py` | 오케스트레이터 (3축) |
| `serve_local.py` | 로컬 HTTP 서버 |
| `doctor.py` | 환경 점검 |
| `setup.py` | 설치·스킬 설치 |

## 제거된 원작 기능 (이 포크 기준)

- Cloudflare Worker / `wrangler.toml` / `prepare_deploy.py` / gh-pages
- BYOK(독자 API 키) 답변·dense 검색 임베딩·웹 검색 토글
- Audio Overview / TTS / 이메일 발송
- PaperBanana 다이어그램 생성
- 원작자 개인 도구(`_dw_*`, dashun, lecture 등)
