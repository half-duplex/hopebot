from __future__ import annotations

from asyncio import (
    CancelledError as AsyncioCancelledError,
    create_task,
    Lock,
    sleep,
)
from dataclasses import fields
from datetime import datetime, timedelta, UTC
from hashlib import sha256
import re
from typing import TYPE_CHECKING

from asyncpg import Record
from asyncpg.exceptions import UniqueViolationError
from dateutil import tz
from maubot.handlers import command, event
from maubot.matrix import MaubotMessageEvent
from maubot.plugin_base import Plugin
from mautrix.api import Method, Path
from mautrix.errors.request import MForbidden, MNotFound, MRoomInUse
from mautrix.types import (
    CanonicalAliasStateEventContent,
    EventType,
    JoinRule,
    JoinRulesStateEventContent,
    Membership,
    Obj,
    PowerLevelStateEventContent,
    RoomAlias,
    RoomAvatarStateEventContent,
    RoomCreatePreset,
    RoomID,
    SpaceChildStateEventContent,
    SpaceParentStateEventContent,
    StateEvent,
    StrippedStateEvent,
)
from mautrix.types.event.state import (
    JoinRestriction,
    JoinRestrictionType,
    NotificationPowerLevels,
)

from .config import Config
from .db import upgrade_table
from .image_gen import draw_flow_field
from .types import Talk, TalkShortcode, TalkSpaceRoomCache
from .util import room_mention


if TYPE_CHECKING:
    from typing import Type

    from mautrix.types import UserID
    from mautrix.util.async_db import UpgradeTable
    from mautrix.util.config import BaseProxyConfig


class HopeBot(Plugin):
    direct_rooms: dict[str, list[RoomID]] = {}
    direct_update_lock = Lock()
    talk_cache: list[Talk] = []
    talk_cache_lock = Lock()
    talk_cache_time = datetime(1970, 1, 1, tzinfo=UTC)
    talk_space_room_cache: dict[RoomID, TalkSpaceRoomCache] = {}

    async def start(self) -> None:
        if not self.config:
            raise Exception("Config not initialized")
        self.config.load_and_update()
        self.token_regex = re.compile(
            self.config["token_regex"],
            re.IGNORECASE,
        )
        self.schedule_talk_regex = re.compile(
            self.config["schedule_talk_regex"],
            re.IGNORECASE,
        )
        self.tz = tz.gettz(self.config["timezone"])
        self.talk_timer_task = create_task(self.talk_timer_loop())

    async def stop(self) -> None:
        self.talk_timer_task.cancel()

    async def talk_timer_loop(self) -> None:
        self.log.debug("Starting talk timer loop")
        try:
            while True:
                now = datetime.now(UTC)
                next_run = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
                await sleep((next_run - now).total_seconds())
                await self.schedule_talk_pins()
        except AsyncioCancelledError:
            self.log.debug("Stopping talk timer loop")
        except Exception:
            self.log.exception("Talk timer loop exception")

    async def space_set_child(
        self,
        space: RoomID,
        child: RoomID,
        order: str | None = None,
        suggested=False,
        present=True,
    ):
        content = SpaceChildStateEventContent()
        if present:
            content.via = [child.split(":")[1]]
            content.suggested = suggested
            content.order = order
        await self.client.send_state_event(space, EventType.SPACE_CHILD, content, child)

    async def schedule_talk_pins(self) -> None:
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        self.log.debug("Updating talk space pins")

        now = datetime.now(UTC)
        # now += timedelta(days=6, hours=15, minutes=0)
        soon = now + timedelta(minutes=10)
        earlier = now - timedelta(minutes=11)

        talks = {
            RoomID(v["room_id"]): v
            for v in await self.database.fetch(
                """SELECT room_id, start_ts, end_ts, talk_title, location FROM talks"""
            )
        }
        talks_nowish = {
            k: v
            for k, v in talks.items()
            if v["start_ts"] < soon and v["end_ts"] > earlier
        }
        self.log.info(
            "There's %s nowish (%r) talks: %r",
            len(talks_nowish),
            now.isoformat(),
            talks_nowish.values(),
        )

        invalidate_cache = False
        talks_to_announce: list[Record] = []
        talks_space = await self.get_space_hierarchy(self.config["rooms"]["talks"])
        livetalks_space = None
        if "livetalks" in self.config["rooms"]:
            livetalks_space = await self.get_space_hierarchy(
                self.config["rooms"]["livetalks"]
            )
        for room_id, alltalks_child_state in talks_space.child_states.items():
            alltalks_child_content = alltalks_child_state.content

            talk = talks[room_id]
            started = talk["start_ts"] < now
            recent_start = started and talk["start_ts"] > now - timedelta(hours=1)
            ended = talk["end_ts"] < now
            live = started and not ended
            nowish = room_id in talks_nowish

            order_set = (
                "t4"
                if ended
                else "t3" if not started else "t2" if recent_start else "t1"
            )
            expect_order = "{}_{:012d}".format(
                order_set, int(talk["start_ts"].timestamp())
            )
            if (
                alltalks_child_content.order != expect_order
                or alltalks_child_content.suggested != nowish
            ):
                self.log.debug("Updating talk space child state for %r", room_id)
                invalidate_cache = True
                await self.space_set_child(
                    self.config["rooms"]["talks"],
                    room_id,
                    order=expect_order,
                    suggested=nowish,
                )

            if livetalks_space:
                in_livetalks = room_id in livetalks_space.child_states
                if in_livetalks or nowish:
                    if (
                        not in_livetalks
                        or not nowish
                        or expect_order
                        != livetalks_space.child_states[room_id].content.order
                    ):
                        self.log.debug(
                            "Updating live talk space child state for %r", room_id
                        )
                        invalidate_cache = True
                        await self.space_set_child(
                            self.config["rooms"]["livetalks"],
                            room_id,
                            order=expect_order,
                            present=nowish,
                        )

            # If new alltalks pin: notify
            if nowish and not alltalks_child_content.suggested:
                # Don't post in announcements if it's already live
                if live or ended:
                    self.log.debug(
                        "Not announcing %r: start %r is before now %r",
                        talk["talk_title"],
                        talk["start_ts"].astimezone(self.tz),
                        now.astimezone(self.tz),
                    )
                    continue

                state_text = "live" if live else "starting soon"
                message = "{} is {}! {}, from {} to {}. @room".format(
                    talk["talk_title"],
                    state_text,
                    talk["location"],
                    talk["start_ts"].astimezone(self.tz).strftime("%H:%M"),
                    talk["end_ts"].astimezone(self.tz).strftime("%H:%M"),
                )
                await self.client.send_text(room_id, html=message)

                talks_to_announce.append(talk)

        if invalidate_cache:
            self.talk_space_room_cache.pop(self.config["rooms"]["talks"])
            if livetalks_space:
                self.talk_space_room_cache.pop(self.config["rooms"]["livetalks"])

        if len(talks_to_announce) > 0:
            announcements_room_message = (
                "Talks and workshops starting soon:\n"
                + "\n".join(
                    [
                        "<b>{}</b> in {} from {} to {}".format(
                            room_mention(
                                talk["room_id"], text=talk["talk_title"], html=True
                            ),
                            talk["location"],
                            talk["start_ts"].astimezone(self.tz).strftime("%H:%M"),
                            talk["end_ts"].astimezone(self.tz).strftime("%H:%M"),
                        )
                        for talk in sorted(
                            talks_to_announce, key=lambda t: t["start_ts"]
                        )
                    ]
                )
            )
            await self.client.send_text(
                self.config["rooms"]["announcements"],
                html=announcements_room_message.replace("\n", "\n<br>"),
            )

    async def get_talks(self) -> list[Talk]:
        if not self.config:
            raise Exception("Config not initialized")
        now = datetime.now(UTC)
        if now - self.talk_cache_time >= timedelta(minutes=15):
            async with self.talk_cache_lock:
                talk_class_fields = [f.name for f in fields(Talk)]
                r = await self.http.get(self.config["pretalx_json_url"])
                data = await r.json()
                conf = data["schedule"]["conference"]
                self.talk_cache = [
                    Talk(**{k: v for k, v in talk.items() if k in talk_class_fields})
                    for day in conf["days"]
                    for room in day["rooms"].values()
                    for talk in room
                ]
                self.talk_cache_time = now
        return self.talk_cache

    async def get_space_hierarchy(self, space: RoomID) -> TalkSpaceRoomCache:
        now = datetime.now(UTC)
        if space not in self.talk_space_room_cache:
            self.talk_space_room_cache[space] = TalkSpaceRoomCache()
        async with self.talk_space_room_cache[space].lock:
            if now - self.talk_space_room_cache[space].time >= timedelta(minutes=15):
                self.talk_space_room_cache[space] = TalkSpaceRoomCache()
                path = Path.v1.rooms[space].hierarchy
                response = await self.client.api.request(
                    Method("GET"),
                    path,
                    query_params={"limit": "50", "max_depth": "0"},
                )
                resp_rooms = response["rooms"]
                resp_space = [room for room in resp_rooms if room["room_id"] == space][
                    0
                ]
                self.talk_space_room_cache[space].space = resp_space
                child_states = [
                    StrippedStateEvent.deserialize(c)
                    for c in resp_space["children_state"]
                ]
                self.talk_space_room_cache[space].child_states = {
                    RoomID(c.state_key): c for c in child_states
                }
                self.talk_space_room_cache[space].children = {
                    RoomID(r["room_id"]): r for r in resp_rooms if r["room_id"] != space
                }
                self.talk_space_room_cache[space].time = now
        return self.talk_space_room_cache[space]

    @command.new()
    async def help(self, evt: MaubotMessageEvent):
        if not self.config:
            raise Exception("Config not initialized")
        if evt.sender in self.config["owners"]:
            await evt.reply(
                self.config["help"]
                + "\n\nFor a list of admin commands, see "
                + "https://github.com/half-duplex/hopebot/blob/main/README.md#usage"
            )
        else:
            await evt.reply(self.config["help"])

    @command.new()
    async def version(self, evt: MaubotMessageEvent):
        await evt.reply(
            "I am HopeBot by @mal:hope.net! "
            "[Source](https://github.com/half-duplex/hopebot)"
        )

    @command.new(name="mod", must_consume_args=False)
    async def call_mod(self, evt: MaubotMessageEvent):
        if not self.config:
            raise Exception("Config not initialized")
        quoted = None
        if evt.content.relates_to.in_reply_to:
            quoted = await evt.client.get_event(
                evt.room_id, evt.content.relates_to.in_reply_to.event_id
            )
        help_evt = await evt.reply("Help is on the way 🚨")
        reply_text = ""
        if quoted:
            reply_text = "<br>In reply to: {}: {}".format(
                quoted.sender, quoted.content.body
            )
        message = (
            "@room, Moderators have been summoned to {} {}{}<br>{} wrote: {}".format(
                room_mention(evt.room_id, text="room", html=True),
                room_mention(evt.room_id, help_evt, text="(context)", html=True),
                reply_text,
                evt.sender,
                evt.content.body,
            )
        )
        await evt.client.send_text(self.config["rooms"]["moderation"], html=message)

    @command.new()
    async def stats(self, evt: MaubotMessageEvent):
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        if evt.sender not in self.config["owners"]:
            return
        async with self.database.acquire() as conn:
            total = {
                row["type"]: row["rowcount"]
                for row in await conn.fetch(
                    "SELECT type, COUNT(*) AS rowcount FROM tokens GROUP BY type",
                )
            }
            used = {
                row["type"]: row["rowcount"]
                for row in await conn.fetch(
                    "SELECT type, COUNT(*) AS rowcount FROM tokens "
                    "WHERE used_by IS NOT NULL GROUP BY type"
                )
            }
        response = ""
        for token_type in total:
            response += "Used {}: {}/{} ({:.2f}%)  \n".format(
                token_type,
                used.get(token_type, 0),
                total[token_type],
                (
                    (used.get(token_type, 0) / total[token_type] * 100)
                    if total[token_type] > 0
                    else 100
                ),
            )
        sum_used = sum(used.values())
        sum_total = sum(total.values())
        response += "Used total: {}/{} ({:.2f}%)  \n".format(
            sum_used, sum_total, (sum_used / sum_total * 100) if sum_total > 0 else 100
        )

        await evt.reply(response)

    @command.new(name="speaker")
    @command.argument("user")
    @command.argument("talk", required=False)
    async def add_speaker(
        self, evt: MaubotMessageEvent, user: UserID, talk: str | None = None
    ):
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        if evt.sender not in self.config["owners"]:
            self.log.warning(
                "Attempt by non-owner %r to set speaker: %r %r", evt.sender, user, talk
            )
            return
        talk_id = None
        async with self.database.acquire() as conn:
            if not talk:  # Not specified - use current channel
                room_id = evt.room_id
                talk_id = await conn.fetchval(
                    "SELECT talk_id FROM talks WHERE room_id=$1", evt.room_id
                )
            else:
                if talk.startswith("http"):  # Got a link
                    shortcode_match = self.schedule_talk_regex.match(talk)
                    if not shortcode_match:
                        await evt.reply(
                            "Sorry, I couldn't understand that. Try a link or shortcode."
                        )
                        return
                    talk = shortcode_match.group(1)
                # Else assume it's the shortcode
                r = await conn.fetchrow(
                    "SELECT talk_id, room_id FROM talks WHERE talk_shortcode=$1", talk
                )
                if not r:
                    await evt.reply("I couldn't find the talk/room.")
                    return
                talk_id, room_id = r

            try:
                await conn.execute(
                    "INSERT INTO speakers (talk_id, user_id) VALUES ($1, $2)",
                    talk_id,
                    user,
                )
            except UniqueViolationError:
                pass
            self.log.info("%r added %r as a speaker for %r", evt.sender, user, talk_id)
            await evt.react("✔️")

            power_level_evt = await evt.client.get_state_event(
                room_id=room_id,
                event_type=EventType.ROOM_POWER_LEVELS,
            )
            if power_level_evt.users.get(user, 0) != 50:
                power_level_evt.users[user] = 50
                await evt.client.send_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_POWER_LEVELS,
                    content=power_level_evt,
                )

            try:  # to invite them to the presenters space
                await evt.client.invite_user(self.config["rooms"]["presenter"], user)
            except (KeyError, MForbidden):
                pass
            else:
                await evt.react("➕")

    @command.new(name="order")
    @command.argument("space")
    @command.argument("room")
    @command.argument("order", required=False)
    async def set_space_order(
        self, evt: MaubotMessageEvent, space: RoomID, room: RoomID, order: str | None
    ):
        if not self.config:
            raise Exception("config not initialized")
        if evt.sender not in self.config["owners"]:
            await evt.react("💩")
            return

        if space[0] not in "!#":
            if space not in self.config["rooms"]:
                await evt.reply("Unknown space")
                return
            space = RoomID(self.config["rooms"][space])

        if room[0] not in "!#":
            if room == "here":
                room = evt.room_id
            elif room in self.config["rooms"]:
                room = RoomID(self.config["rooms"][room])
            else:
                await evt.reply("Unknown room")
                return

        try:
            space_child_evt = await evt.client.get_state_event(
                room_id=space,
                state_key=room,
                event_type=EventType.SPACE_CHILD,
            )
        except MForbidden:
            await evt.reply("Something went wrong. Are your space/room IDs valid?")
            self.log.exception("Exception in set_space_order")
            return
        self.log.info(
            "set_space_order: %r set %r -> %r order to %r",
            evt.sender,
            space,
            room,
            order,
        )
        space_child_evt.order = order
        await evt.client.send_state_event(
            space,
            EventType.SPACE_CHILD,
            space_child_evt,
            room,
        )
        await evt.react("✔️")

    @command.new(name="op")
    @command.argument("room", required=False)
    @command.argument("user", required=False)
    async def add_admin(
        self, evt: MaubotMessageEvent, room: RoomID | None, user: UserID | None
    ):
        if not self.config:
            raise Exception("config not initialized")
        if evt.sender not in self.config["owners"]:
            self.log.warning(
                "Attempt by non-owner %r to get admin in %r",
                evt.sender,
                evt.room_id,
            )
            await evt.react("💩")
            return
        target_room = room if room else evt.room_id
        target_user = user if user else evt.sender

        try:
            power_level_evt = await evt.client.get_state_event(
                room_id=target_room,
                event_type=EventType.ROOM_POWER_LEVELS,
            )
        except MForbidden:
            await evt.reply(
                "That room ID is invalid, I'm not in it, or I don't have admin there."
            )
            return
        if power_level_evt.users.get(target_user, 0) == 100:
            await evt.reply("That user is already admin")
            return
        self.log.warning(
            "Op: %r is granting admin permissions to %r in %r",
            evt.sender,
            target_user,
            target_room,
        )
        power_level_evt.users[target_user] = 100
        await evt.client.send_state_event(
            room_id=target_room,
            event_type=EventType.ROOM_POWER_LEVELS,
            content=power_level_evt,
        )
        await evt.react("✔️")

    @command.new(name="sync_talks")
    @command.argument("target_talk", required=False)
    async def sync_talks(self, evt: MaubotMessageEvent, target_talk: str | None):
        if not self.config or not self.database:
            raise Exception("config or database not initialized")
        if evt.sender not in self.config["owners"]:
            self.log.warning(
                "Attempt by non-owner %r to sync talks %r",
                evt.sender,
                target_talk if target_talk else "(all)",
            )
            return
        if target_talk and target_talk.startswith("http"):  # Link to shortcode
            shortcode_match = self.schedule_talk_regex.match(target_talk)
            if not shortcode_match:
                await evt.reply(
                    "Sorry, I couldn't understand that. Try a link or shortcode."
                )
                return
            target_talk = shortcode_match.group(1)
        target_shortcode = TalkShortcode(target_talk) if target_talk else None
        if target_talk and target_talk in self.config["talk_chat_skip"]:
            await evt.reply("I'm configured to skip that talk.")
            return

        status_msg = None
        if not target_talk:  # All
            status_msg_evt_id = await evt.reply("This will take a long time...")
            status_msg = MaubotMessageEvent(
                await self.client.get_event(evt.room_id, status_msg_evt_id), self.client
            )

        chat_talks: dict[str, list[Talk]] = {}
        for talk in await self.get_talks():
            # Configured to skip, or not the one we want?
            if talk.shortcode in self.config["talk_chat_skip"]:
                continue
            if target_shortcode and talk.shortcode != target_shortcode:
                continue

            await self.sync_talk(evt.client, talk.shortcode)

            chat_talks[talk.chat_name] = chat_talks.get(talk.chat_name, []) + [talk]

        async with self.database.acquire() as conn:
            for talk_set_idx, talk_set_unsorted in enumerate(chat_talks.values()):
                # Sort for determinism (earliest is the one that gets the chat room, etc)
                talk_set = sorted(talk_set_unsorted, key=lambda x: x.start)
                chat_name = talk_set[0].chat_name

                self.log.info(
                    "Syncing talk set %d of %d: %r (*%d)",
                    talk_set_idx,
                    len(chat_talks),
                    chat_name,
                    len(talk_set),
                )

                if not target_talk and status_msg:
                    await status_msg.edit(
                        "This will take a long time. Synced {} of {}...".format(
                            talk_set_idx, len(chat_talks)
                        )
                    )

                # API backoff
                delay = 1

                topic = talk_set[0].abstract + "<br>"
                topic_plain = talk_set[0].abstract + "\n"
                for talk in talk_set:
                    date = talk.start.astimezone(self.tz).strftime("%a %H:%M")
                    topic += "<br><a href='{2}'>{0} in {1}</a>".format(
                        date, talk.room, talk.url
                    )
                    topic_plain += "\n- {} in {}, {}  ".format(
                        date, talk.room, talk.url
                    )

                join_rules_content = JoinRulesStateEventContent(
                    join_rule=JoinRule.RESTRICTED,
                    allow=[
                        JoinRestriction(
                            type=JoinRestrictionType.ROOM_MEMBERSHIP,
                            room_id=self.config["rooms"]["attendee"],
                        ),
                        JoinRestriction(
                            type=JoinRestrictionType.ROOM_MEMBERSHIP,
                            room_id=self.config["rooms"]["talks"],
                        ),
                    ],
                )

                users = (
                    {uid: 50 for uid in self.config["talk_chat_moderators"]}
                    | {uid: 100 for uid in self.config["owners"]}
                    | {evt.client.mxid: 100}  # bot is admin
                )
                speakers = await self.database.fetch(
                    "SELECT user_id FROM speakers WHERE talk_id=$1", talk_set[0].id
                )
                for row in speakers:
                    users[row["user_id"]] = 50

                room_locked_power = 50 if self.config["talk_chats_locked"] else 0
                power_level_content = PowerLevelStateEventContent(
                    ban=50,
                    events={
                        EventType.REACTION: room_locked_power,
                        EventType.ROOM_AVATAR: 100,
                        EventType.ROOM_CANONICAL_ALIAS: 100,
                        EventType.ROOM_ENCRYPTION: 100,
                        EventType.ROOM_HISTORY_VISIBILITY: 100,
                        EventType.ROOM_JOIN_RULES: 100,
                        EventType.ROOM_MESSAGE: room_locked_power,
                        EventType.ROOM_NAME: 100,
                        EventType.ROOM_POWER_LEVELS: 100,
                        EventType.ROOM_REDACTION: room_locked_power,
                        EventType("m.room.server_acl", EventType.Class.UNKNOWN): 100,
                        EventType.ROOM_TOMBSTONE: 100,
                        EventType.ROOM_TOPIC: 100,
                    },
                    events_default=room_locked_power,
                    invite=100,
                    kick=50,
                    notifications=NotificationPowerLevels(room=50),
                    redact=50,
                    state_default=50,
                    users=users,
                )

                row = None
                room_id: RoomID | None = None
                for talk in reversed(
                    talk_set
                ):  # [0] last, to use those IDs for the set
                    row = await conn.fetchrow(
                        """SELECT room_id, start_ts, end_ts, talk_title, location
                        FROM talks WHERE talk_id = $1""",
                        talk.id,
                    )
                    if row is None:
                        continue
                    room_id, start, end, title, location = row

                    # Check times
                    if (
                        start != talk.start
                        or end != talk.end
                        or title != talk.title
                        or location != talk.room
                    ):
                        self.log.debug(
                            "Updating start/end/title/location for $r", talk.id
                        )
                        await conn.execute(
                            """UPDATE talks
                            SET (start_ts, end_ts, talk_title, location) = ($1, $2, $3, $4)
                            WHERE talk_id = $5""",
                            talk.start,
                            talk.end,
                            talk.title,
                            talk.room,
                            talk.id,
                        )

                if row is None:  # create chat for talk
                    delay += 3
                    avatar_url = await self.create_avatar(evt.client, talk_set[0].id)
                    room_id = await evt.client.create_room(
                        name=chat_name,
                        preset=RoomCreatePreset.TRUSTED_PRIVATE,
                        invitees=[],
                        initial_state=[
                            StrippedStateEvent(
                                type=EventType.SPACE_PARENT,
                                state_key=self.config["rooms"]["talks"],
                                content=SpaceParentStateEventContent(
                                    canonical=True,
                                    via=[self.config["rooms"]["talks"].split(":")[1]],
                                ),
                            ),
                            StrippedStateEvent(
                                type=EventType.ROOM_JOIN_RULES,
                                content=join_rules_content,
                            ),
                            StrippedStateEvent(
                                type=EventType.ROOM_TOPIC,
                                # RoomTopicStateEventContent can't do html too
                                # **{} so I can pass m.topic
                                # https://spec.matrix.org/latest/client-server-api/#mroomtopic
                                content=Obj(
                                    **{
                                        "topic": topic_plain,
                                        "m.topic": {
                                            "m.text": [
                                                {
                                                    "mimetype": "text/html",
                                                    "body": topic,
                                                },
                                                {
                                                    "mimetype": "text/plain",
                                                    "body": topic_plain,
                                                },
                                            ],
                                        },
                                    }
                                ),
                            ),
                            StrippedStateEvent(
                                type=EventType.ROOM_AVATAR,
                                content=RoomAvatarStateEventContent(url=avatar_url),
                            ),
                            StrippedStateEvent(
                                type=EventType.ROOM_POWER_LEVELS,
                                content=power_level_content,
                            ),
                        ],
                    )
                    if not room_id:
                        self.log.error("Error creating room for %r", chat_name)
                        continue
                    await evt.client.send_state_event(
                        self.config["rooms"]["talks"],
                        EventType.SPACE_CHILD,
                        content=SpaceChildStateEventContent(
                            via=[room_id.split(":")[1]]
                        ),
                        state_key=room_id,
                    )

                    self.log.info("Created room %r for %r", room_id, chat_name)
                    await conn.executemany(
                        """INSERT INTO talks
                            (talk_id, talk_shortcode, room_id, start_ts,
                                end_ts, talk_title, location)
                            VALUES ($1, $2, $3, $4, $5, $6, $7)""",
                        (
                            (
                                talk.id,
                                talk.shortcode,
                                room_id,
                                talk.start,
                                talk.end,
                                talk.title,
                                talk.room,
                            )
                            for talk in talk_set
                        ),
                    )

                if not room_id:
                    raise Exception(
                        "Impossible! I made it past room find/create without a room_id"
                    )

                # Match room name
                current_name_evt = await evt.client.get_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_NAME,
                )
                if current_name_evt.name != chat_name:
                    delay += 1
                    self.log.debug(
                        "Updating room name for %r from %r to %r",
                        room_id,
                        current_name_evt.name,
                        chat_name,
                    )
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_NAME,
                        content={
                            "name": chat_name,
                        },
                    )

                # Match topic
                current_topic_evt = await evt.client.get_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_TOPIC,
                )
                if current_topic_evt.topic != topic_plain:
                    delay += 1
                    self.log.debug(
                        "Updating room topic for %r (%r)", room_id, chat_name
                    )
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_TOPIC,
                        content=Obj(
                            **{
                                "topic": topic_plain,
                                "m.topic": {
                                    "m.text": [
                                        {
                                            "mimetype": "text/html",
                                            "body": topic,
                                        },
                                        {
                                            "mimetype": "text/plain",
                                            "body": topic_plain,
                                        },
                                    ],
                                },
                            }
                        ),
                    )

                # Set avatar
                try:
                    current_avatar_evt = await evt.client.get_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_AVATAR,
                    )
                    current_avatar = current_avatar_evt.url
                except MNotFound:
                    current_avatar = None
                if not current_avatar:
                    delay += 2
                    self.log.debug(
                        "Setting avatar for for %r (%r)",
                        room_id,
                        chat_name,
                    )
                    avatar_url = await self.create_avatar(evt.client, talk_set[0].id)
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_AVATAR,
                        content={
                            "url": avatar_url,
                        },
                    )

                # Match permissions
                power_level_evt = await evt.client.get_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_POWER_LEVELS,
                )
                if power_level_evt != power_level_content:
                    delay += 1
                    self.log.debug(
                        "Updating permissions for %r (%r), was: %r",
                        room_id,
                        chat_name,
                        power_level_evt,
                    )
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_POWER_LEVELS,
                        content=power_level_content,
                    )

                # Match aliases
                aliases = [
                    RoomAlias("#{}:hope.net".format(talk.shortcode))
                    for talk in talk_set
                ]
                try:
                    canonical_alias_evt = await evt.client.get_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_CANONICAL_ALIAS,
                    )
                    current_aliases = canonical_alias_evt.alt_aliases
                    if canonical_alias_evt.canonical_alias:
                        current_aliases = [
                            canonical_alias_evt.canonical_alias
                        ] + aliases
                except MNotFound:
                    current_aliases = []
                # Except when I break stuff, we probably don't want to clear
                # manually-created aliases
                bad_aliases: set[RoomAlias] = (
                    set()
                )  # set(current_aliases) - set(aliases)
                for alias in bad_aliases:
                    delay += 1
                    self.log.debug(
                        "Removing alias for %r (%r): %r", room_id, chat_name, alias
                    )
                    room_shortcode = alias.split("#")[1].split(":")[0]
                    await evt.client.remove_room_alias(room_shortcode)
                missing_aliases = set(aliases) - set(current_aliases)
                for alias in missing_aliases:
                    delay += 1
                    self.log.debug(
                        "Adding alias for %r (%r): %r",
                        room_id,
                        chat_name,
                        alias,
                    )
                    room_shortcode = alias.split("#")[1].split(":")[0]
                    try:
                        await evt.client.add_room_alias(room_id, room_shortcode)
                    except MRoomInUse:
                        alias_evt = await evt.client.resolve_room_alias(alias)
                        if alias_evt.room_id != room_id:
                            await evt.client.remove_room_alias(room_shortcode)
                            await evt.client.add_room_alias(room_id, room_shortcode)
                if bad_aliases or missing_aliases:
                    delay += 1
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_CANONICAL_ALIAS,
                        content=CanonicalAliasStateEventContent(
                            canonical_alias=aliases[0], alt_aliases=aliases[1:]
                        ),
                    )

                # Match join rules
                current_join_rules_evt = await evt.client.get_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_JOIN_RULES,
                )
                if current_join_rules_evt != join_rules_content:
                    delay += 1
                    self.log.debug(
                        "Updating join rules for %r (%r), was: %r",
                        room_id,
                        chat_name,
                        current_join_rules_evt,
                    )
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_JOIN_RULES,
                        content=join_rules_content,
                    )

                # TODO: match more room info - space membership? perms?

                await sleep(delay * self.config.get("ratelimit_multiplier", 2))

        await evt.client.set_typing(evt.room_id, 0)
        await evt.reply("Done!")

    async def create_avatar(self, client, seed):
        self.log.info("Generating avatar for seed %d", seed)
        avatar_data = await draw_flow_field(500, 500, num=2, seed=seed)
        return await client.upload_media(
            avatar_data,
            mime_type="image/jpeg",
        )

    @command.new(name="load_tokens")
    @command.argument("filename")
    @command.argument("token_type")
    @command.argument("clear", required=False)
    async def load_tokens(
        self, evt: MaubotMessageEvent, filename: str, token_type: str, clear: bool
    ):
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        if evt.sender not in self.config["owners"]:
            self.log.warning("Attempt by non-owner %r to load tokens", evt.sender)
            return
        if token_type not in self.config["rooms"]:
            await evt.reply("Unknown type {}".format(repr(token_type)))
            return
        await evt.reply("Loading tokens from {}...".format(filename))
        self.log.warning(
            "Token load for %ss from %r started by %s, clear=%s",
            token_type,
            filename,
            evt.sender,
            clear,
        )
        async with self.database.acquire() as conn:
            await conn.execute("BEGIN TRANSACTION")
            if clear == "truncate":
                if not self.config["enable_token_clearing"]:
                    await evt.reply("Token clearing is disabled, aborting.")
                    return
                self.log.warning("Truncating table")
                await conn.execute("TRUNCATE TABLE tokens")
            elif clear == "clear":
                if not self.config["enable_token_clearing"]:
                    await evt.reply("Token clearing is disabled, aborting.")
                    return
                self.log.warning("Removing all %s tokens", token_type)
                await conn.execute("DELETE FROM tokens WHERE type = $1", token_type)
            loaded = 0
            with open(filename, "r", encoding="utf-8-sig") as f:
                for line in f:
                    try:
                        await conn.execute(
                            (
                                "INSERT INTO tokens "
                                "(token_hash, type, loaded_at) "
                                "VALUES ($1, $2, NOW())"
                            ),
                            bytes.fromhex(line),
                            token_type,
                        )
                    except UniqueViolationError as e:
                        await evt.reply("Failed: Duplicate token hashes")
                        raise e
                    loaded += 1
            await conn.execute("COMMIT")
        self.log.info("Token load finished, loaded %d", loaded)
        await evt.reply("Done, loaded {} {} tokens".format(loaded, token_type))

    @command.new(name="mark_unused")
    @command.argument("token")
    async def mark_unused(self, evt: MaubotMessageEvent, token: str):
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        if evt.sender not in self.config["owners"]:
            self.log.warning(
                "Attempt by non-owner %r to mark token unused: %r", evt.sender, token
            )
            return

        token_match = self.token_regex.fullmatch(token)
        if token_match:
            token_hash = sha256(token.encode()).digest()
        else:
            try:
                token_hash = bytes.fromhex(token)
            except ValueError:
                await evt.reply("Sorry, that doesn't look like a valid token or hash.")
                return

        async with self.database.acquire() as conn:
            await conn.execute("BEGIN TRANSACTION")
            d = await conn.fetchrow(
                "SELECT * FROM tokens WHERE token_hash = $1 FOR UPDATE",
                token_hash,
            )

            if not d:
                await evt.reply("Sorry, that's not a valid token/hash.")
                return
            if d["used_at"] is None:
                await evt.reply("That token hasn't been used")
                return

            self.log.warning(
                "%r marked token unused (previously used by %r at %s): %r",
                evt.sender,
                d["used_by"],
                d["used_at"],
                token,
            )

            await conn.execute(
                (
                    "UPDATE tokens SET used_at = NULL, used_by = NULL "
                    "WHERE token_hash = $1"
                ),
                token_hash,
            )
            await conn.execute("COMMIT")
        await evt.reply("Done")

    @command.new(name="token_info")
    @command.argument("token")
    async def token_info(self, evt: MaubotMessageEvent, token: str):
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        if evt.sender not in self.config["owners"]:
            self.log.warning(
                "Attempt by non-owner %r to get info for token %r", evt.sender, token
            )
            return

        token_match = self.token_regex.fullmatch(token)
        if token_match:
            token_hash = sha256(token.encode()).digest()
        else:
            token_hash = bytes.fromhex(token)

        d = await self.database.fetchrow(
            "SELECT * FROM tokens WHERE token_hash = $1 FOR UPDATE",
            token_hash,
        )

        if not d:
            await evt.reply("Sorry, that's not a valid token/hash.")
            return
        if d["used_at"] is None:
            await evt.reply("That token hasn't been used")
            return

        await evt.reply(
            "That token was used by {} at {}".format(d["used_by"], d["used_at"])
        )

    @event.on(EventType.ROOM_MEMBER)
    async def new_room(self, evt: StateEvent):
        if (
            evt.content.membership != Membership.INVITE
            or not evt.content.is_direct
            # We get two INVITE events for encrypted rooms
            or evt.unsigned.invite_room_state is not None
        ):
            return
        self.log.info("New room with %r", evt.sender)
        async with self.direct_update_lock:
            direct_rooms = await evt.client.get_account_data(EventType.DIRECT)
            if evt.sender not in direct_rooms:
                direct_rooms[evt.sender] = []
            direct_rooms[evt.sender].append(evt.room_id)
            await evt.client.set_account_data(EventType.DIRECT, direct_rooms)
        await self.sync_direct_rooms(evt.client)

    async def sync_direct_rooms(self, client):
        self.log.info("Resyncing direct rooms")
        self.direct_rooms = await client.get_account_data(EventType.DIRECT)

    @event.on(EventType.ROOM_MESSAGE)
    async def chat_message(self, evt: MaubotMessageEvent):
        if not self.config or not self.database:
            raise Exception("Config or database not initialized")
        if evt.sender == self.client.mxid:
            return

        token_match = self.token_regex.search(evt.content.body)
        talk_shortcodes = self.schedule_talk_regex.findall(evt.content.body)

        if talk_shortcodes and not token_match and not evt.content.body[0] == "!":
            room_ids = [
                await self.database.fetchval(
                    "SELECT room_id FROM talks WHERE talk_shortcode=$1", code
                )
                for code in talk_shortcodes
            ]
            if evt.room_id in room_ids:  # Don't link current channel
                room_ids.remove(evt.room_id)
            if room_ids:
                plural = len(room_ids) > 1
                await evt.reply(
                    (
                        "Here{isare} the discussion room{plur}! {links}".format(
                            isare=" are" if plural else "'s",
                            plur="s" if plural else "",
                            links=" ".join(
                                [room_mention(room_id) for room_id in room_ids]
                            ),
                        )
                    )
                )
            return

        if not self.direct_rooms:
            await self.sync_direct_rooms(evt.client)
        pm = evt.room_id in self.direct_rooms.get(evt.sender, [])

        self.log.debug(
            "%s in %r: %r: %r",
            "PM" if pm else "Room message",
            evt.room_id,
            evt.sender,
            evt.content.body,
        )

        if token_match and not pm:
            self.log.warning(
                "Token found in non-PM room: %r %r", evt.sender, evt.room_id
            )

        if evt.content.body[0] == "!":
            return
        if not token_match:
            if pm:
                await evt.reply(self.config["help"])
            return

        token = token_match.group(0)
        token_hash = sha256(token.encode()).digest()
        async with self.database.acquire() as conn:
            await conn.execute("BEGIN TRANSACTION")
            token_rows = await conn.fetch(
                "SELECT * FROM tokens WHERE token_hash = $1 FOR UPDATE",
                token_hash,
            )

            if not token_rows:
                if pm:
                    await evt.reply("Sorry, that's not a valid token.")
                return
            if not all(row["used_at"] for row in token_rows):
                self.log.info("Marking token used: %r %r", evt.sender, token)
                await conn.execute(
                    (
                        "UPDATE tokens SET used_at = NOW(), used_by = $1 "
                        "WHERE token_hash = $2 AND used_at IS NULL"
                    ),
                    evt.sender,
                    token_hash,
                )
            await conn.execute("COMMIT")

        used_by = {row["used_by"] for row in token_rows}.pop()
        if used_by and used_by != evt.sender:
            self.log.info(
                "Attempted token reuse: %r used %r's %r",
                evt.sender,
                used_by,
                token,
            )
            if pm:
                await evt.reply(
                    "Sorry, that token has already been used by someone else. "
                    "Are you on the correct account?"
                )
            return

        invited_spaces = []
        error_spaces = []
        for d in token_rows:
            self.log.info(
                "Inviting %r to %r (%r)",
                evt.sender,
                d["type"],
                self.config["rooms"].get(d["type"]),
            )
            if d["type"] not in self.config["rooms"]:
                if pm:
                    await evt.reply(
                        (
                            "Sorry, your {} token is configured incorrectly! "
                            "Please try again later, or report this problem to staff."
                        ).format(d["type"])
                    )
                self.log.error(
                    "Token from %r marked as %r but I don't know that space! %r",
                    evt.sender,
                    d["type"],
                    token,
                )
                continue
            try:
                await evt.client.invite_user(
                    self.config["rooms"][d["type"]], evt.sender
                )
            except MForbidden:
                if pm:
                    error_spaces.append(d["type"])
                continue
            if pm or not d["used_at"]:  # PM, or not previously used
                invited_spaces.append(d["type"])

        reply = ""
        for space in invited_spaces:
            reply += "I've invited you to the {} space!  \n".format(space)
        if invited_spaces:
            plural = len(invited_spaces) > 1
            reply += (
                "You should see the invite{} in your spaces list.  \n"
                "Once you accept {}, from there you can join the "
                "rooms and spaces that interest you. Thanks for coming to HOPE!"
            ).format("s" if plural else "", "them" if plural else "it")
            if error_spaces:
                reply += "\n\n"
        for space in error_spaces:
            reply += "I couldn't invite you to the {} space.  \n".format(space)
        if error_spaces:
            plural = len(error_spaces) > 1
            reply += (
                "Are you already in {}? Maybe you haven't "
                "accepted the invite{} - check the spaces list on "
                "the left or in the menu."
            ).format("them" if plural else "it", "s" if plural else "")
        if reply:
            await evt.reply(reply)

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table
