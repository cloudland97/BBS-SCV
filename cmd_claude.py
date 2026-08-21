"""Commands: !help."""

from telegram import Update
from telegram.ext import ContextTypes

import tg_utils as _utils
from tg_utils import admin_only


@admin_only
async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """!help — show command list."""
    text = (
        "📋 *관리봇 명령어*\n\n"
        "📡 *텔레그램 봇*\n"
        "`!re0` — 텔레그램 봇 재시작\n"
        "`!news 내용` — 봉봉뉴스 즉시 발송\n"
        "`!news 08:30 내용` — 예약 발송 (오늘/내일 자동)\n"
        "`!news 내일 08:30 내용` — 내일 예약\n"
        "`!news 4/25 08:30 내용` — 날짜 지정 예약\n"
        "`!news2 내용` — 미디어 캡션으로 봉봉뉴스 발송\n"
        "`!newslist` — 예약된 뉴스 목록\n"
        "`!newscc <ID>` — 예약 취소\n"
        "`!help` — 이 메시지\n\n"
        "🎮 *Discord 봇*\n"
        "`!re` — Discord 봇 재시작\n"
        "`!sync` — slash command 재동기화 + 봇 재시작\n"
        "`!hp` — 루프 헬스 체크\n"
        "`!today` — 일일 요약 (커맨드·루프·에러)\n"
        "`!log [N]` — 에러 로그 (`!log 1` = 어제)\n"
        "`!list` — 구독자 목록\n"
        "`!sub add|del <ID>` — 봉봉뉴스 구독 추가/삭제 (목록은 `!list`)\n"
        "`!se [유저ID] [Nd]` — 구독 이벤트 로그 (기본 30일)\n"
        "`!st [유저ID] [Nd]` — 커맨드 사용 통계\n"
        "`!ud` — 유저 DM 로그 (`!ud 유저ID` 상세)\n"
        "`!dm <ID> <메시지>` — Discord DM 발송\n"
        "`!ban <ID> [분]` — 임시밴 (기본 5분)\n"
        "`!unban <ID>` — 임시밴 해제\n"
        "`!banlist` — 활성 밴 목록\n"
    )
    await _utils.safe_reply(update.message, text)
