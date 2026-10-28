"""SQLite (aiosqlite) 永続化層。

ギルド設定・読み上げ対象チャンネル・ミュートは発言のたびに読むので
メモリにキャッシュし、書き込んだらそのギルドの分だけ捨てる。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import DictEntry, DictScope, GuildConfig, ReadMode

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS guilds (
    guild_id               INTEGER PRIMARY KEY,
    enabled                INTEGER NOT NULL DEFAULT 1,
    read_mode              TEXT    NOT NULL DEFAULT 'joined',
    voice_channel_id       INTEGER,
    joined_text_channel_id INTEGER,
    max_length             INTEGER NOT NULL DEFAULT 120,
    read_name              INTEGER NOT NULL DEFAULT 0,
    bot_messages           INTEGER NOT NULL DEFAULT 0,
    skip_url               INTEGER NOT NULL DEFAULT 1,
    skip_codeblock         INTEGER NOT NULL DEFAULT 1,
    read_emoji             INTEGER NOT NULL DEFAULT 1,
    read_attachment        INTEGER NOT NULL DEFAULT 1,
    read_join_leave        INTEGER NOT NULL DEFAULT 1,
    auto_leave             INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS read_channels (
    guild_id   INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    PRIMARY KEY (guild_id, channel_id)
);

CREATE TABLE IF NOT EXISTS ignored_users (
    guild_id INTEGER NOT NULL,
    user_id  INTEGER NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

-- guild_id が NULL の行は global 辞書
CREATE TABLE IF NOT EXISTS dict_entries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id    INTEGER,
    surface     TEXT    NOT NULL,
    reading     TEXT    NOT NULL,
    accent_type INTEGER,
    priority    INTEGER NOT NULL DEFAULT 5,
    word_uuid   TEXT,
    created_by  INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- 表記の重複禁止。NULL 同士は一意制約で等しいとみなされないので、
-- server と global で別々の部分インデックスにしている
CREATE UNIQUE INDEX IF NOT EXISTS idx_dict_server
    ON dict_entries (guild_id, surface) WHERE guild_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_dict_global
    ON dict_entries (surface) WHERE guild_id IS NULL;
"""

# ON CONFLICT の対象は部分インデックスの条件と一致させる必要があるため、
# server 用と global 用で 2 本に分けている
_UPSERT_DICT_SERVER = """
INSERT INTO dict_entries
    (guild_id, surface, reading, accent_type, priority, word_uuid, created_by)
VALUES (:guild_id, :surface, :reading, :accent_type, :priority, :word_uuid, :created_by)
ON CONFLICT (guild_id, surface) WHERE guild_id IS NOT NULL DO UPDATE SET
    reading     = excluded.reading,
    accent_type = excluded.accent_type,
    priority    = excluded.priority,
    word_uuid   = excluded.word_uuid
"""

_UPSERT_DICT_GLOBAL = """
INSERT INTO dict_entries
    (guild_id, surface, reading, accent_type, priority, word_uuid, created_by)
VALUES (:guild_id, :surface, :reading, :accent_type, :priority, :word_uuid, :created_by)
ON CONFLICT (surface) WHERE guild_id IS NULL DO UPDATE SET
    reading     = excluded.reading,
    accent_type = excluded.accent_type,
    priority    = excluded.priority,
    word_uuid   = excluded.word_uuid
"""


class Database:
    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._conn: aiosqlite.Connection | None = None
        self._guild_cache: dict[int, GuildConfig] = {}
        self._channel_cache: dict[int, set[int]] = {}
        self._ignored_cache: dict[int, set[int]] = {}

    # ---------------------------------------------------------------- 接続管理

    async def connect(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row  # 列名で値を取れるようにする
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()
        log.info("database ready at %s", self._path)

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database.connect() が呼ばれていません")
        return self._conn

    # -------------------------------------------------------------- ギルド設定

    async def get_guild(self, guild_id: int) -> GuildConfig:
        """ギルド設定を返す。初めてのギルドなら既定値の行を作る。"""
        cached = self._guild_cache.get(guild_id)
        if cached is not None:
            return cached

        async with self.conn.execute(
            "SELECT * FROM guilds WHERE guild_id = ?", (guild_id,)
        ) as cur:
            row = await cur.fetchone()

        if row is None:
            config = GuildConfig(guild_id=guild_id)
            await self._insert_guild(config)
        else:
            config = GuildConfig.from_row(row)

        self._guild_cache[guild_id] = config
        return config

    async def _insert_guild(self, config: GuildConfig) -> None:
        row = config.to_row()
        columns = ", ".join(row)
        placeholders = ", ".join(f":{name}" for name in row)
        await self.conn.execute(
            f"INSERT OR IGNORE INTO guilds ({columns}) VALUES ({placeholders})", row
        )
        await self.conn.commit()

    async def update_guild(self, guild_id: int, **changes: Any) -> GuildConfig:
        """指定フィールドだけ更新し、更新後の設定を返す。"""
        if not changes:
            return await self.get_guild(guild_id)

        # 列名を SQL に直接埋め込むので、既知の列名以外は通さない
        unknown = set(changes) - set(GuildConfig.column_names())
        if unknown:
            raise ValueError(f"未知の設定項目: {', '.join(sorted(unknown))}")

        await self.get_guild(guild_id)  # 行が無ければ作っておく

        params: dict[str, Any] = {"guild_id": guild_id}
        for key, value in changes.items():
            if isinstance(value, ReadMode):
                params[key] = value.value
            elif isinstance(value, bool):
                params[key] = int(value)
            else:
                params[key] = value

        assignments = ", ".join(f"{key} = :{key}" for key in changes)
        await self.conn.execute(
            f"UPDATE guilds SET {assignments} WHERE guild_id = :guild_id", params
        )
        await self.conn.commit()

        self._guild_cache.pop(guild_id, None)
        return await self.get_guild(guild_id)

    # ------------------------------------------------------- 読み上げ対象チャンネル

    async def get_read_channels(self, guild_id: int) -> set[int]:
        cached = self._channel_cache.get(guild_id)
        if cached is not None:
            return cached

        async with self.conn.execute(
            "SELECT channel_id FROM read_channels WHERE guild_id = ?", (guild_id,)
        ) as cur:
            channels = {row["channel_id"] for row in await cur.fetchall()}

        self._channel_cache[guild_id] = channels
        return channels

    async def add_read_channel(self, guild_id: int, channel_id: int) -> bool:
        """追加できたら True、既に登録済みなら False。"""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO read_channels (guild_id, channel_id) VALUES (?, ?)",
            (guild_id, channel_id),
        )
        await self.conn.commit()
        self._channel_cache.pop(guild_id, None)
        return cur.rowcount > 0

    async def remove_read_channel(self, guild_id: int, channel_id: int) -> bool:
        """外せたら True、登録されていなければ False。"""
        cur = await self.conn.execute(
            "DELETE FROM read_channels WHERE guild_id = ? AND channel_id = ?",
            (guild_id, channel_id),
        )
        await self.conn.commit()
        self._channel_cache.pop(guild_id, None)
        return cur.rowcount > 0

    async def clear_read_channels(self, guild_id: int) -> int:
        """全部外して、外した件数を返す。"""
        cur = await self.conn.execute(
            "DELETE FROM read_channels WHERE guild_id = ?", (guild_id,)
        )
        await self.conn.commit()
        self._channel_cache.pop(guild_id, None)
        return cur.rowcount

    # ---------------------------------------------------------------- ミュート

    async def get_ignored_users(self, guild_id: int) -> set[int]:
        cached = self._ignored_cache.get(guild_id)
        if cached is not None:
            return cached
        async with self.conn.execute(
            "SELECT user_id FROM ignored_users WHERE guild_id = ?", (guild_id,)
        ) as cur:
            users = {row["user_id"] for row in await cur.fetchall()}
        self._ignored_cache[guild_id] = users
        return users

    async def add_ignored_user(self, guild_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO ignored_users (guild_id, user_id) VALUES (?, ?)",
            (guild_id, user_id),
        )
        await self.conn.commit()
        self._ignored_cache.pop(guild_id, None)
        return cur.rowcount > 0

    async def remove_ignored_user(self, guild_id: int, user_id: int) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM ignored_users WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        await self.conn.commit()
        self._ignored_cache.pop(guild_id, None)
        return cur.rowcount > 0

    # -------------------------------------------------------------------- 辞書

    async def list_dict_entries(
        self, guild_id: int | None, *, keyword: str | None = None
    ) -> list[DictEntry]:
        """`guild_id=None` で global 辞書。長い表記から順に並べて返す。"""
        # `IS ?` は NULL にも一致するので、server / global を同じ SQL で引ける
        sql = "SELECT * FROM dict_entries WHERE guild_id IS ?"
        params: list[Any] = [guild_id]
        if keyword:
            sql += " AND (surface LIKE ? OR reading LIKE ?)"
            params += [f"%{keyword}%", f"%{keyword}%"]
        sql += " ORDER BY LENGTH(surface) DESC, surface ASC"

        async with self.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_entry_from_row(row) for row in rows]

    async def get_dict_entry(
        self, guild_id: int | None, surface: str
    ) -> DictEntry | None:
        async with self.conn.execute(
            "SELECT * FROM dict_entries WHERE guild_id IS ? AND surface = ?",
            (guild_id, surface),
        ) as cur:
            row = await cur.fetchone()
        return _entry_from_row(row) if row else None

    async def upsert_dict_entry(self, entry: DictEntry) -> None:
        """同じ表記があれば読みなどを上書きする。"""
        sql = _UPSERT_DICT_SERVER if entry.guild_id is not None else _UPSERT_DICT_GLOBAL
        await self.conn.execute(
            sql,
            {
                "guild_id": entry.guild_id,
                "surface": entry.surface,
                "reading": entry.reading,
                "accent_type": entry.accent_type,
                "priority": entry.priority,
                "word_uuid": entry.word_uuid,
                "created_by": entry.created_by,
            },
        )
        await self.conn.commit()

    async def delete_dict_entry(self, guild_id: int | None, surface: str) -> bool:
        cur = await self.conn.execute(
            "DELETE FROM dict_entries WHERE guild_id IS ? AND surface = ?",
            (guild_id, surface),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def bulk_upsert_dict_entries(self, entries: Iterable[DictEntry]) -> int:
        count = 0
        for entry in entries:
            await self.upsert_dict_entry(entry)
            count += 1
        return count


def _entry_from_row(row: aiosqlite.Row) -> DictEntry:
    return DictEntry(
        surface=row["surface"],
        reading=row["reading"],
        scope=DictScope.SERVER if row["guild_id"] is not None else DictScope.GLOBAL,
        guild_id=row["guild_id"],
        accent_type=row["accent_type"],
        priority=row["priority"],
        word_uuid=row["word_uuid"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
