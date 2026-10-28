"""接続系コマンド: /join /disconnect /skip /clear /status /help"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import CREDIT, NexusBot
from ..models import ReadMode

log = logging.getLogger(__name__)

ACCENT = discord.Colour(0x8FD14F)  # 埋め込みの色 (ずんだ色)。他の Cog も同じ値


class ConnectionCog(commands.Cog):
    def __init__(self, bot: NexusBot) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ join

    @app_commands.command(name="join", description="ボイスチャンネルに参加して読み上げを開始します")
    @app_commands.describe(channel="参加させたいボイスチャンネル (省略時は自分が今いるチャンネル)")
    @app_commands.guild_only()
    async def join(
        self,
        interaction: discord.Interaction,
        channel: discord.VoiceChannel | None = None,
    ) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        # channel 省略時は、実行した人が今いる VC に入る
        target = channel
        if target is None:
            member = interaction.user
            if isinstance(member, discord.Member) and member.voice and member.voice.channel:
                target = member.voice.channel  # type: ignore[assignment]

        if target is None:
            await interaction.response.send_message(
                "ボイスチャンネルに入ってから実行するか、`channel` を指定してください。",
                ephemeral=True,
            )
            return

        permissions = target.permissions_for(guild.me)
        if not permissions.connect or not permissions.speak:
            await interaction.response.send_message(
                f"{target.mention} に接続・発言する権限がありません。",
                ephemeral=True,
            )
            return

        # VC への接続は 3 秒を超えることがあるので、先に応答を保留しておく
        await interaction.response.defer()

        # 接続済みなら移動だけ。聞く必要はないのでスピーカーミュートで入る
        voice_client = guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
            if voice_client.channel != target:
                await voice_client.move_to(target)
        else:
            voice_client = await target.connect(self_deaf=True)

        self.bot.tts.create(guild.id, voice_client)  # type: ignore[arg-type]

        # /join を実行したテキストチャンネルを読み上げ対象として覚える (joined モード用)
        text_channel_id = interaction.channel_id
        await self.bot.db.update_guild(
            guild.id,
            voice_channel_id=target.id,
            joined_text_channel_id=text_channel_id,
            enabled=True,
        )

        config = await self.bot.db.get_guild(guild.id)
        speaker = await self.bot.voicevox.describe_speaker(
            self.bot.settings.default_speaker_id
        )

        embed = discord.Embed(
            title="接続しました",
            colour=ACCENT,
            description="読み上げを開始します。`/disconnect` で退出します。",
        )
        embed.add_field(name="ボイスチャンネル", value=target.mention, inline=True)
        embed.add_field(
            name="読み上げ対象",
            value=_describe_read_mode(config.read_mode, text_channel_id),
            inline=True,
        )
        embed.add_field(name="声", value=speaker, inline=True)
        embed.set_footer(text=CREDIT)
        await interaction.followup.send(embed=embed)

        await self.bot.speak_system(guild.id, "接続したのだ")

    # ------------------------------------------------------------ disconnect

    @app_commands.command(name="disconnect", description="ボイスチャンネルから退出します")
    @app_commands.guild_only()
    async def disconnect(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        destroyed = await self.bot.tts.destroy(guild.id)

        # プレイヤーが無いのに VC には残っている場合の保険
        voice_client = guild.voice_client
        if isinstance(voice_client, discord.VoiceClient) and voice_client.is_connected():
            await voice_client.disconnect(force=True)
            destroyed = True

        if not destroyed:
            await interaction.response.send_message(
                "ボイスチャンネルに接続していません。", ephemeral=True
            )
            return

        await self.bot.db.update_guild(guild.id, voice_channel_id=None)
        await interaction.response.send_message(
            embed=discord.Embed(title="退出しました", colour=ACCENT)
        )

    # ------------------------------------------------------------ skip/clear

    @app_commands.command(name="skip", description="読み上げ中の 1 件をスキップします")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        player = self.bot.tts.get(interaction.guild.id)
        if player is None:
            await interaction.response.send_message(
                "ボイスチャンネルに接続していません。", ephemeral=True
            )
            return

        if player.skip():
            await interaction.response.send_message("スキップしました。")
        else:
            await interaction.response.send_message(
                "いま読み上げているものはありません。", ephemeral=True
            )

    @app_commands.command(name="clear", description="読み上げ待ちのキューをすべて消します")
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        player = self.bot.tts.get(interaction.guild.id)
        if player is None:
            await interaction.response.send_message(
                "ボイスチャンネルに接続していません。", ephemeral=True
            )
            return

        dropped = player.clear()
        player.skip()  # 再生中の 1 件も止める
        await interaction.response.send_message(f"キューを空にしました ({dropped} 件破棄)。")

    # ---------------------------------------------------------------- status

    @app_commands.command(name="status", description="現在の接続状況と設定を表示します")
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction) -> None:
        assert interaction.guild is not None
        guild = interaction.guild

        config = await self.bot.db.get_guild(guild.id)
        player = self.bot.tts.get(guild.id)
        channels = await self.bot.db.get_read_channels(guild.id)
        speaker = await self.bot.voicevox.describe_speaker(
            self.bot.settings.default_speaker_id
        )

        embed = discord.Embed(title="NexusBox の状態", colour=ACCENT)

        if player is not None and player.channel is not None:
            embed.add_field(name="接続先", value=player.channel.mention, inline=True)
            embed.add_field(
                name="キュー",
                value=f"{player.queue_size} 件待機 / {'再生中' if player.is_playing else '待機中'}",
                inline=True,
            )
        else:
            embed.add_field(name="接続先", value="未接続", inline=True)
            embed.add_field(name="キュー", value="-", inline=True)

        embed.add_field(
            name="読み上げ", value="ON" if config.enabled else "OFF", inline=True
        )
        embed.add_field(
            name="対象チャンネル",
            value=_describe_read_mode(config.read_mode, config.joined_text_channel_id)
            + (f"\n登録済み {len(channels)} 件" if channels else ""),
            inline=False,
        )
        params = self.bot.settings.voice_params
        embed.add_field(name="声 (固定)", value=speaker, inline=True)
        embed.add_field(
            name="話速/音高/抑揚/音量",
            value=f"{params.speed:.2f} / {params.pitch:+.2f} / "
            f"{params.intonation:.2f} / {params.volume:.2f}",
            inline=True,
        )

        # 合成キャッシュの効き具合 (起動してからの累計)
        cache = self.bot.tts.cache
        total = cache.hits + cache.misses
        hit_rate = f"{cache.hits / total * 100:.0f}%" if total else "-"
        embed.add_field(
            name="VOICEVOX ENGINE",
            value=(
                f"v{self.bot.engine_version}\n"
                f"`{self.bot.voicevox.base_url}`"
            ),
            inline=False,
        )
        embed.add_field(
            name="キャッシュ",
            value=f"{len(cache)} 件 / ヒット率 {hit_rate}",
            inline=True,
        )
        embed.add_field(
            name="読み上げ中のサーバー", value=str(self.bot.tts.active_guilds), inline=True
        )
        embed.set_footer(text=CREDIT)

        await interaction.response.send_message(embed=embed)

    # ------------------------------------------------------------------ help

    @app_commands.command(name="help", description="コマンド一覧を表示します")
    async def help_command(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="NexusBox コマンド一覧",
            colour=ACCENT,
            description="ずんだもんの声でテキストチャンネルを読み上げる BOT です。",
        )
        embed.add_field(
            name="接続",
            value=(
                "`/join [channel]` 参加して読み上げ開始\n"
                "`/disconnect` 退出\n"
                "`/skip` 今の 1 件をスキップ\n"
                "`/clear` 読み上げ待ちを全消去\n"
                "`/status` 状態表示"
            ),
            inline=False,
        )
        embed.add_field(
            name="読み上げ対象チャンネル",
            value=(
                "`/channel add <channel>` 対象に追加\n"
                "`/channel remove <channel>` 対象から外す\n"
                "`/channel list` 一覧\n"
                "`/channel clear` 全解除\n"
                "`/config read_mode <joined|listed|all>` 対象の決め方\n"
                "接続中のボイスチャンネルのチャットは、設定に関わらず常に読み上げます"
            ),
            inline=False,
        )
        embed.add_field(
            name="読み上げの切り替え",
            value=(
                "`/tts` 読み上げ全体の ON/OFF (引数省略で反転)\n"
                "`/mute <user>` / `/unmute <user>` 特定ユーザーを読まない\n"
                "`/ignore-me` 自分の発言を読ませない (トグル)\n"
                "`/config bot_messages <True/False>` 他の BOT の発言を読むか\n"
                "`/config show` 設定一覧"
            ),
            inline=False,
        )
        embed.add_field(
            name="読み方辞書",
            value=(
                "`/dict add <表記> <読み> [accent] [scope]` 登録\n"
                "`/dict remove <表記> [scope]` 解除\n"
                "`/dict list [scope]` / `/dict search <語>` 一覧・検索\n"
                "`/dict test <文章>` 読み上げテキストの確認\n"
                "`/dict export` / `/dict import <file>` JSON で入出力\n"
                "scope は `server` (このサーバーのみ) と `global` (ENGINE 全体) から選べます"
            ),
            inline=False,
        )
        embed.set_footer(text=CREDIT)
        await interaction.response.send_message(embed=embed, ephemeral=True)


def _describe_read_mode(mode: ReadMode, joined_channel_id: int | None) -> str:
    """読み上げ対象の説明文 (/join と /status の表示用)。"""
    if mode is ReadMode.ALL:
        return "見えているすべてのテキストチャンネル"
    if mode is ReadMode.LISTED:
        return "`/channel add` で登録したチャンネル"
    if joined_channel_id:
        return f"<#{joined_channel_id}> (+ 登録チャンネル)"
    return "`/join` を実行したチャンネル"


async def setup(bot: NexusBot) -> None:
    await bot.add_cog(ConnectionCog(bot))
