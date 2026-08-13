from __future__ import annotations

from typing import TYPE_CHECKING

from mautrix.util.config import BaseProxyConfig


if TYPE_CHECKING:
    from mautrix.util.config import ConfigUpdateHelper


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        options = [
            "token_regex",
            "pretalx_json_url",
            "schedule_talk_regex",
            "enable_token_clearing",
            "talk_chats_locked",
            "help",
            "rooms",
            "talk_chat_skip",
            "owners",
            "privileges",
            "ratelimit_multiplier",
            "timezone",
        ]
        for option in options:
            helper.copy(option)
