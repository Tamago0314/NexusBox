"""ギルドごとの読み上げキューと再生ワーカー。

1 ギルドにつき 1 本の consumer タスクが順番に合成 -> 再生する。
ENGINE への同時リクエスト数は VoicevoxClient 側の Semaphore で制限している。
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections import OrderedDict
from dataclasses import dataclass

import discord

from .models import VoiceParams
from .voicevox import VoicevoxClient, VoicevoxError

log = logging.getLogger(__name__)

#: この文字数以下のテキストだけキャッシュする (短い定型句ほど繰り返されるため)
CACHE_TEXT_LIMIT = 40


@dataclass(slots=True)
class SpeechItem:
    """読み上げキューに積む 1 件。"""

    text: str
    speaker_id: int
    params: VoiceParams


class SynthesisCache:
    """合成済み WAV の LRU キャッシュ。全ギルドで共有する。"""

    def __init__(self, max_size: int = 256) -> None:
        self._max_size = max_size
        self._store: OrderedDict[tuple, bytes] = OrderedDict()
        self.hits = 0
        self.misses = 0

    async def synthesize(self, client: VoicevoxClient, item: SpeechItem) -> bytes:
        cacheable = len(item.text) <= CACHE_TEXT_LIMIT and self._max_size > 0
        key = (item.text, item.speaker_id, item.params.cache_key())

        if cacheable:
            cached = self._store.get(key)
            if cached is not None:
                self._store.move_to_end(key)  # 最近使ったものを末尾へ
                self.hits += 1
                return cached

        self.misses += 1
        wav = await client.synthesize(item.text, item.speaker_id, item.params)

        if cacheable:
            self._store[key] = wav
            self._store.move_to_end(key)
            # 上限を超えたら、いちばん長く使っていないもの (先頭) から捨てる
            while len(self._store) > self._max_size:
                self._store.popitem(last=False)

        return wav

    def clear(self) -> None:
        self._store.clear()

    def __len__(self) -> int:
        return len(self._store)


class GuildPlayer:
    """1 ギルド分の読み上げキュー。生成と同時に再生ワーカーが動き出す。"""

    def __init__(
        self,
        guild_id: int,
        voice_client: discord.VoiceClient,
        client: VoicevoxClient,
        cache: SynthesisCache,
        *,
        max_queue_size: int = 20,
    ) -> None:
        self.guild_id = guild_id
        self.voice_client = voice_client
        self._client = client
        self._cache = cache
        self._queue: asyncio.Queue[SpeechItem] = asyncio.Queue(maxsize=max_queue_size)
        self._finished = asyncio.Event()
        self._closing = False
        self._task = asyncio.create_task(self._run(), name=f"tts-player-{guild_id}")

    # ------------------------------------------------------------------ 状態

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def is_playing(self) -> bool:
        return self.voice_client.is_connected() and self.voice_client.is_playing()

    @property
    def channel(self) -> discord.VoiceChannel | discord.StageChannel | None:
        return self.voice_client.channel  # type: ignore[return-value]

    # ------------------------------------------------------------------ 操作

    async def enqueue(self, item: SpeechItem) -> bool:
        """キューに積む。溢れていたら False (その発言は捨てる)。"""
        if self._closing or not item.text:
            return False
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull:
            log.debug("guild %s: 読み上げキューが満杯のため破棄", self.guild_id)
            return False
        return True

    def skip(self) -> bool:
        """再生中の 1 件を打ち切る。"""
        if self.voice_client.is_playing():
            self.voice_client.stop()
            return True
        return False

    def clear(self) -> int:
        """待機中のキューを空にする (再生中の 1 件はそのまま)。"""
        dropped = 0
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            dropped += 1
        return dropped

    async def stop(self) -> None:
        """ワーカーを止めて VC からも切断する。"""
        self.abort()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        if self.voice_client.is_connected():
            await self.voice_client.disconnect(force=True)

    def abort(self) -> None:
        """await せずにワーカーを止める (同期的な後始末用)。"""
        self._closing = True
        self.clear()
        self._task.cancel()

    # ------------------------------------------------------------------ 内部

    async def _run(self) -> None:
        """キューから 1 件ずつ取り出して読み上げる。止まるのは cancel されたときだけ。"""
        while True:
            item = await self._queue.get()
            try:
                await self._speak(item)
            except VoicevoxError as exc:
                log.warning("guild %s: 音声合成に失敗: %s", self.guild_id, exc)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - ワーカーを絶対に落とさない
                log.exception("guild %s: 読み上げ中に予期しないエラー", self.guild_id)
            finally:
                self._queue.task_done()

    async def _speak(self, item: SpeechItem) -> None:
        if not self.voice_client.is_connected():
            return

        wav = await self._cache.synthesize(self._client, item)

        # 合成を待っている間に切断されることがある
        if not self.voice_client.is_connected():
            return

        # 前の再生が残っていることは基本無いが、念のため待つ
        while self.voice_client.is_playing():
            await asyncio.sleep(0.05)

        loop = asyncio.get_running_loop()
        self._finished = asyncio.Event()
        finished = self._finished

        # after は discord.py の再生スレッドから呼ばれるので、
        # イベントループへは call_soon_threadsafe で戻す
        def _after(error: Exception | None) -> None:
            if error is not None:
                log.warning("guild %s: 再生エラー: %s", self.guild_id, error)
            loop.call_soon_threadsafe(finished.set)

        source = discord.FFmpegPCMAudio(io.BytesIO(wav), pipe=True, options="-vn")
        self.voice_client.play(source, after=_after)
        await finished.wait()


class TTSManager:
    """ギルド ID -> GuildPlayer の管理。"""

    def __init__(
        self,
        client: VoicevoxClient,
        *,
        cache_size: int = 256,
        max_queue_size: int = 20,
    ) -> None:
        self._client = client
        self._players: dict[int, GuildPlayer] = {}
        self._max_queue_size = max_queue_size
        self.cache = SynthesisCache(cache_size)

    def get(self, guild_id: int) -> GuildPlayer | None:
        """接続中のプレイヤーを返す。未接続なら None。"""
        player = self._players.get(guild_id)
        if player is not None and not player.voice_client.is_connected():
            # 何らかの理由で切断済みなら、ワーカーごと片付けて未接続扱いにする
            self._players.pop(guild_id, None)
            player.abort()
            return None
        return player

    def create(self, guild_id: int, voice_client: discord.VoiceClient) -> GuildPlayer:
        # 既にあれば (/join で別の VC へ移動したときなど) キューを残したまま使い回す
        existing = self._players.get(guild_id)
        if existing is not None:
            existing.voice_client = voice_client
            return existing

        player = GuildPlayer(
            guild_id,
            voice_client,
            self._client,
            self.cache,
            max_queue_size=self._max_queue_size,
        )
        self._players[guild_id] = player
        return player

    async def destroy(self, guild_id: int) -> bool:
        """ワーカーを止めて VC から切断する。プレイヤーが無ければ False。"""
        player = self._players.pop(guild_id, None)
        if player is None:
            return False
        await player.stop()
        return True

    async def destroy_all(self) -> None:
        for guild_id in list(self._players):
            await self.destroy(guild_id)

    @property
    def active_guilds(self) -> int:
        return len(self._players)
