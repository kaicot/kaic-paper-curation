---
name: kaic-paper-curation
description: "kaicot 포크의 논문 자동 큐레이션 풀 파이프라인 (Paper Curation kaicot fork). PDF/주소로 논문을 Zotero에 등록하고, 리뷰·분류·검색 페이지까지 자동 생성. 트리거: '@kaic-paper-curation', '논문 큐레이션', '이 논문 넣어줘', '이 주소 논문 넣어줘', '논문 리뷰해줘', '최신 논문 찾아줘', '논문 수집', 'kaic paper curation', '오늘 나온 논문', 'curate papers', '논문 모니터링'."
---

# Paper Curation — Dispatcher

<Purpose>
사용자 요청을 `pipeline/run_full.py` 의 단일 진입점으로 매핑한다.
세부 운영 내용·복구 흐름·환경 변수는 `docs/operations.md` 참조.
파이프라인의 모든 단계는 `pipeline/api` 에서 함수로도 호출 가능
(`from pipeline.api import curate, classify, timeline, ...`).
생성은 **Codex saved-auth(ChatGPT 로그인)** 만 사용하며 유료 API 키로의 fallback 은 없다.
사용자가 localhost·터미널·명령어를 몰라도 되도록, LLM이 **설치부터 매일 사용까지
전 과정을 step-by-step으로 안내**한다. 사용자에게는 질문에 답하고 결과를
브라우저로 보는 것만 요구한다.
</Purpose>

<Trigger_To_Mode>

사용자 요청을 mode + source로 매핑하고 `pipeline/run_full.py`를 호출한다:

| 요청 신호 | mode | source | 추가 플래그 |
|---|---|---|---|
| "이번 주/오늘 논문", "최신 논문 찾아줘", "수집" | `curate` | `web` | `--days 7` |
| "가지고 있는 논문으로", "Zotero에 있는 거 정리" | `curate` | `zotero` | |
| "신규 논문만 리뷰", "업데이트" | `curate` | `zotero` | |
| "전체 재생성", "다시 만들어" (위험) | `rebuild` | `zotero` | `--yes` 사용자 명시 시만 |
| "분류만 다시", "재분류" | `reclassify` | (none) | |
| "타임라인 재생성" | `retime` | (none) | `--images all` |
| "감사", "오매칭 확인" | `audit` | — | |
| "복구", "리뷰 다시" | `fix-matching` | — | `--yes` |
| "Zotero 중복 정리" | `dedup` | — | `--yes`로 execute |
| "검증" | `validate` | — | |

특정 슬러그만 다루라는 요청 (`088,1093 다시`)이 있으면 `--slugs A,B,C` 와 함께 `--mode rebuild --strict-pdf` 사용.

> ⚠️ `deploy` 모드는 제거되었다 (`--mode deploy` 는 exit 2). "배포해줘" 요청에는 로컬 서버(`serve_local.py`) 열람을 안내한다.

</Trigger_To_Mode>

<Wizard_Installation>

사용자가 "설치해줘", "이거 써보고 싶어", "github 주소" 를 언급하면 아래를 진행한다.
**한 번에 하나씩만 묻고, 답을 기다린다.**

1. 사전 확인 — `codex login status` 가 `Logged in using ChatGPT` 인지,
   Python 3.12 인지 확인한다. 안 되면 안내하고 기다린다.
2. Zotero 준비 — "리뷰할 논문을 Zotero의 컬렉션(폴더) 하나에 넣고, Zotero 앱에서
   동기화를 눌러 PDF를 내려받아 주세요"라고 안내한다.
3. 정보 수집 (하나씩) —
   a. Zotero API 키: `ZOTERO_API_KEY` 환경변수 또는 `pipeline/tools/inspect_local_zotero.py`
      실행 결과 `found: true` 면 키 없이 진행 가능.
   b. 이메일
   c. **Zotero 컬렉션 이름** — 입력 후 API/로컬 DB로 존재 여부 검증. 없으면
      `inspect_local_zotero.py --json` 의 컬렉션 목록을 보여주고 재질문.
   d. 토픽 alias — 영문 소문자·숫자·`-`·`_` 만 (예: `dementia2025`)
   e. Zotero PDF 폴더 — 보통 `C:\Users\<이름>\Zotero` (자동 감지 가능)
4. `config.json` 생성 → `doctor.py` 로 검증 → 첫 파이프라인 실행.
5. 마무리 안내 — "브라우저에서 `http://localhost:8000/<토픽>/` 을 열면 됩니다.
   서버가 꺼져 있으면 말씀해 주세요. 제가 켜 드릴게요."

</Wizard_Installation>

<Wizard_Daily_Use>

사용자가 아래 같은 일상 표현을 쓰면, **터미널·localhost 개념 없이** 안내한다:

| 사용자 말 | LLM 행동 |
|---|---|
| "이 논문 좀 넣어줘" + PDF 파일/드래그 | `pipeline/tools/add_paper_to_zotero.py --pdf <경로> --collection <컬렉션>` 실행 (컬렉션 없으면 자동 생성). 완료 후 curation 자동 실행 |
| "이 주소 논문 넣어줘" + URL | `add_paper_to_zotero.py --url <URL> --collection <컬렉션>` 실행 (arXiv/DOI 메타 자동 추출) |
| "새 논문 리뷰해줘", "Zotero에 있는 거 정리해줘" | topic 확인 후 `run_full curate --source zotero` 실행, 완료되면 URL 안내 |
| "오늘/이번 주 논문 찾아줘" | `curate --source web --days 7` 실행 |
| "분류 다시 해줘" | `run_full --mode reclassify` |
| "타임라인 다시 만들어줘" | `run_full --mode retime` |
| "결과 보고 싶어", "웹에서 보여줘" | `serve_local.py` 가 실행 중인지 확인. 꺼져 있으면 실행하고 `http://localhost:8000/<토픽>/` 안내 |
| "이 논문만 다시 해줘" (슬러그 언급) | `--slugs A,B,C --mode rebuild --strict-pdf --yes` (파괴적 — 사용자 명시 확인 후) |
| "무슨 컬렉션이 있지?", "컬렉션 목록" | `inspect_local_zotero.py --json` 실행 후 목록 표시 |
| "몇 편이나 리뷰됐어?", "진행 상황" | `docs/papers/` 의 review.md 개수·`_papers_index.json` 확인 후 요약 |

실행 후 반드시 **결과 요약과 열람 URL**을 사용자에게 알린다:
"방금 N편의 리뷰가 완료됐습니다. 브라우저에서 `http://localhost:8000/<토픽>/` 를
여시면 카테고리별로 보실 수 있어요." (localhost 라는 단어는 부가 설명 없이
링크처럼 안내)

**논문 등록 위자드** (PDF/URL → Zotero):
- PDF: 사용자가 파일 경로를 주면 제목을 PDF 첫 페이지에서 추출해
  `add_paper_to_zotero.py` 로 등록한다. 컬렉션 이름을 물어보고, 없으면 자동 생성.
- URL: arXiv/DOI 주소면 자동으로 제목·저자·초록을 추출해 등록.
- 등록이 끝나면 `--no-run` 없이 curation 까지 자동 실행하고 결과 URL 안내.
- **주의**: Zotero 무료 계정 클라우드 저장공간(300MB)이 가득 차 있으면
  PDF 클라우드 업로드는 실패한다. 도구는 `linked_file`(로컬 링크) 방식을 쓰므로
  로컬 PDF 경로가 있으면 저장공간 없이 등록된다. 등록한 PDF는 로컬 경로에
  있어야 curation 에서 읽을 수 있다.

</Wizard_Daily_Use>

<Quick_Recipes>

```bash paper-curation-command
# 주간 — 검색 + Zotero 등록 + 신규 리뷰
PYTHONUTF8=1 python pipeline/run_full.py --topic ai4s --mode curate --source web --days 7

# 로컬 — Zotero 컬렉션만 동기화 + 신규 리뷰
PYTHONUTF8=1 python pipeline/run_full.py --topic ai4s --mode curate --source zotero

# 특정 슬러그 force-rebuild (복구)
PYTHONUTF8=1 python pipeline/run_full.py --topic ai4s --mode rebuild --slugs 088,1093 --strict-pdf --yes

# 분류만 다시 (HDBSCAN approximate_predict, 로컬 결정론)
PYTHONUTF8=1 python pipeline/run_full.py --topic ai4s --mode reclassify

# 타임라인 narrative + 이미지
PYTHONUTF8=1 python pipeline/run_full.py --topic ai4s --mode retime --images all

# 실행 계획만 미리보기
PYTHONUTF8=1 python pipeline/run_full.py --topic ai4s --mode curate --source web --dry-run
```

</Quick_Recipes>

<Use_When>
- "최신 논문 찾아줘", "이번 주/오늘 논문" → `--mode curate --source web --days N`
- "논문 큐레이션" / "paper curation" → 사용자에게 topic 확인 후 `curate` 실행
- "분류만 다시" / "재분류" → `--mode reclassify`
- "감사", "오매칭" → `--mode audit`
- "타임라인" 단독 언급 → `--mode retime`
</Use_When>

<Do_Not_Use_When>
- 단일 논문 추가 → `zotero-add` 스킬
- 단일 논문 리뷰 → `paper-review` 스킬
- 보고서 작성용 자료 수집 → `report-gen` 스킬
</Do_Not_Use_When>

<Safety>
- `--mode rebuild` 는 review.md/figures 를 모두 재생성하는 파괴적 동작 — 사용자가 명시적으로 요청하기 전엔 절대 실행 금지. 실행할 때 `--yes` 필수.
- 사용자가 "force update" 같은 표현을 써도, `--slugs` 범위 제한이 가능하면 그 쪽을 먼저 제안.
- 생성은 Codex saved-auth 만 사용. `--llm-mode off` 는 생성 단계를 전부 건너뛰고 결정론 단계만 실행한다 (정책 거부 exit 3). 유료 API 키 설정(`allow_paid_api: true`)은 영구 거부.
- Codex 크레딧 소진 시 생성 단계가 실패 처리된다 — `--resume` 으로 재충전 후 이어서 실행. 유료 키 fallback 없음.
- Phase 3 이후 모든 review.md 는 schema v1 frontmatter 를 가진다. 원본은 `docs/papers/.legacy/{slug}_v0.md` 백업.
</Safety>

<Programmatic_API>

스크립트 단위 호출은 `pipeline.api` 에서:

```python
from pipeline.api import (
    search, register, sync, dedup_zotero,        # ingest
    curate,                                       # full batch
    build_papers_index, topic_model, classify,    # index + classify
    category_summary, insights, timeline,         # narrative (Codex)
    network, search_index, topic_index,           # render
    review_to_html,                               # publish
    validate, audit_matching, fix_matching, cleanup,  # safety
)

# Cache & figure helpers
from pipeline.api._llm import cached_call, paper_cache_dir, topic_cache_dir
from pipeline.api.extract import pre_validate_figure
```

세부 시그니처/플래그는 `docs/operations.md` 참조.

</Programmatic_API>

<Final_Checklist>
- [ ] 사용자 요청을 위 표의 한 줄로 매핑
- [ ] `pipeline/run_full.py` 단일 진입점으로 실행 (`Bash` tool)
- [ ] 실패 시 `--dry-run` 으로 plan 확인 후 재실행
- [ ] 로컬 열람은 `pipeline/serve_local.py` (localhost:8000)
- [ ] 사용자에게 결과 요약 + 열람 URL(`http://localhost:8000/<토픽>/`) 안내
- [ ] 서버가 꺼져 있으면 자동으로 실행하고 URL 안내
- [ ] 설치 요청이면 <Wizard_Installation> 흐름, 일상 사용이면 <Wizard_Daily_Use> 흐름 적용
- [ ] 자세한 운영 / 환경 / 복구는 `docs/operations.md`
</Final_Checklist>
