"""読み方辞書 (2 層構成)。

server スコープ
    SQLite に持ち、合成前に単純なテキスト置換を行う。そのサーバー内だけで有効。
    メンバー名やサーバー内スラング向け。

global スコープ
    VOICEVOX ENGINE のユーザー辞書 (/user_dict_word) に登録する。形態素解析の
    段階で効くのでアクセントまで自然になる代わりに、ENGINE 全体 = 全サーバーに効く。
"""

from __future__ import annotations

import logging
import re
from typing import Iterable

from .db import Database
from .models import DictEntry, DictScope
from .textproc import MentionMap, TextRules, finalize, prepare
from .voicevox import VoicevoxClient, VoicevoxError

log = logging.getLogger(__name__)

#: 読みとして受け付ける文字 (カタカナ・長音・中黒・踊り字)
_KATAKANA_ONLY = re.compile(r"^[ァ-ヴー・ヽヾ]+$")
#: ぁ〜ゖ。カタカナとは文字コードが 0x60 ずれている
_HIRAGANA_RANGE = (0x3041, 0x3096)

MAX_SURFACE_LENGTH = 64
MAX_READING_LENGTH = 64


class DictionaryError(RuntimeError):
    """ユーザーにそのまま見せられる日本語のエラー。"""


def to_katakana(text: str) -> str:
    """ひらがなをカタカナに寄せる (読みの入力を楽にするため)。"""
    return "".join(
        chr(ord(ch) + 0x60) if _HIRAGANA_RANGE[0] <= ord(ch) <= _HIRAGANA_RANGE[1] else ch
        for ch in text
    )


def validate_surface(surface: str) -> str:
    """表記の前後の空白を除いて返す。空や長すぎるものは DictionaryError。"""
    surface = surface.strip()
    if not surface:
        raise DictionaryError("表記が空です。")
    if len(surface) > MAX_SURFACE_LENGTH:
        raise DictionaryError(f"表記は {MAX_SURFACE_LENGTH} 文字までです。")
    return surface


def validate_reading(reading: str) -> str:
    """読みをカタカナにそろえて返す。カタカナ以外が混ざると DictionaryError。"""
    reading = to_katakana(reading.strip())
    if not reading:
        raise DictionaryError("読みが空です。")
    if len(reading) > MAX_READING_LENGTH:
        raise DictionaryError(f"読みは {MAX_READING_LENGTH} 文字までです。")
    if not _KATAKANA_ONLY.match(reading):
        raise DictionaryError(
            "読みはカタカナ (またはひらがな) で入力してください。例: `ずんだもん` / `ズンダモン`"
        )
    return reading


def build_replacer(entries: Iterable[DictEntry]) -> tuple[re.Pattern[str] | None, dict[str, str]]:
    """置換用の正規表現と対応表を作る。長い表記が短い表記より先にマッチする。

    大文字小文字は区別しないので、対応表のキーは小文字にしてある。
    """
    mapping: dict[str, str] = {}
    surfaces: list[str] = []
    # 正規表現の | は左から順に試すので、長い表記を先に並べる
    for entry in sorted(entries, key=lambda e: len(e.surface), reverse=True):
        key = entry.surface.lower()
        if key in mapping:  # 大文字小文字違いの重複は先勝ち
            continue
        mapping[key] = entry.reading
        surfaces.append(entry.surface)

    if not surfaces:
        return None, mapping

    pattern = re.compile("|".join(re.escape(s) for s in surfaces), re.IGNORECASE)
    return pattern, mapping


class DictionaryService:
    """辞書の登録・解除と、読み上げテキストへの適用。"""

    def __init__(self, db: Database, client: VoicevoxClient) -> None:
        self._db = db
        self._client = client
        #: ギルド ID -> build_replacer の結果
        self._cache: dict[int, tuple[re.Pattern[str] | None, dict[str, str]]] = {}

    def invalidate(self, guild_id: int) -> None:
        """server 辞書を変えたら呼ぶ (次の適用時に作り直される)。"""
        self._cache.pop(guild_id, None)

    # ------------------------------------------------------------------ 適用

    async def _replacer(self, guild_id: int) -> tuple[re.Pattern[str] | None, dict[str, str]]:
        cached = self._cache.get(guild_id)
        if cached is not None:
            return cached
        entries = await self._db.list_dict_entries(guild_id)
        built = build_replacer(entries)
        self._cache[guild_id] = built
        return built

    async def apply(self, guild_id: int, text: str) -> str:
        """server 辞書で置換する。global 辞書は ENGINE 側で効くのでここでは触らない。"""
        pattern, mapping = await self._replacer(guild_id)
        if pattern is None:
            return text
        return pattern.sub(lambda m: mapping.get(m.group(0).lower(), m.group(0)), text)

    async def render(
        self,
        guild_id: int,
        content: str,
        rules: TextRules,
        mentions: MentionMap | None = None,
    ) -> str:
        """読み上げテキストを組み立てる (整形 -> 辞書置換 -> 仕上げ)。"""
        text = prepare(content, rules, mentions)
        text = await self.apply(guild_id, text)
        return finalize(text, rules)

    # ------------------------------------------------------------------ 登録

    async def add(
        self,
        *,
        scope: DictScope,
        guild_id: int,
        surface: str,
        reading: str,
        accent_type: int | None = None,
        created_by: int | None = None,
        priority: int = 5,
    ) -> DictEntry:
        """登録する。同じ表記が既にあれば上書き。"""
        surface = validate_surface(surface)
        reading = validate_reading(reading)

        if scope is DictScope.SERVER:
            # server スコープは単なるテキスト置換なのでアクセントは持たない
            entry = DictEntry(
                surface=surface,
                reading=reading,
                scope=scope,
                guild_id=guild_id,
                accent_type=None,
                priority=priority,
                created_by=created_by,
            )
            await self._db.upsert_dict_entry(entry)
            self.invalidate(guild_id)
            return entry

        # --- global: ENGINE のユーザー辞書へ ---
        if accent_type is None:
            accent_type = await self._client.estimate_accent_type(reading)

        # 登録済みなら ENGINE 側も上書きして、同じ単語が二重に登録されないようにする
        existing = await self._db.get_dict_entry(None, surface)
        word_uuid = existing.word_uuid if existing else None

        try:
            if word_uuid:
                await self._client.update_user_dict_word(
                    word_uuid, surface, reading, accent_type, priority=priority
                )
            else:
                word_uuid = await self._client.add_user_dict_word(
                    surface, reading, accent_type, priority=priority
                )
        except VoicevoxError as exc:
            raise DictionaryError(f"ENGINE への登録に失敗しました: {exc}") from exc

        entry = DictEntry(
            surface=surface,
            reading=reading,
            scope=scope,
            guild_id=None,
            accent_type=accent_type,
            priority=priority,
            word_uuid=word_uuid,
            created_by=created_by,
        )
        await self._db.upsert_dict_entry(entry)
        return entry

    async def remove(self, *, scope: DictScope, guild_id: int, surface: str) -> bool:
        """解除する。登録されていなければ False。"""
        surface = validate_surface(surface)
        key_guild = guild_id if scope is DictScope.SERVER else None  # global は NULL

        entry = await self._db.get_dict_entry(key_guild, surface)
        if entry is None:
            return False

        if scope is DictScope.GLOBAL and entry.word_uuid:
            try:
                await self._client.delete_user_dict_word(entry.word_uuid)
            except VoicevoxError as exc:
                # ENGINE 側に既に無いだけなら DB の行は消してよい
                log.warning("ENGINE のユーザー辞書削除に失敗 (%s): %s", surface, exc)

        removed = await self._db.delete_dict_entry(key_guild, surface)
        if scope is DictScope.SERVER:
            self.invalidate(guild_id)
        return removed

    async def list_entries(
        self, *, scope: DictScope, guild_id: int, keyword: str | None = None
    ) -> list[DictEntry]:
        key_guild = guild_id if scope is DictScope.SERVER else None
        return await self._db.list_dict_entries(key_guild, keyword=keyword)

    # ------------------------------------------------------------ 起動時の復元

    async def restore_global_dict(self) -> int:
        """ENGINE 側の辞書が失われていたら DB の内容から復元する。

        data/voicevox を消した・ENGINE を作り直した、などの場合に効く。
        再登録した件数を返す。
        """
        entries = await self._db.list_dict_entries(None)
        if not entries:
            return 0

        try:
            engine_dict = await self._client.get_user_dict()
        except VoicevoxError as exc:
            log.warning("ENGINE のユーザー辞書を取得できませんでした: %s", exc)
            return 0

        known_uuids = set(engine_dict)
        restored = 0
        for entry in entries:
            if entry.word_uuid and entry.word_uuid in known_uuids:
                continue  # ENGINE に残っているものはそのまま
            try:
                accent = entry.accent_type
                if accent is None:
                    accent = await self._client.estimate_accent_type(entry.reading)
                word_uuid = await self._client.add_user_dict_word(
                    entry.surface, entry.reading, accent, priority=entry.priority
                )
            except VoicevoxError as exc:
                log.warning("global 辞書の復元に失敗 (%s): %s", entry.surface, exc)
                continue

            entry.word_uuid = word_uuid
            entry.accent_type = accent
            await self._db.upsert_dict_entry(entry)
            restored += 1

        if restored:
            log.info("global 辞書を %d 件 ENGINE に復元しました", restored)
        return restored
