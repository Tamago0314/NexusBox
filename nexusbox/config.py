"""環境変数から読み込む BOT 設定。"""

from __future__ import annotations

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from .models import VoiceParams


class Settings(BaseSettings):
    """`.env` または環境変数の値。項目名は大文字小文字を区別しない。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    discord_token: str
    #: compose 内のサービス名で指定する
    voicevox_url: str = "http://voicevox:50021"

    database_path: str = "data/nexusbox.db"
    log_level: str = "INFO"

    #: 読み上げに使う話者 (3 = ずんだもん ノーマル)。BOT 全体で固定。
    default_speaker_id: int = 3

    #: 合成パラメータもサーバー共通で固定する。範囲外の値は起動時に丸められる。
    default_speed: float = 1.0
    default_pitch: float = 0.0
    default_intonation: float = 1.0
    default_volume: float = 1.0

    #: ENGINE への同時合成リクエスト数 (CPU では 2 前後が速い)
    synthesis_concurrency: int = 2
    #: 合成済み WAV の LRU キャッシュ件数
    cache_size: int = 256
    #: 1 ギルドあたりの読み上げキュー上限
    max_queue_size: int = 20

    #: 開発用。カンマ区切りのギルド ID を書くと、そのギルドにだけコマンドを即時同期する。
    #: list 型で受けると pydantic-settings が JSON として解釈して落ちるため、
    #: 文字列のまま受けて guild_ids で分解する。
    guild_ids_raw: str = Field(default="", validation_alias="guild_ids")

    @property
    def guild_ids(self) -> list[int]:
        """GUILD_IDS を数値のリストにする。空なら [] (= グローバル同期)。"""
        ids: list[int] = []
        for part in self.guild_ids_raw.replace(" ", "").split(","):
            if not part:
                continue
            try:
                ids.append(int(part))
            except ValueError as exc:
                raise ValueError(
                    f"GUILD_IDS に数値でない値が含まれています: {part!r}"
                ) from exc
        return ids

    @property
    def voice_params(self) -> VoiceParams:
        """.env で指定された合成パラメータ。VoiceParams の許容範囲に丸めて返す。"""
        return VoiceParams(
            speed=VoiceParams.clamp("speed", self.default_speed),
            pitch=VoiceParams.clamp("pitch", self.default_pitch),
            intonation=VoiceParams.clamp("intonation", self.default_intonation),
            volume=VoiceParams.clamp("volume", self.default_volume),
        )

    @property
    def voicevox_base_url(self) -> str:
        """末尾の / を除いた URL (パスを連結するため)。"""
        return self.voicevox_url.rstrip("/")


def load_settings() -> Settings:
    # 必須項目 (discord_token) は .env から入るので、引数なしで生成してよい
    return Settings()  # type: ignore[call-arg]
