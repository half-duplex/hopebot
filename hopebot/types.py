from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import re
from string import ascii_uppercase, digits
from typing import NewType, TYPE_CHECKING
from urllib.parse import urlparse


# from .bot import aaaaaaa

if TYPE_CHECKING:
    from typing import Any, Dict, List

TITLE_XOFY_REGEX = re.compile(
    r"(.*?),? \((?:(?:Day )?(?:\d+)(?:(?: of | ?/ ?)\d+)?|(?:Fri|Sat|Sun) ONGOING)\)"
)
ROOM_SHORTEN_REGEX = re.compile(
    r"^(?:The )?(.*?)( \(.*|/.*|Auditorium|Theat(?:er|re)|lage|race| ONGOING)*$"
)

LOGGER = logging.getLogger(__name__)

ConfRoom = NewType("ConfRoom", str)
ConfTrack = NewType("ConfTrack", str)
TalkShortcode = NewType("TalkShortcode", str)
TalkSlug = NewType("TalkSlug", str)
TalkType = NewType("TalkType", str)


@dataclass
class Talk:
    id: int
    guid: str
    type: TalkType
    title: str
    subtitle: str
    url: str
    slug: TalkSlug
    date: str
    """The date as iso8601. You probably want `start` instead."""
    duration: str
    """The duration as hh:mm. You probably want `duration_obj` instead."""
    room: ConfRoom
    track: ConfTrack
    abstract: str
    persons: List[Dict[str, Any]]
    logo: str | None = None
    merged: bool = False

    def __post_init__(self) -> None:
        self.start = datetime.fromisoformat(self.date)

        valid_chars = ascii_uppercase + digits
        parsed_url = urlparse(self.url)
        # /hope16/talk/YDTPAD/ -> YDTPAD
        shortcode = parsed_url.path.rsplit("/", 2)[1]
        if (
            len(shortcode) < 5
            or len(shortcode) > 6
            or not all(char in valid_chars for char in shortcode)
        ):
            LOGGER.error("Couldn't get valid shortcode from url %r", self.url)
            raise ValueError("Invalid shortcode")
        self.shortcode = TalkShortcode(shortcode)

        # Turn date+duration into useful objects
        duration_hours, duration_minutes = self.duration.rsplit(":", 1)
        if not duration_hours.isnumeric():
            raise Exception(
                "Unexpected talk duration format: {}".format(repr(duration_hours))
            )
        self.duration_obj = timedelta(
            hours=int(duration_hours), minutes=int(duration_minutes)
        )
        self.end = self.start + self.duration_obj

        # Shorten room name
        room_short = room = str(self.room)
        if room == "Other":
            room_short = ""
        room_short_match = ROOM_SHORTEN_REGEX.fullmatch(room)
        if room_short_match:
            room_short = room_short_match.group(1)
        self.room_short = ConfRoom(room_short.replace(" ", "").replace("-", ""))

        # Clean "Foo (1 of 3)" titles and build chatroom name
        # Fri-12-Marillac: The Five Pillars for Rewriting History and Culture
        # Fri-12-Marillac : The Five Pillars for Rewriting History and Culture
        # Fri-11-ScriptKittyVil: Make Your Very Own IoT Cat Lamp With WLED!
        # Fri-11-Script Kitty Vil: Make Your Very Own IoT Cat Lamp With WLED!.
        title = self.title
        short_title_match = TITLE_XOFY_REGEX.fullmatch(self.title)
        if short_title_match is not None:
            self.merged = True
            title = short_title_match.group(1)

        chat_name = ""
        if not self.merged:
            chat_name += self.start.strftime("%a-%H")
            if self.start.minute != 0:
                chat_name += self.start.strftime("%M")
        if self.room_short:
            if chat_name:
                chat_name += "-"
            chat_name += self.room_short
        if chat_name:
            chat_name += ": "
        chat_name += title

        self.chat_name: str = chat_name
