import pytest

from nexusbox.db import Database
from nexusbox.dictionary import (
    DictionaryError,
    DictionaryService,
    build_replacer,
    to_katakana,
    validate_reading,
    validate_surface,
)
from nexusbox.models import DictEntry, DictScope
from nexusbox.textproc import TextRules

GUILD = 1234


class FakeVoicevox:
    """ENGINE を立てずに global スコープの流れを確かめるためのスタブ。"""

    def __init__(self) -> None:
        self.words: dict[str, dict] = {}
        self._next = 0

    async def estimate_accent_type(self, reading: str, speaker_id: int = 3) -> int:
        return 1

    async def add_user_dict_word(self, surface, pronunciation, accent_type, *, priority=5):
        self._next += 1
        uuid = f"uuid-{self._next}"
        self.words[uuid] = {
            "surface": surface,
            "pronunciation": pronunciation,
            "accent_type": accent_type,
        }
        return uuid

    async def update_user_dict_word(
        self, word_uuid, surface, pronunciation, accent_type, *, priority=5
    ):
        self.words[word_uuid] = {
            "surface": surface,
            "pronunciation": pronunciation,
            "accent_type": accent_type,
        }

    async def delete_user_dict_word(self, word_uuid):
        self.words.pop(word_uuid, None)

    async def get_user_dict(self):
        return dict(self.words)


@pytest.fixture
async def service(tmp_path):
    db = Database(tmp_path / "test.db")
    await db.connect()
    engine = FakeVoicevox()
    yield DictionaryService(db, engine), db, engine
    await db.close()


# ------------------------------------------------------------------ 純粋関数


def test_to_katakana() -> None:
    assert to_katakana("ずんだもん") == "ズンダモン"
    assert to_katakana("ズンダモン") == "ズンダモン"


def test_validate_reading_accepts_hiragana() -> None:
    assert validate_reading("ずんだもん") == "ズンダモン"


def test_validate_reading_rejects_kanji() -> None:
    with pytest.raises(DictionaryError):
        validate_reading("枝豆")


def test_validate_reading_rejects_empty() -> None:
    with pytest.raises(DictionaryError):
        validate_reading("   ")


def test_validate_surface_rejects_too_long() -> None:
    with pytest.raises(DictionaryError):
        validate_surface("あ" * 100)


def test_replacer_prefers_longer_surface() -> None:
    entries = [
        DictEntry(surface="ずんだ", reading="ズンダ", scope=DictScope.SERVER),
        DictEntry(surface="ずんだもん", reading="ズンダモン", scope=DictScope.SERVER),
    ]
    pattern, mapping = build_replacer(entries)
    assert pattern is not None
    result = pattern.sub(lambda m: mapping[m.group(0).lower()], "ずんだもんが好き")
    assert result == "ズンダモンが好き"


def test_replacer_is_case_insensitive() -> None:
    entries = [DictEntry(surface="NexusBox", reading="ネクサスボックス", scope=DictScope.SERVER)]
    pattern, mapping = build_replacer(entries)
    assert pattern is not None
    assert pattern.sub(lambda m: mapping[m.group(0).lower()], "nexusbox です") == (
        "ネクサスボックス です"
    )


def test_replacer_escapes_regex_characters() -> None:
    entries = [DictEntry(surface="a.b", reading="エービー", scope=DictScope.SERVER)]
    pattern, mapping = build_replacer(entries)
    assert pattern is not None
    assert pattern.sub(lambda m: mapping[m.group(0).lower()], "axb と a.b") == (
        "axb と エービー"
    )


def test_replacer_with_no_entries() -> None:
    pattern, mapping = build_replacer([])
    assert pattern is None and mapping == {}


# --------------------------------------------------------------- server scope


@pytest.mark.asyncio
async def test_server_entry_is_applied_and_removed(service) -> None:
    svc, _db, _engine = service

    await svc.add(
        scope=DictScope.SERVER, guild_id=GUILD, surface="草", reading="くさ"
    )
    assert await svc.apply(GUILD, "それは草") == "それはクサ"

    assert await svc.remove(scope=DictScope.SERVER, guild_id=GUILD, surface="草") is True
    assert await svc.apply(GUILD, "それは草") == "それは草"


@pytest.mark.asyncio
async def test_server_entry_is_isolated_per_guild(service) -> None:
    svc, _db, _engine = service
    await svc.add(scope=DictScope.SERVER, guild_id=GUILD, surface="草", reading="クサ")
    assert await svc.apply(9999, "草") == "草"


@pytest.mark.asyncio
async def test_add_overwrites_existing_reading(service) -> None:
    svc, _db, _engine = service
    await svc.add(scope=DictScope.SERVER, guild_id=GUILD, surface="草", reading="クサ")
    await svc.add(scope=DictScope.SERVER, guild_id=GUILD, surface="草", reading="ワラ")
    assert await svc.apply(GUILD, "草") == "ワラ"


@pytest.mark.asyncio
async def test_remove_missing_entry_returns_false(service) -> None:
    svc, _db, _engine = service
    assert await svc.remove(scope=DictScope.SERVER, guild_id=GUILD, surface="無い") is False


@pytest.mark.asyncio
async def test_render_runs_dictionary_between_prepare_and_finalize(service) -> None:
    svc, _db, _engine = service
    await svc.add(scope=DictScope.SERVER, guild_id=GUILD, surface="w", reading="ダブリュー")

    # 辞書は finalize (w 連打 -> わら) より先に効く
    rendered = await svc.render(GUILD, "w", TextRules())
    assert rendered == "ダブリュー"


# --------------------------------------------------------------- global scope


@pytest.mark.asyncio
async def test_global_entry_goes_to_engine(service) -> None:
    svc, _db, engine = service

    entry = await svc.add(
        scope=DictScope.GLOBAL, guild_id=GUILD, surface="四十物", reading="あいもの"
    )
    assert entry.word_uuid in engine.words
    assert engine.words[entry.word_uuid]["pronunciation"] == "アイモノ"
    assert entry.accent_type == 1  # スタブの推定値

    # global はテキスト置換に使わない (ENGINE 側で解決される)
    assert await svc.apply(GUILD, "四十物") == "四十物"

    assert await svc.remove(scope=DictScope.GLOBAL, guild_id=GUILD, surface="四十物") is True
    assert engine.words == {}


@pytest.mark.asyncio
async def test_global_entry_update_reuses_uuid(service) -> None:
    svc, _db, engine = service
    first = await svc.add(
        scope=DictScope.GLOBAL, guild_id=GUILD, surface="四十物", reading="あいもの"
    )
    second = await svc.add(
        scope=DictScope.GLOBAL, guild_id=GUILD, surface="四十物", reading="よそもの"
    )
    assert first.word_uuid == second.word_uuid
    assert len(engine.words) == 1
    assert engine.words[second.word_uuid]["pronunciation"] == "ヨソモノ"


@pytest.mark.asyncio
async def test_restore_global_dict_reregisters_lost_words(service) -> None:
    svc, _db, engine = service
    await svc.add(scope=DictScope.GLOBAL, guild_id=GUILD, surface="四十物", reading="あいもの")

    engine.words.clear()  # ENGINE のボリュームを失った状況

    assert await svc.restore_global_dict() == 1
    assert len(engine.words) == 1

    # 2 回目は何も再登録しない
    assert await svc.restore_global_dict() == 0
