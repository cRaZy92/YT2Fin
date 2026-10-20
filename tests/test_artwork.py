from pathlib import Path

import pytest

from app import artwork

JPG = b"\xff\xd8\xff\xe0 jpeg"
PNG = b"\x89PNG\r\n\x1a\n png"


def test_sniff_ext():
    assert artwork.sniff_ext(JPG) == ".jpg"
    assert artwork.sniff_ext(PNG) == ".png"
    assert artwork.sniff_ext(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == ".webp"
    assert artwork.sniff_ext(b"<html>") is None


def test_install_replaces_other_formats_and_records_it(session, make_mapping):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    artwork.install(folder, mapping, "fanart", JPG, artwork.CHANNEL, "u1")
    artwork.install(folder, mapping, "fanart", PNG, artwork.CHANNEL, "u2")
    assert sorted(p.name for p in folder.iterdir()) == ["fanart.png"]
    assert mapping.artwork["fanart"] == {"source": "channel", "file": "fanart.png", "url": "u2"}
    with pytest.raises(artwork.ArtworkError):
        artwork.install(folder, mapping, "poster", b"<html>", artwork.CUSTOM)


def test_foreign_images_are_never_replaced_but_legacy_episode_posters_are(session, make_mapping):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    (folder / "banner.jpg").write_bytes(b"someone else's")
    assert artwork.wanted(folder, mapping, {"banner": "u"}) == {}

    (folder / "Season 01").mkdir()
    (folder / "Season 01" / "Show S01E01 A-thumb.jpg").write_bytes(b"thumb")
    (folder / "poster.jpg").write_bytes(b"thumb")  # copied by an older version
    assert artwork.wanted(folder, mapping, {"poster": "u"}) == {"poster": "u"}
    (folder / "poster.jpg").write_bytes(b"hand-made")
    assert artwork.wanted(folder, mapping, {"poster": "u"}) == {}


def test_wanted_skips_current_images_and_uploads(session, make_mapping):
    mapping = make_mapping()
    folder = Path(mapping.folder)
    artwork.install(folder, mapping, "poster", JPG, artwork.CHANNEL, "u1")
    artwork.install(folder, mapping, "fanart", JPG, artwork.CUSTOM)
    assert artwork.wanted(folder, mapping, {"poster": "u1", "fanart": "f"}) == {}
    assert artwork.wanted(folder, mapping, {"poster": "u2"}) == {"poster": "u2"}
    assert artwork.wanted(folder, mapping, {"poster": "u1"}, force=True) == {"poster": "u1"}
    assert mapping.artwork["fanart"]["url"] == "f"  # remembered for "use channel image"


def test_download_all_survives_one_failure():
    def fetch(url):
        if url == "bad":
            raise OSError("boom")
        return JPG
    assert artwork.download_all({"poster": "bad", "fanart": "ok"}, fetch) == {"fanart": JPG}
