# kaic-paper-curation — Setup Guide (포크 기준)

**kaicot 포크**의 설치 가이드다. 원작(`jehyunlee/paper-curation`)이 아니라
이 저장소(`kaicot/kaic-paper-curation`)를 기준으로 한다.

## 사전 준비

- **Codex(ChatGPT) 로그인** — `codex login status` → `Logged in using ChatGPT`
- **Zotero 앱** — 논문을 컬렉션에 넣고, 해당 컬렉션의 PDF가 로컬
  (`C:\Users\<이름>\Zotero\storage`)에 내려받아져 있어야 함
- **Python 3.12** (Windows면 `PYTHONUTF8=1` 접두사)

> 💡 **유료 API 키 불필요**: Anthropic/OpenAI/Google 키는 설치·운영에 필요 없다.
> 생성은 Codex saved-auth 만 사용. Zotero API 키도 로컬 Zotero 감지로 대체 가능.

## Codex에서 설치 (권장)

Codex(또는 Codex 스킬이 설치된 에이전트)에게 한 줄만 입력:

> **"여기에 kaic-paper-curation 설치해줘: https://github.com/kaicot/kaic-paper-curation"**

LLM이 아래를 **한 번에 하나씩** 진행한다:

1. 레포 클론 + Python 의존성 설치
2. 인터랙티브 설정 (하나씩 질문):
   - Zotero API 키 (없으면 로컬 Zotero 감지)
   - 이메일
   - Zotero 컬렉션 이름 (검증 후 없으면 목록 제시)
   - 토픽 alias (영문)
   - Zotero PDF 폴더
3. 환경 확인 — Codex saved-auth·py312
4. Zotero 연결 테스트
5. 스킬 설치 — `~/.codex/skills/kaic-paper-curation/`
6. 첫 파이프라인 실행 + `http://localhost:8000/{토픽}/` 안내

> **⚠ Codex 크레딧**: 생성 단계는 ChatGPT 구독의 Codex 크레딧을 소비한다.
> 소진 시 생성 단계가 실패 → 재충전 후 `--resume`으로 이어서 실행.
> 유료 API 키로 자동 전환되지 않는다.

## 수동 설치

```bash paper-curation-command
git clone https://github.com/kaicot/kaic-paper-curation.git
cd kaic-paper-curation
pip install -r requirements.txt   # Python 3.12 환경

# 대화형 설정 → 첫 파이프라인
PYTHONUTF8=1 python pipeline/setup.py

# 스킬만 설치 (이미 config 있으면)
PYTHONUTF8=1 python pipeline/setup.py --install-skill

# 환경 점검
PYTHONUTF8=1 python pipeline/doctor.py --format json
```

## config.json 직접 편집

```json
{
  "schema_version": 2,
  "runtime": { "llm_mode": "codex", "allow_paid_api": false },
  "zotero": {
    "api_key": "YOUR_ZOTERO_API_KEY",
    "user_id": "9951495",
    "email": "you@example.com",
    "collections": {
      "mypapers": "내Zotero컬렉션이름"
    },
    "pdf_dir": "C:\\Users\\<이름>\\Zotero"
  },
  "unpaywall_email": "you@example.com"
}
```

- `collections`는 `"토픽alias": "Zotero컬렉션이름"` 매핑. 여러 개 가능.
- 컬렉션 이름 대신 8자리 Zotero key(`C4MGY283`)도 가능.
- 새 컬렉션은 여기에 한 줄 추가 → `--topic <alias>`로 실행.

## 사용법

설치 후에는 말만 하면 된다:

| 말 | LLM 행동 |
|---|---|
| "이 논문 넣어줘" + PDF/URL | Zotero 등록 + 리뷰 자동 |
| "새 논문 리뷰해줘" | 컬렉션 갱신 + 리뷰 |
| "웹에서 보고 싶어" | 서버 켜고 URL 안내 |
| "몇 편 리뷰됐어?" | 진행 상황 요약 |

스킬 호출: Codex 입력창에 `@kaic-paper-curation` 또는 `$kaic-paper-curation`.

## 설치 확인 & 문제 해결

### 설치 확인

```bash paper-curation-command
PYTHONUTF8=1 python pipeline/doctor.py --format json
# status: ready 가 나오면 정상
```

### 문제 해결

| 증상 | 해결 |
|---|---|
| Codex 로그인 안 됨 | `codex login status` → ChatGPT 로그인 |
| 컬렉션 이름 오류 | 사용 가능한 컬렉션 목록을 보고 다시 입력 |
| PDF 없음 (`no_pdf`) | Zotero 앱에서 동기화 → PDF 로컬 다운로드 |
| `SAFE RUN OWNERSHIP DENIED` | 토픽 alias 영문으로 (한글 금지) |
| 분류 실패 | `prepare_local_models.py --specter2` 로 `.cache/` 준비 |
| 크레딧 소진 | 재충전 후 `--resume` |

## 삭제

제거 절차는 [AGENTS.md](../AGENTS.md)의 "삭제 방법" 참고.
