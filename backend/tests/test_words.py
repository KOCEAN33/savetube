from savetube.words import extract, matches, normalize_words, rank

UTAWAKU = "【歌枠】ちょっと疲れちゃってるんだと思うよ【VESPERBELL ヨミ】"
GUERRILLA = "【ゲリラ(豪雨)】私魔女のキキ(危機)、こっちは配信すっぽかし女の【VESPERBELL カスカ】"


def test_extract_takes_bracket_text():
    assert extract(UTAWAKU) == {"歌枠", "VESPERBELL ヨミ"}


def test_extract_takes_only_the_outermost_bracket():
    assert extract(GUERRILLA) == {"ゲリラ(豪雨)", "危機", "VESPERBELL カスカ"}


def test_extract_takes_every_bracket_kind_and_hashtags():
    title = "a [Eng] b （全角） c 「鉤」 d (Cover) / VESPERBELL #歌ってみた #RKMFes_2026"
    assert extract(title) == {"Eng", "全角", "鉤", "Cover", "歌ってみた", "RKMFes_2026"}


def test_extract_strips_the_hash_inside_a_bracket():
    assert extract("【歌枠】 なんかちょっと久しぶり？ 【#ヨミネロ】") == {"歌枠", "ヨミネロ"}


def test_an_unclosed_bracket_does_not_hide_later_words():
    assert extract("ヨミ(笑 【歌枠】") == {"歌枠"}


def test_rank_counts_videos_and_merges_case_and_hash():
    titles = [
        "IRIS OUT - 米津玄師 (Cover) / VESPERBELL ヨミ #cover #歌ってみた",
        "No.13 - ELLEGARDEN (Cover) / VESPERBELL カスカ #cover #歌ってみた",
        "狭心症 - RADWIMPS (Cover) / VESPERBELL ヨミ",
        UTAWAKU,
        "【歌枠】笑えばいいと思うよ【VESPERBELL ヨミ】",
        GUERRILLA,
    ]
    ranked = rank(titles)
    # The spec fixes no order among equal counts.
    assert ranked[0] == ("Cover", 3)
    assert sorted(ranked[1:]) == sorted([("歌ってみた", 2), ("歌枠", 2), ("VESPERBELL ヨミ", 2)])


def test_rank_keeps_the_top_30():
    titles = [f"#w{i} #all" for i in range(40) for _ in range(2)]
    ranked = rank(titles)
    assert len(ranked) == 30
    assert ranked[0] == ("all", 80)


def test_matches_with_include():
    assert matches(UTAWAKU, ["歌枠"], [])
    assert not matches(GUERRILLA, ["歌枠"], [])


def test_matches_is_case_insensitive_and_exclude_wins():
    assert matches("IRIS OUT (Cover)", [], [])
    assert matches("IRIS OUT (Cover)", ["cover", "nope"], [])
    assert not matches("IRIS OUT (Cover)", ["cover"], ["iris"])
    assert not matches("IRIS OUT (Cover)", [], ["COVER"])


def test_normalize_words():
    assert normalize_words([" 歌枠 ", "", "  ", "Cover", "cover", "歌枠"]) == ["歌枠", "Cover"]


def test_an_unclosed_inner_bracket_keeps_the_outer_word():
    assert extract("【a(b】 c") == {"a(b"}
