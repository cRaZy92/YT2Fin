from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_runtime_dependencies_are_pinned_except_yt_dlp():
    lines = [line.strip() for line in (ROOT / "requirements.lock").read_text().splitlines()
             if line.strip() and not line.lstrip().startswith("#")]
    names = {line.split("==")[0].lower() for line in lines}
    assert {"fastapi", "starlette", "sqlalchemy", "alembic", "uvicorn", "jinja2", "deno"} <= names
    assert all("==" in line for line in lines)
    # yt-dlp must stay current (YouTube changes constantly); the image installs and self-updates it
    assert not names & {"yt-dlp", "yt-dlp-ejs"}


def test_dockerfile_installs_from_the_lock():
    dockerfile = (ROOT / "Dockerfile").read_text()
    assert "pip install -r requirements.lock" in dockerfile
    assert "--no-deps ." in dockerfile


def _locked_version(name: str) -> str:
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        if line.lower().startswith(name + "=="):
            return line.split("==", 1)[1].strip()
    raise AssertionError(f"{name} is not pinned in requirements.lock")


def test_po_token_plugin_and_server_image_are_the_same_version():
    """The plugin refuses to talk to a provider server of a different version."""
    plugin = _locked_version("bgutil-ytdlp-pot-provider")
    compose = (ROOT / "docker-compose.yml").read_text()
    assert f"image: brainicism/bgutil-ytdlp-pot-provider:{plugin}" in compose
    assert "POT_PROVIDER_URL" in compose
