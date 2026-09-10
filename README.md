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
   Or use `start_bot.bat` for auto-restart on Windows.

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
| `!news content` | Broadcast text via BongBong News (or schedule it if a time is included) |
| `!news2 content` | Broadcast with media (attach image/video) |
| `!newslist` | Show pending scheduled news |
| `!newscc <ID>` | Cancel a scheduled news item |
| `!newscc <ID> force` | Force-release a row stuck in `sending` (e.g. after a crash mid-broadcast) back to `cancelled` |
| `!sub [list\|add\|del] [ID]` | Manage news subscribers |
| `!se [userID] [Nd]` | Subscription event log (default: last 30d, global) |
| `!dm userID message` | Send Discord DM |
| `!ud [userID]` | View user DM logs |
| `!sync` | Write sync flag and restart Discord bot to sync slash commands |

## Architecture

- **Single-user**: Only `CHAT_ID` can interact
- **Polling-based**: No webhooks or inline keyboards
- **Shared DB**: Reads `bbs.db` from BBS Discord bot (`BBS_DB_PATH`). Runs standalone without it — DB-backed features (news scheduling, stats, loop monitor) skip gracefully until the bot exists
- **Loop monitor**: Checks Discord bot health every 5min, alerts on downtime
- **Single instance**: claims `logs/bridge.pid` on startup; a second instance exits instead of racing the first (a stale/dead pid file never blocks startup)
- **Daily maintenance**: log retention (`_cleanup_old_logs`, 30 days) and a heartbeat log line run once a day in-process, not just at `post_init` — otherwise a bridge that stays up for weeks never prunes, and a healthy-but-quiet bridge is indistinguishable from a dead one in the logs
- **Token redaction**: a logging filter strips any Telegram bot token (`bot<id>:<secret>` pattern) from log records before they're written, regardless of which library emitted the line
- **`!log`** reads the BBS bot's own logs — `logs/error.log` for today, `logs/error.log.YYYY-MM-DD` for older days
- **`!re` restart** targets processes under the `BBS_RESTART_ROOT` env var (set to the BBS Discord bot path before the kill command runs), so it never touches sibling checkouts like `bbs-web` or `claxx`
- **Scheduled news status**: `pending` → `sending` → `sent` / `failed` (or `cancelled`). `failed` means the broadcast ran but delivered to nobody (0 successful sends)

## Files

```
telegram_bridge.py    Main bot: polling, handlers, media/news routing
tg_config.py          Env loading, paths, constants
tg_utils.py           Formatting helpers, admin_only, safe_reply
discord_helpers.py    Discord API helpers, username cache, DM, bongnews
cmd_*.py              !command handlers (discord, restart/stats, subscribers, news schedule, help)
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
