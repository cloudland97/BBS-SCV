# 디버깅 플레이북

### P1. 명령어가 다른 핸들러로 잘못 라우팅됨

**원인:** `_CMD_ROUTES`가 `startswith`로 매칭되므로 짧은 prefix가 먼저 등록되면 긴 prefix를 가로챔  
(과거 사례: `!dmlog` → `!dm`으로 잡히는 패턴 — 짧은 prefix보다 긴 prefix를 위에 등록)

| 단계 | 액션 | 목적 |
|------|------|------|
| 1 | `Grep "startswith" telegram_bridge.py` | 라우팅 방식이 `startswith`인지 확인 |
| 2 | `Grep "!<충돌prefix>" telegram_bridge.py -C2` | `_CMD_ROUTES` 내 등록 순서 확인 |
| 3 | 더 긴 prefix를 더 짧은 prefix 위 줄로 이동 | 긴 prefix가 먼저 매칭되도록 수정 |

**체크리스트:** `!re`/`!re0`, `!news`/`!news2`/`!newslist`/`!newscc` 등 같은 시작문자 쌍 전수 확인

---

### P2. 봇 출력 시간이 한국시간이 아님

**원인:** DB에 UTC ISO로 저장된 `created_at`/`last_at`을 변환 없이 문자열 슬라이싱(`[5:16]`)으로 출력, 또는 `datetime.now()`가 서버 로컬시간 반환

| 단계 | 액션 | 목적 |
|------|------|------|
| 1 | `Grep "\[5:16\]" cmd_discord.py` | 슬라이싱으로 시간 출력하는 위치 전수 확인 |
| 2 | `Grep "datetime\.now()" cmd_discord.py` | 타임존 없는 `now()` 호출 위치 확인 |
| 3 | `cmd_discord.py` 상단(import 아래)에 `_KST`와 `_to_kst()` 헬퍼 추가 | UTC ISO → KST `MM-DD HH:MM` 변환 함수 정의 |
| 4 | `r["created_at"][5:16]` → `_to_kst(r["created_at"])` 로 교체 (전수) | DB 시간을 KST로 변환 후 표시 |
| 5 | `datetime.now()` → `datetime.now(_KST)` 로 교체 (날짜 파일룩업) | 자정 전후 날짜 오차 방지 |

**헬퍼 템플릿:**
```python
_KST = timezone(timedelta(hours=9))

def _to_kst(iso: str) -> str:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=9)).strftime("%m-%d %H:%M")
```

---

### P3. 봇 시작 시 `telegram.error.TimedOut` / `httpx.ConnectTimeout`

**원인:** Windows ProactorEventLoop + anyio 비동기 connect가 Windows Defender 첫 연결 검역에서 타임아웃. `urllib`(blocking socket)은 통과하지만 `httpx`/`curl`(비동기 connect)은 차단됨.

| 단계 | 액션 | 목적 |
|------|------|------|
| 1 | `python -c "import urllib.request; print(urllib.request.urlopen('https://api.telegram.org', timeout=8).status)"` | urllib 성공 여부로 네트워크 문제 vs httpx 문제 즉시 구분 |
| 2 | urllib 성공 + httpx 실패 확인 시 → `telegram_bridge.py:ApplicationBuilder` 에 `HTTPXRequest` 주입 | Windows Defender 검역 타임아웃 흡수 + 재시도로 우회 |
| 3 | urllib도 실패 시 → VPN/네트워크 환경 확인 후 재진단 | 실제 네트워크 차단 여부 확인 |

**HTTPXRequest 주입 템플릿** (`telegram_bridge.py`의 `ApplicationBuilder` 호출 위치):
```python
import httpx
from telegram.request import HTTPXRequest

_req = HTTPXRequest(
    connect_timeout=20.0,
    read_timeout=20.0,
    write_timeout=20.0,
    httpx_kwargs={
        "transport": httpx.AsyncHTTPTransport(retries=2, local_address="0.0.0.0"),
    },
)
app = (
    ApplicationBuilder()
    .token(BOT_TOKEN)
    .request(_req)
    .get_updates_request(_req)
    .post_init(post_init)
    .build()
)
```

**`__main__` 블록 추가** (Selector 루프로 asyncio SSL 결함 완화):
```python
if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    app.run_polling()
```

**venv 환경** (`start_bot.bat`이 `%~dp0.venv`를 탐색):
```
py -3.11 -m venv C:\DEV\BBS-SCV\.venv
C:\DEV\BBS-SCV\.venv\Scripts\pip install "python-telegram-bot==22.7" "aiohttp~=3.9" tzdata
```
