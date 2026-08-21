# BBS-SCV — Repo Guidance

이 파일은 `C:\Dev\bbs-scv` 이하 전체에 적용된다.

## 프로젝트 개요

디스코드 봇(BBS) 원격 모니터링 및 관리용 텔레그램봇.

- 메인 파일: `telegram_bridge.py`
- polling 단방향 구조
- `InlineKeyboard` 콜백 미작동 전제
- 버튼 UI 사용 금지

## 명령어 접두어 규칙

- `!명령어`: 텔레그램 관리봇 커맨드
- `/명령어`: 디스코드 봇 슬래시 커맨드

Keep all management commands under the `!` prefix.

## 커맨드 목록

전체 커맨드 표는 `README.md`가 단일 진실원본 (코드 `telegram_bridge.py`의 `_CMD_ROUTES` + 별도 라우팅되는 `!news`/`!news2`/`!newslist`/`!newscc` 기준으로 최신화).

## 봉봉뉴스 발송 구조

- 구독자 목록: `data/bbs.db` → `bongnews_subscribers`
- 디스코드 봇과 DB 공유
- 발송 방식: Discord DM 병렬 발송
- 미디어 발송 시 임시파일 정리까지 포함해 본다

## 디버깅 플레이북

### P1. 명령어가 다른 핸들러로 잘못 라우팅됨

원인:

- `_CMD_ROUTES`가 `startswith` 기반이면 짧은 prefix가 긴 prefix를 가로챌 수 있음

Check common conflicts first:

- `!re` / `!re0`
- `!news` / `!news2`

수정 원칙:

- 더 긴 prefix를 더 위에 둔다

### P2. 봇 출력 시간이 한국시간이 아님

원인:

- UTC ISO 문자열 슬라이싱 출력
- `datetime.now()`를 타임존 없이 사용

Always convert stored UTC timestamps to KST before displaying them.

권장:

- `_KST`, `_to_kst()` 헬퍼 사용
- 날짜 파일 조회 기준은 `datetime.now(_KST)`

### P3. `telegram.error.TimedOut` / `httpx.ConnectTimeout`

원인 후보:

- Windows ProactorEventLoop + `httpx` / Defender 조합

원칙:

- 필요 시 `HTTPXRequest` 주입
- Windows에서는 `WindowsSelectorEventLoopPolicy()` 고려
- `urllib` 성공 / `httpx` 실패면 네트워크 차단보다 transport 이슈 쪽을 먼저 본다

## 코딩 / 수정 규칙

Do not add button-based UI here.

재시작 로직 수정 시:

- 대상 프로세스 매칭 범위를 좁게 유지
- 다른 프로젝트(`bbs-web`, `claxx` 등) 프로세스를 건드리면 안 됨

운영 로그 / 오류 메시지는 너무 장황하지 않게 유지한다.

## 운영 우선순위

이 repo는 기능 추가보다 아래를 우선한다.

- 재시작 신뢰성
- 대상 프로세스 매칭 정확도
- 오류 가시성
- 운영 명령 충돌 방지

## 관련 repo 연동

- 디스코드 봇 본체: `C:\Dev\bbs`
- 이 repo 수정 시 `bbs`의 재시작 방식, 배치파일과 연결되는지 같이 확인한다
