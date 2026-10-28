"""読み上げ前のテキスト正規化。

discord の型に依存しない純粋関数にしてあるのでそのままテストできる。
処理は 2 段階に分かれていて、間にサーバー辞書の置換が挟まる:

    prepare()  ->  DictionaryService.apply()  ->  finalize()

こうしておくと「w」のような組み込みの読み替えをユーザー辞書で上書きできる。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_CODEBLOCK = re.compile(r"```[\s\S]*?```")
_INLINE_CODE = re.compile(r"`([^`\n]*)`")
_SPOILER = re.compile(r"\|\|([\s\S]*?)\|\|")
_CUSTOM_EMOJI = re.compile(r"<a?:([A-Za-z0-9_]+):\d+>")
_USER_MENTION = re.compile(r"<@!?(\d+)>")
_ROLE_MENTION = re.compile(r"<@&(\d+)>")
_CHANNEL_MENTION = re.compile(r"<#(\d+)>")
_TIMESTAMP = re.compile(r"<t:\d+(?::[tTdDfFR])?>")
_URL = re.compile(r"https?://[^\s<>|]+")
_QUOTE_LINE = re.compile(r"^\s*>+\s?", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*\*|\*\*|\*|___|__|_|~~)")
#: 同じ文字の 4 連続以上 (3 文字に縮める)
_REPEAT = re.compile(r"(.)\1{3,}")
#: 笑いの「www」。後ろに英字が続くもの (www.example など) は笑いではないので除く
_LAUGH = re.compile(r"[wｗ]{2,}(?![A-Za-zＡ-Ｚａ-ｚ])")
_WHITESPACE = re.compile(r"[ \t\u3000]+")
_BLANK_LINES = re.compile(r"\n{2,}")
_AROUND_PUNCT = re.compile(r"\s*\u3001\s*")
_WAYPOINT = re.compile(r"xaero-waypoint:", re.IGNORECASE)

#: URL を省略して読むときの読み。VOICEVOX が素直に読めるよう平仮名で持つ。
URL_PLACEHOLDER = "ゆーあーるえるしょうりゃく"
#: 添付ファイルがあるときに付ける読み (件数は付けない)。
ATTACHMENT_PLACEHOLDER = "てんぷふぁいる"
#: Xaero's Minimap のウェイポイント共有。座標やフラグの羅列を読んでも
#: 意味がないので、メッセージ全体をこの読みに置き換える。
WAYPOINT_PLACEHOLDER = "うぇいぽいんときょうゆう"


@dataclass(slots=True)
class TextRules:
    """ギルド設定のうち、テキスト整形に関わる部分だけを取り出したもの。"""

    max_length: int = 120
    skip_url: bool = True
    skip_codeblock: bool = True
    read_emoji: bool = True


@dataclass(slots=True)
class MentionMap:
    """メンション ID から表示名への対応表。"""

    users: dict[int, str] = field(default_factory=dict)
    roles: dict[int, str] = field(default_factory=dict)
    channels: dict[int, str] = field(default_factory=dict)


def prepare(content: str, rules: TextRules, mentions: MentionMap | None = None) -> str:
    """discord 固有の記法を素のテキストに開く (辞書置換の前に行う)。"""
    # ウェイポイント共有は本文ごと差し替える。共有文字列は名前・座標・セット名を
    # : で連ねたもので、トークンだけを切り出すのは難しい (名前に空白も : も入る)。
    if _WAYPOINT.search(content):
        return WAYPOINT_PLACEHOLDER

    mentions = mentions or MentionMap()
    text = content

    if rules.skip_codeblock:
        text = _CODEBLOCK.sub("、コード、", text)
    else:
        text = _CODEBLOCK.sub(lambda m: m.group(0).strip("`"), text)

    text = _INLINE_CODE.sub(r"\1", text)
    text = _SPOILER.sub("、ネタバレ、", text)
    text = _TIMESTAMP.sub("、日時、", text)

    text = _USER_MENTION.sub(
        lambda m: mentions.users.get(int(m.group(1)), "誰か") + "さん", text
    )
    text = _ROLE_MENTION.sub(
        lambda m: mentions.roles.get(int(m.group(1)), "ロール"), text
    )
    text = _CHANNEL_MENTION.sub(
        lambda m: mentions.channels.get(int(m.group(1)), "チャンネル"), text
    )

    if rules.read_emoji:
        text = _CUSTOM_EMOJI.sub(r"\1", text)
    else:
        text = _CUSTOM_EMOJI.sub("", text)

    if rules.skip_url:
        text = _URL.sub(f"、{URL_PLACEHOLDER}、", text)

    # 引用の「>」と、太字・斜体・取り消し線の記号は読まない
    text = _QUOTE_LINE.sub("", text)
    text = _EMPHASIS.sub("", text)
    return text


def finalize(text: str, rules: TextRules) -> str:
    """辞書置換のあとに走る仕上げ。連打の圧縮・改行の読点化・長さ制限。"""
    text = _LAUGH.sub("、わら、", text)
    text = _REPEAT.sub(r"\1\1\1", text)  # 長さ制限より先に縮める

    text = _BLANK_LINES.sub("\n", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "、")
    text = _WHITESPACE.sub(" ", text)

    # 置換で生まれた「読点まわりの空白」と「読点の連続」を畳む
    text = _AROUND_PUNCT.sub("、", text)
    while "、、" in text:
        text = text.replace("、、", "、")
    text = text.strip().strip("、").strip()

    if rules.max_length > 0 and len(text) > rules.max_length:
        text = text[: rules.max_length] + "、以下略"

    return text


def compose_speech(
    body: str,
    *,
    speaker_name: str | None = None,
    extras: list[str] | None = None,
) -> str:
    """発言者名や添付ファイルの案内を本文に付ける。"""
    parts: list[str] = []
    if speaker_name:
        parts.append(f"{speaker_name}さん")
    if body:
        parts.append(body)
    if extras:
        parts.extend(extras)
    return "、".join(part for part in parts if part)
