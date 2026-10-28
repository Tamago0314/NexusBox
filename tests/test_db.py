import pytest

from nexusbox.db import Database
from nexusbox.models import GuildConfig, ReadMode

GUILD = 42
USER = 7


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.mark.asyncio
async def test_guild_row_is_created_with_defaults(db) -> None:
    config = await db.get_guild(GUILD)
    assert config == GuildConfig(guild_id=GUILD)
    assert config.read_mode is ReadMode.JOINED
    assert config.enabled is True
    assert config.bot_messages is False


@pytest.mark.asyncio
async def test_update_guild_persists_and_invalidates_cache(db) -> None:
    await db.get_guild(GUILD)
    await db.update_guild(GUILD, enabled=False, max_length=42, read_mode=ReadMode.ALL)

    config = await db.get_guild(GUILD)
    assert config.enabled is False
    assert config.max_length == 42
    assert config.read_mode is ReadMode.ALL


@pytest.mark.asyncio
async def test_update_guild_survives_reconnect(db, tmp_path) -> None:
    await db.update_guild(GUILD, max_length=200)
    await db.close()

    reopened = Database(tmp_path / "test.db")
    await reopened.connect()
    try:
        assert (await reopened.get_guild(GUILD)).max_length == 200
    finally:
        await reopened.close()


@pytest.mark.asyncio
async def test_update_guild_rejects_unknown_field(db) -> None:
    with pytest.raises(ValueError):
        await db.update_guild(GUILD, nonexistent=1)


@pytest.mark.asyncio
async def test_read_channels(db) -> None:
    assert await db.add_read_channel(GUILD, 100) is True
    assert await db.add_read_channel(GUILD, 100) is False
    assert await db.get_read_channels(GUILD) == {100}

    await db.add_read_channel(GUILD, 200)
    assert await db.get_read_channels(GUILD) == {100, 200}

    assert await db.remove_read_channel(GUILD, 100) is True
    assert await db.remove_read_channel(GUILD, 100) is False
    assert await db.get_read_channels(GUILD) == {200}

    assert await db.clear_read_channels(GUILD) == 1
    assert await db.get_read_channels(GUILD) == set()


@pytest.mark.asyncio
async def test_ignored_users(db) -> None:
    assert await db.add_ignored_user(GUILD, USER) is True
    assert await db.get_ignored_users(GUILD) == {USER}
    assert await db.remove_ignored_user(GUILD, USER) is True
    assert await db.get_ignored_users(GUILD) == set()


@pytest.mark.asyncio
async def test_dict_entries_are_ordered_by_surface_length(db) -> None:
    from nexusbox.models import DictEntry, DictScope

    for surface, reading in (("ずんだ", "ズンダ"), ("ずんだもん", "ズンダモン"), ("も", "モ")):
        await db.upsert_dict_entry(
            DictEntry(surface=surface, reading=reading, scope=DictScope.SERVER, guild_id=GUILD)
        )

    entries = await db.list_dict_entries(GUILD)
    assert [e.surface for e in entries] == ["ずんだもん", "ずんだ", "も"]


@pytest.mark.asyncio
async def test_global_and_server_dict_do_not_collide(db) -> None:
    from nexusbox.models import DictEntry, DictScope

    await db.upsert_dict_entry(
        DictEntry(surface="草", reading="クサ", scope=DictScope.SERVER, guild_id=GUILD)
    )
    await db.upsert_dict_entry(
        DictEntry(surface="草", reading="ワラ", scope=DictScope.GLOBAL, guild_id=None)
    )

    server = await db.get_dict_entry(GUILD, "草")
    glob = await db.get_dict_entry(None, "草")
    assert server is not None and server.reading == "クサ"
    assert glob is not None and glob.reading == "ワラ"
