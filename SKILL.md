---
name: kaic-paper-curation
description: "Use for the kaicot paper-curation fork: add paper PDFs/URLs to Zotero, collect recent papers, review/classify a Zotero collection, or view its local searchable review pages. Trigger on 논문 큐레이션, 이 논문 넣어줘, 새 논문 리뷰, 이번 주 논문 찾기, and @kaic-paper-curation. Not for extracting a document's reference list or writing journal peer-review reports."
---

# KAIC Paper Curation

## 실행 위치와 필요한 문서

설치본에는 이 파일만 있다. 스킬 폴더를 실행 저장소로 취급하거나, 그 안에서 상대 경로로 `pipeline/`을 찾지 않는다.
사용자가 지정한 실행 checkout을 우선 확인한다. 이 PC의 원본은 `D:\workspace\kaic-paper-curation`; OneDrive의 같은 이름 폴더는 소스 보관본이다.
checkout에 `pipeline/run_full.py`와 `VERSION`이 있는지 확인하고 그 루트에서 실행한다. 없으면 위치를 묻는다. URL 조회나 설명 요청만으로 설치하지 않는다.

아래 파일은 확인한 **checkout 기준 절대 경로**로 필요한 것만 읽는다:

| 상황 | 읽을 파일 |
|---|---|
| 설치·설정이 없거나 연결 문제 | `docs/setup-guide.md` |
| 실행 명령·제한·실패 복구 | `docs/operations.md`의 해당 절 |
| 저장소 코드/문서 유지보수 | `AGENTS.md` |
| 내부 구조·함수 API를 수정해야 할 때 | `docs/architecture.md`, 해당 `pipeline/api/` 소스 |

## 요청 → 행동

알려진 컬렉션·topic·PDF 위치는 재사용한다. 빠진 대상/기간만 확인하고, 초보자의 설치 안내는 필요한 질문을 하나씩 한다.
스킬 업데이트·진단·진행 조회는 논문 등록이나 생성 요청이 아니다.

| 요청 | 진입점과 범위 |
|---|---|
| PDF/URL 논문 넣기 | `pipeline/tools/add_paper_to_zotero.py --pdf <파일>` 또는 `--url <주소>`, `--collection <이름> --topic <alias>`; 기본 등록→curation 유지 |
| 등록만, 리뷰하지 말기 | 위 등록 명령에 `--no-run` |
| Zotero 논문 리뷰·신규 갱신 | `pipeline/run_full.py --topic <alias> --mode curate --source zotero` |
| 최신 논문 수집·리뷰 | `--mode curate --source web --days N`; 오늘=1, 이번 주=7, 별도 기간은 요청 기준 |
| 재분류 / 타임라인 | `--mode reclassify` / `--mode retime`; 이미지 생성 옵션을 추가하지 않는다 |
| 오매칭·중복 확인 / 산출물 검증 | `--mode audit`, `--mode fix-matching`, `--mode dedup` / `--mode validate` |
| 컬렉션 목록 / 진행 현황 | `pipeline/tools/inspect_local_zotero.py --json` / 해당 topic의 인덱스와 리뷰를 읽기만 한다 |
| 결과 열람 | `pipeline/serve_local.py`의 기존 서버와 요청 topic 경로 확인; 필요할 때만 서버 실행 |

문서의 참고문헌 일괄 등록은 `kaic-zotero-push`, 투고 원고 심사 의견은 `kaic-peer-review`로 넘긴다.
지속 모니터링 요청은 기간·빈도·종료 조건과 사용 가능한 예약 수단을 별도로 확인한다. 한 번 검색하고 모니터링이 설치됐다고 말하지 않는다.

## 실행 전 알아둘 점

- 프로젝트가 검증한 Python 3.12 런타임을 사용한다. Windows PowerShell에서는 `$env:PYTHONUTF8 = '1'`; Bash식 환경변수 접두사를 붙이지 않는다.
- 로컬 Zotero 감지는 읽기/설정 안내다. `found: true`만으로 쓰기 권한을 얻는 것이 아니다. 현재 등록 도구는 Zotero API 쓰기 키와 사용자 ID가 필요하며 SQLite에 직접 쓰지 않는다.
- 생성은 Codex saved-auth만 사용하고 `allow_paid_api: false`를 유지한다. 대화 에이전트의 모델 선택과 `pipeline/model-config.json`의 생성 역할 설정은 독립적이다. 모델을 자동 변경하거나 유료 API로 우회하지 않는다.
- `rebuild`는 기존 리뷰 재생성이므로 명시적 요청·백업·대상 확인이 필요하다. 가능한 `--slugs A,B --strict-pdf`로 제한한다. `--yes`를 안전장치가 구현되었다는 근거로 삼지 않는다.
- 현재 안전 경로는 `--images changed/all`, `--insights`, `--local-fallback`, `--dedup-execute`를 거부한다. `dedup`·`fix-matching`은 미리보기이며 `--yes`로 삭제 실행이 되지 않는다. 삭제를 요청받아도 운영 문서의 별도 범위/백업 확인을 먼저 따른다.
- `--llm-mode off`는 현재 전체 실행을 exit 3으로 거부한다. 결정론 작업 완료로 보고하지 않는다. `--dry-run`은 계획 확인이지 연결·생성·산출물 성공 검증이 아니다.
- 실패 시 단계·종료 코드·이미 등록된 item key와 변경된 산출물을 구분한다. 등록 명령을 맹목적으로 반복하지 않는다. `run_full.py`에는 `--resume`이 없다; 실제 상태/캐시를 확인하고 운영 문서의 복구 절차를 따른다.
- 결과는 로컬 열람만 지원한다. 공개 웹 배포나 새 예약 작업은 자동으로 수행하지 않는다.

## 완료 보고

실제 출력과 topic 산출물로 등록·리뷰·검증·실패 수를 구분한다. 등록 성공 후 리뷰 실패를 전체 성공이라고 하지 않는다.
열람 요청 또는 생성 결과 확인에 필요한 경우에만 서버를 확인/실행하고, 실제 HTTP 응답과 topic 경로를 확인한 URL을 제공한다.
조회·진단만 한 경우 서버를 시작하지 않는다. 실행하지 않은 단계와 남은 장애를 명시한다.
