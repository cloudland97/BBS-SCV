# BBS-SCV

BBS Discord bot remote management via Telegram.

Monitor loop health, view logs, manage subscribers, and send BongBong News broadcasts — all from your phone.

## Setup

1. **Python 3.11+** required
2. Create `tg.env` in the project root:
   ```
   BOT_TOKEN=your_telegram_bot_token
   CHAT_ID=your_telegram_user_id
   PROJECTS_DISCORD=C:\DEV\BBS
   ```
   Don't know your `CHAT_ID`? Start the bridge with a placeholder and send
   `!chatid` to the bot — it replies with your ids (works without auth).
3. Install dependencies:
   ```
   pip install -r requirements.txt
   ```
4. Run:
   ```
   python telegram_bridge.py
   ```
   Or use `start_bot.bat` on Windows: it restarts the bridge whenever it exits
   (10s backoff, doubling to 60s). Close its console window to stop it.

## Commands

| Command | Description |
|---------|-------------|
| `!help` | Show command list |
| `!re` | Restart Discord bot |
| `!re0` | Restart Telegram bridge |
| `!reboot ok` | Reboot the host PC after a 60s delay (`!reboot cc` aborts) |
| `!hp` | Loop health status |
| `!log [N]` | Error logs (N=1 for yesterday) |
| `!st [userID] [Nd]` | Command usage stats |
| `!list` | All subscribers by category |
| `!news content` | Broadcast text via BongBong News immediately (`\n` becomes a newline) |
| `!news2 content` | Broadcast with media (attach image/video) |
| `!sub [list\|add\|del] [ID]` | Manage news subscribers |
| `!se [userID] [Nd]` | Subscription event log (default: last 30d, global) |
| `!ud [userID]` | View user DM logs |

## Architecture

- **Single-user**: Only `CHAT_ID`, in a private chat with the bot, can interact
- **Polling-based**: No webhooks or inline keyboards
- **Shared DB**: Reads `bbs.db` from BBS Discord bot (`BBS_DB_PATH`). Runs standalone without it — DB-backed features (stats, loop monitor) skip gracefully until the bot exists
- **Loop monitor**: Checks Discord bot health every 5min. Alerts when a loop exceeds its threshold (`LOOP_ALERT_THRESHOLDS`, ~3-4x its interval) or when no loop has run for 15min (bot down), and again when it recovers. Alert cooldowns persist in `logs/loop_alert_state.json`
- **Single instance**: claims `logs/bridge.pid` on startup; a second instance exits instead of racing the first (a stale/dead pid file never blocks startup)
- **Daily maintenance**: log retention (`_cleanup_old_logs`, 30 days) and a heartbeat log line run once a day in-process, not just at `post_init` — otherwise a bridge that stays up for weeks never prunes, and a healthy-but-quiet bridge is indistinguishable from a dead one in the logs
- **Token redaction**: a logging filter strips any Telegram bot token (`bot<id>:<secret>` pattern) from log records before they're written, regardless of which library emitted the line
- **`!log`** reads the BBS bot's `logs/error.log` and every rotated `logs/error.log.*`, keeping the lines whose date prefix matches the requested day (BBS rotates only when the next error is written)
- **`!re` restart** targets processes under the `BBS_RESTART_ROOT` env var (set to the BBS Discord bot path before the kill command runs), so it never touches sibling checkouts like `bbs-web` or `claxx`
- **Scheduled news** was removed; the `scheduled_news` table stays in `bbs.db`, and leftover `pending` rows are only logged as a WARNING at boot, never sent

## Files

```
telegram_bridge.py    Main bot: polling, handlers, media/news routing
tg_config.py          Env loading, paths, constants
tg_utils.py           Formatting helpers, admin_only, safe_reply
discord_helpers.py    Discord API helpers, username cache, DM, bongnews
cmd_*.py              !command handlers (discord, restart/stats, subscribers, help)
tg.env                Credentials (git-ignored)
logs/                 Daily log files (git-ignored, auto-cleaned after 30 days)
tests/                Unit tests
```

## Tests

```
pip install pytest
pytest -q
```

No `tg.env` needed — `tests/conftest.py` injects dummy credentials.

## License

MIT — see [LICENSE](LICENSE).

No API credentials are included. Clone, then create your own `tg.env`.
Rate limits and terms of the Telegram and Discord APIs are the operator's
responsibility.
