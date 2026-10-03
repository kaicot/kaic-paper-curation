# CLAUDE.md

Claude Code가 이 저장소에서 작업할 때는 `AGENTS.md`를 먼저 읽는다.
사용자 요청의 스킬 진입점은 `SKILL.md`이며, 설치본에는 SKILL.md만 있으므로 실행 checkout을 확인한 뒤 그곳의 필요한 운영 문서만 읽는다.

특히 다음 경계를 유지한다:

- 원본 작업은 `D:\workspace\kaic-paper-curation`; OneDrive 폴더는 소스 보관본이다.
- 원작 `jehyunlee/paper-curation` 및 upstream과의 fetch/pull/merge/push·PR·이슈·동기화는 금지한다.
- 등록은 기본 등록→리뷰, "등록만"은 `--no-run`이다. 조회·진단·유지보수는 생성 요청이 아니다.
- 로컬 Zotero 감지는 읽기 전용이며 API 쓰기 권한을 대신하지 않는다.
- 생성은 Codex saved-auth만 사용하고 유료 API fallback과 공개 웹 배포는 지원하지 않는다.
- 대화 에이전트의 모델 선택을 파이프라인 역할 설정에 자동 적용하지 않는다.
- 실제 결과와 페이지 응답으로 완료를 확인하고, 조회만 한 경우 서버를 시작하지 않는다.

세부 실행 제한·복구·삭제는 `docs/operations.md`, 설치는 `docs/setup-guide.md`를 참조한다.
