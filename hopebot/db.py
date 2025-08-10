from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from mautrix.util.async_db import Scheme, UpgradeTable


if TYPE_CHECKING:
    from mautrix.util.async_db import Connection

LOGGER = logging.getLogger(__name__)
upgrade_table = UpgradeTable()


@upgrade_table.register(description="Initial schema")  # type: ignore
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


@upgrade_table.register(description="Add talk chat persistence")  # type: ignore
async def upgrade_db_v2(conn: Connection):
    await conn.execute(
        """CREATE TABLE talks (
            talk_id        INTEGER NOT NULL PRIMARY KEY,
            talk_shortcode VARCHAR(16) NOT NULL UNIQUE,
            room_id        VARCHAR(128) NOT NULL
        )"""
    )


@upgrade_table.register(description="Add press token type")  # type: ignore
async def upgrade_db_v3(conn: Connection):
    await conn.execute("ALTER TYPE token_type ADD VALUE IF NOT EXISTS 'press'")


@upgrade_table.register(description="Add speaker permission persistence")  # type: ignore
async def upgrade_db_v4(conn: Connection):
    await conn.execute(
        """CREATE TABLE speakers (
            talk_id INTEGER NOT NULL,
            user_id VARCHAR(256) NOT NULL,
            CONSTRAINT speakers_pkey PRIMARY KEY (talk_id, user_id)
        )"""
    )


@upgrade_table.register(description="Allow duplicate tokens with different types")  # type: ignore
async def upgrade_db_v5(conn: Connection):
    await conn.execute(
        """ALTER TABLE tokens
            DROP CONSTRAINT tokens_pkey,
            ADD PRIMARY KEY (token_hash, type);
        """
    )


@upgrade_table.register(description="Prepare for automatic talk announce/suggest")  # type: ignore
async def upgrade_db_v6(conn: Connection):
    await conn.execute(
        """
        ALTER TABLE talks
            ADD COLUMN start_ts timestamp with time zone,
            ADD COLUMN end_ts timestamp with time zone,
            ADD COLUMN talk_title character varying(128);
        CREATE INDEX start_ts_idx ON talks (start_ts);
        CREATE INDEX end_ts_idx ON talks (end_ts);
        """
    )


@upgrade_table.register(description="Also save talk location")  # type: ignore
async def upgrade_db_v7(conn: Connection):
    await conn.execute(
        """ALTER TABLE talks ADD COLUMN location character varying(128);"""
    )
