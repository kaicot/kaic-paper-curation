# AGENTS.md

이 저장소를 유지보수하는 Codex·Claude 및 유사 에이전트의 지침이다.
사용자 작업의 진입점은 `SKILL.md`; 설치는 `docs/setup-guide.md`, 실행·복구는 `docs/operations.md`, 내부 구조는 `docs/architecture.md`에서 필요한 절만 읽는다.

## 작업 위치와 소스 보관본

- 실제 작업 원본은 `D:\workspace\kaic-paper-curation`이다. 코드 수정·실행·Git 작업은 여기서만 한다.
- `D:\OneDrive\AI\kaic-paper-curation`은 커밋된 소스 보관본이다. 그 위치에서 이 문서를 읽었다면 원본으로 전환한다. 보관본에 `.git`, 실행 환경, 캐시를 만들지 않는다.
- 검증 후 로컬 커밋하면 post-commit 훅이 소스 보관본을 갱신한다. 수동 갱신은 `scripts/update-source-snapshot.ps1`이다. GitHub push는 별도 요청 범위다.
- 보관본의 직접 수정이 감지되면 덮어쓰지 않는다. 훅 실패 시 커밋은 보존되므로 갱신 결과와 `git status`를 따로 확인한다.
- 기존 리뷰·PDF·설정·사용자의 미커밋 수정은 보존한다. 정리/삭제 절차는 운영 문서의 "삭제"를 따른다.

## 원작과의 모든 교류 금지 (운영자 지시 2026-08-13)

- 이 저장소는 kaicot 포크다. 원작 `jehyunlee/paper-curation`에 PR·이슈·기여·push·동기화를 하지 않는다.
- `upstream` remote는 조회용으로만 남긴다. fetch/pull/merge/push 또는 `fetch --all`을 하지 않는다.
- 허용된 원격 작업은 이 포크의 `origin`에만 수행한다. 설치·호출·스킬 이름은 `kaic-paper-curation`이다.

## 요청과 실행 경계

- 사용자는 코드를 직접 입력하지 않아도 된다. 알려진 설정을 재사용하고 필요한 대상만 확인한다. 초보자 설치 안내는 필요한 질문을 하나씩 한다.
- PDF/URL 등록은 기본적으로 등록→리뷰를 유지한다. "등록만"은 `--no-run`을 사용한다. 조회·진단·스킬 유지보수 요청으로 논문 등록·생성·서버 실행을 시작하지 않는다.
- 로컬 Zotero 감지는 읽기 전용이다. 등록 도구의 API 쓰기 키·사용자 ID 요구를 로컬 감지만으로 충족한다고 안내하지 않는다.
- `rebuild`, 삭제, 기존 리뷰 교체는 명시된 대상·백업·권한을 확인한다. `--yes`가 실제로 전달되거나 강제된다고 가정하지 않는다.
- CLI의 `--help`는 문법, `--dry-run`은 계획만 확인한다. 승인된 실행의 종료 코드와 산출물 검증을 별도로 확인한다.
- 등록 성공/리뷰 실패를 구분하며, 실패한 등록 명령의 재실행 전 기존 item key를 확인한다. 실제 topic 페이지 응답을 확인해야 열람 가능한 URL로 보고한다.
- 현재 안전 프로파일의 제한과 복구는 운영 문서가 기준이다. 미지원 옵션을 우회하는 직접 스크립트 호출을 자동 선택하지 않는다.

## Python·모델·보안

- 검증된 프로젝트 Python 3.12 계열과 `_env_guard`/공통 런타임 선택기를 사용한다. 시스템 `py`/PATH 변경을 이유로 우회하지 않는다.
- Windows PowerShell 환경변수는 `$env:PYTHONUTF8 = '1'`로 지정한다. 패치 후보는 별도 검증 후 전환하고, 다른 minor 계열을 자동 채택하지 않는다.
- 생성은 Codex saved-auth만 사용한다. 유료 API fallback은 거부하고 `allow_paid_api: false`를 유지한다. `--llm-mode off`의 exit 3은 완료가 아니다.
- 파이프라인 생성 역할의 단일 기준은 `pipeline/model-config.json`이다. 대화 에이전트/서브에이전트 모델 지침과 혼동하지 않는다. 현재 Terra/Luna 설정은 자동 변경하지 않고 Astra도 자동 fallback하지 않는다.
- CLI의 서명·격리·구조화 출력 호환성과 로컬 검증 기록을 사용한다. CLI 버전 번호를 코드에 다시 고정하지 않는다.
- 조회/진단에서는 생성하지 않는다. 명시적 재검증 또는 승인된 생성에서만 최소 생성 검사를 수행한다.
- 모델·CLI·Python 변경만으로 기존 리뷰를 일괄 재생성하지 않는다. 비교 결과는 `.omo` 아래 별도 보관한다.
- 키·개인 설정을 출력하지 않는다. `config.json`, `.cache`, `.omo`, 논문 산출물과 실행 환경을 소스 보관본/원격에 새로 포함하지 않는다.

## 검증과 버전 관리

- 변경 범위에 맞는 기존 테스트·명령 검증을 먼저 사용한다. 실제 Zotero 쓰기/모델 생성은 문서 유지보수 테스트로 수행하지 않는다.
- 커밋 전 `scripts/scan-secrets.py --working-tree`, push 전 새 Git 객체에 대한 비밀 검사를 통과시킨다.
- 프로젝트 버전은 `VERSION`이 단일 기준이다. SemVer에 따라 호환되는 지침/문서 수정은 PATCH, 새 기능은 MINOR, 호환성 파괴는 MAJOR다.
- 릴리스 요청 범위에서는 VERSION·CHANGELOG·README를 맞추고 `vMAJOR.MINOR.PATCH` 태그를 사용한다. 검증한 파일만 커밋하고 origin 커밋/태그, 설치본 및 소스 보관본 해시를 확인한다.
- 설치는 `pipeline/setup.py --install-skill`; 갱신은 `--replace-skill`을 함께 사용한다. 설치 폴더가 정션이면 연결을 제거하거나 대상 전체를 복사하지 않고 SKILL.md만 갱신한다.
