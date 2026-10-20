from pathlib import Path

from app.models import Mapping, MappingType

JPG = b"\xff\xd8\xff\xe0 jpeg"
PNG = b"\x89PNG\r\n\x1a\n png"


def test_upload_shows_and_resets_custom_artwork(client, session, make_mapping):
    mapping = make_mapping()  # manual list: reset just removes the upload
    folder = Path(mapping.folder)
    page = client.get(f"/mappings/{mapping.id}").text
    assert "Artwork" in page and "No backdrop" in page

    response = client.post(f"/mappings/{mapping.id}/artwork/fanart", files={"file": ("art.png", PNG, "image/png")},
                           follow_redirects=False)
    assert response.status_code == 303
    assert (folder / "fanart.png").read_bytes() == PNG
    assert client.get(f"/mappings/{mapping.id}/artwork/fanart").content == PNG
    page = client.get(f"/mappings/{mapping.id}").text
    assert "uploaded" in page and f"/mappings/{mapping.id}/artwork/fanart/reset" in page

    client.post(f"/mappings/{mapping.id}/artwork/fanart/reset")
    assert not (folder / "fanart.png").exists()
    session.expire_all()
    assert "fanart" not in session.get(Mapping, mapping.id).artwork
    assert client.get(f"/mappings/{mapping.id}/artwork/fanart").status_code == 404


def test_upload_rejects_non_images_and_music_video_mappings(client, make_mapping):
    mapping = make_mapping()
    bad = client.post(f"/mappings/{mapping.id}/artwork/poster", files={"file": ("x.txt", b"hello", "text/plain")})
    assert bad.status_code == 400 and "JPEG" in bad.text
    assert client.post(f"/mappings/{mapping.id}/artwork/nope", files={"file": ("a.jpg", JPG)}).status_code == 404
    music = make_mapping("Music", type=MappingType.MUSIC_VIDEO)
    assert "Artwork" not in client.get(f"/mappings/{music.id}").text
    assert client.post(f"/mappings/{music.id}/artwork/poster", files={"file": ("a.jpg", JPG)}).status_code == 400
