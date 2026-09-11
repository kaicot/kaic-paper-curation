# kaic-paper-curation — Operations Manual (포크 기준)

이 문서는 **kaicot 포크**에서 파이프라인을 운영하는 방법을 설명한다.
원작(jehyunlee)의 배포·BYOK·dense 검색 등은 이 포크에 없다.

## 작업 위치와 OneDrive 보관

실제 저장소는 `D:\workspace\kaic-paper-curation`이다. OneDrive의
`AI\kaic-paper-curation`에는 로컬 커밋의 소스·문서만 게시한다. `.git`, `.tools`, `.cache`,
`.worktrees`, `.omo`, `config.json`은 게시하지 않는다. GitHub 업로드는 이 작업과 별도다.

`scripts/update-source-snapshot.ps1` 또는 로컬 post-commit 훅으로 보관본을 갱신한다.
새 작업 저장소에서는 `scripts/install-source-snapshot-hook.ps1`로 훅을 설치한다. 기존 훅이나
공유된 외부 hooks 경로는 자동으로 덮어쓰지 않는다. 보관본 갱신에 실패해도 로컬 커밋은 보존된다.
대상 위치 설정은 작업 원본의 `.omo/source-snapshot-config.json`이며 Git에 포함하지 않는다.
보관본의 `.source-snapshot.json`에는 커밋 번호·원본 위치·파일 해시·생성 시각을 기록한다.
직접 편집한 보관 파일이 있으면 갱신을 중단하며, 관리 목록에 없는 파일은 삭제하지 않는다.
게시 중 중단되면 다음 게시가 journal을 검사해 이어서 처리한다. 중단 후 사람이 편집한 파일은
알려진 이전/새 해시와 다르므로 덮어쓰지 않는다. 아직 커밋하지 않은 수정은 D 드라이브에만 있다.

## 파이프라인 개요

`run_full.py` 가 단일 진입점이다. 3축:

- `--mode`: `curate`(기본) / `reclassify` / `retime` / `audit` / `fix-matching` / `dedup` / `validate`
- `--source`: `zotero`(기본) / `web` / `fixture`
- `--images`: `skip`(기본) / `changed` / `all`

`--mode deploy` 는 **제거됨** (exit 2). "배포" 는 로컬 서버(`serve_local.py`) 열람을 의미한다.

## 주요 명령

```bash paper-curation-command
# 매일 — Zotero 컬렉션 신규 논문 리뷰
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --source zotero

# 웹 검색 + Zotero 등록 + 리뷰 (이번 주 논문)
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --source web --days 7

# 분류만 다시
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode reclassify

# 타임라인만 다시
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode retime --images all

# 실행 계획 미리보기 (변경 없음)
PYTHONUTF8=1 python pipeline/run_full.py --topic <토픽> --mode curate --dry-run

# 결과 보기
PYTHONUTF8=1 python pipeline/serve_local.py   # http://localhost:8000/<토픽>/

# PDF/URL로 Zotero 등록 (컬렉션 자동 생성 + curation)
PYTHONUTF8=1 python pipeline/tools/add_paper_to_zotero.py --pdf paper.pdf --collection "내 논문"
PYTHONUTF8=1 python pipeline/tools/add_paper_to_zotero.py --url https://arxiv.org/abs/2401.00001 --collection "내 논문"

# 로컬 Zotero 확인 (API 키 없이)
PYTHONUTF8=1 python pipeline/tools/inspect_local_zotero.py
```

## 안전 플래그

| 플래그 | 효과 |
|---|---|
| `--strict-pdf` | fuzzy 매칭 차단, ID(DOI/arXiv)로만 PDF 매칭 |
| `--slugs A,B,C` | 특정 논문만 처리 |
| `--dry-run` | 실행 계획만 출력 (변경 0) |
| `--skip-dedup` / `--dedup-execute` | Zotero 중복 검사 제어 |
| `--insights` | 크로스카테고리 인사이트 생성 (opt-in) |
| `--llm-mode off` | 결정론 단계만 (Codex 생성 거부, exit 3) |

## Python 환경

- **검증된 Python 3.12 계열**을 사용한다. 시스템 Python이나 `py` 연결이 변경되어도 프로젝트 런타임을 우선 선택한다. 패치 업데이트는 별도 후보 환경에서 의존성과 실행 검사를 통과한 뒤 적용한다. 다른 minor 계열은 호환성 정책 변경 전 자동 채택하지 않는다.
- Windows: 모든 명령에 `PYTHONUTF8=1`.
- SPECTER2 모델 캐시: `.cache/` (없으면 `prepare_local_models.py --specter2` 로 준비).

## 한국 망 우회

SPECTER2 다운로드가 한국 ISP에서 막히면:

```bash
mkdir -p .cache && cd .cache
curl -L -o specter2_0.tar.gz "https://ai2-s2-research-public.s3.amazonaws.com/specter2_0/specter2_0.tar.gz"
tar -xzf specter2_0.tar.gz   # base/ + adapters/
```

arXiv 429 가 잦으면 `search_papers.py --skip-arxiv` (OpenAlex+S2 만).

## Schema v1 frontmatter

모든 `review.md` 는 `---` + `schema_version: v1` frontmatter 를 가진다.
없으면 검증(`validate_default_artifacts`)이 실패한다. 생성 템플릿이 자동 포함.

## 캐시·재개

- 각 단계는 상태 파일(`pipeline/_safe_update_state/`)로 추적된다.
- 실패 시 이전 단계 해시 보존, `--resume` 으로 실패 단계부터 재실행.
- LLM 생성은 `.llm_cache` 로 캐시 — 동일 입력이면 재호출 없음.

## 모델·도구 업데이트

작업별 모델은 `pipeline/model-config.json`에서 관리한다. `review`는 GPT-5.6 Terra/high,
`answer`, `timeline`, `connections`는 GPT-5.6 Terra/high,
`category_summary`, `topic_labels`는 GPT-5.6 Luna/medium이다. 사용자 설정을 읽지 않는
격리된 Codex CLI 호출에 이 값을 명시적으로 전달하므로 대화창의 모델 선택과 독립적이다.
Fast mode와 유료 API fallback은 사용하지 않는다.

GPT-6 Astra는 `roles.review.model`을 `gpt-6-astra`로 명시적으로 바꾸어 사용할 수 있다.
2026-09-11 이 환경의 같은 본문 비교에서 Astra medium은 2편에서 유용한 추가 검토를 제공했으나
1편이 600초 제한을 넘겼고, 보완된 실행기에서도 180초 제한 재검증에 실패했다.
따라서 기본 리뷰는 정상 완료된 Terra/high를 유지한다. 이 판단은 해당 환경·표본에 한정하며
Astra 전반의 품질이나 가용성에 대한 결론이 아니다. 자동으로 다른 모델이나 유료 API로
fallback하지 않으며, 실패·부분 결과는 성공 캐시에 게시하지 않는다.

CLI의 버전·경로·해시는 로컬 검증 기록이고, 필요한 격리·구조화 출력 기능은 호환성 계약이다.
새 실행 파일은 서명과 기능 검사를 통과한 뒤 최소 JSON 생성 검사로 검증한다.
조회/진단에서는 생성 호출을 하지 않는다. 정상적인 생성 요청에서 변경을 감지하면 필요한
검증을 먼저 수행하며, 실패한 후보로 기존 검증 기록을 덮어쓰지 않는다.

관리자가 사용할 수 있는 점검 명령은 다음과 같다. 일반 사용자는 에이전트에게 점검이나
업데이트를 요청하면 된다. `--verify-only`는 생성하지 않고, 명시적인 CLI 재검증은
사용할 모델별 최소 생성 검사를 포함한다.

```powershell
$env:PYTHONUTF8 = '1'
& .\.tools\python312\python.exe pipeline/tools/requalify_codex.py --verify-only
& .\.tools\python312\python.exe pipeline/tools/requalify_codex.py --accept-current-signed-binary
& .\.tools\python312\python.exe pipeline/tools/bootstrap_python_runtime.py --check-only --project-root .
& .\.tools\python312\python.exe pipeline/tools/bootstrap_python_runtime.py --list-candidates --project-root .
```

Python은 `--stage-candidate <탐색된-python.exe-경로>`로 별도 환경을 설치·검증하고,
`--promote-staged`로 사용 환경을 전환한다. `--rollback-previous`는 이전 환경으로 되돌린다.
`--qualify-candidate <경로>`는 준비와 전환을 한 번에 수행한다. 모두
`bootstrap_python_runtime.py --project-root .`의 옵션이다. 시스템 Python은 수정하지 않는다.
새 환경은 버전별 고유 경로에 유지하며 활성 환경 기록만 전환하므로, 이미 실행 중인
프로세스의 이전 Python 경로는 사라지지 않는다. 의존성은 전체 해시 잠금 파일
`requirements-lock-py312-full.txt`로 설치한다.

생성 캐시 v2는 실제 실행 환경 이력을 보존하면서 호환성 계약을 통과한 CLI 변경에는 결과를
재사용할 수 있다. 모델·추론 수준·프롬프트·스키마·원문·정책 변경은 캐시를 무효화한다.
다른 작업의 모델 설정을 바꾼 경우에는 해당 작업의 결과만 영향을 받는다.
기존 v1 캐시는 삭제하지 않으며 새 생성 시 v2 형식으로 저장한다.

새 리뷰는 최대 40,000자 입력을 사용한다. 원문이 이 한도 안이면 전체 본문을 그대로 전달한다.
한도를 넘으면 Methods/Results/Discussion/Conclusion 및 한국어 대응 절과 분산된 본문 구간을
선택한다. 소제목을 탐지하지 못했다는 이유로 해당 내용이 없다고 단정하지 않는다. 원문은 변경하지
않으며, 선택·누락·생략 정보를 `review-generation-v1.json`에 모델·추론 수준과 함께 기록한다.
기존 `review.md`는 모델 변경만으로 일괄 재생성하지 않는다. 생성 이력이 없는 기존 리뷰는
현재 모델로 검증되었다고 간주하지 않고, 필요할 때 범위를 지정해 비교한다.

`pipeline/tools/compare_review_models.py`는 3~5편의 동일 입력·동일 추론 수준을 Terra와
Astra로 비교한다. 기본값은 계획 조회이며 `--execute`가 있을 때만 생성한다. 결과는 `.omo`
아래 별도 폴더에 기록하고 원래 리뷰를 수정하지 않는다. JSON/한국어 형식 검사를 통과해도
수치·근거 정확성이나 논문 평가의 타당성까지 자동 입증되는 것은 아니다.

## Citedby (선택)

```bash paper-curation-command
PYTHONUTF8=1 python pipeline/run_citedby.py --doi 10.xxxx/xxxxx --pdf-first --build-index --serve --open
```

DOI 하나에서 인용 계보·타임라인·Deep(er) Research 를 로컬 HTML 로 생성한다.

## 검색 품질 회귀 테스트

```bash paper-curation-command
python pipeline/evaluate_retrieval.py \
  --queries pipeline/eval/retrieval_queries.jsonl \
  --vectors pipeline/eval/retrieval_query_vectors.json \
  --all --baseline pipeline/eval/retrieval_baseline.json \
  --min-recall-at-5 0 --strict --output pipeline/eval/results/latest.json
```

## 문제 해결

| 증상 | 해결 |
|---|---|
| `ModuleNotFoundError: config_loader` | 저장소 루트에서 실행 (패키지 경로 자동 삽입) |
| PDF 를 못 찾음 (`no_pdf`) | Zotero 앱에서 동기화 → PDF 로컬 다운로드 확인 |
| `SAFE RUN OWNERSHIP DENIED` | 토픽 alias 를 영문 소문자·숫자로 (한글 금지) |
| 분류 실패 (`specter2`) | `.cache/` 준비 (`prepare_local_models.py --specter2`) |
| 크레딧 소진 | 생성 단계 실패 → 재충전 후 `--resume` |

## 삭제 (포크 제거)

전체 제거 절차는 `AGENTS.md` 의 "삭제 방법" 참고.
