# Paper Curation — 논문 리뷰 자동화 도우미

> **이 도구를 쓰기 위해 코드를 치거나 파일을 수정할 필요가 없습니다.**
> LLM(Codex)에게 한국어로 말만 하면, 나머지는 전부 LLM이 처리합니다.

**이런 분들을 위한 도구입니다:**

- 대학원에 막 들어와서 논문을 정리해야 하는데 어떻게 시작할지 모르는 분
- Zotero가 뭔지, localhost가 뭔지, 터미널이 뭔지 모르는 분
- 논문 PDF나 주소만 있으면 리뷰·분류·검색까지 자동으로 만들고 싶은 분

---

## 이렇게만 하세요 (3단계)

### 1단계 — 설치 요청

Codex(또는 ChatGPT의 Codex)에게 아래 한 줄을 보내세요:

> **"여기에 kaic-paper-curation 설치해줘: https://github.com/kaicot/kaic-paper-curation"**

그러면 LLM이 **한 번에 하나씩** 물어봅니다. 물어보는 대로 답하면 됩니다:

1. "Codex 로그인이 되어 있나요?" → `codex login status` 확인 후 "되어 있어요" 또는 안내대로 진행
2. "Zotero에 논문을 넣어두셨나요?" → **아니면 아래 0단계부터**
3. "리뷰할 Zotero 컬렉션 이름이 뭔가요?" → Zotero 앱에서 만든 폴더 이름
4. "이 컬렉션을 뭐라고 부를까요?" → 아무 영문 이름 (예: `mypapers`)
5. 설치 완료 → "브라우저에서 이 주소를 여세요"라고 알려줌

> 💡 **스킬로 부르기**: 설치가 끝나면 Codex 입력창에 `@`를 누르고
> `kaic-paper-curation` 을 선택하면 됩니다. 이후 PDF 드래그 앤 드롭이나
> 논문 주소 붙여넣기만으로 "이 논문 넣어줘"라고 하면 스킬이 처리합니다.

### 0단계 — Zotero에 논문 넣기 (아직 없을 때)

**Zotero에 논문을 넣는 것조차 LLM에게 시킬 수 있습니다.** 이렇게 말하세요:

> **"이 논문 좀 넣어줘"** (PDF 파일을 끌어다 놓으면서)
> 또는 **"이 논문 넣어줘: https://arxiv.org/abs/2401.00001"** (주소만 붙여넣기)

LLM이 Zotero 컬렉션을 자동으로 만들고, 논문을 등록하고, 리뷰까지 진행합니다.
PDF는 로컬 파일로 연결되므로 **Zotero 클라우드 저장공간이 없어도** 됩니다.

> 💡 Zotero 앱이 없어도? LLM이 Zotero 설치·가입 방법을 단계별로 안내해 드립니다.
> Zotero API 키가 없어도 **로컬 Zotero를 감지해서** 진행할 수 있습니다.

### 2단계 — 매일 사용 (말만 하면 됩니다)

| 하고 싶은 일 | 이렇게 말하세요 |
|---|---|
| 논문 리뷰/정리 | "새 논문 리뷰해줘" |
| 논문 추가 | "이 논문 넣어줘" + PDF 또는 주소 |
| 웹에서 새 논문 찾기 | "이번 주 논문 찾아줘" |
| 결과 보기 | "웹에서 보고 싶어" |
| 분류 다시 하기 | "분류 다시 해줘" |
| 진행 상황 | "몇 편 리뷰됐어?" |
| 컬렉션 목록 | "무슨 컬렉션 있어?" |

### 3단계 — 결과 보기

"웹에서 보고 싶어"라고 말하면 LLM이 서버를 켜고 **주소를 알려줍니다**.
그 주소를 브라우저에 붙여넣으면 됩니다. (주소가 뭔지 몰라도 됩니다 —
그냥 알려준 대로 열면 됩니다.)

브라우저에서 볼 수 있는 것:

- 카테고리별 논문 카드
- 각 논문의 **한국어 리뷰** (핵심 요약·방법·한계·평가)
- 검색창 — 궁금한 걸 물어보면 논문 근거로 답변
- 연구 동향 타임라인

---

## 자주 묻는 질문

**Q. 코드를 직접 쳐야 하나요?**
아니요. LLM에게 말로만 하면 됩니다. 설치·실행·서버·주소 안내 모두 LLM이 합니다.

**Q. 이거 삭제하고 싶어요. 어떻게 하나요?**
LLM에게 "kaic-paper-curation 삭제해줘"라고 말하면 됩니다. LLM이 스킬·config·생성된
리뷰를 정리해 드립니다. 수동 절차는 [AGENTS.md](AGENTS.md)의 "삭제 방법"에
있습니다.

**Q. Zotero를 안 써봤어요.**
괜찮습니다. LLM이 Zotero 설치 → 가입 → 컬렉션 만들기 → 논문 넣기까지
단계별로 안내하고, 심지어 **대신 등록해 주기도** 합니다.

**Q. API 키가 뭔가요?**
유료 API 키는 **필요 없습니다**. Zotero API 키가 없어도 로컬 Zotero가 있으면
진행됩니다. LLM이 필요할 때 발급 방법을 안내합니다.

**Q. 비용이 드나요?**
생성은 ChatGPT 구독에 포함된 Codex 크레딧을 사용합니다. 유료 API 키 기반
과금은 없습니다.

---

## (참고) LLM이 처리하는 일 — 이 프로젝트가 하는 일

Paper Curation은 Zotero에 있는 논문 PDF를 읽어서:

1. **한국어 리뷰** 작성 (핵심·동기·성과·방법·독창성·한계·평가)
2. **자동 분류** — 논문들을 주제 카테고리로 묶기
3. **논문 연결** — 비슷한 논문끼리 "같이 보면 좋은 논문"
4. **타임라인** — 연구 동향 정리
5. **검색 가능한 웹 페이지** 생성 — 브라우저에서 열람

이 모든 과정이 로컬에서 실행되며, **생성에만 Codex 크레딧**을 사용합니다.

---

## 원작과의 차이 (개발자용)

이 저장소는 [jehyunlee/paper-curation](https://github.com/jehyunlee/paper-curation)
원작을 **로컬 실행·저비용·초보자 친화** 관점에서 개선한 포크입니다.

- **유료 API → Codex saved-auth**: 생성은 ChatGPT 로그인만 사용, 유료 API fallback 영구 차단
- **dense 검색/배포 → BM25 로컬**: 검색은 로컬 BM25 기본, 배포 제거
- **API 키 없이 시작**: 로컬 Zotero 감지 도구(`inspect_local_zotero.py`)
- **PDF/URL → Zotero 자동 등록**: `add_paper_to_zotero.py` (컬렉션 자동 생성,
  linked_file로 클라우드 용량 불필요)
- **LLM 위자드**: 설치·일상 사용 모두 step-by-step 안내 (SKILL.md)
- **실행 차단 버그 수정**: 모듈 경로, PDF 매칭, frontmatter, 검증 불일치,
  SPECTER2 레이아웃, sha256 인덱스 등 (v0.2.0)

실제 사용자라면 위 3단계만 기억하면 됩니다. 아래는 개발자·운영자를 위한 문서입니다.

---

## 개발자 문서

### 명령어

```bash paper-curation-command
# Zotero 컬렉션 논문 리뷰·분류·인덱스
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --source zotero

# 결과 보기
PYTHONUTF8=1 python pipeline/serve_local.py

# PDF/URL로 Zotero 등록 (컬렉션 자동 생성 + curation)
PYTHONUTF8=1 python pipeline/tools/add_paper_to_zotero.py --pdf paper.pdf --collection "내 논문"
PYTHONUTF8=1 python pipeline/tools/add_paper_to_zotero.py --url https://arxiv.org/abs/2401.00001 --collection "내 논문"

# 로컬 Zotero 컬렉션/PDF 상태 확인
PYTHONUTF8=1 python pipeline/tools/inspect_local_zotero.py

# 환경 점검
PYTHONUTF8=1 python pipeline/doctor.py --format json
```

### 문서

| 문서 | 내용 |
|---|---|
| [Setup Guide](docs/setup-guide.md) | 사전 준비 · 설치 · config.json · 문제 해결 |
| [Operations Manual](docs/operations.md) | 모드 · 안전 플래그 · 복구 |
| [Architecture & Internals](docs/architecture.md) | 파이프라인 상세 |

---

## 버전 관리 이력

### [0.2.0] - 2026-08-13

- LLM 위자드 설치·일상 사용 안내, API 키 없는 시작 경로
- PDF/URL → Zotero 자동 등록 도구 (`add_paper_to_zotero.py`)
- 실제 36편 컬렉션 end-to-end 실행 완료, 실행 차단 버그 9종 수정

### [0.1.0] - 2026-08-08

- Codex saved-auth 생성 게이트웨이, fail-closed 정책, BM25 검색,
  release evidence, `238 passed, 0 failed, 0 skipped`

### 버전 규칙

- SemVer. `VERSION` 파일이 단일 기준. 릴리스 시 VERSION·CHANGELOG·README
  갱신 후 `vMAJOR.MINOR.PATCH` 태그.

---

*Built with Codex.* 🐱
