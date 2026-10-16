# YouTube → Jellyfin Sync

Self-hosted service that keeps YouTube playlists (or hand-picked lists) in your Jellyfin library,
named the way Jellyfin expects:

- **Shows:** `Season 01/Show S01E01 Video Title.mkv` + `.nfo` + `-thumb.jpg` + `tvshow.nfo`
- **Music videos:** `Artist - Song.mkv` + `.nfo` + `-thumb.jpg`

## Run

```bash
docker compose up -d --build
```

Open http://localhost:8080 and create a mapping: pick a type, a folder inside `/media`, and optionally a
playlist or channel URL (`https://www.youtube.com/@name`). Leave it empty for a manual list and paste video URLs on the mapping page.
A channel is downloaded completely, oldest upload first, so new uploads become the next episodes; tick
"Include Shorts" to also take the channel's Shorts.

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | 1000 | Owner of downloaded files (match Jellyfin's user). `/media` must be writable by this user; an empty folder Docker auto-created as root is claimed automatically |
| `TZ` | UTC | Time zone for the UI |
| `AUTH_USER` / `AUTH_PASSWORD` | unset | Enable HTTP basic auth |
| `PORT` | 8080 | Web UI port |

Volumes: `/config` (database, cookies, logs) and `/media`: the folder your Jellyfin libraries point at.
The service only writes files there; it never talks to Jellyfin itself.

## Jellyfin setup

1. Create a **Shows** library pointing at the folder that holds your show mappings
   (e.g. `/media/youtube/shows`), and a **Music Videos** library for music-video mappings.
2. In each library: enable the **Nfo** metadata reader. Disable online metadata downloaders
   (TheMovieDb, TheTVDB, …) for these libraries so YouTube channels are not matched to unrelated shows.
3. Enable **real-time monitoring** so new downloads show up without a manual scan.

## Behaviour

- Sync only **adds** new playlist videos at the end. Your order, renames, season breaks and removals
  always win. Deleting a video from a playlist-backed mapping remembers it as *ignored*.
- Episode numbers follow the list order. Reordering, deleting or adding a season break renames the
  files on disk. Unavailable videos don't use up a number.
- Downloads are throttled with a random delay (default 30–90 s) and request pacing. On HTTP 429 or
  "confirm you're not a bot", the whole queue pauses (60 min, doubling up to 12 h).
- yt-dlp updates itself at start and daily; the app restarts itself once nothing is downloading.
- Age-restricted or bot-checked videos: upload a `cookies.txt` on the Settings page.

## Development

```bash
uv venv --python 3.12 .venv && uv pip install --python .venv -e '.[dev]'
.venv/bin/pytest                    # unit + web tests
.venv/bin/pytest -m integration     # downloads a real 19-second video
MEDIA_ROOT=./media CONFIG_DIR=./config .venv/bin/python -m app
```

### Dependencies

Runtime dependencies are pinned in `requirements.lock`, which the Docker image installs. yt-dlp is
deliberately left unpinned because it must keep up with YouTube. To update the pins, run
`scripts/lock-deps.sh`.

### Database migrations

The schema is managed by Alembic (`app/migrations/`). The app migrates itself on startup.
Before applying pending migrations to an existing database, it saves a copy to
`/config/backups/` and keeps the newest 5.

After changing `app/models.py`:

```bash
.venv/bin/alembic revision --autogenerate -m "describe the change"   # compares against ./config/app.db
# review the new file in app/migrations/versions/, then restart the app
```

`tests/test_migrations.py::test_migrations_match_the_models` fails if a model changes without a
migration. SQLite can't alter columns in place, so migrations use batch mode (table rebuilds) and
run with foreign keys off, so rebuilding a table never cascade-deletes its children.
