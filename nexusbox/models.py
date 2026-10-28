"""BOT が扱うデータ構造。"""

from __future__ import annotations

from dataclasses import dataclass, field, fields
from enum import StrEnum
from typing import Any, Mapping, Self


class ReadMode(StrEnum):
    """どのテキストチャンネルを読み上げ対象にするか。"""

    #: /join を実行したチャンネルのみ
    JOINED = "joined"
    #: /channel add で登録したチャンネルのみ
    LISTED = "listed"
    #: BOT から見える全テキストチャンネル
    ALL = "all"


class DictScope(StrEnum):
    """読み方辞書の効く範囲。"""

    #: このサーバー内だけで有効な置換辞書 (SQLite)
    SERVER = "server"
    #: VOICEVOX ENGINE のユーザー辞書 (全サーバー共通)
    GLOBAL = "global"


@dataclass(slots=True)
class VoiceParams:
    """VOICEVOX の audio_query に上書き適用する合成パラメータ。"""

    speed: float = 1.0
    pitch: float = 0.0
    intonation: float = 1.0
    volume: float = 1.0

    #: 各パラメータの (最小値, 最大値)
    RANGES = {
        "speed": (0.5, 2.0),
        "pitch": (-0.15, 0.15),
        "intonation": (0.0, 2.0),
        "volume": (0.0, 2.0),
    }

    def apply_to(self, query: dict[str, Any]) -> dict[str, Any]:
        """audio_query の結果を書き換える (同じ dict を返す)。"""
        query["speedScale"] = self.speed
        query["pitchScale"] = self.pitch
        query["intonationScale"] = self.intonation
        query["volumeScale"] = self.volume
        return query

    def cache_key(self) -> tuple[float, float, float, float]:
        return (self.speed, self.pitch, self.intonation, self.volume)

    @classmethod
    def clamp(cls, name: str, value: float) -> float:
        """value を RANGES[name] の範囲に収める。"""
        low, high = cls.RANGES[name]
        return max(low, min(high, value))


@dataclass(slots=True)
class GuildConfig:
    """サーバーごとの読み上げ設定。カラム名と 1:1 で対応する。"""

    guild_id: int

    # 接続状態
    enabled: bool = True
    read_mode: ReadMode = ReadMode.JOINED
    voice_channel_id: int | None = None
    joined_text_channel_id: int | None = None

    # 読み上げ挙動
    max_length: int = 120
    read_name: bool = False
    bot_messages: bool = False
    skip_url: bool = True
    skip_codeblock: bool = True
    read_emoji: bool = True
    read_attachment: bool = True
    read_join_leave: bool = True
    auto_leave: bool = True

    @classmethod
    def column_names(cls) -> list[str]:
        return [f.name for f in fields(cls)]

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> Self:
        """DB の行から作る。0/1 は bool に、文字列は ReadMode に戻す。"""
        data: dict[str, Any] = {}
        for f in fields(cls):
            value = row[f.name]
            if f.name == "read_mode":
                data[f.name] = ReadMode(value)
            elif f.name in _BOOL_FIELDS:
                data[f.name] = bool(value)
            else:
                data[f.name] = value
        return cls(**data)

    def to_row(self) -> dict[str, Any]:
        """DB に書ける形 (bool は 0/1、ReadMode は文字列) にする。"""
        row: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, bool):
                row[f.name] = int(value)
            elif isinstance(value, ReadMode):
                row[f.name] = value.value
            else:
                row[f.name] = value
        return row


#: SQLite に 0/1 で保存している項目 (読み出し時に bool へ戻す)
_BOOL_FIELDS = {
    "enabled",
    "read_name",
    "bot_messages",
    "skip_url",
    "skip_codeblock",
    "read_emoji",
    "read_attachment",
    "read_join_leave",
    "auto_leave",
}


@dataclass(slots=True)
class DictEntry:
    """読み方辞書の 1 エントリ。"""

    #: 置き換え前の表記
    surface: str
    #: 読み (カタカナ)
    reading: str
    scope: DictScope
    #: global 辞書では None
    guild_id: int | None = None
    #: アクセント核の位置。global 辞書でだけ使う
    accent_type: int | None = None
    priority: int = 5
    #: ENGINE のユーザー辞書での ID。global 辞書でだけ使う
    word_uuid: str | None = None
    created_by: int | None = None
    created_at: str | None = None
