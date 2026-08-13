# kaic-paper-curation — Operations Manual (포크 기준)

이 문서는 **kaicot 포크**에서 파이프라인을 운영하는 방법을 설명한다.
원작(jehyunlee)의 배포·BYOK·dense 검색 등은 이 포크에 없다.

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

- **Python 3.12 단독**. py314 는 numba 호환 문제로 금지 — `_env_guard` 가 자동 라우팅.
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
