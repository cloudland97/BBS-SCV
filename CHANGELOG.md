# BBS-SCV 변경 이력

> 2026-04-06 BBS repo에서 독립 분리. 이전 개발 이력은 BBS/CHANGELOG.md 참조.

---

### 2026-09-10

- `!reboot ok` 추가 — 크롬 원격이 안 떠 있을 때 텔레그램으로 호스트 PC를 재부팅하는 탈출구(`shutdown /r /f /t 60`, `!reboot cc`로 취소). 인자 없는 `!reboot`과 `!reb*` 오타는 안내만(`!re` Discord 재시작으로 흘러가지 않음). 이 PC는 자동 로그인이 꺼져 있어 재부팅 후 크롬 원격으로 로그인해야 시작프로그램이 봇 2개를 다시 띄운다.

### 2026-09-07

- `!ud`/`!st`/`!se` 핸들러가 `bbs.db`를 이벤트 루프 위에서 직접 열던 것을 `fetch_rows_async`(워커 스레드에서 db_read) 경유로 변경 — BBS 쪽 쓰기가 진행 중이면 커넥트 타임아웃까지 폴링이 멈추던 문제 해소, Discord 조회 전에 커넥션도 닫힘.

### 2026-09-06

- **전수조사 2차(BBS `docs/audit-findings-2026-09-06.md`) 후속 조치.**
- 브릿지 단일 인스턴스 락(`logs/bridge.pid`) 추가 — `!re0` 연타나 `start_bot.bat` 중복 기동 시 브릿지가 2개 뜨는 경쟁 상태 방지(R4). 죽은/깨진 pid 파일은 기동을 막지 않음.
- 토큰 로그 유출 방지: 텔레그램 봇 토큰 패턴(`bot<id>:<secret>`)을 로그 레코드에서 걸러내는 리댁션 필터 추가 — 과거 httpx가 요청 URL을 INFO로 찍어 로그 20개 파일에 토큰이 평문 노출된 사고(N1) 재발 방지.
- 일일 유지보수 태스크 추가 — 로그 정리(`_cleanup_old_logs`)가 기존엔 `post_init` 1회뿐이라 장기 구동 시 정리가 멈췄음. 하루 1회 heartbeat 로그도 함께 추가(장시간 무로그 상태와 죽은 프로세스를 구분 못 하던 문제, N14/R11).
- `!newscc <ID> force` 추가 — 크래시로 `sending`에 고착된 행을 수동으로 `cancelled`로 되돌리는 탈출구.
- `!log`가 읽는 BBS 에러 로그 경로를 실제 파일명(`error.log`, `error.log.YYYY-MM-DD`)에 맞게 수정 — 기존 `error_<date>.log` 추정이 매번 빗나가 "No error log"만 반환했음(N4).
- `!re` 대상 프로세스 매칭에 `BBS_RESTART_ROOT` 환경변수를 명시적으로 넘기도록 정리 — `bbs-web`/`claxx` 등 형제 프로젝트 오탐 방지.

### 2026-08-22

- 폴링 소음 억제: httpx가 `getUpdates`를 매 10초 INFO로 찍어 하루 8,428줄 중 8,408줄이 노이즈였음(30일 보관 39MB). httpx/httpcore/PTB updater를 WARNING으로 고정 — 실제 네트워크 장애는 여전히 보임.
- pytest를 pre-push 훅으로 게이트화 + `scripts/run_tests.py`(venv 자동 탐색). 이 저장소는 pre-commit 설정 자체가 없었고 GitHub Actions는 계정 단위로 차단된 상태라 자동 검증이 0이었음.
- `!newscc` 취소 경쟁조건 수정: SELECT 후 status 가드 없이 UPDATE해서, 스케줄러가 그 사이 claim하면 **발송 중인 뉴스를 "취소됨"으로 덮어쓰고 운영자에게 취소 성공이라 보고**했음. `claim_for_send`와 같은 원자적 가드 적용, 경쟁 시 실패를 정직하게 통보(회귀 테스트 4건).
- 유저명 조회 실패가 로그조차 안 남던 침묵 except 수정(`!st`/`!list`/`!ud`/`!se` 표시 영향).
- `start_bot.bat`의 `timeout` → `Start-Sleep`(stdin이 콘솔이 아니면 timeout.exe가 즉시 실패해 대기가 아예 없었음).

### 2026-08-21

- **MIT 공개 릴리스 (v1.0).** LICENSE 추가, 히스토리 squash 후 force push(개인 1:1 대화 수집기 `telegram_archive` 흔적 제거 목적, 공개 전 수행).
- `telegram_archive` 패키지 + `!search` 제거 — 브릿지 기능이 아니라 별도 프로젝트로 이관.
- 수동 밴 3종(`!ban`/`!unban`/`!banlist`)·`!today` 은퇴 — 자동 밴이 이미 텔레그램 알림을 보내고 있어 중복.
- 독립 클론 대응: `tg.env` 없이도 기동·테스트 가능(env 폴백 + `tests/conftest.py`), `bbs.db` 부재 시 부팅 크래시 수정, CI에 pytest 설치 누락 수정(exit 127로 계속 실패 중이었음).

### 2026-07-23
- **버그 수정 (긴 뉴스 발행 실패)**: `!news 내용` 본문이 2000자를 넘으면 `_send_discord_dm`이 `{"content": ...}`를 분할 없이 POST → Discord 400 → 전 구독자 발송 실패("발행 안 됨"). 원인: 텔레그램측 헬퍼에 길이 분할 부재(BBS 본체 `split_message`와 달리). 수면위생 뉴스(본문 2076자 + `**bongnews**` 접두 = 2089자)에서 재현.
  - `_send_discord_dm`에서 `tg_utils.split_message(text, limit=2000)`로 분할 후 순차 전송(중복 헬퍼 신설 대신 기존 검증된 스플리터 재사용). 첫 청크만 미디어 첨부와 함께, 나머지는 후속 텍스트 메시지.
  - 회귀 테스트 추가(`test_news_broadcast.py`): 2000자 초과 분할·무손실, fake session 다중 POST 검증.

### 2026-07-08
- **버그 수정 (예약 뉴스 중복 발송 차단)**: 스케줄러가 `broadcast()` → `mark_status('sent')` 순서라 mark 실패(락 재시도 소진/크래시) 시 행이 'pending'으로 남아 다음 30초 사이클에서 재발송 = 구독자 전원 중복 뉴스. **claim-before-send** 도입으로 해결.
  - `scheduled_news`에 `claimed_at TEXT` 컬럼 추가 — `_ensure_table`에서 `PRAGMA table_info` 확인 후 없을 때만 `ALTER TABLE ADD COLUMN`(공유 bbs.db 대상, 멱등·비파괴 마이그레이션).
  - `claim_for_send(id)`: `UPDATE ... SET status='sending', claimed_at=? WHERE id=? AND status='pending'` 후 rowcount==1 반환. WHERE 가드로 단 하나의 claim만 승리 → 발송 창 동안 get_due(pending-only)가 재선택 불가.
  - 스케줄러/부팅 발송: `claim → broadcast → mark('sent')`, 실패 시 `mark('pending')`으로 되돌려 재시도 허용. mark('sent')가 발송 후 실패하면 행은 'sending'에 머물러 재발송되지 않음(중복 방지).
  - 부팅 시 `get_stale_sending`으로 5분 이상 'sending'에 멈춘 고아 행 탐지 → 관리자에게 경고만(자동 재발송 금지: 발송 성공 후 크래시면 이중 발송 위험).
  - 테스트 추가: claim 경쟁, 마이그레이션 멱등/레거시 ALTER, stale-sending 임계값.

### 2026-07-07
- **대정리 3연 (BBS LLM 스택 폐기 후속)**: ① `!llm` 모델 스위처 제거 ② `!dmlog` 제거 + bbs.db LLM 테이블 4종(dm_chat_history 등) DROP ③ **Claude CLI 브릿지 전체 제거**(claude_runner.py·sessions.json, !cs/!p/!reset, 자유텍스트→Claude, 이미지 Claude 분석) — 세션 단절로 미사용 판정. `!news2` 미디어 발송·`!ud`는 보존. usage-cache 헬퍼·PROJECTS all/bot 데드키·스테일 문서(!fb, telegram_commands.py) 청소.
- **start_bot.bat 수리**: `telegram_archive.collect`를 `start /min` 백그라운드로 — collect 행(Telethon)이 bridge 기동을 무한 차단하던 구멍. 같은 날 봇 침묵 원인이 이것+Startup .lnk 죽은 경로였음 → Startup을 `BBS_Bots.lnk`(start-bots.bat)로 교체, 재부팅 자동복구 확보.

### 2026-06-09
- **신규 기능**: 개인 텔레그램 1:1 대화 아카이브 (`telegram_archive/`, BBS와 분리)
  - Telethon **user client**(봇 토큰 X — 봇은 개인 DM 못 읽음)로 지정 대화방 수집
  - `start_bot.bat` 기동 시 1회 증분 수집 (마지막 msg_id 이후만, 첫 실행은 전체 백필)
  - SQLite + **FTS5 trigram** 저장 → 한글/파일명 substring 검색 (2글자 쿼리는 LIKE 폴백)
  - 첨부파일은 원본 미다운로드, 파일명·메타만 인덱싱 (원본은 텔레그램 다운로드 폴더에 존재)
  - 검색 2채널: CLI `python -m telegram_archive.search <kw>` + 봇 `!search 키워드`
  - 자격증명/세션/DB는 git 제외 (`archive.env`, `*.session`, `archive.db`)
- **버그 수정**: error handler 등록 (`app.add_error_handler`) — `NetworkError`/`TimedOut`(Bad Gateway 등 일시 장애) 풀 traceback 덤프 → 1줄 WARNING으로 압축, polling 로그 정리
- **버그 수정**: 미디어 핸들러가 요청 큐 백프레셔(qsize≥5) 우회하던 것 복구 — `claude_runner.enqueue_coro()` public 헬퍼 도입, 거부 시 코루틴 `.close()` (never-awaited 경고 방지)
- **인프라**: startup 시 `_cleanup_orphan_temp()` — 크래시로 남은 1h+ 묵은 `_tg_*` 다운로드 임시파일 정리
- **버그 수정**: `!p` 접두어 greedy 매칭 제거 (`!p`/`!p <name>`만 매칭, `!pXXX` 오발 차단)
- **정리**: `datetime`/`timezone`/`timedelta`/`ZoneInfo` 모듈 top-level hoist, 함수 내 inline import 4곳 제거

### 2026-04-15
- **리팩토링**: `telegram_commands.py` (886줄) → 4개 모듈 분리 (`cmd_claude`, `cmd_discord`, `cmd_subscribers`, `cmd_moderation`)
- **리팩토링**: `admin_only` 데코레이터 추출, 중복 내부 import 제거, `handle_photo/document` 공통 함수 추출, dead import 제거

### 2026-04-11
- **리팩토링**: `telegram_bridge.py` (857줄) → 4개 모듈 분리 (토큰 절약 목적)
  - `tg_config.py` (~100줄) — 환경변수, 경로, 상수, DB 연결
  - `tg_utils.py` (~100줄) — 메시지 분할, HUD, 포맷 헬퍼
  - `claude_runner.py` (~175줄) — Claude CLI 실행, 세션 관리, 요청 큐
  - `discord_helpers.py` (~155줄) — Discord API, 유저네임 캐시, DM, 봉봉뉴스
  - `telegram_bridge.py` (~270줄) — 핸들러 연결 허브 + re-export
- **telegram_commands.py**: `_br.` 참조 80개+ → 4개 모듈 직접 import로 전환
- **성능**: 유저네임 캐시에 eviction 추가 (500개 초과 시 만료 항목 정리)
- **로깅**: silent except 블록에 `logger.debug` 추가 (usage cache, bongnews subscribers)
- **추가 리팩토링**: `telegram_bridge.py` re-export 14개 제거, `handle()` 17-branch if/elif → `_dispatch_text()` + `_CMD_ROUTES` 접두어 테이블로 교체
- **버그 수정**: `telegram_commands.py` `datetime.utcnow()` 2곳 → `datetime.now(timezone.utc)` (Python 3.12+ deprecation)
- **모델**: Claude Sonnet 재전환 (4월 11일~)

### 2026-04-10
- **유지보수**: DB 경로 통합, WAL 모드, 로그 수정
- **인프라**: 로그 로테이션 추가, README 작성

### 2026-04-09
- **버그 수정**: `!re` cmd.exe 프로세스 매칭 경로를 `discord_path` 변수로 변경

### 2026-04-06 (2차)
- **경로 수정**: `tg.env`에 `PROJECTS_DISCORD/ALL/BOT` 명시 (`C:\DEV\BBS`)
- **루프 자동 알림**: 5분마다 `loop_health` 체크, 임계값 초과 시 텔레그램 push (루프별 6h 쿨다운)
- **`!log [N]`**: 날짜 인자 지원 (`!log 1` = 어제, `!log 2` = 그제)
- **`!sub`**: 봉봉뉴스 구독자 직접 관리 (`list` / `add <id>` / `del <id>`)
- **`!today`**: 오늘 커맨드 TOP5 + 루프 상태 + 에러 수 일일 요약
- **최적화**: `split_message` 줄 단위 분할, `import telegram_commands` 모듈 레벨 이동, `_open_db` 헬퍼 공통화

### 2026-04-06
- **프로젝트 독립**: `BBS/telegram-bot/` → `C:\DEV\BBS-SCV` 독립 repo로 분리
- **백업**: GitHub cloudland97/BBS-SCV 신규 생성, git 추적 시작

---

### 이전 이력 (BBS repo 내 공동 개발, ~2026-04-05)

#### 2026-04-05
- **보안**: SQL 컬럼 화이트리스트 (`_ALLOWED_COLS`) 추가

#### 2026-03-30
- **텔레그램 에러 알림**: 임시밴·에러 로그 → 텔레봇 전송 (5분 쿨다운, 억제 카운트)

#### 2026-03-29
- **`!help`** 3카테고리 재구성
- **커맨드명 정리**: `!rr` → `!re`, `!rr0` → `!re0`
- **최적화**: 텔레그램 유저명 캐시 (5분 TTL)

#### 2026-03-28
- **신규 커맨드**: `!ban`/`!unban`/`!banlist`, `!log`, `!dm`, `!st`, `!ud`
- **`!list`** SQLite 전환
