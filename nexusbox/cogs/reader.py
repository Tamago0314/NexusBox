"""メッセージ読み上げ本体と、ボイスチャンネルの入退室処理。"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..bot import NexusBot
from ..models import GuildConfig, ReadMode
from ..player import GuildPlayer, SpeechItem
from ..textproc import (
    ATTACHMENT_PLACEHOLDER,
    MentionMap,
    TextRules,
    compose_speech,
    finalize,
)
from ..voicevox import VoicevoxError

log = logging.getLogger(__name__)

#: この接頭辞で始まる発言は読み上げない (他 BOT のコマンドやメモ用)
IGNORE_PREFIXES = ("//", ";;")


class ReaderCog(commands.Cog):
    def __init__(self, bot: NexusBot) -> None:
        self.bot = bot

    # --------------------------------------------------------------- 読み上げ

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """読み上げるかを順に判定し、読むならキューに積む。"""
        if message.guild is None or message.author.id == self.bot.user.id:  # type: ignore[union-attr]
            return
        # ピン留め通知や参加メッセージなどのシステムメッセージは読まない
        if message.type not in (discord.MessageType.default, discord.MessageType.reply):
            return

        player = self.bot.tts.get(message.guild.id)
        if player is None:
            return

        config = await self.bot.db.get_guild(message.guild.id)
        if not config.enabled:
            return
        if message.author.bot and not config.bot_messages:
            return
        if message.content.startswith(IGNORE_PREFIXES):
            return
        if not await self._is_target_channel(message, config, player):
            return

        ignored = await self.bot.db.get_ignored_users(message.guild.id)
        if message.author.id in ignored:
            return

        try:
            text = await self._build_text(message, config)
        except VoicevoxError as exc:
            log.warning("読み上げテキストの生成に失敗: %s", exc)
            return

        if not text:
            return

        speaker_id, params = self.bot.voice
        await player.enqueue(SpeechItem(text=text, speaker_id=speaker_id, params=params))

    async def _is_target_channel(
        self, message: discord.Message, config: GuildConfig, player: GuildPlayer
    ) -> bool:
        """このメッセージのチャンネルが読み上げ対象か。"""
        assert message.guild is not None

        # 接続中のボイスチャンネルに内蔵されたテキストチャットは常に読み上げる。
        # モード判定より先に見るので listed/joined のどちらでも効く。
        if player.channel is not None and message.channel.id == player.channel.id:
            return True

        if config.read_mode is ReadMode.ALL:
            return True

        # スレッドは親チャンネルが対象なら読む
        channel_ids = {message.channel.id}
        parent_id = getattr(message.channel, "parent_id", None)
        if parent_id is not None:
            channel_ids.add(parent_id)

        registered = await self.bot.db.get_read_channels(message.guild.id)
        if channel_ids & registered:
            return True

        if config.read_mode is ReadMode.JOINED:
            return config.joined_text_channel_id in channel_ids
        return False

    async def _build_text(self, message: discord.Message, config: GuildConfig) -> str:
        """読み上げる文章を作る。読むものが無ければ空文字。"""
        assert message.guild is not None
        rules = self.bot.text_rules(config)

        body = await self.bot.dictionary.render(
            message.guild.id, message.content, rules, _mention_map(message)
        )
        extras = _describe_extras(message, config)

        # 本文も添付も無いもの (埋め込みだけの BOT 投稿など) は読み上げない。
        # ここで弾かないと read_name が ON のとき発言者名だけを喋ってしまう。
        if not body and not extras:
            return ""

        speaker_name: str | None = None
        if config.read_name:
            speaker_name = await self._read_name(message, rules)

        return compose_speech(body, speaker_name=speaker_name, extras=extras)

    async def _read_name(self, message: discord.Message, rules: TextRules) -> str:
        """表示名も辞書を通す (ニックネームの読みを登録できるように)。"""
        assert message.guild is not None
        name = await self.bot.dictionary.apply(
            message.guild.id, message.author.display_name
        )
        return finalize(name, TextRules(max_length=20, skip_url=rules.skip_url))

    # ----------------------------------------------------------- 入退室・切断

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """入退室の読み上げ・自動退出・BOT 自身の切断の後始末。"""
        guild = member.guild
        player = self.bot.tts.get(guild.id)

        # BOT 自身が (キックなどで) 切断された場合はプレイヤーを片付ける
        if member.id == self.bot.user.id:  # type: ignore[union-attr]
            if after.channel is None:
                await self.bot.tts.destroy(guild.id)
                await self.bot.db.update_guild(guild.id, voice_channel_id=None)
            return

        if player is None or player.channel is None:
            return

        current = player.channel
        config = await self.bot.db.get_guild(guild.id)

        if config.read_join_leave and not member.bot:
            if before.channel != current and after.channel == current:
                await self.bot.speak_system(guild.id, f"{member.display_name}さんが入室したのだ")
            elif before.channel == current and after.channel != current:
                await self.bot.speak_system(guild.id, f"{member.display_name}さんが退室したのだ")

        # 誰かが BOT のいる VC から出たとき、人間が残っていなければ退出する
        if config.auto_leave and before.channel == current:
            remaining = [m for m in current.members if not m.bot]
            if not remaining:
                log.info("guild %s: 誰もいなくなったので自動退出します", guild.id)
                await self.bot.tts.destroy(guild.id)
                await self.bot.db.update_guild(guild.id, voice_channel_id=None)
                await self._notify_auto_leave(guild, config)

    async def _notify_auto_leave(self, guild: discord.Guild, config: GuildConfig) -> None:
        """/join を実行したチャンネルに自動退出を知らせる。送れなくても無視する。"""
        channel_id = config.joined_text_channel_id
        if channel_id is None:
            return
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.abc.Messageable):
            try:
                await channel.send(
                    embed=discord.Embed(
                        title="自動退出しました",
                        description="ボイスチャンネルが空になったため退出しました。",
                        colour=discord.Colour(0x8FD14F),
                    )
                )
            except discord.HTTPException:
                pass


def _mention_map(message: discord.Message) -> MentionMap:
    """本文中のメンションを名前で読むための対応表を作る。"""
    guild = message.guild
    mapping = MentionMap()

    for user in message.mentions:
        name = getattr(user, "display_name", user.name)
        mapping.users[user.id] = name
    for role in message.role_mentions:
        mapping.roles[role.id] = role.name
    for channel in message.channel_mentions:
        mapping.channels[channel.id] = getattr(channel, "name", "チャンネル")

    # サーバーのメンバー情報が取れれば、そのサーバーでの表示名 (ニックネーム) を優先する
    if guild is not None:
        for member_id in {m.id for m in message.mentions}:
            member = guild.get_member(member_id)
            if member is not None:
                mapping.users[member_id] = member.display_name

    return mapping


def _describe_extras(message: discord.Message, config: GuildConfig) -> list[str]:
    """本文のあとに付ける読み (添付ファイル・スタンプ)。埋め込みは読まない。"""
    extras: list[str] = []
    if config.read_attachment:
        if message.attachments:
            # 件数は読まない (画像を連投しても「てんぷふぁいる」1 回で済ませる)
            extras.append(ATTACHMENT_PLACEHOLDER)
        if message.stickers:
            extras.append("スタンプ")
    return extras


async def setup(bot: NexusBot) -> None:
    await bot.add_cog(ReaderCog(bot))
