from __future__ import annotations

from typing import TYPE_CHECKING

from mautrix.util.config import BaseProxyConfig


if TYPE_CHECKING:
    from mautrix.util.config import ConfigUpdateHelper


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        options = [
            "token_regex",
            "schedule_talk_regex",
            "enable_token_clearing",
            "rooms",
            "help",
            "owners",
            "talk_chat_skip",
            "talk_chat_moderators",
            "talk_chats_locked",
            "pretalx_json_url",
            "ratelimit_multiplier",
        ]
        for option in options:
            helper.copy(option)
