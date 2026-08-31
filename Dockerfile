# syntax=docker/dockerfile:1.6
# Stable production image for yt-dlp Control Center + TikTok Clipper
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# System deps: ffmpeg (clipper + yt-dlp merging), curl (healthcheck), ca-certificates, tini (init), fonts for drawtext
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    tini \
    fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

WORKDIR /app

# Dependencies first for better layer caching
COPY requirements.txt pyproject.toml ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt \
    && yt-dlp --version && ffmpeg -version | head -n1

# App code - COPY with fallback for optional dirs (assets/fixtures may not exist in fresh clone)
COPY app ./app
COPY storage ./storage
COPY .env.example .env.example
# Ensure storage + optional dirs exist; do NOT hard-require assets/fixtures in build context (fresh clone may lack them)
RUN mkdir -p assets fixtures storage/downloads storage/uploads storage/renders storage/previews storage/cache \
    && chmod -R 775 storage

# Ensure storage dirs exist with correct perms (downloads persisten) + fonts and entrypoint
RUN mkdir -p storage/downloads storage/uploads storage/renders storage/previews storage/cache assets fixtures \
    && chmod -R 775 storage \
    && printf '#!/bin/sh\nset -e\nif [ ! -f /app/.env ] && [ -f /app/.env.example ]; then cp /app/.env.example /app/.env; echo "[entrypoint] .env created from .env.example"; fi\n# ensure storage writable for appuser\nmkdir -p /app/storage/downloads /app/storage/uploads /app/storage/renders /app/storage/previews /app/storage/cache /app/assets /app/fixtures\nchown -R 1000:1000 /app/storage 2>/dev/null || true\nchown -R 1000:1000 /app/assets /app/fixtures 2>/dev/null || true\nexec "$@"\n' > /entrypoint.sh && chmod +x /entrypoint.sh

# Create non-root user (stable uid 1000)
RUN useradd -m -u 1000 -s /bin/bash appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8000

# Healthcheck hits yt-dlp health (no redis required) + fallback to /health
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:8000/api/ytdlp/health || curl -f http://localhost:8000/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
CMD ["uvicorn", "app.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
