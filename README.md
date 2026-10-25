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
playlist, show (`https://www.youtube.com/show/VL…`) or channel URL (`https://www.youtube.com/@name`). Leave it empty for a manual list and paste video URLs on the mapping page.
A channel is downloaded completely, oldest upload first, so new uploads become the next episodes; tick
"Include Shorts" to also take the channel's Shorts. "Only titles containing" limits a mapping to one series
of a channel (e.g. one mapping per "| Episode N" series, each in its own show folder).

| Variable | Default | Meaning |
|---|---|---|
| `PUID` / `PGID` | 1000 | Owner of downloaded files (match Jellyfin's user). `/media` must be writable by this user; an empty folder Docker auto-created as root is claimed automatically |
| `TZ` | UTC | Time zone for the UI |
| `AUTH_USER` / `AUTH_PASSWORD` | unset | Enable HTTP basic auth |
| `PORT` | 8080 | Web UI port |

Volumes: `/config` (database, cookies, logs) and `/media`: the folder your Jellyfin libraries point at.
The service only writes files there; it never talks to Jellyfin itself.

## Deploy on Dokploy

1. Push this repository to a Git provider Dokploy can reach (GitHub, GitLab, Gitea, Bitbucket).
2. In Dokploy: **Create Service → Compose**, choose that repository, compose path `./docker-compose.yml`.
3. **Environment** tab: paste the variables from `.env.example` and adjust them:
   - `HOST_PORT=3230`: the host port the UI is published on.
   - `CONFIG_PATH=../files/config`: Dokploy's persistent files folder. Don't use a path inside the repo,
     because Dokploy re-clones the repository on every deploy.
   - `MEDIA_PATH`: the absolute server path your Jellyfin libraries point at.
   - `PUID`/`PGID`: the owner of that folder (`stat -c '%u %g' <MEDIA_PATH>`).
   - `AUTH_USER`/`AUTH_PASSWORD`: optional. Leave them unset behind an auth proxy such as Pangolin, but
     firewall the published port so nobody can reach the app around the proxy.
4. **Deploy**, then open `http://<server>:3230`. The UI is published straight on the port, so no Dokploy
   domain is needed. If you'd rather use a domain with HTTPS, add one under **Domains** pointing at
   container port `8080`.

The database is backed up to `CONFIG_PATH/backups/` before every schema migration. Back up `CONFIG_PATH`
itself too: it holds your mappings, order and renames.

## Avoiding YouTube blocks (HTTP 403)

YouTube restricts IPs that download a lot, for example during a big channel backfill. Three things work against that:

- **PO token provider.** The compose file runs `brainicism/bgutil-ytdlp-pot-provider` next to the app.
  It generates the "proof of origin" tokens YouTube's own player sends, so requests aren't refused with 403.
  It's internal only, and Settings shows whether it's connected. Its image tag must match the
  `bgutil-ytdlp-pot-provider` version in `requirements.lock`, because the plugin refuses a server of
  another version (a test enforces this).
- **Cool-down.** A 429, a "confirm you're not a bot", or 3 HTTP 403s in a row pause all downloads
  (60 min, doubling up to 12 h).
- **Pacing.** By default the app waits a random 60–180 s between downloads. Raise this under
  Settings → Throttling for very large backfills.



1. Create a **Shows** library pointing at the folder that holds your show mappings
   (e.g. `/media/youtube/shows`), and a **Music Videos** library for music-video mappings.
2. In each library: enable the **Nfo** metadata reader. Disable online metadata downloaders
   (TheMovieDb, TheTVDB, …) for these libraries so YouTube channels are not matched to unrelated shows.
3. Enable **real-time monitoring** so new downloads show up without a manual scan.

## Behaviour

- Sync only **adds** new playlist videos at the end. Your order, renames, season breaks and removals
  always win. Deleting a video from a playlist-backed mapping remembers it as *ignored*.
- Show folders get the channel's avatar as `poster`, its full channel art as the `fanart` backdrop and the
  cropped strip as `banner`, plus a `tvshow.nfo` with the playlist/channel description, the channel as studio
  and the first air date. These are refreshed weekly. Upload your own images in the mapping's Artwork panel;
  an uploaded image (or one you put in the folder yourself) is never overwritten. Manual lists use the first
  episode's thumbnail as poster.
- Episode and music video NFOs include the runtime and the channel as studio.
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
