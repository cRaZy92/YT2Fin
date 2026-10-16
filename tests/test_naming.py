import pytest

from app.naming import NamingItem, compute_stems, episode_code, sanitize, season_episode


@pytest.mark.parametrize("raw, expected", [
    ('AC/DC: "Live" <2024>?', "AC DC Live 2024"),
    ("  lots   of\tspace  ", "lots of space"),
    ("trailing dots...", "trailing dots"),
    (".hidden", "hidden"),
    ("", "untitled"),
    ("???", "untitled"),
    ("日本語のタイトル 🎵", "日本語のタイトル 🎵"),
])
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_sanitize_truncates_and_strips_after_cut():
    assert sanitize("a" * 10 + " . b", max_len=12) == "a" * 10


def test_sanitize_custom_fallback():
    assert sanitize("///", fallback="") == ""


def test_season_episode_with_breaks():
    items = [NamingItem(1), NamingItem(2), NamingItem(3, season_break=True), NamingItem(4)]
    assert season_episode(items) == {1: (1, 1), 2: (1, 2), 3: (2, 1), 4: (2, 2)}


def test_break_on_first_item_is_ignored():
    items = [NamingItem(1, season_break=True), NamingItem(2)]
    assert season_episode(items) == {1: (1, 1), 2: (1, 2)}


def test_episode_code():
    assert episode_code(1, 2) == "S01E02"
    assert episode_code(12, 105) == "S12E105"


def test_show_stems():
    items = [NamingItem(1, title="Intro"), NamingItem(2, title="Part: Two", season_break=True)]
    assert compute_stems("show", "My Show", items) == {
        1: "Season 01/My Show S01E01 Intro",
        2: "Season 02/My Show S02E01 Part Two",
    }


def test_music_video_stems_with_duplicates():
    items = [
        NamingItem(1, artist="Queen", song="Bohemian Rhapsody"),
        NamingItem(2, artist="queen", song="bohemian rhapsody"),
        NamingItem(3, artist="Queen", song="Bohemian Rhapsody (2)"),
        NamingItem(4, artist="", song=""),
    ]
    stems = compute_stems("music_video", "", items)
    assert stems[1] == "Queen - Bohemian Rhapsody"
    assert stems[2] == "queen - bohemian rhapsody (2)"
    assert stems[3] == "Queen - Bohemian Rhapsody (2) (2)"
    assert stems[4] == "Unknown Artist - Untitled"


def test_hostile_titles():
    items = [
        NamingItem(1, title="???"),
        NamingItem(2, title="🔥" * 5 + " 日本 "),
        NamingItem(3, title="x" * 300),
    ]
    stems = compute_stems("show", "Show", items, max_len=100)
    assert stems[1] == "Season 01/Show S01E01"
    assert stems[2] == "Season 01/Show S01E02 🔥🔥🔥🔥🔥 日本"
    name = stems[3].split("/")[1]
    assert name.startswith("Show S01E03 x")
    assert len(name) <= 100
    assert len(set(stems.values())) == 3


def test_unknown_type_raises():
    with pytest.raises(ValueError):
        compute_stems("movie", "", [NamingItem(1)])
