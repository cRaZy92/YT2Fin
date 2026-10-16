import pytest

from app.errors import ErrorKind, classify


@pytest.mark.parametrize("message, kind", [
    ("ERROR: [youtube] abc: Private video. Sign in if you've been granted access", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Video unavailable", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: This video has been removed by the uploader", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: The uploader has not made this video available in your country", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: This video is only available to Music Premium members", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Join this channel to get access to members-only content", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Sign in to confirm your age.", ErrorKind.PERMANENT),
    ("ERROR: [youtube] abc: Sign in to confirm you’re not a bot", ErrorKind.RATE_LIMIT),
    ("ERROR: unable to download video data: HTTP Error 429: Too Many Requests", ErrorKind.RATE_LIMIT),
    ("ERROR: unable to download video data: HTTP Error 403: Forbidden", ErrorKind.TRANSIENT),
    ("ERROR: [youtube] abc: Premieres in 3 hours", ErrorKind.NOT_YET),
    ("ERROR: [youtube] abc: This live event will begin in 2 days.", ErrorKind.NOT_YET),
    ("Connection reset by peer", ErrorKind.TRANSIENT),
])
def test_classify(message, kind):
    assert classify(message) == kind
