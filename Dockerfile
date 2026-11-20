FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=10 \
    PATH="/opt/venv/bin:$PATH" \
    MEDIA_ROOT=/media \
    CONFIG_DIR=/config \
    PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg gosu ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
WORKDIR /app
# Pinned dependencies first, in their own cached layer so a retry after a network hiccup doesn't redo them.
# setuptools is installed here so building the app below needs no network (no build isolation).
COPY requirements.lock ./
RUN pip install -r requirements.lock setuptools \
    && deno --version
COPY pyproject.toml ./
COPY app ./app
# Then the app itself, then the current yt-dlp (deliberately unpinned).
RUN pip install --no-build-isolation --no-deps . \
    && pip install "yt-dlp[default]"

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

VOLUME ["/config", "/media"]
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD python -c "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\", \"8080\")}/healthz', timeout=4)"
ENTRYPOINT ["/entrypoint.sh"]
