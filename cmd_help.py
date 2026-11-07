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
        "`!reboot ok` — 이 PC 재부팅 (`!reboot cc` 취소)\n"
        "`!news 내용` — 봉봉뉴스 즉시 발송\n"
        "`!news2 내용` — 미디어 캡션으로 봉봉뉴스 발송\n"
        "`!help` — 이 메시지\n\n"
        "🎮 *Discord 봇*\n"
        "`!re` — Discord 봇 재시작\n"
        "`!hp` — 루프 헬스 체크\n"
        "`!log [N]` — 에러 로그 (`!log 1` = 어제)\n"
        "`!list` — 구독자 목록\n"
        "`!sub add|del <ID>` — 봉봉뉴스 구독 추가/삭제 (목록은 `!list`)\n"
        "`!se [유저ID] [Nd]` — 구독 이벤트 로그 (기본 30일)\n"
        "`!st [유저ID] [Nd]` — 커맨드 사용 통계\n"
        "`!ud` — 유저 DM 로그 (`!ud 유저ID` 상세)\n"
    )
    await _utils.safe_reply(update.message, text)
