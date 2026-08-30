# syntax=docker/dockerfile:1.6
# Stable production image for yt-dlp Control Center + TikTok Clipper
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps: ffmpeg (clipper + yt-dlp merging), curl (healthcheck), ca-certificates, tini (init)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    tini \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Dependencies first for better layer caching
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# App code
COPY app ./app
COPY storage ./storage
COPY assets ./assets
COPY fixtures ./fixtures

# Ensure storage dirs exist with correct perms (downloads persisten)
RUN mkdir -p storage/downloads storage/uploads storage/renders storage/previews storage/cache \
    && chmod -R 775 storage

# Create non-root user (stable uid 1000)
RUN useradd -m -u 1000 -s /bin/bash appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Healthcheck hits yt-dlp health (no redis required) + fallback to /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/ytdlp/health || curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
