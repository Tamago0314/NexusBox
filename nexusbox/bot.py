"""BOT 本体。各サービスの生成と Cog のロードを担当する。"""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import Settings
from .db import Database
from .dictionary import DictionaryError, DictionaryService
from .models import GuildConfig, VoiceParams
from .player import SpeechItem, TTSManager
from .textproc import TextRules
from .voicevox import VoicevoxClient, VoicevoxError

log = logging.getLogger(__name__)

#: 起動時に読み込む Cog (スラッシュコマンドとイベント処理)
EXTENSIONS = (
    "nexusbox.cogs.connection",
    "nexusbox.cogs.reader",
    "nexusbox.cogs.channels",
    "nexusbox.cogs.settings",
    "nexusbox.cogs.dictionary",
)

#: VOICEVOX の利用規約で必須のクレジット表記
CREDIT = "VOICEVOX:ずんだもん"


class NexusBot(commands.Bot):
    def __init__(self, settings: Settings) -> None:
        # message_content と members は特権インテント。Developer Portal での有効化も必要
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        intents.guilds = True

        # スラッシュコマンドだけ使うので、プレフィックスは形だけ。
        # BOT の発言でメンション通知が飛ばないようにしておく。
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        self.settings = settings
        self.db = Database(settings.database_path)
        self.voicevox = VoicevoxClient(
            settings.voicevox_base_url, concurrency=settings.synthesis_concurrency
        )
        self.dictionary = DictionaryService(self.db, self.voicevox)
        self.tts = TTSManager(
            self.voicevox,
            cache_size=settings.cache_size,
            max_queue_size=settings.max_queue_size,
        )
        # /status で表示する ENGINE のバージョン (setup_hook で埋める)
        self.engine_version: str = "不明"

    # ------------------------------------------------------------- ライフサイクル

    async def setup_hook(self) -> None:
        """Discord に接続する前の準備。ENGINE が起動するまでここで待つ。"""
        await self.db.connect()
        await self.voicevox.start()

        self.engine_version = await self.voicevox.wait_until_ready()
        await self.voicevox.speakers()  # 話者名をキャッシュしておく
        await self.dictionary.restore_global_dict()

        for extension in EXTENSIONS:
            await self.load_extension(extension)
            log.debug("loaded extension %s", extension)

        await self._sync_commands()

    async def _sync_commands(self) -> None:
        """スラッシュコマンドを Discord に登録する。

        ギルド指定なら即時反映、グローバル同期は反映に最大 1 時間かかる。
        """
        if self.settings.guild_ids:
            for guild_id in self.settings.guild_ids:
                guild = discord.Object(id=guild_id)
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("guild %s に %d 個のコマンドを同期しました", guild_id, len(synced))
        else:
            synced = await self.tree.sync()
            log.info("グローバルに %d 個のコマンドを同期しました", len(synced))

    async def on_ready(self) -> None:
        log.info("logged in as %s (%s)", self.user, getattr(self.user, "id", "?"))
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.listening, name="/join で読み上げ開始"
            )
        )

    async def close(self) -> None:
        await self.tts.destroy_all()
        await self.voicevox.close()
        await self.db.close()
        await super().close()

    # ------------------------------------------------------------------ ヘルパ

    @staticmethod
    def text_rules(config: GuildConfig) -> TextRules:
        """ギルド設定からテキスト整形のルールだけを取り出す。"""
        return TextRules(
            max_length=config.max_length,
            skip_url=config.skip_url,
            skip_codeblock=config.skip_codeblock,
            read_emoji=config.read_emoji,
        )

    @property
    def voice(self) -> tuple[int, VoiceParams]:
        """読み上げに使う声。BOT 全体で固定で、変更は .env のみ。"""
        return self.settings.default_speaker_id, self.settings.voice_params

    async def speak_system(self, guild_id: int, text: str) -> bool:
        """入退室通知などを読み上げる。未接続か読み上げ OFF なら False。"""
        player = self.tts.get(guild_id)
        if player is None:
            return False
        config = await self.db.get_guild(guild_id)
        if not config.enabled:
            return False
        speaker_id, params = self.voice
        return await player.enqueue(
            SpeechItem(text=text, speaker_id=speaker_id, params=params)
        )


async def reply_error(interaction: discord.Interaction, message: str) -> None:
    """コマンドのエラー返信 (本人にだけ見える)。"""
    embed = discord.Embed(
        title="エラー", description=message, colour=discord.Colour.red()
    )
    # defer 済みなら最初の応答は使えないので followup で送る
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


def format_command_error(error: BaseException) -> str:
    """例外をユーザー向けの日本語に変換する。"""
    from discord import app_commands

    # コマンド内で投げた例外は CommandInvokeError に包まれて届く
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original

    # 自前の例外と ValueError は、メッセージがそのまま見せられる日本語になっている
    if isinstance(error, (DictionaryError, VoicevoxError)):
        return str(error)
    if isinstance(error, app_commands.MissingPermissions):
        return "このコマンドを実行する権限がありません。"
    if isinstance(error, app_commands.CommandOnCooldown):
        return f"クールダウン中です。{error.retry_after:.0f} 秒待ってください。"
    if isinstance(error, ValueError):
        return str(error)

    log.exception("未処理のコマンドエラー", exc_info=error)
    return "予期しないエラーが発生しました。しばらくしてからもう一度お試しください。"
