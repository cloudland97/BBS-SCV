"""Dummy Telegram credentials so tg_config imports without a real tg.env.

A fresh clone (and the CI runner) has no tg.env; without these, importing any
module that touches tg_config raised RuntimeError during collection and every
test errored out.

TG_LOG_DIR must be set before tg_config is imported: its file handler opens
LOG_DIR/bridge.log at import time, which would otherwise be the live bridge log.
"""

import os
import tempfile

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "1")
os.environ.setdefault("TG_LOG_DIR", os.path.join(tempfile.gettempdir(), "bbs_scv_test_logs"))
