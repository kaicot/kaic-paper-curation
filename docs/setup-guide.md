# kaic-paper-curation — Setup Guide (포크 기준)

설치 대상은 `kaicot/kaic-paper-curation`이다. 원작 저장소와 동기화하지 않는다.
이 PC에서는 `D:\workspace\kaic-paper-curation`이 실행 원본이고 OneDrive 폴더는 소스 보관본이다.
다른 PC의 설치에서는 사용자가 지정한 실행 checkout을 사용한다.

## 설치 요청과 사전 준비

사용자는 "kaic-paper-curation 설치해줘"라고 요청하면 된다. 에이전트가 알려진 설정을 재사용하고, 부족한 정보만 필요한 순서로 묻는다.
저장소 URL을 설명해 달라는 요청만으로 설치·설정·첫 리뷰를 시작하지 않는다.

- 생성: Codex saved-auth(ChatGPT 로그인). `codex login status`로 상태를 확인한다. 유료 모델 API 키는 필요 없다.
- 환경: 검증된 프로젝트 Python 3.12. 런타임 선택과 PowerShell 환경변수 지정은 [운영 문서](operations.md#python-환경)를 따른다.
- 자료: 대상 컬렉션과 로컬 PDF 위치. 로컬 linked_file은 클라우드 저장용량 대신 해당 파일 경로를 참조하므로 원본 파일을 이동/삭제하지 않는다.

### 로컬 감지와 API 권한을 구분

`pipeline/tools/inspect_local_zotero.py --json`은 API 키 없이 로컬 컬렉션·PDF 상태를 읽을 수 있다.
이는 준비 상태 조회이지 쓰기 권한이나 전체 파이프라인의 keyless 실행 보장이 아니다.
현재 `add_paper_to_zotero.py`로 등록하려면 **Zotero API 쓰기 키와 사용자 ID**가 필요하고,
`setup.py`의 연결 검증 및 Zotero API 동기화에도 해당 설정이 필요하다.
키가 없으면 감지/설정 안내까지만 수행하고 실제 등록에 필요한 권한을 안내한다. SQLite에 직접 쓰지 않는다.
키 값은 대화·로그에 표시하지 않는다.

## 에이전트 설치 흐름

1. 기존 실행 checkout을 확인한다. 없을 때만 사용자 지정 위치에 이 포크를 clone한다.
2. 프로젝트의 검증된 Python 런타임과 의존성을 준비한다. 시스템 Python의 minor 버전을 임의로 바꾸지 않는다.
3. 필요한 설정만 확인한다: API 권한/사용자 ID, 연락 이메일, 컬렉션, 영문 topic alias, PDF 폴더.
   컬렉션 이름은 실제 목록으로 확인하고, alias는 영문 소문자·숫자·`-`·`_`를 사용한다.
4. `config.json`에 설정하고 `doctor.py`로 준비 상태를 점검한다.
5. `pipeline/setup.py --install-skill`로 스킬을 설치한다. 기존 설치 갱신은 `--replace-skill`을 추가한다.
6. 첫 리뷰는 사용자가 요청한 경우에만 실행한다. 열람이 필요하면 서버와 해당 topic의 실제 HTTP 응답을 확인한 뒤 URL을 알려준다.

`doctor.py`의 기본 진단은 생성하지 않는다. `--codex-canary`는 별도 생성 검사이므로 명시적 재검증/생성 요청에서만 사용한다.
`status: ready`는 준비 상태이며 실제 리뷰 생성·논문 평가의 정확성을 입증하지 않는다.

## 수동 명령 (Bash)

아래는 **Bash** 예시다. `python`은 검증된 프로젝트 Python을 가리켜야 한다.
Windows PowerShell은 운영 문서의 `$curationPython` 선택 및 `$env:PYTHONUTF8` 예시를 사용한다.

```bash
git clone https://github.com/kaicot/kaic-paper-curation.git
cd kaic-paper-curation
# 의존성은 프로젝트 Python 3.12의 검증/잠금 절차에 따라 준비한다.
```

```bash paper-curation-command
# 대화형 설정 (기존 config가 없을 때)
PYTHONUTF8=1 python pipeline/setup.py
# 스킬만 설치: config/첫 리뷰 생성과 독립적
PYTHONUTF8=1 python pipeline/setup.py --install-skill
# 기존 설치를 갱신: 정션이면 연결은 보존하고 SKILL.md만 교체
PYTHONUTF8=1 python pipeline/setup.py --install-skill --replace-skill
# 생성 없는 준비 상태 진단
PYTHONUTF8=1 python pipeline/doctor.py --format json
```

## config.json 예시

```json
{
  "schema_version": 2,
  "runtime": { "llm_mode": "codex", "allow_paid_api": false },
  "zotero": {
    "api_key": "YOUR_ZOTERO_API_KEY_HERE",
    "user_id": "YOUR_ZOTERO_USER_ID",
    "email": "you@example.com",
    "collections": { "mypapers": "내Zotero컬렉션이름" },
    "pdf_dir": "C:/Users/<이름>/Zotero"
  },
  "unpaywall_email": "you@example.com"
}
```

`collections`는 `"topic alias": "컬렉션 이름 또는 8자리 key"` 매핑이다.
메일·사용자 ID를 추정해서 채우지 않는다. config는 Git/소스 보관본에 포함하지 않는다.

## 요청별 사용

| 요청 | 행동 |
|---|---|
| "이 논문 넣어줘" + PDF/URL | 대상 컬렉션·alias 확인 → 등록 + 리뷰 |
| "등록만 해줘" | 등록 명령에 `--no-run` |
| "새 논문 리뷰해줘" | 해당 컬렉션 갱신 + 신규 리뷰 |
| "웹에서 보고 싶어" | 해당 topic 페이지/서버 확인 후 URL |
| "몇 편 리뷰됐어?" | 해당 topic의 산출물만 조회; 생성·서버 실행 안 함 |

스킬 호출: Codex의 `@kaic-paper-curation` 또는 `$kaic-paper-curation`.
실행 플래그·제한과 실패 복구는 [운영 문서](operations.md)를 참조한다.
크레딧 소진 시 유료 API로 전환하지 않으며 `run_full.py --resume`은 지원하지 않는다.

## 문제 해결 및 삭제

PDF 없음은 로컬 다운로드와 파일 경로를, 컬렉션 오류는 실제 목록/권한을 확인한다.
`SAFE RUN OWNERSHIP DENIED`는 alias 형식만의 문제가 아니다. 동시 실행·재개 상태를 운영 문서에서 확인한다.
삭제는 [운영 문서의 삭제 절](operations.md#삭제-포크-제거)을 따른다. 설치 정션과 연결 대상·리뷰 데이터를 혼동하지 않는다.
