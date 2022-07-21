from __future__ import annotations

from asyncio import Lock, sleep
from datetime import datetime
from hashlib import sha256
import logging
import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from aiohttp import ClientSession as HTTPClientSession
from asyncpg.exceptions import UniqueViolationError
from maubot import Plugin
from maubot.handlers import command, event
from mautrix.errors.request import MForbidden, MNotFound
from mautrix.types import EventType, JoinRule, Membership, RoomCreatePreset, StateEvent
from mautrix.util.async_db import Scheme, UpgradeTable
from mautrix.util.config import BaseProxyConfig

from .image_gen import draw_flow_field

if TYPE_CHECKING:
    from typing import Type

    from maubot import MessageEvent
    from mautrix.util.async_db import Connection
    from mautrix.util.config import ConfigUpdateHelper


TITLE_XOFY_REGEX = re.compile(r"(.*),? \(?(\d+) of \d+\)?")
LOGGER = logging.getLogger(__name__)
upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial schema")
async def upgrade_db_v1(conn: Connection, scheme: Scheme):
    if scheme != Scheme.POSTGRES:
        LOGGER.error(
            "Error: Only psql is supported. Currently configured: %r",
            scheme,
        )
        return None
    await conn.execute(
        """
        CREATE TYPE token_type AS ENUM ('attendee', 'volunteer', 'presenter');
        CREATE TABLE tokens (
            token_hash BIT(256) NOT NULL PRIMARY KEY,
            type       token_type NOT NULL,
            loaded_at  TIMESTAMP WITH TIME ZONE,
            used_at    TIMESTAMP WITH TIME ZONE DEFAULT NULL,
            used_by    TEXT DEFAULT NULL
        )"""
    )


@upgrade_table.register(description="Add talk chat persistence")
async def upgrade_db_v2(conn: Connection):
    await conn.execute(
        """CREATE TABLE talks (
            talk_id        INTEGER NOT NULL PRIMARY KEY,
            talk_shortcode VARCHAR(16) NOT NULL UNIQUE,
            room_id        VARCHAR(128) NOT NULL
        )"""
    )


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("token_regex")
        helper.copy("enable_token_clearing")
        helper.copy("spaces")
        helper.copy("help")
        helper.copy("owners")
        helper.copy("talk_chat_space")
        helper.copy("pretalx_json_url")


class HopeBot(Plugin):
    direct_rooms = None
    direct_update_lock = Lock()
    httpsession = HTTPClientSession()

    async def start(self):
        self.config.load_and_update()
        self.token_regex = re.compile(
            self.config["token_regex"],
            re.IGNORECASE,
        )

    async def stop(self):
        LOGGER.info("Hopebot plugin stopping")
        self.httpsession.close()

    @command.new()
    async def help(self, evt: MessageEvent):
        await evt.reply(self.config["help"])

    @command.new()
    async def adminhelp(self, evt: MessageEvent):
        await evt.reply(
            "For a list of admin commands, see "
            "https://github.com/half-duplex/hopebot/blob/main/README.md#usage"
        )

    @command.new()
    async def stats(self, evt: MessageEvent):
        if evt.sender not in self.config["owners"]:
            return
        async with self.database.acquire() as conn:
            r = await conn.fetchrow(
                "SELECT COUNT(*) AS rowcount FROM tokens",
            )
            total_tokens = r[0]
            r = await conn.fetchrow(
                (
                    "SELECT COUNT(*) AS rowcount FROM tokens "
                    "WHERE used_by IS NOT NULL"
                ),
            )
            used_tokens = r[0]
        await evt.reply(
            "Total tokens: {}  \nUsed tokens: {} ({:.2f}%)".format(
                total_tokens,
                used_tokens,
                (used_tokens / total_tokens * 100) if total_tokens > 0 else 100,
            )
        )

    @command.new(name="sync_talks")
    @command.argument("clear", required=False)
    async def sync_talks(self, evt: MessageEvent, clear: bool):
        if evt.sender not in self.config["owners"]:
            LOGGER.warning("Attempt by non-owner %r to sync talks", evt.sender)
            return
        await evt.reply("This will take a long time.")

        r = await self.httpsession.get(self.config["pretalx_json_url"])
        data = await r.json()
        conf = data["schedule"]["conference"]

        all_talks = {}
        for day in conf["days"]:
            for talks in day["rooms"].values():
                for talk in talks:

                    # Shorten room names
                    if talk["room"] == "Other":
                        room_short = ""
                    elif talk["room"].startswith("Workshop "):
                        room_short = talk["room"].split(" ")[1]
                    else:
                        room_short = talk["room"].split(" ")[0]

                    title = talk["title"]
                    # Deduplicate "Foo (1 of 3)" titles
                    xofy = TITLE_XOFY_REGEX.fullmatch(title)
                    if xofy:
                        if room_short:
                            room_short += ": "
                        room_name = room_short + xofy.group(1)
                    else:
                        # add day/time prefix
                        time = datetime.fromisoformat(talk["date"])
                        if room_short:
                            room_short = "-" + room_short
                        room_name = "{}{}: {}".format(
                            time.strftime("%a-%H"), room_short, title
                        )

                    all_talks[room_name] = all_talks.get(room_name, []) + [talk]

        async with self.database.acquire() as conn:
            for room_name, talks_unsorted in all_talks.items():
                delay = 1
                LOGGER.info("Syncing talks: %d of: %r", len(talks), room_name)
                talks = sorted(talks_unsorted, key=lambda x: x["date"])

                topic = talks[0]["abstract"] + "<br>"
                topic_plain = talks[0]["abstract"] + "\n"
                for talk in talks:
                    date = datetime.fromisoformat(talk["date"]).strftime("%a %H:%M %Z")
                    topic += "<br><a href='{2}'>{0} in {1}</a>".format(
                        date, talk["room"], talk["url"]
                    )
                    topic_plain += "\n{} in {}, {}  ".format(
                        date, talk["room"], talk["url"]
                    )

                room_id = await conn.fetchval(
                    "SELECT room_id FROM talks WHERE talk_id = $1", talks[0]["id"]
                )
                if not room_id:
                    delay += 3
                    avatar_url = await self.create_avatar(evt.client, talks[0]["id"])
                    room_id = await evt.client.create_room(
                        name=room_name,
                        preset=RoomCreatePreset.TRUSTED_PRIVATE,
                        invitees=[],  # ["@mal:hope.net"],  # self.config["owners"],
                        initial_state=[
                            StateEvent(
                                type=EventType.SPACE_PARENT,
                                room_id=None,
                                event_id=None,
                                sender=None,
                                timestamp=None,
                                state_key=self.config["talk_chat_space"],
                                content={
                                    "canonical": True,
                                    "via": [
                                        self.config["talk_chat_space"].split(":")[1]
                                    ],
                                },
                            ),
                            StateEvent(
                                type=EventType.ROOM_JOIN_RULES,
                                room_id=None,
                                event_id=None,
                                sender=None,
                                timestamp=None,
                                state_key=None,
                                content={
                                    "join_rule": JoinRule.RESTRICTED,
                                    "allow": [
                                        {
                                            "type": "m.room_membership",
                                            "room_id": self.config["talk_chat_space"],
                                        }
                                    ],
                                },
                            ),
                            StateEvent(
                                type=EventType.ROOM_TOPIC,
                                room_id=None,
                                event_id=None,
                                sender=None,
                                timestamp=None,
                                state_key=None,
                                content={
                                    "topic": topic_plain,
                                    "m.topic": [
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
                            ),
                            StateEvent(
                                type=EventType.ROOM_AVATAR,
                                room_id=None,
                                event_id=None,
                                sender=None,
                                timestamp=None,
                                state_key=None,
                                content={
                                    "url": avatar_url,
                                },
                            ),
                            StateEvent(
                                type=EventType.ROOM_POWER_LEVELS,
                                room_id=None,
                                event_id=None,
                                sender=None,
                                timestamp=None,
                                state_key=None,
                                content={
                                    "ban": 50,
                                    "events": {
                                        EventType.ROOM_AVATAR: 100,
                                        EventType.ROOM_CANONICAL_ALIAS: 100,
                                        EventType.ROOM_ENCRYPTION: 100,
                                        EventType.ROOM_HISTORY_VISIBILITY: 100,
                                        EventType.ROOM_NAME: 100,
                                        EventType.ROOM_TOPIC: 100,
                                        EventType.ROOM_POWER_LEVELS: 100,
                                        "m.room.server_acl": 100,
                                        EventType.ROOM_TOMBSTONE: 100,
                                        EventType.ROOM_JOIN_RULES: 100,
                                    },
                                    "invite": 100,
                                    "kick": 50,
                                    # "notifications": {},
                                    "redact": 50,
                                    "state_default": 50,
                                    "users": {
                                        uid: 100
                                        for uid in self.config["owners"]
                                        + [evt.client.mxid]
                                    },
                                },
                            ),
                        ],
                    )
                    if not room_id:
                        LOGGER.error("Error creating room for %r", room_name)
                        continue
                    await evt.client.send_state_event(
                        self.config["talk_chat_space"],
                        EventType.SPACE_CHILD,
                        content={"via": [room_id.split(":")[1]]},
                        state_key=room_id,
                    )

                    LOGGER.info("Created room %r for %r", room_id, room_name)
                    await conn.executemany(
                        (
                            "INSERT INTO talks "
                            "(talk_id, talk_shortcode, room_id) VALUES ($1, $2, $3)"
                        ),
                        (
                            (
                                talk["id"],
                                self.get_talk_shortcode(talk["url"]),
                                room_id,
                            )
                            for talk in talks
                        ),
                    )

                # Match room name
                current_name_evt = await evt.client.get_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_NAME,
                )
                if current_name_evt.name != room_name:
                    delay += 1
                    LOGGER.debug(
                        "Updating room name for %r from %r to %r",
                        room_id,
                        current_name_evt.name,
                        room_name,
                    )
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_NAME,
                        content={
                            "name": room_name,
                        },
                    )

                # Match topic
                current_topic_evt = await evt.client.get_state_event(
                    room_id=room_id,
                    event_type=EventType.ROOM_TOPIC,
                )
                if current_topic_evt.topic != topic_plain:
                    delay += 1
                    LOGGER.debug("Updating room topic for %r (%r)", room_id, room_name)
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_TOPIC,
                        content={
                            "topic": topic_plain,
                            "m.topic": [
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
                    delay += 1
                    LOGGER.debug(
                        "Setting avatar for for %r (%r)",
                        room_id,
                        room_name,
                    )
                    avatar_url = await self.create_avatar(evt.client, talks[0]["id"])
                    await evt.client.send_state_event(
                        room_id=room_id,
                        event_type=EventType.ROOM_AVATAR,
                        content={
                            "url": avatar_url,
                        },
                    )

                # Match permissions
                # power_levels = await evt.client.get_state_event(
                #    room_id=room_id,
                #    event_type=EventType.ROOM_POWER_LEVELS,
                # )
                # LOGGER.critical("PLs: %r", power_levels)

                # TODO: match more room info - space membership? perms?

                # Comment if using a ratelimit-exempt account
                await sleep(delay * 2)
        await evt.reply("Done!")

    async def create_avatar(self, client, seed):
        LOGGER.info("Generating avatar for seed %d", seed)
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
        self, evt: MessageEvent, filename: str, token_type: str, clear: bool
    ):
        if evt.sender not in self.config["owners"]:
            LOGGER.warning("Attempt by non-owner %r to load tokens", evt.sender)
            return
        if token_type not in self.config["spaces"]:
            await evt.reply("Unknown type {}".format(repr(token_type)))
            return
        await evt.reply("Loading tokens from {}...".format(filename))
        LOGGER.warning(
            "Token load for %ss from %r started by %s, clear=%s",
            token_type,
            filename,
            evt.sender,
            clear,
        )
        async with self.database.acquire() as conn:
            await conn.execute("BEGIN TRANSACTION")
            if clear == "clear":
                if not self.config["enable_token_clearing"]:
                    await evt.reply("Token clearing is disabled, aborting.")
                    return
                LOGGER.warning("Truncating table")
                await conn.execute("TRUNCATE TABLE tokens")
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
        LOGGER.info("Token load finished, loaded %d", loaded)
        await evt.reply("Done, loaded {} {} tokens".format(loaded, token_type))

    @command.new(name="mark_unused")
    @command.argument("token")
    async def mark_unused(self, evt: MessageEvent, token: str):
        if evt.sender not in self.config["owners"]:
            LOGGER.warning(
                "Attempt by non-owner %r to mark token unused: %r", evt.sender, token
            )
            return

        token_match = self.token_regex.fullmatch(token)
        if token_match:
            token_hash = sha256(token.encode()).digest()
        else:
            token_hash = bytes.fromhex(token)

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

            LOGGER.warning(
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
    async def token_info(self, evt: MessageEvent, token: str):
        if evt.sender not in self.config["owners"]:
            LOGGER.warning(
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
        LOGGER.info("New room with %r", evt.sender)
        async with self.direct_update_lock:
            direct_rooms = await evt.client.get_account_data(EventType.DIRECT)
            if evt.sender not in direct_rooms:
                direct_rooms[evt.sender] = []
            direct_rooms[evt.sender].append(evt.room_id)
            await evt.client.set_account_data(EventType.DIRECT, direct_rooms)
        await self.sync_direct_rooms(evt.client)
        await evt.client.send_markdown(evt.room_id, self.config["help"])

    async def sync_direct_rooms(self, client):
        LOGGER.info("Resyncing direct rooms")
        self.direct_rooms = await client.get_account_data(EventType.DIRECT)

    @event.on(EventType.ROOM_MESSAGE)
    async def token_attempt(self, evt: MessageEvent):
        if evt.sender == self.client.mxid:
            return
        token_match = self.token_regex.search(evt.content.body)

        if self.direct_rooms is None:
            await self.sync_direct_rooms(evt.client)
        if evt.room_id not in self.direct_rooms.get(evt.sender, []):
            LOGGER.debug(
                "Room message in %r: %r: %r (%r)",
                evt.room_id,
                evt.sender,
                evt.content.body,
                self.direct_rooms.get(evt.sender, []),
            )
            if token_match:
                # TODO: delete message? tell user?
                LOGGER.warning(
                    "Token found in non-PM room: %r %r", evt.sender, evt.room_id
                )
            return
        LOGGER.debug("PM message: %r: %r", evt.sender, evt.content.body)

        if evt.content.body[0] == "!":
            return
        if not token_match:
            await evt.reply(self.config["help"])
            return

        token = token_match.group(0)
        token_hash = sha256(token.encode()).digest()
        async with self.database.acquire() as conn:
            await conn.execute("BEGIN TRANSACTION")
            d = await conn.fetchrow(
                "SELECT * FROM tokens WHERE token_hash = $1 FOR UPDATE",
                token_hash,
            )

            if not d:
                await evt.reply("Sorry, that's not a valid token.")
                return
            if d["used_at"] is None:
                LOGGER.info("Marking token used: %r %r", evt.sender, token)
                await conn.execute(
                    (
                        "UPDATE tokens SET used_at = NOW(), used_by = $1 "
                        "WHERE token_hash = $2"
                    ),
                    evt.sender,
                    token_hash,
                )
            await conn.execute("COMMIT")

        if d["used_at"] is None or d["used_by"] == evt.sender:
            LOGGER.info(
                "Inviting %r to %r (%r)",
                evt.sender,
                d["type"],
                self.config["spaces"].get(d["type"]),
            )
            if d["type"] not in self.config["spaces"]:
                await evt.reply(
                    "Sorry, your token is configured incorrectly! "
                    "Please try again later, or report this problem to staff."
                )
                LOGGER.error(
                    "Token from %r marked as %r but I don't know that space!",
                    evt.sender,
                    d["type"],
                    token,
                )
                return
            try:
                await evt.client.invite_user(
                    self.config["spaces"][d["type"]], evt.sender
                )
            except MForbidden:
                await evt.reply(
                    (
                        "I couldn't invite you to the Space for {}s. "
                        "Are you already in it? Maybe you haven't "
                        "accepted the invite - check the Spaces list on "
                        "the left or in the menu."
                    ).format(d["type"])
                )
                return
            await evt.reply(
                (
                    "I've invited you to the Space for {}s!  \n"
                    "You should see the invite in your Spaces list.  \n"
                    "Once you accept it, from there you can join the "
                    "rooms and spaces that interest you. Thanks for coming to HOPE!"
                ).format(d["type"])
            )
        else:
            LOGGER.info(
                "Attempted token reuse: %r used %r's (%r)",
                evt.sender,
                d["used_by"],
                token,
            )
            await evt.reply(
                "Sorry, that token has already been used by someone else. "
                "Are you on the correct account?"
            )

    @classmethod
    def get_config_class(cls) -> Type[BaseProxyConfig]:
        return Config

    @classmethod
    def get_db_upgrade_table(cls) -> UpgradeTable:
        return upgrade_table

    def get_talk_shortcode(self, url: str):
        # TODO: sanity check url, maybe catch exceptions
        return urlparse(url).path.split("/")[3]
