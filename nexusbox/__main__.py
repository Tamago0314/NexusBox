"""エントリポイント: `python -m nexusbox`"""

from __future__ import annotations

import asyncio
import logging
import sys

import discord

from .bot import NexusBot, format_command_error, reply_error
from .config import load_settings


def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # ライブラリの INFO ログは多すぎるので警告以上だけ出す
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


async def main() -> None:
    settings = load_settings()
    setup_logging(settings.log_level)
    log = logging.getLogger("nexusbox")

    bot = NexusBot(settings)

    # スラッシュコマンドで起きた例外は、ここでまとめて日本語のエラー返信にする
    async def on_app_command_error(
        interaction: discord.Interaction, error: Exception
    ) -> None:
        await reply_error(interaction, format_command_error(error))

    bot.tree.on_error = on_app_command_error  # type: ignore[assignment]

    try:
        await bot.start(settings.discord_token)
    except discord.LoginFailure:
        log.error("DISCORD_TOKEN が不正です。.env を確認してください。")
        raise
    except discord.PrivilegedIntentsRequired:
        log.error(
            "特権インテントが有効になっていません。Discord Developer Portal の Bot 設定で "
            "MESSAGE CONTENT INTENT と SERVER MEMBERS INTENT を有効にしてください。"
        )
        raise
    finally:
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
