"""VOICEVOX ENGINE の REST クライアント。

ENGINE の API リファレンスは起動後 http://<host>:50021/docs で確認できる。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

from .models import VoiceParams

log = logging.getLogger(__name__)


class VoicevoxError(RuntimeError):
    """ENGINE が 4xx / 5xx を返したとき。message はユーザーに見せられる日本語。"""

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class Style:
    """話者のスタイル (ノーマル・あまあま など)。id が合成時の話者 ID になる。"""

    id: int
    name: str


@dataclass(frozen=True, slots=True)
class Speaker:
    """キャラクター 1 人分。"""

    name: str
    uuid: str
    styles: tuple[Style, ...]


class VoicevoxClient:
    def __init__(
        self,
        base_url: str,
        *,
        concurrency: int = 2,
        timeout: float = 60.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None
        self._semaphore = asyncio.Semaphore(concurrency)
        self._speakers: list[Speaker] | None = None
        #: 話者 ID -> (キャラクター名, スタイル名)
        self._style_index: dict[int, tuple[str, str]] = {}

    # ---------------------------------------------------------------- ライフサイクル

    async def start(self) -> None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self._timeout)

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    @property
    def session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            raise RuntimeError("VoicevoxClient.start() が呼ばれていません")
        return self._session

    @property
    def base_url(self) -> str:
        return self._base_url

    async def wait_until_ready(self, *, timeout: float = 600.0, interval: float = 3.0) -> str:
        """ENGINE が応答するまで待つ。起動直後はモデルの読み込みに時間がかかる。"""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last_error: Exception | None = None

        while loop.time() < deadline:
            try:
                version = await self.version()
            except Exception as exc:  # noqa: BLE001 - 起動待ちなので全部握る
                last_error = exc
                await asyncio.sleep(interval)
                continue
            log.info("VOICEVOX ENGINE %s に接続しました (%s)", version, self._base_url)
            return version

        raise VoicevoxError(
            f"VOICEVOX ENGINE ({self._base_url}) に接続できませんでした: {last_error}"
        )

    # ------------------------------------------------------------------ 低レベル

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        expect: str = "json",
    ) -> Any:
        """expect: "json" (既定) / "bytes" (WAV など) / "none" (本文なし)"""
        url = f"{self._base_url}{path}"
        async with self.session.request(method, url, params=params, json=json) as resp:
            if resp.status >= 400:
                raise VoicevoxError(await _format_error(resp), status=resp.status)
            if expect == "bytes":
                return await resp.read()
            if expect == "none" or resp.status == 204:
                return None
            return await resp.json()

    # -------------------------------------------------------------------- 情報

    async def version(self) -> str:
        return await self._request("GET", "/version")

    async def speakers(self, *, force: bool = False) -> list[Speaker]:
        """話者一覧。一度取ったらキャッシュし、force=True で取り直す。"""
        if self._speakers is not None and not force:
            return self._speakers

        raw = await self._request("GET", "/speakers")
        speakers = [
            Speaker(
                name=item["name"],
                uuid=item["speaker_uuid"],
                styles=tuple(
                    Style(id=style["id"], name=style["name"]) for style in item["styles"]
                ),
            )
            for item in raw
        ]
        self._speakers = speakers
        self._style_index = {
            style.id: (speaker.name, style.name)
            for speaker in speakers
            for style in speaker.styles
        }
        return speakers

    async def describe_speaker(self, speaker_id: int) -> str:
        """話者 ID を「ずんだもん (ノーマル)」のような表示名にする。"""
        if not self._style_index:
            await self.speakers()
        found = self._style_index.get(speaker_id)
        if found is None:
            return f"話者 ID {speaker_id}"
        return f"{found[0]} ({found[1]})"

    # -------------------------------------------------------------------- 合成

    async def audio_query(self, text: str, speaker_id: int) -> dict[str, Any]:
        """テキストを解析して合成用のクエリ (読み・アクセント・パラメータ) を作る。"""
        return await self._request(
            "POST", "/audio_query", params={"text": text, "speaker": speaker_id}
        )

    async def synthesis(self, query: dict[str, Any], speaker_id: int) -> bytes:
        """クエリから WAV を作る。"""
        return await self._request(
            "POST",
            "/synthesis",
            params={"speaker": speaker_id},
            json=query,
            expect="bytes",
        )

    async def synthesize(
        self, text: str, speaker_id: int, params: VoiceParams | None = None
    ) -> bytes:
        """テキストから WAV バイト列を作る。ENGINE への同時実行数は Semaphore で制限。"""
        async with self._semaphore:
            query = await self.audio_query(text, speaker_id)
            if params is not None:
                params.apply_to(query)
            # 発話の前後に僅かな無音を入れて頭切れを防ぐ
            query["prePhonemeLength"] = max(query.get("prePhonemeLength", 0.1), 0.1)
            query["postPhonemeLength"] = max(query.get("postPhonemeLength", 0.1), 0.1)
            return await self.synthesis(query, speaker_id)

    async def estimate_accent_type(self, reading: str, speaker_id: int = 3) -> int:
        """読み (カタカナ) からアクセント核の位置を推定する。

        ENGINE にアクセント推定専用のエンドポイントは無いので、audio_query を通して
        解析結果の accent_phrases から取り出す。失敗したら 0 (平板型) を返す。
        """
        try:
            query = await self.audio_query(reading, speaker_id)
            phrases = query.get("accent_phrases") or []
            if phrases:
                return int(phrases[0].get("accent", 0))
        except Exception:  # noqa: BLE001 - 推定は best effort
            log.debug("アクセント推定に失敗: %s", reading, exc_info=True)
        return 0

    # ------------------------------------------------------------- ユーザー辞書

    async def get_user_dict(self) -> dict[str, dict[str, Any]]:
        """{word_uuid: {"surface": ..., "pronunciation": ..., ...}} を返す。"""
        return await self._request("GET", "/user_dict")

    async def add_user_dict_word(
        self,
        surface: str,
        pronunciation: str,
        accent_type: int,
        *,
        priority: int = 5,
    ) -> str:
        """登録して word_uuid を返す。"""
        return await self._request(
            "POST",
            "/user_dict_word",
            params={
                "surface": surface,
                "pronunciation": pronunciation,
                "accent_type": accent_type,
                "priority": priority,
            },
        )

    async def update_user_dict_word(
        self,
        word_uuid: str,
        surface: str,
        pronunciation: str,
        accent_type: int,
        *,
        priority: int = 5,
    ) -> None:
        """登録済みの単語を上書きする (word_uuid は変わらない)。"""
        await self._request(
            "PUT",
            f"/user_dict_word/{word_uuid}",
            params={
                "surface": surface,
                "pronunciation": pronunciation,
                "accent_type": accent_type,
                "priority": priority,
            },
            expect="none",
        )

    async def delete_user_dict_word(self, word_uuid: str) -> None:
        await self._request("DELETE", f"/user_dict_word/{word_uuid}", expect="none")


async def _format_error(resp: aiohttp.ClientResponse) -> str:
    """ENGINE の 422 などをユーザーに見せられる文字列にする。"""
    try:
        payload = await resp.json()
    except Exception:  # noqa: BLE001
        return f"VOICEVOX ENGINE がエラーを返しました (HTTP {resp.status})"

    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str):
        return f"{detail} (HTTP {resp.status})"
    if isinstance(detail, list) and detail:
        messages = [str(item.get("msg", item)) for item in detail if isinstance(item, dict)]
        if messages:
            return f"{' / '.join(messages)} (HTTP {resp.status})"
    return f"VOICEVOX ENGINE がエラーを返しました (HTTP {resp.status})"
