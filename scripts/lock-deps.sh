#!/bin/sh
# Regenerate requirements.lock (pinned runtime deps for the Docker image), leaving yt-dlp unpinned.
set -eu
cd "$(dirname "$0")/.."
tmp=$(mktemp)
uv pip compile pyproject.toml --python-version 3.12 --python-platform x86_64-manylinux_2_28 \
  --no-header --no-annotate -q -o "$tmp"
{
  echo "# Pinned runtime dependencies for the Docker image (regenerate: scripts/lock-deps.sh)."
  echo "# yt-dlp and yt-dlp-ejs are intentionally NOT pinned: they are installed fresh and self-update."
  grep -viE '^(yt-dlp|yt-dlp-ejs)==' "$tmp"
} > requirements.lock
rm -f "$tmp"
echo "requirements.lock updated; sync your venv with: uv pip install --python .venv -r requirements.lock"
echo "If bgutil-ytdlp-pot-provider changed, set the same tag on the pot-provider image in docker-compose.yml."
