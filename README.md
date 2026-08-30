# TikTok v2 — AI Video Clipper (Anti-Duplication)

Automated vertical 9:16 clipper that bypasses TikTok/Shopee duplicate detection via visual/audio/metadata alterations.

## Pipeline

1. **Transcription** — FFmpeg extract → chunk 3min → Groq Whisper Large v3 (`timestamp_granularities=word`) → stitch with offset.
2. **Brain** — Muse Spark analyzes transcript → `ClipPlan` {clips 15-45s, dead_air, broll_cues} via hardcoded System Prompt.
3. **B-Roll** — Coverr API `GET /videos?query=&urls=true&sort=trending` → vertical filter → SQLite cache → download `mp4_preview`.
4. **Render** — FFmpeg DNA alterations:
   - Auto-framing 16:9→9:16 via MediaPipe face tracking + EMA smoothing
   - 5% dynamic `zoompan`, 1-frame noise every 7s
   - Pitch +1% `asetrate`, trending music @10%, ultrasonic 19kHz via `aevalsrc+sine+amix`
   - Jump cuts (`dead_air`), B-Roll glitch overlay, kinetic `drawtext` hook 0-3s, `-map_metadata -1` + randomized `creation_time`

## Setup

```bash
uv python pin 3.11   # or pyenv install 3.11
uv venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # fill GROQ_API_KEY, MUSE_API_KEY, MUSE_BASE_URL, COVERR_API_KEY

# Redis
docker compose up -d redis
# Or: redis-server --daemonize yes

# API + Worker
uvicorn app.api.main:app --reload --port 8000
celery -A app.workers.celery_app worker --loglevel=info --concurrency=2
```

## API

- `GET /health` — ffmpeg + redis status
- `POST /clip` — multipart `file` upload → returns `job_id` (202) or `SUCCESS` with renders if redis unavailable (eager fallback)
- `GET /jobs/{job_id}` — status + render paths
- `GET /renders/{job_id}/{filename}` — download MP4

## Env Vars

See `.env.example`: `GROQ_API_KEY`, `GROQ_WHISPER_MODEL`, `MUSE_API_KEY`, `MUSE_BASE_URL`, `MUSE_MODEL`, `COVERR_API_KEY`, `COVERR_BASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, `MUSIC_PATH`

## Tests

```bash
pytest -v
pytest tests/test_transcription.py tests/test_llm.py tests/test_coverr.py tests/test_render.py -v
ruff check .
mypy app/
```

## FFmpeg Filters Verified

`zoompan`, `asetrate`, `aresample`, `atempo`, `aevalsrc`, `sine`, `amix`, `noise`, `crop`, `scale`, `overlay`, `drawtext`, `-map_metadata -1`

## Notes

- Groq mock if `GROQ_API_KEY` missing; Muse mock if `MUSE_API_KEY` missing; Coverr mock `mock.coverr.co` → generates black 720x1280 placeholder.
- Ultrasonic 19kHz may be stripped by AAC lowpass (~16kHz); disable via `RenderEngine(enable_ultrasonic=False)` if spectrogram check fails.
- Auto-framing falls back to center crop if no face detected.
```
