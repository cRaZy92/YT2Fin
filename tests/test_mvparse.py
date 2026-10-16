import pytest

from app.mvparse import clean_channel, parse_artist_song, strip_noise

CASES = [
    # (title, channel, yt_artist, yt_track, expected)
    ("Rick Astley - Never Gonna Give You Up (Official Music Video)", "Rick Astley", None, None,
     ("Rick Astley", "Never Gonna Give You Up")),
    ("Daft Punk - Get Lucky (Official Audio) ft. Pharrell Williams, Nile Rodgers", "Daft Punk", None, None,
     ("Daft Punk", "Get Lucky ft. Pharrell Williams, Nile Rodgers")),
    ("Billie Eilish – bad guy [4K]", "Billie Eilish", None, None, ("Billie Eilish", "bad guy")),
    ("Eminem - Lose Yourself (feat. Someone) [Official Video]", "EminemVEVO", None, None,
     ("Eminem", "Lose Yourself (feat. Someone)")),
    ('Artist - "Quoted Song" (Official Video)', "x", None, None, ("Artist", "Quoted Song")),
    ("Coldplay - Yellow | Official Video", "Coldplay", None, None, ("Coldplay", "Yellow")),
    ("Kanye West - Stronger (HD)", "x", None, None, ("Kanye West", "Stronger")),
    ("Hello", "AdeleVEVO", None, None, ("Adele", "Hello")),
    ("Song Name (Lyrics)", "Some Artist - Topic", None, None, ("Some Artist", "Song Name")),
    ("whatever title", "x", "Queen", "Bohemian Rhapsody (Remastered 2011)", ("Queen", "Bohemian Rhapsody")),
    ("Mystery Track", None, None, None, ("Unknown Artist", "Mystery Track")),
    ("Artist - Song (Live at Wembley)", "x", None, None, ("Artist", "Song (Live at Wembley)")),
]


@pytest.mark.parametrize("title, channel, yt_artist, yt_track, expected", CASES)
def test_parse_artist_song(title, channel, yt_artist, yt_track, expected):
    assert parse_artist_song(title, channel, yt_artist, yt_track) == expected


def test_strip_noise_keeps_feat():
    assert strip_noise("Song (feat. X) (Visualizer)") == "Song (feat. X)"


@pytest.mark.parametrize("channel, expected", [
    ("TaylorSwiftVEVO", "TaylorSwift"),
    ("Some Artist - Topic", "Some Artist"),
    ("Band Official", "Band"),
    ("Band Official Channel", "Band"),
    (None, ""),
])
def test_clean_channel(channel, expected):
    assert clean_channel(channel) == expected
