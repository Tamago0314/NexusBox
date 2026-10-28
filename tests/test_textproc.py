from nexusbox.textproc import (
    ATTACHMENT_PLACEHOLDER,
    URL_PLACEHOLDER,
    WAYPOINT_PLACEHOLDER,
    MentionMap,
    TextRules,
    compose_speech,
    finalize,
    prepare,
)


def render(content: str, rules: TextRules | None = None, mentions=None) -> str:
    rules = rules or TextRules()
    return finalize(prepare(content, rules, mentions), rules)


def test_url_is_replaced() -> None:
    assert render("これ見て https://example.com/a?b=1 やば") == (
        f"これ見て、{URL_PLACEHOLDER}、やば"
    )
    assert URL_PLACEHOLDER == "ゆーあーるえるしょうりゃく"


def test_waypoint_share_replaces_whole_message() -> None:
    """座標の羅列ごと読みに差し替える。"""
    shared = "xaero-waypoint:拠点:拠:1234:70:-567:6:false:0:Internal-overworld-waypoints"
    assert render(shared) == WAYPOINT_PLACEHOLDER
    assert WAYPOINT_PLACEHOLDER == "うぇいぽいんときょうゆう"


def test_waypoint_wins_over_surrounding_text() -> None:
    """前後に文章が付いていても全体を置き換える。"""
    assert render("ここだよ xaero-waypoint:a:a:1:2:3:0:false:0:x 見て") == (
        WAYPOINT_PLACEHOLDER
    )


def test_waypoint_match_is_case_insensitive() -> None:
    assert render("XAERO-WAYPOINT:a:a:1:2:3:0:false:0:x") == WAYPOINT_PLACEHOLDER


def test_url_is_kept_when_disabled() -> None:
    rules = TextRules(skip_url=False)
    assert "example.com" in render("https://example.com", rules)


def test_mentions_use_display_names() -> None:
    mentions = MentionMap(
        users={1: "ずんだ"}, roles={2: "運営"}, channels={3: "雑談"}
    )
    assert render("<@1> と <@&2> は <#3> にいる", mentions=mentions) == (
        "ずんださん と 運営 は 雑談 にいる"
    )


def test_unknown_mention_falls_back() -> None:
    assert render("<@999> やあ") == "誰かさん やあ"


def test_custom_emoji() -> None:
    assert render("<:zundamon:123> だ") == "zundamon だ"
    assert render("<:zundamon:123> だ", TextRules(read_emoji=False)) == "だ"


def test_codeblock_is_skipped() -> None:
    content = "これ\n```py\nprint('hello')\n```\nどう？"
    assert render(content) == "これ、コード、どう？"


def test_codeblock_can_be_read() -> None:
    rules = TextRules(skip_codeblock=False)
    assert "print" in render("```\nprint(1)\n```", rules)


def test_spoiler_and_inline_code() -> None:
    assert render("||秘密|| と `code`") == "ネタバレ、と code"


def test_repeated_characters_are_compressed() -> None:
    assert render("すごーーーーーーい") == "すごーーーい"


def test_laugh_is_read() -> None:
    assert render("それなwww") == "それな、わら"


def test_quote_and_emphasis_are_stripped() -> None:
    assert render("> **強調** した _文_") == "強調 した 文"


def test_newlines_become_punctuation() -> None:
    assert render("いち\nに\nさん") == "いち、に、さん"


def test_repeat_compression_runs_before_length_check() -> None:
    # 同じ文字の連打は圧縮されるので、長さ制限には引っかからない
    rules = TextRules(max_length=10)
    assert render("あ" * 50, rules) == "あああ"


def test_max_length_on_long_sentence() -> None:
    rules = TextRules(max_length=5)
    assert render("あいうえおかきくけこ", rules) == "あいうえお、以下略"


def test_compose_speech_adds_name_and_extras() -> None:
    assert compose_speech(
        "やあ", speaker_name="ずんだ", extras=[ATTACHMENT_PLACEHOLDER]
    ) == "ずんださん、やあ、てんぷふぁいる"
    assert ATTACHMENT_PLACEHOLDER == "てんぷふぁいる"


def test_compose_speech_without_body() -> None:
    assert compose_speech("", extras=["スタンプ"]) == "スタンプ"


def test_url_placeholder_is_not_applied_when_disabled() -> None:
    """skip_url が OFF のときは URL をそのまま読む。"""
    rules = TextRules(skip_url=False)
    rendered = render("https://example.com/a", rules)
    assert URL_PLACEHOLDER not in rendered
    assert "example.com" in rendered


def test_multiple_urls_are_all_replaced() -> None:
    rendered = render("https://a.example https://b.example")
    assert rendered == URL_PLACEHOLDER + "、" + URL_PLACEHOLDER
