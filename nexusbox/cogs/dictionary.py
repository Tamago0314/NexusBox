"""読み方辞書: /dict add|remove|list|search|test|export|import"""

from __future__ import annotations

import io
import json
import logging

import discord
from discord import app_commands
from discord.ext import commands

from ..bot import CREDIT, NexusBot
from ..dictionary import DictionaryError, validate_reading, validate_surface
from ..models import DictEntry, DictScope
from ..player import SpeechItem

log = logging.getLogger(__name__)

ACCENT = discord.Colour(0x8FD14F)
#: /dict list の 1 ページの件数
PER_PAGE = 15
#: /dict import で一度に取り込める件数
MAX_IMPORT = 500

SCOPE_CHOICES = [
    app_commands.Choice(name="server — このサーバーだけで有効 (既定)", value="server"),
    app_commands.Choice(name="global — ENGINE 全体 (全サーバー共通)", value="global"),
]


def _scope_of(choice: app_commands.Choice[str] | None) -> DictScope:
    """scope 引数を DictScope にする。省略時は server。"""
    return DictScope(choice.value) if choice else DictScope.SERVER


def _require_permission(interaction: discord.Interaction, scope: DictScope) -> None:
    """辞書を書き換える権限があるか確認し、無ければ DictionaryError。

    global は全サーバーに効くので「サーバー管理」、server は「メッセージの管理」で足りる。
    """
    user = interaction.user
    if not isinstance(user, discord.Member):
        raise DictionaryError("サーバー内で実行してください。")

    perms = user.guild_permissions
    if scope is DictScope.GLOBAL and not perms.manage_guild:
        raise DictionaryError(
            "global 辞書の編集には「サーバー管理」権限が必要です。"
            "このサーバーだけで使うなら `scope: server` を指定してください。"
        )
    if scope is DictScope.SERVER and not (perms.manage_messages or perms.manage_guild):
        raise DictionaryError("辞書の編集には「メッセージの管理」権限が必要です。")


class DictPaginator(discord.ui.View):
    """/dict list 用のページ送り。"""

    def __init__(self, entries: list[DictEntry], scope: DictScope, author_id: int) -> None:
        super().__init__(timeout=180)
        self.entries = entries
        self.scope = scope
        self.author_id = author_id
        self.page = 0
        self.pages = max(1, -(-len(entries) // PER_PAGE))  # 切り上げの割り算
        self._update_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "このページ送りはコマンドを実行した人だけが操作できます。", ephemeral=True
            )
            return False
        return True

    def _update_buttons(self) -> None:
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.pages - 1

    def embed(self) -> discord.Embed:
        chunk = self.entries[self.page * PER_PAGE : (self.page + 1) * PER_PAGE]
        lines = [
            f"`{entry.surface}` → **{entry.reading}**"
            + (f"  (アクセント {entry.accent_type})" if entry.accent_type is not None else "")
            for entry in chunk
        ]
        embed = discord.Embed(
            title=f"読み方辞書 ({self.scope.value})",
            description="\n".join(lines) or "_登録がありません_",
            colour=ACCENT,
        )
        embed.set_footer(text=f"{self.page + 1} / {self.pages} ページ・全 {len(self.entries)} 件")
        return embed

    @discord.ui.button(label="前へ", style=discord.ButtonStyle.secondary)
    async def previous(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = max(0, self.page - 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="次へ", style=discord.ButtonStyle.secondary)
    async def next(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.page = min(self.pages - 1, self.page + 1)
        self._update_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="閉じる", style=discord.ButtonStyle.danger)
    async def close(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(view=None)
        self.stop()


class DictionaryCog(commands.Cog):
    dict_group = app_commands.Group(
        name="dict",
        description="読み方の辞書を登録・解除します",
        guild_only=True,
    )

    def __init__(self, bot: NexusBot) -> None:
        self.bot = bot

    # -------------------------------------------------------------- 補完候補

    async def surface_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """/dict remove の表記の入力候補。"""
        if interaction.guild is None:
            return []

        # 入力途中の scope を見て候補を切り替える
        raw_scope = None
        if isinstance(interaction.namespace, object):
            raw_scope = getattr(interaction.namespace, "scope", None)
        scope = DictScope(raw_scope) if raw_scope in ("server", "global") else DictScope.SERVER

        entries = await self.bot.dictionary.list_entries(
            scope=scope, guild_id=interaction.guild.id, keyword=current or None
        )
        # Discord の制限: 候補は 25 件まで、表示名は 100 文字まで
        return [
            app_commands.Choice(name=f"{e.surface} → {e.reading}"[:100], value=e.surface)
            for e in entries[:25]
        ]

    # ------------------------------------------------------------------ add

    @dict_group.command(name="add", description="単語の読み方を登録します")
    @app_commands.describe(
        surface="読み方を変えたい表記 (例: 草)",
        reading="読み。カタカナ推奨 (例: クサ)",
        accent="アクセント核の位置。省略すると自動推定 (global のみ有効)",
        scope="server=このサーバーのみ / global=ENGINE 全体",
    )
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def add(
        self,
        interaction: discord.Interaction,
        surface: str,
        reading: str,
        accent: app_commands.Range[int, 0, 32] | None = None,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = _scope_of(scope)
        _require_permission(interaction, target)

        await interaction.response.defer()
        entry = await self.bot.dictionary.add(
            scope=target,
            guild_id=interaction.guild.id,
            surface=surface,
            reading=reading,
            accent_type=accent,
            created_by=interaction.user.id,
        )

        embed = discord.Embed(title="辞書に登録しました", colour=ACCENT)
        embed.add_field(name="表記", value=f"`{entry.surface}`", inline=True)
        embed.add_field(name="読み", value=entry.reading, inline=True)
        embed.add_field(name="スコープ", value=target.value, inline=True)
        if target is DictScope.GLOBAL:
            embed.add_field(
                name="アクセント",
                value=f"{entry.accent_type}" + ("" if accent is not None else " (自動推定)"),
                inline=True,
            )
            embed.set_footer(text="global 辞書は ENGINE 全体 = すべてのサーバーに効きます")
        await interaction.followup.send(embed=embed)

    # --------------------------------------------------------------- remove

    @dict_group.command(name="remove", description="登録した読み方を解除します")
    @app_commands.describe(surface="解除したい表記", scope="server / global")
    @app_commands.choices(scope=SCOPE_CHOICES)
    @app_commands.autocomplete(surface=surface_autocomplete)
    async def remove(
        self,
        interaction: discord.Interaction,
        surface: str,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = _scope_of(scope)
        _require_permission(interaction, target)

        await interaction.response.defer()
        removed = await self.bot.dictionary.remove(
            scope=target, guild_id=interaction.guild.id, surface=surface
        )

        message = (
            f"`{surface}` を {target.value} 辞書から解除しました。"
            if removed
            else f"`{surface}` は {target.value} 辞書に登録されていません。"
        )
        await interaction.followup.send(
            embed=discord.Embed(description=message, colour=ACCENT)
        )

    # ----------------------------------------------------------- list/search

    @dict_group.command(name="list", description="登録済みの読み方を一覧表示します")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def list_entries(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._send_list(interaction, _scope_of(scope), None)

    @dict_group.command(name="search", description="辞書を表記・読みで検索します")
    @app_commands.describe(keyword="検索キーワード")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def search(
        self,
        interaction: discord.Interaction,
        keyword: str,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        await self._send_list(interaction, _scope_of(scope), keyword)

    async def _send_list(
        self, interaction: discord.Interaction, scope: DictScope, keyword: str | None
    ) -> None:
        assert interaction.guild is not None
        entries = await self.bot.dictionary.list_entries(
            scope=scope, guild_id=interaction.guild.id, keyword=keyword
        )

        if not entries:
            text = (
                f"{scope.value} 辞書に「{keyword}」に一致する登録はありません。"
                if keyword
                else f"{scope.value} 辞書はまだ空です。`/dict add` で登録できます。"
            )
            await interaction.response.send_message(
                embed=discord.Embed(description=text, colour=ACCENT), ephemeral=True
            )
            return

        # 1 ページに収まるならボタンは付けない
        view = DictPaginator(entries, scope, interaction.user.id)
        await interaction.response.send_message(
            embed=view.embed(), view=view if view.pages > 1 else None
        )

    # ----------------------------------------------------------------- test

    @dict_group.command(
        name="test", description="実際にどう読まれるか確認します (辞書と整形を適用)"
    )
    @app_commands.describe(text="確認したい文章", speak="ボイスチャンネルで読み上げるか")
    async def test(
        self, interaction: discord.Interaction, text: str, speak: bool = False
    ) -> None:
        assert interaction.guild is not None
        # 読み上げないときは結果を本人にだけ見せる
        await interaction.response.defer(ephemeral=not speak)

        config = await self.bot.db.get_guild(interaction.guild.id)
        rules = self.bot.text_rules(config)
        rendered = await self.bot.dictionary.render(interaction.guild.id, text, rules)

        embed = discord.Embed(title="読み上げテキストの確認", colour=ACCENT)
        embed.add_field(name="入力", value=f"```\n{text[:1000]}\n```", inline=False)
        embed.add_field(
            name="読み上げ結果",
            value=f"```\n{rendered[:1000] or '(読み上げなし)'}\n```",
            inline=False,
        )
        embed.set_footer(text=CREDIT)
        await interaction.followup.send(embed=embed)

        if speak and rendered:
            player = self.bot.tts.get(interaction.guild.id)
            if player is not None:
                speaker_id, params = self.bot.voice
                await player.enqueue(
                    SpeechItem(text=rendered, speaker_id=speaker_id, params=params)
                )

    # ---------------------------------------------------------- export/import

    @dict_group.command(name="export", description="辞書を JSON ファイルで書き出します")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def export(
        self,
        interaction: discord.Interaction,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = _scope_of(scope)
        entries = await self.bot.dictionary.list_entries(
            scope=target, guild_id=interaction.guild.id
        )

        payload = {
            "scope": target.value,
            "entries": [
                {
                    "surface": e.surface,
                    "reading": e.reading,
                    "accent_type": e.accent_type,
                }
                for e in entries
            ],
        }
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

        await interaction.response.send_message(
            content=f"{target.value} 辞書 {len(entries)} 件を書き出しました。",
            file=discord.File(io.BytesIO(data), filename=f"nexusbox-dict-{target.value}.json"),
            ephemeral=True,
        )

    @dict_group.command(name="import", description="JSON ファイルから辞書を一括登録します")
    @app_commands.describe(file="/dict export で出力した JSON", scope="server / global")
    @app_commands.choices(scope=SCOPE_CHOICES)
    async def import_entries(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment,
        scope: app_commands.Choice[str] | None = None,
    ) -> None:
        assert interaction.guild is not None
        target = _scope_of(scope)
        _require_permission(interaction, target)

        if file.size > 1_000_000:
            raise DictionaryError("ファイルが大きすぎます (1MB まで)。")

        await interaction.response.defer()

        raw = await file.read()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DictionaryError(f"JSON として読み込めませんでした: {exc}") from exc

        # export の形式 {"entries": [...]} と、エントリの配列だけのものを受け付ける
        items = payload.get("entries") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise DictionaryError(
                '形式が違います。`{"entries": [{"surface": "...", "reading": "..."}]}` の形にしてください。'
            )
        if len(items) > MAX_IMPORT:
            raise DictionaryError(f"一度に取り込めるのは {MAX_IMPORT} 件までです。")

        # 1 件ずつ登録し、失敗したものは理由を集めて最後にまとめて見せる
        added = 0
        failed: list[str] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            surface = str(item.get("surface", ""))
            reading = str(item.get("reading", ""))
            accent = item.get("accent_type")
            try:
                validate_surface(surface)
                validate_reading(reading)
                await self.bot.dictionary.add(
                    scope=target,
                    guild_id=interaction.guild.id,
                    surface=surface,
                    reading=reading,
                    accent_type=int(accent) if isinstance(accent, int) else None,
                    created_by=interaction.user.id,
                )
                added += 1
            except DictionaryError as exc:
                failed.append(f"`{surface}`: {exc}")

        embed = discord.Embed(
            title="辞書を取り込みました",
            description=f"{target.value} 辞書に **{added} 件** 登録しました。",
            colour=ACCENT,
        )
        if failed:
            preview = "\n".join(failed[:10])
            if len(failed) > 10:
                preview += f"\n… ほか {len(failed) - 10} 件"
            embed.add_field(name=f"失敗 {len(failed)} 件", value=preview[:1000], inline=False)
        await interaction.followup.send(embed=embed)


async def setup(bot: NexusBot) -> None:
    await bot.add_cog(DictionaryCog(bot))
