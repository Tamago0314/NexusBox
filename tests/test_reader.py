"""reader cog のうち、discord への接続が要らない純粋な部分のテスト。"""

from types import SimpleNamespace

import pytest

from nexusbox.cogs.reader import _describe_extras
from nexusbox.models import GuildConfig

GUILD = 42


def _message(*, attachments=0, stickers=0, embeds=0, content="やあ"):
    return SimpleNamespace(
        attachments=[object()] * attachments,
        stickers=[object()] * stickers,
        embeds=[object()] * embeds,
        content=content,
    )


def test_single_attachment_is_read_as_tempufairu() -> None:
    config = GuildConfig(guild_id=GUILD)
    assert _describe_extras(_message(attachments=1), config) == ["てんぷふぁいる"]


def test_multiple_attachments_still_read_once() -> None:
    """件数は読まない (画像を連投しても 1 回だけ)。"""
    config = GuildConfig(guild_id=GUILD)
    assert _describe_extras(_message(attachments=5), config) == ["てんぷふぁいる"]


def test_attachments_are_silent_when_disabled() -> None:
    config = GuildConfig(guild_id=GUILD, read_attachment=False)
    assert _describe_extras(_message(attachments=3), config) == []


def test_sticker_is_unchanged() -> None:
    config = GuildConfig(guild_id=GUILD)
    assert _describe_extras(_message(stickers=1), config) == ["スタンプ"]


def test_embed_only_message_says_nothing() -> None:
    """本文の無い埋め込み (BOT の参加通知など) は読まない。"""
    config = GuildConfig(guild_id=GUILD)
    assert _describe_extras(_message(embeds=1, content=""), config) == []


def test_embed_is_ignored_when_body_exists() -> None:
    config = GuildConfig(guild_id=GUILD)
    assert _describe_extras(_message(embeds=1, content="本文あり"), config) == []


# ------------------------------------------------- 読み上げ対象チャンネルの判定

class _FakeDB:
    def __init__(self, registered: set[int]) -> None:
        self._registered = registered

    async def get_read_channels(self, guild_id: int) -> set[int]:
        return self._registered


def _cog(registered: set[int] = frozenset()):
    from nexusbox.cogs.reader import ReaderCog

    cog = ReaderCog.__new__(ReaderCog)
    cog.bot = SimpleNamespace(db=_FakeDB(set(registered)))
    return cog


def _msg_in(channel_id: int, parent_id=None):
    channel = SimpleNamespace(id=channel_id)
    if parent_id is not None:
        channel.parent_id = parent_id
    return SimpleNamespace(guild=SimpleNamespace(id=GUILD), channel=channel)


def _player_on(channel_id: int | None):
    return SimpleNamespace(
        channel=None if channel_id is None else SimpleNamespace(id=channel_id)
    )


@pytest.mark.asyncio
async def test_connected_vc_chat_is_always_read() -> None:
    """接続中の VC 内チャットは listed モードで未登録でも読む。"""
    from nexusbox.models import ReadMode

    cog = _cog()
    config = GuildConfig(guild_id=GUILD, read_mode=ReadMode.LISTED)
    assert await cog._is_target_channel(_msg_in(500), config, _player_on(500)) is True


@pytest.mark.asyncio
async def test_other_channel_is_not_read_in_listed_mode() -> None:
    from nexusbox.models import ReadMode

    cog = _cog()
    config = GuildConfig(guild_id=GUILD, read_mode=ReadMode.LISTED)
    assert await cog._is_target_channel(_msg_in(999), config, _player_on(500)) is False


@pytest.mark.asyncio
async def test_registered_channel_still_works() -> None:
    from nexusbox.models import ReadMode

    cog = _cog({777})
    config = GuildConfig(guild_id=GUILD, read_mode=ReadMode.LISTED)
    assert await cog._is_target_channel(_msg_in(777), config, _player_on(500)) is True


@pytest.mark.asyncio
async def test_joined_text_channel_still_works() -> None:
    cog = _cog()
    config = GuildConfig(guild_id=GUILD, joined_text_channel_id=321)
    assert await cog._is_target_channel(_msg_in(321), config, _player_on(500)) is True


@pytest.mark.asyncio
async def test_no_crash_when_player_has_no_channel() -> None:
    """切断直後などで player.channel が None でも落ちないこと。"""
    cog = _cog()
    config = GuildConfig(guild_id=GUILD, joined_text_channel_id=321)
    assert await cog._is_target_channel(_msg_in(321), config, _player_on(None)) is True
    assert await cog._is_target_channel(_msg_in(999), config, _player_on(None)) is False
