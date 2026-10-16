FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/opt/venv/bin:$PATH" \
    MEDIA_ROOT=/media \
    CONFIG_DIR=/config \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
WORKDIR /app
COPY pyproject.toml requirements.lock ./
COPY app ./app
# Pinned dependencies first, then the app itself, then the current yt-dlp (deliberately unpinned).
RUN pip install -r requirements.lock \
    && pip install --no-deps . \
    && pip install "yt-dlp[default]" \
    && deno --version

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/media"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/healthz', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
