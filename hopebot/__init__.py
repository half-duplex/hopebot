from __future__ import annotations

from asyncio import Lock
from hashlib import sha256
import logging
import re
from typing import TYPE_CHECKING

from asyncpg.exceptions import UniqueViolationError
from maubot import Plugin
from maubot.handlers import command, event
from mautrix.errors.request import MForbidden
from mautrix.types import EventType, Membership
from mautrix.util.async_db import Scheme, UpgradeTable
from mautrix.util.config import BaseProxyConfig

if TYPE_CHECKING:
    from typing import Type

    from maubot import MessageEvent
    from mautrix.util.async_db import Connection
    from mautrix.util.config import ConfigUpdateHelper
    from mautrix.types import StateEvent


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


class Config(BaseProxyConfig):
    def do_update(self, helper: ConfigUpdateHelper):
        helper.copy("token_regex")
        helper.copy("enable_token_clearing")
        helper.copy("spaces")
        helper.copy("help")
        helper.copy("owners")


class HopeBot(Plugin):
    direct_rooms = None
    direct_update_lock = Lock()

    async def start(self):
        self.config.load_and_update()
        self.token_regex = re.compile(
            self.config["token_regex"],
            re.IGNORECASE,
        )

    @command.new()
    async def help(self, evt: MessageEvent):
        await evt.reply(self.config["help"])

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
        if evt.content.membership != Membership.INVITE or not evt.content.is_direct:
            return
        LOGGER.info("New room with %r", evt.sender)
        async with self.direct_update_lock:
            direct_rooms = await evt.client.get_account_data(EventType.DIRECT)
            if evt.sender not in direct_rooms:
                direct_rooms[evt.sender] = []
            direct_rooms[evt.sender].append(evt.room_id)
            await evt.client.set_account_data(EventType.DIRECT, direct_rooms)
        await self.sync_direct_rooms(evt.client)

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
