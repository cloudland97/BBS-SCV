"""Dummy Telegram credentials so tg_config imports without a real tg.env.

A fresh clone (and the CI runner) has no tg.env; without these, importing any
module that touches tg_config raised RuntimeError during collection and every
test errored out.
"""

import os

os.environ.setdefault("TG_BOT_TOKEN", "test-token")
os.environ.setdefault("TG_CHAT_ID", "1")
