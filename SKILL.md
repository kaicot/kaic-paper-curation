---
name: paper-curation
description: "최신 학술 논문 자동 큐레이션 풀 파이프라인. 검색 → Zotero 등록 → Paper Review → 로컬 인덱스까지 단일 진입점 (pipeline/run_full.py). 트리거: '논문 큐레이션', '최신 논문 찾아줘', '논문 수집', 'paper curation', '오늘 나온 논문', '최신 논문 Zotero에', 'curate papers', '논문 모니터링'."
---

# Paper Curation — Dispatcher

<Purpose>
사용자 요청을 `pipeline/run_full.py` 의 단일 진입점으로 매핑한다.
세부 운영 내용·복구 흐름·환경 변수는 `docs/operations.md` 참조.
파이프라인의 모든 단계는 `pipeline/api` 에서 함수로도 호출 가능
(`from pipeline.api import curate, classify, timeline, ...`).
생성은 **Codex saved-auth(ChatGPT 로그인)** 만 사용하며 유료 API 키로의 fallback 은 없다.
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
- [ ] 자세한 운영 / 환경 / 복구는 `docs/operations.md`
</Final_Checklist>
