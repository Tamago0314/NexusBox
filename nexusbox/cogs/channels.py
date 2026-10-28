"""読み上げ対象チャンネルの管理: /channel add|remove|list|clear"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import NexusBot
from ..models import ReadMode

ACCENT = discord.Colour(0x8FD14F)

#: テキストを投稿できるチャンネル種別
TextLike = discord.TextChannel | discord.VoiceChannel | discord.Thread | discord.StageChannel


class ChannelsCog(commands.Cog):
    # 既定では「サーバー管理」権限を持つ人だけに表示される
    channel_group = app_commands.Group(
        name="channel",
        description="読み上げ対象のテキストチャンネルを管理します",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: NexusBot) -> None:
        self.bot = bot

    @channel_group.command(name="add", description="読み上げ対象にチャンネルを追加します")
    @app_commands.describe(channel="追加するチャンネル (省略時はこのチャンネル)")
    async def add(
        self, interaction: discord.Interaction, channel: TextLike | None = None
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if target is None:
            await interaction.response.send_message(
                "チャンネルを特定できませんでした。", ephemeral=True
            )
            return

        added = await self.bot.db.add_read_channel(interaction.guild.id, target.id)
        message = (
            f"{target.mention} を読み上げ対象に追加しました。"
            if added
            else f"{target.mention} はすでに読み上げ対象です。"
        )
        await interaction.response.send_message(
            embed=discord.Embed(description=message, colour=ACCENT)
        )

    @channel_group.command(name="remove", description="読み上げ対象からチャンネルを外します")
    @app_commands.describe(channel="外すチャンネル (省略時はこのチャンネル)")
    async def remove(
        self, interaction: discord.Interaction, channel: TextLike | None = None
    ) -> None:
        assert interaction.guild is not None
        target = channel or interaction.channel
        if target is None:
            await interaction.response.send_message(
                "チャンネルを特定できませんでした。", ephemeral=True
            )
            return

        removed = await self.bot.db.remove_read_channel(interaction.guild.id, target.id)

        # /join したチャンネルは登録とは別に覚えているので、そちらも外す
        config = await self.bot.db.get_guild(interaction.guild.id)
        if config.joined_text_channel_id == target.id:
            await self.bot.db.update_guild(
                interaction.guild.id, joined_text_channel_id=None
            )
            removed = True

        message = (
            f"{target.mention} を読み上げ対象から外しました。"
            if removed
            else f"{target.mention} は読み上げ対象ではありません。"
        )
        await interaction.response.send_message(
            embed=discord.Embed(description=message, colour=ACCENT)
        )

    @channel_group.command(name="list", description="読み上げ対象のチャンネル一覧を表示します")
    async def list_channels(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        config = await self.bot.db.get_guild(interaction.guild.id)
        channels = await self.bot.db.get_read_channels(interaction.guild.id)

        embed = discord.Embed(title="読み上げ対象チャンネル", colour=ACCENT)

        if config.read_mode is ReadMode.ALL:
            embed.description = (
                "**モード: all** — BOT から見えるすべてのテキストチャンネルを読み上げます。\n"
                "`/config read_mode listed` で登録制に切り替えられます。"
            )
        else:
            mode_label = "joined" if config.read_mode is ReadMode.JOINED else "listed"
            lines = [f"**モード: {mode_label}**"]
            if config.read_mode is ReadMode.JOINED and config.joined_text_channel_id:
                lines.append(f"<#{config.joined_text_channel_id}> (`/join` を実行したチャンネル)")
            if channels:
                lines.extend(f"<#{cid}>" for cid in sorted(channels))
            elif config.read_mode is ReadMode.LISTED:
                lines.append("_登録されているチャンネルはありません_")
            embed.description = "\n".join(lines)

        await interaction.response.send_message(embed=embed)

    @channel_group.command(name="clear", description="登録した読み上げ対象チャンネルをすべて解除します")
    async def clear(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        count = await self.bot.db.clear_read_channels(interaction.guild.id)
        await interaction.response.send_message(
            embed=discord.Embed(
                description=f"登録済みチャンネルを {count} 件解除しました。", colour=ACCENT
            )
        )


async def setup(bot: NexusBot) -> None:
    await bot.add_cog(ChannelsCog(bot))
