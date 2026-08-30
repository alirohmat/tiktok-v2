# Deploy Docker Stabil — yt-dlp Control Center

## 1. Lean yt-dlp only (tanpa Redis, paling stabil untuk download video)

```bash
cp .env.example .env   # opsional, default sudah jalan tanpa API key
docker compose up -d ytdlp
# atau
docker compose up -d
```

Buka: http://localhost:8000  (UI), http://localhost:8000/docs (API), http://localhost:8000/api/ytdlp/health

Logs & health:
```bash
docker logs -f tiktok-ytdlp
docker inspect --format='{{.State.Health.Status}}' tiktok-ytdlp
curl http://localhost:8000/api/ytdlp/health
```

Update yt-dlp (image sudah include yt-dlp>=2024, ffmpeg, tini, curl):
```bash
docker compose build --no-cache ytdlp && docker compose up -d ytdlp
```

## 2. Full stack Clipper (butuh Redis + Celery)

```bash
docker compose --profile full up -d
# services: ytdlp, redis, api, worker
```

## 3. Dev (hot-reload)

`docker-compose.override.yml` aktif otomatis. Untuk prod tanpa reload:

```bash
docker compose -f docker-compose.yml up -d ytdlp
```

## 4. Persistensi

- Host bind: `./storage:/app/storage` — file tetap di host
- Named volume: `ytdlp_downloads:/app/storage/downloads` — aman meski bind dihapus
- Cek volume: `docker volume ls | grep ytdlp`

## 5. Troubleshooting

- Port bentrok: ubah `APP_PORT=8001` di `.env` lalu `docker compose up -d`
- Permission: container jalan sebagai `appuser` (uid 1000), storage sudah `chmod 775`
- Healthcheck gagal: `docker logs tiktok-ytdlp` → pastikan `curl` bisa akses `localhost:8000`
- Tanpa docker: `bash start-ytdlp.sh` atau `/tmp/yt-venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000`

## 6. Struktur file

- `Dockerfile` — python:3.11-slim, ffmpeg, tini, non-root, HEALTHCHECK
- `docker-compose.yml` — ytdlp (lean) + redis/api/worker (profile full)
- `.dockerignore` — exclude .venv, storage mp4, cache
- `storage/downloads/.gitkeep` — persist folder
