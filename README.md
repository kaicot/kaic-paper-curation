# Paper Curation (kaicot fork)

**Zotero 컬렉션에 PDF만 있으면, 나머지는 자동입니다.**

이 저장소는 [jehyunlee/paper-curation](https://github.com/jehyunlee/paper-curation)
원작을 **로컬 실행·저비용·초보자 친화** 관점에서 다시 만든 포크입니다.

논문 PDF → 한국어 구조화 리뷰 → 자동 분류 → 연구 동향 타임라인 → 검색 가능한
토픽 페이지까지, **저장된 ChatGPT Codex 로그인(saved-auth)만으로** 처리합니다.
유료 API 키는 필요 없습니다.

---

## 이 포크에서 무엇을 어떻게 바꿨는가

원작은 기능이 많지만 배포·BYOK·dense 검색 등 운영 부담이 컸습니다. 이 포크는
**“개인 PC에서 논문 리뷰를 자동으로 쌓는 것”**에 집중해, 기본 동작을
무료·로컬로 바꾸고 실행 중 실제로 막히는 문제들을 고쳤습니다.

### 핵심 방향

| 원작 | 이 포크 (kaicot) |
|---|---|
| 유료 API(Anthropic/OpenAI/Google)로 생성 | **Codex saved-auth(ChatGPT 로그인)로만 생성** — 유료 API fallback 영구 차단 |
| dense 검색 + 웹 배포 기본 | **BM25 로컬 검색 기본**, 배포는 제거 (로컬 서버 열람) |
| 설치 시 여러 API 키 요구 | **API 키 불필요** — 로컬 Zotero(`zotero.sqlite`)만으로 시작 가능 |
| 사용자가 명령어를 직접 익혀야 함 | **LLM 위자드 설치** — “설치해줘” 한 줄로 step-by-step 진행 |

### 실제 실행에서 고친 것들 (v0.2.0)

이 포크는 실제 Zotero 컬렉션(36편)으로 첫 end-to-end 실행을 하면서 발견한
문제를 모두 수정했습니다.

- **모듈 경로 오류** — `sync_zotero.py`, `build_papers_index.py`,
  `review_to_html.py`, `build_rss.py`가 저장소 루트에서 실행되면 import 실패.
  패키지 경로로 수정.
- **Zotero 클라우드 PDF 매칭** — `imported_url`(클라우드 첨부) 방식은 로컬
  `storage/<childKey>/`에서 PDF를 찾지 못함. child `filename` 기반 매칭 추가로
  19편의 PDF를 복구.
- **리뷰 frontmatter** — 검증 로직이 요구하는 `schema_version: v1` 헤더가
  생성 템플릿에 없음. 템플릿 수정 + 기존 파일 일괄 보정.
- **분류/연결/RSS 검증 불일치** — 실제 생성 형식(assignments 리스트, Atom feed,
  소규모 카테고리 연결 누락)과 검증 로직이 달라 실패. 검증 로직을 실제 형식에
  맞춤.
- **SPECTER2 어댑터 레이아웃** — HuggingFace의 새 `allenai/specter2` 구조
  (`proximity/` 폴더 없음)에서 모델 준비 실패. 어댑터 루트 fallback 추가.
- **sha256 인덱스** — `_papers_index.json`이 16자 축약 해시를 저장해
  `source-hash-invalid` 실패. 전체 sha256으로 수정.
- **첫 실행 크래시** — `docs/papers/` 폴더가 없으면 중단. 자동 생성.

### 새로 추가한 것

- **`inspect_local_zotero.py`** — 로컬 Zotero DB에서 컬렉션 목록과 PDF 동기화
  상태를 읽어주는 읽기 전용 도구 (API 키 없이 안내용):

  ```bash paper-curation-command
  PYTHONUTF8=1 python pipeline/tools/inspect_local_zotero.py --json
  ```

- **LLM 위자드 설치 흐름** — 아래 “설치하기” 참고.

---

## 설치하기

### 준비물

- **Codex 로그인** — `codex login status` → `Logged in using ChatGPT`
- **Zotero 앱** — 리뷰할 논문들을 컬렉션 하나에 넣고, 해당 컬렉션의 PDF가
  로컬(`C:\Users\<이름>\Zotero\storage`)에 내려받아져 있어야 합니다.
  (Zotero 앱에서 동기화 버튼을 누르면 자동 다운로드)
- **Python 3.12** (Windows면 `PYTHONUTF8=1` 접두사)

### 🤖 LLM이 위자드처럼 안내하는 설치 (추천)

Codex(또는 Codex 스킬이 설치된 에이전트)에게 한 줄만 입력하세요:

> "여기에 paper-curation을 설치해줘: https://github.com/kaicot/kaic-paper-curation"

LLM은 아래 순서로 **한 번에 하나씩** 필요한 정보를 요청하고, 답변이 오면
자동으로 진행합니다. 사용자는 질문에 답만 하면 됩니다.

1. 사전 확인 — Codex 로그인·Python 3.12
2. Zotero 준비 안내 — 컬렉션에 논문 넣기, PDF 로컬 동기화
3. 정보 요청 (하나씩):
   - "Zotero API 키를 알려주세요" — **없으면 로컬 Zotero DB에서 자동 감지**
   - "이메일을 알려주세요"
   - "리뷰할 논문이 들어있는 **Zotero 컬렉션 이름**이 뭔가요?"
   - "이 컬렉션을 앞으로 뭐라고 부를까요? (영문 짧은 이름, 예: `dementia2025`)"
   - "Zotero PDF 저장 폴더 경로를 알려주세요" (예: `C:\Users\<이름>\Zotero`)
4. 컬렉션 이름 검증 — 없으면 사용 가능한 컬렉션 목록을 보여주고 재질문
5. 설치·실행 — `config.json` 생성 → 첫 파이프라인 → `http://localhost:8000/{토픽}/` 열람 안내

### 🚫 API 키 없이 시작하기 (Zotero 초보·키 발급이 어려운 경우)

Zotero 앱이 이미 PC에 설치되어 있다면 **API 키 없이** 시작할 수 있습니다.

1. Zotero 데이터 폴더 확인 — 보통 `C:\Users\<이름>\Zotero`
2. LLM에게 "Zotero 데이터 폴더는 `C:\Users\<이름>\Zotero` 입니다"라고 알려주면,
   LLM이 로컬 DB에서 컬렉션 목록과 PDF 상태를 직접 읽어 안내합니다.
3. 이후 동일하게 "어느 컬렉션을 큐레이션할까요?"에 답하면 됩니다.

> ⚠️ PDF가 로컬에 없으면 리뷰를 만들 수 없습니다. Zotero 앱에서 동기화를
> 눌러 PDF를 내려받으세요.

### 🤖 일상 사용도 LLM에게 (localhost를 몰라도 됩니다)

설치 후에는 **명령어를 직접 칠 필요가 없습니다.** Codex(또는 이 저장소의
`paper-curation` 스킬이 설치된 에이전트)에게 일상 표현으로 말하면 됩니다:

| 사용자 말 | LLM이 하는 일 |
|---|---|
| "새 논문 리뷰해줘" | Zotero 컬렉션 갱신 + 리뷰 실행, 완료 후 URL 안내 |
| "웹에서 보고 싶어" | 로컬 서버를 켜고 `http://localhost:8000/<토픽>/` 안내 |
| "오늘 나온 논문 찾아줘" | 웹 검색 + Zotero 등록 + 리뷰 실행 |
| "분류 다시 해줘" / "타임라인 다시 만들어줘" | 해당 단계만 재실행 |
| "무슨 컬렉션이 있지?" | 로컬 Zotero 컬렉션 목록 표시 |
| "몇 편 리뷰됐어?" | 진행 상황 요약 |

LLM은 실행이 끝나면 **결과 요약과 열람 주소**를 알려줍니다. 서버가 꺼져
있으면 자동으로 켜서 안내합니다.

### 수동 설치

```bash
git clone https://github.com/kaicot/kaic-paper-curation.git
cd kaic-paper-curation
# Python 3.12 환경에서
pip install -r requirements.txt
PYTHONUTF8=1 python pipeline/setup.py   # 대화형 설정 → 첫 파이프라인
PYTHONUTF8=1 python pipeline/doctor.py --format json   # 환경 점검
```

---

## 사용하기

### 매일 하는 일 (Zotero 컬렉션 갱신)

```bash paper-curation-command
# Zotero 컬렉션의 논문을 리뷰·분류·인덱스까지 (새 논문만 처리)
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --source zotero

# 결과 보기 (브라우저)
PYTHONUTF8=1 python pipeline/serve_local.py   # http://localhost:8000/<토픽>/
```

`--topic`은 config.json의 `zotero.collections`에 등록된 이름입니다. 새 컬렉션은
config에 `"토픽이름": "Zotero컬렉션이름"` 한 줄만 추가하면 됩니다.

### 주요 명령

```bash paper-curation-command
# 검색 + Zotero 등록까지 (웹에서 논문 수집)
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --source web --days 7

# 분류만 / 타임라인만
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode reclassify
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode retime --images all

# 실행 계획 미리보기
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --dry-run

# 로컬 Zotero 확인 (API 키 없이)
PYTHONUTF8=1 python pipeline/tools/inspect_local_zotero.py
```

### 브라우저에서

- 카테고리별 논문 카드, 논문별 한국어 리뷰(6개 섹션)
- **검색** — BM25 로컬 검색 (키 불필요)
- **Deep Research** — 자연어 질문 → 근거 인용 답변 (로컬 서버 필요)

---

## 원작 (jehyunlee/paper-curation) 설명 요약

이 포크의 기반이 된 원작의 전체 기능입니다. 이 포크에서는 아래 중 **로컬
실행에 해당하는 것**만 기본으로 남기고, 배포·BYOK·dense 검색 등은 제거하거나
옵션으로 격리했습니다.

### 기능

| 기능 | 설명 |
|---|---|
| 구조화 리뷰 | PDF 텍스트/Figure 추출 → Codex가 6섹션 한국어 리뷰 작성 |
| 자동 분류 | SPECTER2 + HDBSCAN + UMAP 카테고리 자동 생성·배정 |
| 논문 연결 | 임베딩 후보를 Codex가 선별 — 관계 유형 + 한국어 이유 |
| Deep Research | 자연어 질의 → 검색 → 로컬 서버에서 근거 답변 |
| Audio Overview | 리뷰/답변을 한국어 팟캐스트 오디오로 (선택) |
| 타임라인 | 카테고리별 연구 동향 내러티브 + 다이어그램 |
| Citedby | DOI 한 편에서 인용 계보·타임라인 분석 (선택) |

### 파이프라인 단계

1. 데이터 수집 — Zotero PDF → `text.md` + `figures/`
2. 구조화 리뷰 — Codex(Terra) 6섹션 한국어 `review.md`
3. 토픽 모델링·분류 — SPECTER2 + HDBSCAN + UMAP
4. 논문 연결 — Codex(Luna)가 후보 선별
5. 요약 + 타임라인 + 검색 인덱스(BM25)
6. 토픽 인덱스 `index.html` → 로컬 열람

### 실패·재개

각 단계는 상태 파일로 추적됩니다. 실패 시 이전 산출물 해시는 보존되고,
`--resume`으로 실패한 단계부터만 재실행됩니다.

---

## 문서

| 문서 | 내용 |
|---|---|
| [Setup Guide](docs/setup-guide.md) | 사전 준비 · Codex/수동 설치 · config.json · 문제 해결 |
| [Operations Manual](docs/operations.md) | 모드/안전 플래그 · Concurrency · 한국 망 우회 · 복구 |
| [Architecture & Internals](docs/architecture.md) | 파이프라인 단계 상세 · 내부 구조 |
| [English README](README.en.md) | 원작 기준 영문 문서 |

---

## 버전 관리 이력

### [0.2.0] - 2026-08-13

첫 실제 사용 릴리스: 36편 Zotero 컬렉션으로 end-to-end 실행 완료, 실행 차단
버그 수정, LLM 위자드 설치·API 키 없는 시작 경로 추가.

- **Added**: LLM 위자드 설치 흐름(README/AGENTS.md), 키 없는 시작 경로
  (`inspect_local_zotero.py`), `.gitignore` tools 화이트리스트
- **Fixed**: 모듈 경로 import, Zotero 클라우드 PDF 매칭, 리뷰 frontmatter,
  분류/연결/RSS 검증, SPECTER2 어댑터 레이아웃, 전체 sha256 인덱스,
  첫 실행 `docs/papers/` 생성, 소규모 카테고리 연결 누락
- **Verified**: `고령치매2025_1` 컬렉션 19편 리뷰·분류·타임라인·HTML·BM25·RSS
  `status: succeeded`, 로컬 서버·검색·Deep Research 답변 정상, 비밀 검사 통과

### [0.1.0] - 2026-08-08

첫 버전 릴리스 (원작 기준 정리):

- Codex saved-auth 생성 게이트웨이와 유료 API fallback 영구 차단
- fail-closed setup/doctor/run_full 정책
- BM25 기본 검색 + 로컬 Deep Research answer 서버
- one-paper fixture 검증, release evidence 도구
- Codex CLI `0.147.0` 바운더리 재검증, `238 passed, 0 failed, 0 skipped`

### 버전 규칙

- SemVer(`MAJOR.MINOR.PATCH`). `VERSION` 파일이 단일 기준.
- 모든 릴리스는 `VERSION`, `CHANGELOG.md`, README 릴리스 요약을 함께 갱신하고
  `vMAJOR.MINOR.PATCH` Git tag를 사용.

---

*Built with Codex.* 🐱
