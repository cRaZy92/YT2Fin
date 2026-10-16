# YouTube → Jellyfin Sync — Design

Date: 2026-10-16
Status: Approved in brainstorming, pending spec review

## 1. Purpose

A self-hosted Docker service that syncs YouTube playlists into folders of a Jellyfin media
library, downloading and naming media the way Jellyfin expects. A web UI lets the user map
playlists to folders, create manual (playlist-less) mappings, and edit the contents of any
mapping: add, remove, rename, reorder.

Single user, home server / LAN. No Jellyfin API integration in this version (filesystem only).

## 2. Scope

### In scope (v1)
- Mapping types: **Show** and **Music video**. The type system is pluggable so Movie / Audio
  can be added later without restructuring.
- Mappings with a YouTube playlist (synced periodically + on demand) and manual mappings
  (no playlist).
- Add / remove / rename / reorder videos in any mapping.
- Season breaks inside a Show mapping at arbitrary positions.
- Automatic artist/song extraction for music videos, editable.
- Realtime web UI (progress, statuses, logs) via Server-Sent Events.
- Settings page (quality, format, throttling, retries, cookies, yt-dlp updates, …).
- Anti-ban throttling and automatic cool-down on rate limiting.

### Out of scope (v1)
- Jellyfin API integration (library picker, triggered rescans). Design keeps a hook for it.
- Movie and audio-only types.
- Multi-user accounts (only optional single basic-auth credential).
- Two-way sync (nothing is ever written back to YouTube).

## 3. Architecture

One container, one Python process.

```
┌──────────────── yt-jellyfin-sync container ────────────────┐
│  FastAPI web app (Jinja + htmx UI, SSE)  ── port 8080       │
│        │                                                    │
│  SQLite (/config/app.db)                                    │
│        │                                                    │
│  Scheduler ──► Sync: fetch playlist → add new Video rows    │
│  Worker    ──► Download queue (yt-dlp + ffmpeg)             │
│  Organizer ──► computes target paths, renames/moves,        │
│                writes .nfo + thumbnails                     │
│  EventBus  ──► in-process pub/sub feeding SSE clients       │
└────────────────────────────────────────────────────────────┘
 Volumes: /config (db, cookies.txt, logs)   /media (shared with Jellyfin)
```

**Stack:** Python 3.12, FastAPI, Jinja2, htmx (+ SSE extension), SortableJS (drag-and-drop),
SQLAlchemy + SQLite, APScheduler (or an asyncio scheduler loop), yt-dlp used as a Python
library, ffmpeg. No JS build step.

### Units and responsibilities

| Unit | Responsibility | Depends on |
|---|---|---|
| `models` | SQLAlchemy models + DB session | — |
| `naming` | Pure functions: compute target relative path for a video given mapping, position, season breaks, title/artist/song; sanitize filenames; parse music-video artist/song | — |
| `organizer` | Make disk match DB: plan renames (collision-safe), execute them with companions (`.nfo`, `-thumb.jpg`, `.srt`), write NFOs/artwork, startup reconciliation | `naming`, `models` |
| `ytdlp_client` | Thin wrapper over yt-dlp: flat playlist fetch, single video metadata, download with progress hook; builds options from settings; error classification | settings |
| `sync` | Additive playlist sync for one mapping | `ytdlp_client`, `models` |
| `worker` | Download queue, concurrency, throttling delays, retries/backoff, global cool-down, pause/resume | `ytdlp_client`, `organizer`, `events` |
| `scheduler` | Periodic staggered syncs, daily yt-dlp update, retry wake-ups | `sync`, `worker` |
| `events` | In-process pub/sub; SSE endpoint fans out to browsers | — |
| `settings` | Global settings stored in DB with typed defaults; per-mapping override resolution | `models` |
| `web` | Routes, templates, htmx partials, SSE endpoint, folder picker | everything above |

**Key principle:** the DB is the source of truth for what a mapping contains, its order and its names.
Every user edit changes only the DB, then calls `organizer.apply(mapping)`, which recomputes the
target path of every video in the mapping and performs only the renames that are needed. The
worker, sync, and UI all go through this one function.

## 4. Data model

**Mapping**
- `id`, `name`, `type` (`show` | `music_video`)
- `folder`: absolute path, must resolve under `/media`; unique across mappings
- `playlist_url`: nullable (null = manual mapping)
- `enabled`, `sync_interval_minutes` (nullable → global default), `last_synced_at`, `last_sync_error`
- Show only: `show_title` (used in filenames and `tvshow.nfo`), optional poster image
- Per-mapping setting overrides: JSON column (nullable keys fall back to global settings)

**Video**
- `id`, `mapping_id`, `youtube_id` (unique per mapping)
- Original metadata: `yt_title`, `channel`, `description`, `upload_date`, `duration`,
  `thumbnail_url`, `yt_artist`, `yt_track` (from yt-dlp when present)
- `position`: integer order within mapping (contiguous, 1-based)
- Show: `custom_title` (nullable → `yt_title`)
- Music video: `artist`, `song` (initially auto-parsed, user-editable)
- `status`: `pending` | `downloading` | `done` | `failed` | `unavailable`
- `error`, `attempts`, `next_retry_at`
- `file_path`: current relative path on disk of the main media file (nullable until downloaded)
- `added_at`, `source` (`sync` | `manual`)

**SeasonBreak**
- `mapping_id`, `video_id` — "a new season starts at this video". Season 1 always starts at
  position 1 implicitly. Season number = 1 + count of breaks at or before the video's position.
  If the video carrying a break is deleted, the break moves to the next video (or is dropped if
  none follows).

**IgnoredVideo**
- `mapping_id`, `youtube_id`, `title`, `ignored_at` — never re-added by sync; can be un-ignored.

**Setting**
- key/value rows (JSON values) with typed defaults defined in code.

## 5. Naming rules

All names pass through `sanitize()`: strip `< > : " / \ | ? *` and control chars, collapse
whitespace, trim trailing dots/spaces, cap length (default 180 chars before extension).

### Show
```
<folder>/
├── tvshow.nfo                 (title = show_title)
├── poster.jpg                 (optional, user-supplied or first video's thumbnail)
└── Season 01/
    ├── <Show> S01E01 <Title>.mkv
    ├── <Show> S01E01 <Title>.nfo          (episodedetails: title, plot, aired, season, episode)
    ├── <Show> S01E01 <Title>-thumb.jpg
    └── <Show> S01E01 <Title>.en.srt       (if subtitles = sidecar)
```
- Season folders: `Season NN` (zero-padded to 2).
- Episode number restarts at 1 after each season break; zero-padded to 2 (3 if > 99).
- Episode NFO `<title>` is the video title so Jellyfin displays it cleanly.

### Music video
```
<folder>/
├── <Artist> - <Song>.mkv
├── <Artist> - <Song>.nfo      (musicvideo: title, artist, plot, premiered)
└── <Artist> - <Song>-thumb.jpg
```
- Flat layout.
- Duplicate names get ` (2)`, ` (3)` suffixes in position order.

**Artist/song parsing**, in priority order:
1. yt-dlp `artist` / `track` fields (YouTube Music metadata) when present.
2. Split the title on the first ` - ` / ` – ` / ` — `: left = artist, right = song.
3. Fallback: artist = channel name with `VEVO`, ` - Topic`, `Official` suffixes stripped;
   song = title.

Then strip noise from the song: `(Official Video)`, `(Official Music Video)`, `(Lyric Video)`,
`(Lyrics)`, `(Audio)`, `[HD]`, `[4K]`, `(Visualizer)`, `(Remastered…)` and similar (case-insensitive
regex list, bracketed or parenthesized). Keep `(feat. X)` / `(ft. X)`.

## 6. Sync

- A flat playlist fetch (`extract_flat`) lists ids and titles without downloading.
- For each id not already in the mapping and not in IgnoredVideo: insert a Video at
  `max(position)+1`, `status=pending`, `source=sync`. Full metadata is fetched when the video is
  downloaded.
- Videos missing from the playlist are left untouched (additive only).
- Manual "Add video" (paste URL, single video or playlist) does the same with `source=manual`,
  for any mapping type.
- Removing a video from a playlist-backed mapping asks "Also ignore (don't re-add on sync)?",
  defaulting to yes. For a manual mapping, ignore is irrelevant.

## 7. Download worker

1. Picks the next `pending` video (or `failed` with `next_retry_at <= now`), ordered by
   mapping then position. Concurrency is a setting, default 1.
2. Downloads to the staging dir `/media/.ytjs-tmp/<video_id>/` (same filesystem so the move is
   an atomic rename) with progress hooks publishing events.
3. Container mkv (default) or mp4; embeds metadata and chapters; subtitles per settings;
   SponsorBlock per settings.
4. Format selector built from settings, with a fallback chain:
   `bv*[height<=H][vcodec^=PREF]+ba / bv*[height<=H]+ba / b[height<=H] / b`.
5. Fills metadata (artist/song via parsing for music videos), then `organizer.place(video)`
   moves the media file and companions into place, writes the NFO and thumbnail, and runs
   `organizer.apply(mapping)`.
6. **Throttle:** waits a random delay in `[min_delay, max_delay]` before the next download.

### Reliability
- yt-dlp options: `retries`, `fragment_retries`, `extractor_retries`, `socket_timeout`, plus
  optional `cookiefile`.
- **Error classification** by matching yt-dlp error text and exception type:
  - *Permanent* → `unavailable`, no auto-retry: private, removed, unavailable, copyright
    claim, region-blocked, members-only.
  - *Rate-limit* → global cool-down (below): HTTP 429, "Sign in to confirm you're not a bot".
  - *Transient* (everything else) → `failed`, retried with backoff `[10m, 1h, 6h]` (configurable),
    then stays `failed` until a manual retry.
- **yt-dlp self-update:** the entrypoint runs `pip install -U yt-dlp` before starting the app
  (toggleable). The daily check (and the "check now" button in settings) installs any newer version,
  then waits until no download is active and exits with a restart code. The entrypoint loop
  restarts the app, which picks up the new library. The UI shows the current version and
  "update installed, restarting when idle".

### Anti-ban throttling
- Random delay between downloads: `min_delay`/`max_delay`, default 30–90 s.
- yt-dlp `sleep_requests` (default 1 s), `sleep_interval`/`max_sleep_interval` (default off).
- Download `ratelimit` (default unlimited).
- Scheduled syncs run sequentially with random jitter (0–5 min) and are never back-to-back.
- **Global cool-down:** on a rate-limit error the queue pauses for `cooldown` (default 60 min),
  doubling on consecutive hits up to `cooldown_max` (default 12 h). It resets after a successful
  download. Syncs are also suspended during cool-down. The UI shows a banner with the resume time
  and a "Resume now" button.
- Manual global pause/resume.

## 8. Organizer

- `target_path(video)` comes from `naming`.
- `apply(mapping)`: for every `done` video, compute the target. Build a rename plan for files whose
  current path differs. Execute it in two phases (move each to a unique temp name in the same
  directory, then to the final name) so swaps and chains never collide. Move all companions with
  the same stem. Rewrite NFOs whose content changed (episode number, season, title, artist).
  Create season dirs as needed and remove empty season dirs. Update `file_path` in the DB within
  the same transaction scope (DB updated per successful rename).
- Writes `tvshow.nfo` for show mappings when the show title changes.
- **Startup reconciliation:** clean the staging dir, reset `downloading` → `pending`, verify each
  `done` video's file exists (missing → `pending`), then run `apply` on every mapping.
- **Deletes:** deleting a video removes its media and companions. Deleting a mapping asks whether
  to delete its files too.
- **Safety:** every path is resolved and must stay under `/media`. Mapping folders cannot overlap
  (neither may be inside the other).
- Hook for later: `on_mapping_changed(mapping)` is a no-op now and the future place to trigger a
  Jellyfin rescan.

## 9. Web UI

Server-rendered Jinja + htmx. Realtime via one SSE stream (`/events`). Events are named per
target (`video-<id>`, `mapping-<id>`, `queue`, `log`, `banner`) and htmx `sse-swap` replaces the
matching fragments.

**Live elements**
- Per-video progress bar: %, speed, ETA, stage (queued / downloading / merging / organizing).
- Status badges, attempt counts, error messages.
- Sync activity per mapping ("Checking playlist…", "3 new videos").
- Global activity panel on every page: current download(s), queue length, "Next download in
  47 s" countdown, cool-down/paused banner.
- Live log page streaming app + yt-dlp log lines (ring buffer of the last N lines plus a file
  in `/config/logs`).

**Pages**
- **Dashboard:** mapping cards (type, folder, counts done/pending/failed/unavailable, last sync,
  Sync now), the queue, and global pause/resume.
- **Mapping detail:** ordered list with drag-and-drop reorder, inline edit (title for shows,
  artist/song for music videos), live target filename preview, "Start new season here" toggle
  per row (shows), add video by URL, delete (+ ignore), retry, and a collapsible ignored list with
  un-ignore. Edits trigger `organizer.apply`.
- **New/Edit mapping:** name, type, folder picker (browses directories under `/media`, can
  create a new folder), optional playlist URL, show title, sync interval, and per-mapping
  overrides of quality settings.
- **Settings:**
  - Quality: max resolution (2160/1440/1080/720/480), container (mkv/mp4), preferred
    codec (any/h264/av1/vp9)
  - Subtitles: off / embed / sidecar, languages, include auto-generated
  - SponsorBlock: off / mark chapters / remove segments (categories)
  - Sync: default interval, jitter
  - Throttling: min/max delay between downloads, sleep_requests, sleep_interval, rate limit,
    concurrency, cool-down base/max
  - Retries: backoff schedule
  - yt-dlp: version, auto-update toggle, check now
  - Cookies: upload/remove `cookies.txt`
  - Naming: max filename length

## 10. Deployment

- `Dockerfile`: `python:3.12-slim` + `ffmpeg`. The app runs as `PUID:PGID` (entrypoint drops
  privileges) so files are readable by Jellyfin.
- Volumes: `/config`, `/media`.
- Env: `PUID`, `PGID`, `TZ`, optional `AUTH_USER` + `AUTH_PASSWORD` (HTTP basic auth; no auth if
  unset), `PORT` (default 8080).
- An example `docker-compose.yml` sits alongside Jellyfin sharing the same media volume.

## 11. Testing (pytest)

- `naming`: episode numbering with season breaks, padding, sanitization, length cap; artist/song
  parsing against a fixture list of real-world messy titles; duplicate music-video names.
- `organizer`: rename planning and execution in a temp dir, covering moves, swaps, cycles,
  season changes, companions, empty season dir cleanup, and startup reconciliation.
- `sync`: yt-dlp mocked; additive behaviour, ignored ids skipped, positions appended.
- `worker`: error classification on sample yt-dlp messages; backoff scheduling; cool-down
  doubling/reset; throttle delay bounds (clock/sleep injected).
- `settings`: override resolution.
- Web: route smoke tests with FastAPI TestClient (CRUD, reorder, season break toggle).
- Optional manual integration test (marked, skipped by default) downloading one short real video.
