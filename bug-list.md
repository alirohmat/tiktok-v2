# Bug List — tiktok-v2

> Sumber: analisis `app/`, `tests/`, `pyproject.toml`, `docker-compose.yml`. Total 27 bug.

## Fase 1 — P0 Kritis (SELESAI)
Patch: `app/api/routes.py`, `app/api/main.py`, `app/api/ytdlp.py`, `app/services/ytdlp_service.py`, `app/core/config.py`

| # | Bug | Lokasi | Severity | Status |
|---|-----|--------|----------|--------|
| 1 | Tanpa auth — POST/DELETE hapus storage terbuka | `app/api/routes.py:492`, `app/api/ytdlp.py:71` | Kritis | ✅ Selesai — `api_key` + middleware `X-API-Key`/`Bearer` `app/api/main.py:34`, opt-in via `CLIPPER_API_KEY` |
| 2 | SSRF — `url` cuma cek `http://` | `app/services/ytdlp_service.py:137`, `app/api/routes.py:652` | Kritis | ✅ Selesai — `validate_download_url()` `ipaddress+getaddrinfo` block `127/10/172.16/192.168/169.254/metadata/localhost` |
| 3 | `asyncio.run()` dalam `BackgroundTasks` crash | `app/api/routes.py:665`, `app/api/ytdlp.py:91` | Kritis | ✅ Selesai — ganti `new_event_loop()+run_until_complete` + close |
| 6 | Thread leak — tiap clip bikin `Thread` tanpa pool | `app/api/routes.py:192` | Tinggi | ✅ Selesai — `ThreadPoolExecutor(max_workers=2)` `app/api/routes.py:15,193` |
| 11 | Regex `^[a-f0-9-]{8,}$` lolos `--------` | `app/api/routes.py:26`, `app/api/main.py:41` | Sedang | ✅ Selesai — `^(UUID\|hex8-32)$` |

Verifikasi Fase 1: `validate_download_url` block loopback/metadata OK, `_JOB_ID_RE` UUID True / `--------` False, `ruff` no new error, `pytest` 17/19 (2 fail pre-existing unrelated).

## Fase 2 — P1 Stabilitas (SELESAI)
Patch: `app/api/routes.py`, `app/api/ytdlp.py`, `app/services/transcription.py`, `app/workers/celery_app.py`, `app/utils/cache.py`, `app/services/ytdlp_service.py`

| # | Bug | Lokasi | Severity | Status |
|---|-----|--------|----------|--------|
| 5 | Race `CLIP_JOBS/CLIP_LOGS` dict tanpa lock (thread+SSE) | `app/api/routes.py:92` | Tinggi | ✅ Selesai — `_CLIP_LOCK` `threading.Lock` semua `CLIP_JOBS/LOGS` `read+write` + `SSE` `snap` |
| 7 | SSE `redis.from_url` tiap tick 1.5s tanpa close — FD leak | `app/api/routes.py:445` | Tinggi | ✅ Selesai — `try/finally` `close()` per tick + `socket_keepalive` |
| 8 | `_groq_throttle` sleep di luar lock — throttle bocor | `app/services/transcription.py:22` | Tinggi | ✅ Selesai — `sleep` di dalam `with _groq_lock` |
| 9 | `task_time_limit=600` kill podcast panjang | `app/workers/celery_app.py:20` | Tinggi | ✅ Selesai — `3600`/`3300` |
| 10 | `CoverrCache.get` DELETE pakai koneksi beda — `database locked` | `app/utils/cache.py:36,85` | Tinggi | ✅ Selesai — `get_sync` 1 conn `timeout=5`, `async get` 1 `aiosqlite` conn |
| 13 | Fallback glob 5 file terbaru — salah file concurrent | `app/services/ytdlp_service.py:596` | Sedang | ✅ Selesai — `claimed` set + `iterdir` `mtime` closest ke `job.created_at` |
| 25 | `_dir_stats` `rglob` block event loop | `app/services/ytdlp_service.py:405`, `app/api/routes.py:217` | Rendah | ✅ Selesai — `_dir_stats_async` `asyncio.to_thread` + `SSE` `to_thread(_disk_info)` |
| 4 | Rate-limit in-memory bypass multi-worker | `app/api/ytdlp.py:75` | Tinggi | ✅ Selesai — `redis scard("ytdlp:active")` fallback in-memory, `sadd/srem` di `run_download_job` |

Verifikasi Fase 2: `py_compile` 6 file OK, `pytest` 17/19 (2 fail pre-existing `test_orchestration` `GROQ_API_KEY` + `test_render` `asetrate`), Throttle lock + Celery 3600 + FD close cek manual OK.

## Fase 3 — P2 Correctness (SELESAI)
Patch: `app/services/llm.py`, `app/utils/ffmpeg_builder.py`, `app/services/render.py`, `app/api/routes.py` (verifikasi)

| # | Bug | Lokasi | Severity | Status |
|---|-----|--------|----------|--------|
| 12 | `get_job` return PENDING bukan 404 untuk job tak ada | `app/api/routes.py:591` | Sedang | ✅ Selesai — verifikasi `raise 404` storage+celery, `GET /jobs/{uuid}` 404 bukan PENDING |
| 14 | Silent `_mock_plan` saat key kosong — user kira sukses | `app/services/llm.py:137` | Sedang | ✅ Selesai — `logger.warning` + `niche_advisory=[MOCK]` + `niche_score min(60)` |
| 15 | `_extract_json` return invalid JSON | `app/services/llm.py:26` | Sedang | ✅ Selesai — `candidate invalid → pass` return original text trigger repair flow |
| 16 | Label FFmpeg `v80/v90` collision | `app/utils/ffmpeg_builder.py:242` | Sedang | ✅ Selesai — `_next_v/_next_a` counter `v200+/a200+` ganti semua `len+80/90/44/45/10` |
| 17 | `rel[:70]` potong kata tanpa warning | `app/utils/ffmpeg_builder.py:130` | Sedang | ✅ Selesai — `logger.warning ASS truncate N ->70 (clip s-e)` |
| 18 | Filename `:.0f` collision (`0.4` vs `0.6` → `0`) | `app/services/render.py:148` | Sedang | ✅ Selesai — `int(s*1000):06d` ms precision anti-collision |
| 19 | `random_creation_time()` non-deterministik | `app/utils/ffmpeg_builder.py:137` | Sedang | ✅ Selesai — `seed=src:clip_start:clip_end` hash deterministik, fallback random compat |
| 20 | `_escape_drawtext` tidak escape `=` `,` | `app/utils/ffmpeg_builder.py:41` | Sedang | ✅ Selesai — `= -> \=`, `, -> \,` |

Verifikasi Fase 3: `py_compile` 4 file OK, `_escape_drawtext` `=` `,` OK, `random_creation_time` deterministik OK, `_extract_json` invalid→repair OK, `pytest` 17/19 (2 fail pre-existing `test_orchestration` `meta` + `test_render` `asetrate`), `GET /jobs/{uuid}` 404 OK.

## Fase 4 — P3 Hardening (BELUM)
| # | Bug | Lokasi | Severity | Status |
|---|-----|--------|----------|--------|
| 21 | `cap.release()` tidak di `finally` — leak | `app/utils/autoframe.py:90` | Rendah | ⬜ Belum |
| 22 | `ffprobe` tanpa timeout — hang file korup | `app/utils/audio.py:10` | Rendah | ⬜ Belum |
| 23 | `tmp_json` race concurrent job sama src | `app/services/auto_editor.py:95` | Rendah | ⬜ Belum |
| 24 | `extra="ignore"` — typo env diam | `app/core/config.py:11` | Rendah | ⬜ Belum |
| 26 | `AsyncResult` polling tanpa timeout | `app/workers/tasks.py:315` | Rendah | ⬜ Belum |
| 27 | CORS `allow_methods/headers=["*"]` longgar | `app/api/main.py:25` | Rendah | ⬜ Belum |

## Ringkas
- Fase 1: 5/5 selesai.
- Fase 2: 8/8 selesai.
- Fase 3: 8/8 selesai.
- Fase 4: 6 bug belum — next Fase 4.
- Skipped: RBAC penuh, diarization — add when multi-tenant.
