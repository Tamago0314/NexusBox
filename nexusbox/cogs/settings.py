"""読み上げの切り替えと挙動設定: /tts /mute /unmute /ignore-me /config ..."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import NexusBot
from ..models import GuildConfig, ReadMode

ACCENT = discord.Colour(0x8FD14F)

MANAGE_GUILD = discord.Permissions(manage_guild=True)


def _onoff(value: bool) -> str:
    return "ON" if value else "OFF"


class SettingsCog(commands.Cog):
    # /config は既定で「サーバー管理」権限を持つ人だけに表示される
    config_group = app_commands.Group(
        name="config",
        description="読み上げの挙動を設定します",
        guild_only=True,
        default_permissions=MANAGE_GUILD,
    )

    def __init__(self, bot: NexusBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------- ON/OFF

    @app_commands.command(name="tts", description="読み上げ全体の ON/OFF を切り替えます")
    @app_commands.describe(enabled="省略すると現在の状態を反転します")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def tts(
        self, interaction: discord.Interaction, enabled: bool | None = None
    ) -> None:
        assert interaction.guild is not None
        config = await self.bot.db.get_guild(interaction.guild.id)
        value = (not config.enabled) if enabled is None else enabled
        await self.bot.db.update_guild(interaction.guild.id, enabled=value)

        # OFF にしたら、溜まっている読み上げ待ちも捨てる
        if not value:
            player = self.bot.tts.get(interaction.guild.id)
            if player is not None:
                player.clear()

        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"読み上げを **{_onoff(value)}** にしました。", colour=ACCENT
            )
        )

    # -------------------------------------------------------------- ミュート

    @app_commands.command(name="mute", description="指定ユーザーの発言を読み上げないようにします")
    @app_commands.describe(user="読み上げ対象から外すユーザー")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def mute(self, interaction: discord.Interaction, user: discord.User) -> None:
        assert interaction.guild is not None
        added = await self.bot.db.add_ignored_user(interaction.guild.id, user.id)
        message = (
            f"{user.mention} の発言を読み上げないようにしました。"
            if added
            else f"{user.mention} はすでにミュート済みです。"
        )
        await interaction.response.send_message(
            embed=discord.Embed(description=message, colour=ACCENT)
        )

    @app_commands.command(name="unmute", description="ミュートを解除します")
    @app_commands.describe(user="ミュートを解除するユーザー")
    @app_commands.guild_only()
    @app_commands.default_permissions(manage_guild=True)
    async def unmute(self, interaction: discord.Interaction, user: discord.User) -> None:
        assert interaction.guild is not None
        removed = await self.bot.db.remove_ignored_user(interaction.guild.id, user.id)
        message = (
            f"{user.mention} のミュートを解除しました。"
            if removed
            else f"{user.mention} はミュートされていません。"
        )
        await interaction.response.send_message(
            embed=discord.Embed(description=message, colour=ACCENT)
        )

    # 自分自身のことなので、権限が無くても誰でも使える
    @app_commands.command(
        name="ignore-me", description="自分の発言を読み上げるかどうかを切り替えます"
    )
    @app_commands.guild_only()
    async def ignore_me(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        ignored = await self.bot.db.get_ignored_users(interaction.guild.id)

        if interaction.user.id in ignored:
            await self.bot.db.remove_ignored_user(interaction.guild.id, interaction.user.id)
            message = "あなたの発言を読み上げるようにしました。"
        else:
            await self.bot.db.add_ignored_user(interaction.guild.id, interaction.user.id)
            message = "あなたの発言を読み上げないようにしました。"

        await interaction.response.send_message(
            embed=discord.Embed(description=message, colour=ACCENT), ephemeral=True
        )

    # ------------------------------------------------------------- /config

    async def _set_flag(
        self, interaction: discord.Interaction, field: str, value: bool, label: str
    ) -> None:
        """ON/OFF 系の /config で共通の、保存して結果を返す処理。"""
        assert interaction.guild is not None
        await self.bot.db.update_guild(interaction.guild.id, **{field: value})
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"{label} を **{_onoff(value)}** にしました。", colour=ACCENT
            )
        )

    @config_group.command(name="read_mode", description="どのチャンネルを読み上げるか決めます")
    @app_commands.describe(mode="joined=/join したチャンネル / listed=登録チャンネルのみ / all=全部")
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="joined — /join したチャンネル + 登録チャンネル", value="joined"),
            app_commands.Choice(name="listed — /channel add で登録したチャンネルのみ", value="listed"),
            app_commands.Choice(name="all — 見えているすべてのテキストチャンネル", value="all"),
        ]
    )
    async def read_mode(
        self, interaction: discord.Interaction, mode: app_commands.Choice[str]
    ) -> None:
        assert interaction.guild is not None
        await self.bot.db.update_guild(interaction.guild.id, read_mode=ReadMode(mode.value))
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"読み上げ対象モードを **{mode.value}** にしました。", colour=ACCENT
            )
        )

    @config_group.command(name="bot_messages", description="他の BOT の発言を読み上げるか")
    async def bot_messages(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "bot_messages", enabled, "他 BOT の読み上げ")

    @config_group.command(name="read_name", description="「〇〇さん」と発言者名を読むか")
    async def read_name(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "read_name", enabled, "発言者名の読み上げ")

    @config_group.command(name="read_join_leave", description="ボイスチャンネルの入退室を読むか")
    async def read_join_leave(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "read_join_leave", enabled, "入退室の読み上げ")

    @config_group.command(name="auto_leave", description="VC に誰もいなくなったら自動退出するか")
    async def auto_leave(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "auto_leave", enabled, "自動退出")

    @config_group.command(
        name="skip_url", description="URL を「ゆーあーるえるしょうりゃく」と省略して読むか"
    )
    async def skip_url(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "skip_url", enabled, "URL の省略")

    @config_group.command(name="skip_codeblock", description="コードブロックを省略して読むか")
    async def skip_codeblock(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "skip_codeblock", enabled, "コードブロックの省略")

    @config_group.command(name="read_emoji", description="カスタム絵文字を名前で読むか")
    async def read_emoji(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "read_emoji", enabled, "カスタム絵文字の読み上げ")

    @config_group.command(
        name="read_attachment", description="添付ファイルを「てんぷふぁいる」と読むか"
    )
    async def read_attachment(self, interaction: discord.Interaction, enabled: bool) -> None:
        await self._set_flag(interaction, "read_attachment", enabled, "添付ファイルの読み上げ")

    @config_group.command(name="max_length", description="1 発言で読み上げる最大文字数")
    @app_commands.describe(length="1〜500 文字。超えた分は「以下略」になります")
    async def max_length(
        self,
        interaction: discord.Interaction,
        length: app_commands.Range[int, 1, 500],
    ) -> None:
        assert interaction.guild is not None
        await self.bot.db.update_guild(interaction.guild.id, max_length=length)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"読み上げの最大文字数を **{length}** 文字にしました。",
                colour=ACCENT,
            )
        )

    @config_group.command(name="show", description="現在の設定を一覧表示します")
    async def show(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.bot.db.get_guild(interaction.guild.id)
        channels = await self.bot.db.get_read_channels(interaction.guild.id)
        ignored = await self.bot.db.get_ignored_users(interaction.guild.id)
        speaker = await self.bot.voicevox.describe_speaker(
            self.bot.settings.default_speaker_id
        )
        params = self.bot.settings.voice_params

        embed = discord.Embed(title="現在の設定", colour=ACCENT)
        embed.add_field(name="読み上げ", value=_onoff(config.enabled), inline=True)
        embed.add_field(name="対象モード", value=config.read_mode.value, inline=True)
        embed.add_field(name="最大文字数", value=str(config.max_length), inline=True)
        embed.add_field(name="声 (固定)", value=speaker, inline=True)
        embed.add_field(
            name="話速/音高/抑揚/音量",
            value=f"{params.speed:.2f} / {params.pitch:+.2f} / "
            f"{params.intonation:.2f} / {params.volume:.2f}",
            inline=True,
        )
        embed.add_field(name="登録チャンネル", value=f"{len(channels)} 件", inline=True)
        embed.add_field(
            name="スイッチ",
            value="\n".join(
                [
                    f"他 BOT の発言: {_onoff(config.bot_messages)}",
                    f"発言者名: {_onoff(config.read_name)}",
                    f"入退室: {_onoff(config.read_join_leave)}",
                    f"自動退出: {_onoff(config.auto_leave)}",
                    f"URL 省略: {_onoff(config.skip_url)}",
                    f"コードブロック省略: {_onoff(config.skip_codeblock)}",
                    f"カスタム絵文字: {_onoff(config.read_emoji)}",
                    f"添付ファイル: {_onoff(config.read_attachment)}",
                ]
            ),
            inline=False,
        )
        # 埋め込みの文字数制限があるので、表示は 20 人まで
        if ignored:
            preview = " ".join(f"<@{uid}>" for uid in list(ignored)[:20])
            if len(ignored) > 20:
                preview += f" ほか {len(ignored) - 20} 人"
            embed.add_field(name="ミュート中", value=preview, inline=False)

        await interaction.response.send_message(embed=embed)

    @config_group.command(name="reset", description="このサーバーの設定を初期状態に戻します")
    async def reset(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        # 接続状態 (VC と /join したチャンネル) は戻さない
        defaults = GuildConfig(guild_id=interaction.guild.id)
        changes = {
            key: value
            for key, value in defaults.to_row().items()
            if key not in ("guild_id", "voice_channel_id", "joined_text_channel_id")
        }
        changes["read_mode"] = defaults.read_mode
        await self.bot.db.update_guild(interaction.guild.id, **changes)
        await interaction.response.send_message(
            embed=discord.Embed(
                description="設定を初期状態に戻しました (辞書と読み上げ対象チャンネルは残ります)。",
                colour=ACCENT,
            )
        )


async def setup(bot: NexusBot) -> None:
    await bot.add_cog(SettingsCog(bot))
