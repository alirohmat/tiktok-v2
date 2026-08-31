# TikTok v2 — AI Video Clipper (Anti-Duplication) — Plan

## Goal

Build an automated, high-retention AI Video Clipper that ingests long-form source video and outputs multiple 15–45s vertical (9:16) clips engineered to bypass TikTok / Shopee Affiliate duplication detection (visual fingerprinting, audio matching, metadata analysis). The system must run as a Python 3.11+ service with FastAPI + Celery + Redis orchestration, Groq Whisper Large v3 transcription with word-level timestamps, Muse Spark semantic clipping, Coverr B-Roll sourcing with SQLite caching, and an FFmpeg rendering engine with DNA-alteration filters (auto-framing, zoompan, noise injection, pitch shift, ultrasonic jamming, jump cuts, glitch B-Roll, kinetic typography, metadata wipe).

Primary deliverable: runnable project in `/root/tiktok-v2` with clean modular typed code, `requirements.txt` / `pyproject.toml`, Pydantic validation, and Celery chain Phases 1→4.

## Success Criteria

- **Setup**: `pip install -r requirements.txt` succeeds on Python 3.11; `uvicorn` + `celery worker` + `redis` boot via `docker-compose.yml` or documented local commands; project structure matches plan (modules + models + tasks).
- **Phase 1**: `async_chunk_audio()` splits any input >25 MB into ≤3-min chunks via FFmpeg, dispatches Celery tasks respecting Groq rate limits (concurrency + exponential backoff on 429), requests `timestamp_granularities=["word"]` + `response_format=verbose_json`, stitches word segments with corrected global timestamps; preserves `word.start/end` for every word.
- **Phase 2**: Muse Spark call hardcodes System Prompt, validates response with Pydantic schema (`clips{start_time,end_time,hook_text,virality_score}`, `dead_air{start,end}`, `broll_cues{timestamp,keywords_en,fallback_en}`), enforces 15–45s clip duration (rejects/clips out-of-range), returns deterministic JSON only.
- **Phase 3**: Coverr client `GET https://api.coverr.co/videos?query={keyword}&urls=true&sort=trending` with Bearer auth, filters `is_vertical==true`, caches `keyword→video_id` in SQLite (TTL 24h), downloads `mp4_preview` (fallback to `mp4`), handles empty results via `fallback_en`.
- **Phase 4**: FFmpeg command builder produces single re-muxed output per clip with all DNA alterations verifiable via `ffprobe`/`ffmpeg -filters` inspection: MediaPipe auto-crop 16:9→9:16, 5% dynamic `zoompan`, 1-frame transparent noise every 7s, `asetrate` +1% pitch shift, background music mix at 10%, 18–20kHz ultrasonic sine via `aevalsrc`/`amix`, `dead_air` jump-cut removal, Coverr B-Roll glitch overlay at cues, 0–3s kinetic hook typography (word-by-word), final `-map_metadata -1` + randomized `creation_time`.
- **Orchestration**: Celery chain `extract→transcribe→analyze→source_broll→render` runs end-to-end for a 10-minute 16:9 fixture and produces at least 2 valid 9:16 MP4s <60s each; failed chunk retries do not lose stitched state.
- **Quality**: `mypy --strict` passes on core modules; `pytest` covers transcription stitching, LLM schema, Coverr cache, FFmpeg builder; no EXIF/moov leak in output (`ffprobe -show_format` shows no original tags).

## Context And Current Facts

- **Workspace**: `/root/tiktok-v2` is empty except `.agents/plans/` (created this session). No git repo, no `requirements.txt`, no `pyproject.toml`. Verified via `ls -la /root/tiktok-v2` — only `.` `..` `.agents`.
- **Toolchain**: Python 3.10.12 (`python3 --version`) vs spec 3.11+ — gap. FFmpeg 4.4.2 + ffprobe available with `libx264`, `libx265`, `libvpx`, `rubberband`, `soxr`. No `redis-cli` (Redis not installed). `uv 0.11.24` available. `celery` not yet on PATH. 4 GB RAM, ~1.7 GB available — sufficient for FFmpeg but not for local Whisper large.
- **Prior art in host**: `/root/workspace/skill_short_video_generator.md` documents a Kaggle-dispatch short-video generator (Pexels + edge-tts + FFmpeg concat) triggered by `kaggle kernels push`. Not reusable directly — different APIs (Groq/Coverr/Muse Spark) and local Celery architecture required. Reference for Pexels/FFmpeg concat patterns only.
- **External facts gathered (discovery-level)**:
  - Groq Whisper uses `whisper-large-v3-turbo` / `whisper-large-v3`, supports `timestamp_granularities: ["word"]` + `verbose_json`, has 25 MB request limit requiring chunking; free-tier 429s require backoff (seen in github.com/viljolehmus-cyber/clipping_tool and github.com/adrbn/subifi snippets).
  - Coverr API docs at `https://api.coverr.co/docs` describe `GET /videos?query=&urls=true&sort=trending` with Bearer auth; response shape disputed (`hits` vs `videos`), `mp4_preview` vs `mp4_download` fields (github.com/harry0703/MoneyPrinterTurbo/pull/1008). Discovery only — underlying docs not yet fetched as non-empty authoritative body.
  - `ffmpeg-python` is a thin wrapper over `subprocess.Popen(["ffmpeg", ...])`; production guidance favors raw `subprocess` for exact filter parity and injection safety (micropyramid.com/blog, ffmpeg-python issues). `MoviePy` is convenient but slower and less precise for complex `filter_complex` graphs.
  - MediaPipe Face Detection / FaceLandmarker (468 points) + OpenCV provides stable 9:16 auto-crop via sampled frame averaging (clipforge, hector-ai-91/video-podcast-clipper patterns).

## Constraints And Non-Goals

- **Stack locked**: Python 3.11+, FastAPI, Celery+Redis, Groq Whisper Large v3, Muse Spark (OpenAI-compatible), Coverr API + SQLite cache, FFmpeg via `ffmpeg-python` or `subprocess`, MoviePy allowed, OpenCV+MediaPipe. Do not substitute providers.
- **Rate-limit respect**: Groq Free Tier must not be hammered; sequential or throttled async chunk processing with retry/backoff is required, not fire-all-chunks.
- **No local GPU assumption**: System must not require CUDA/local Whisper; all heavy transcription via Groq.
- **Security**: Coverr/Groq/Muse Spark keys via `.env` + `python-dotenv`, never hardcoded; SQLite cache only for `keyword→video_id`, not for video bytes.
- **Non-goals (v1)**:
  - No recommendation algorithm beyond Muse Spark virality scoring.
  - No TikTok/Shopee direct upload — output is local MP4s.
  - No web UI beyond FastAPI `/health`, `/clip` (async job submit), `/jobs/{id}` (status/result) and optional minimal upload form.
  - No distributed storage (S3) — local `storage/{uploads,previews,renders}`.
  - No advanced speaker diarization.

## Key Decisions

### D1: FFmpeg invocation — `subprocess` (argv list) primary, `ffmpeg-python` optional helper
- **Recommended**: Build final render commands with `subprocess.run(["ffmpeg", "-y", ...], check=True)` and explicit `filter_complex` strings. Keep `ffmpeg-python` as optional utility for simple extractions only.
- **Why**: Full parity with CLI, exact filter ordering for `zoompan`, `asetrate`, `aevalsrc`, `glitch`, `-map_metadata -1`; avoids quoting/injection issues; easier to test via command-string assertions. `ffmpeg-python` compilation obscures complex graphs and lags FFmpeg feature coverage.
- **Rejected**: Pure `ffmpeg-python` for render — hides filter graph errors until runtime, harder to inject ultrasonic + zoom + noise in one `filter_complex`.
- **Evidence gap**: Cited blog/issue snippets are discovery; will verify filter syntax against `ffmpeg -h filter=zoompan` / `aevalsrc` locally before finalizing command builder.

### D2: Async strategy — Celery + Redis (spec-mandated) with rate-limit throttling
- **Recommended**: Celery tasks `transcribe_chunk`, `stitch_transcript` with `rate_limit="10/m"` per worker, `acks_late`, `retry_backoff` on 429, plus Redis result backend. `async_chunk_audio()` in FastAPI creates chain, not direct `asyncio.gather` to Groq.
- **Why**: Satisfies spec crucial requirement; decouples 3-min chunk fan-out from API limits; Celery canvases (`group` + `chord`) naturally model stitch-after-all-chunks.
- **Rejected**: Pure `asyncio` + `httpx` without Celery — violates spec and loses durability across 429 retries; RQ/Dramatiq — not spec.

### D3: Transcription stitching — offset correction, not naive concat
- **Recommended**: Each chunk returns `words[]` with local `start/end`; stitcher adds `chunk_offset_seconds` (computed from chunk `start_time`) to every word/segment, merges sorted list, validates monotonicity.
- **Why**: Groq word timestamps are chunk-relative; without offset correction jump-cut and kinetic typography drift.
- **Alternative rejected**: Concatenating raw JSON arrays — produces overlapping 0.0 timestamps.

### D4: LLM output contract — Pydantic strict validation + repair loop
- **Recommended**: `ClipPlan` / `Clip` / `DeadAir` / `BrollCue` Pydantic v2 models with `field_validator` for `15 <= end-start <= 45`, `0<=timestamp<=duration`. System Prompt forces JSON-only; parser tries `json.loads`, then `json repair` (strip markdown fences), then single retry to LLM with error feedback.
- **Why**: Muse Spark can emit trailing commas / prose; naive `json.loads` fails pipeline.
- **Rejected**: Regex extraction — brittle.

### D5: Coverr caching — SQLite with `aiosqlite` + TTL
- **Recommended**: `coverr_cache.db` with `CREATE TABLE cache(keyword TEXT PRIMARY KEY, video_id TEXT, preview_url TEXT, is_vertical INT, created_at INTEGER)`; lookup before API call; 24h TTL; `is_vertical` filter in Python (Coverr field `is_vertical` or `height>width` fallback).
- **Why**: Minimizes Coverr calls, handles trending sort turnover; SQLite is zero-ops vs Redis for this use.
- **Rejected**: Redis cache for Coverr — extra dependency, no TTL persistence needed.

### D6: Auto-framing — MediaPipe Face Detection (BlazeFace) + temporal smoothing
- **Recommended**: Sample 1 fps via OpenCV, run `mp.solutions.face_detection` (or `tasks.vision.FaceLandmarker`), compute per-frame face centroid x, derive `crop_x = clamp(centroid - 0.5*crop_w, 0, W-crop_w)` where `crop_w = H*9/16`, smooth with EMA `alpha=0.15`, encode as `crop` filter keyframes or pre-compute static average crop if variance low.
- **Why**: Pure center crop cuts speaker; MediaPipe > Haar for side angles; smoothing prevents jitter.
- **Tradeoff**: Full per-frame `crop` expressions require `sendcmd` or pre-rendered segments; v1 may use averaged static crop + document dynamic upgrade path.

### D7: Ultrasonic jamming — `aevalsrc` 19kHz sine mixed at low volume
- **Recommended**: `aevalsrc=0:d=DURATION:s=48000 [ultra]; [ultra] volume=0.02, lowpass=f=20000, highpass=f=18000 [ultra_f]; [0:a][ultra_f] amix=inputs=2:duration=longest:dropout_transition=0 [mixed]`
- **Caution**: Audibility and codec survival (AAC 128k lowpasses ~16kHz) may nullify 18–20kHz; plan validates with `ffmpeg -lavfi` + spectral analysis (`ffprobe -show_streams`, `sox spectrogram`) and documents fallback: if codec strips ultrasound, reduce to 16kHz + low volume or make filter configurable. Spec says CRITICAL but implementation must be testable.

### D8: Project structure — modular `app/` with typed boundaries
- **Recommended**: `app/core/config.py`, `app/models/schemas.py`, `app/services/transcription.py`, `app/services/llm.py`, `app/services/coverr.py`, `app/services/render.py`, `app/workers/celery_app.py` + `tasks.py`, `app/api/routes.py`, `app/utils/ffmpeg_builder.py`, `tests/`.
- **Rejected**: Single `main.py` — untestable, violates clean modular + typed requirement.

## Recommended Approach

Implement as **monolithic FastAPI + Celery worker** (single repo, single Redis) to minimize ops. Phases executed as Celery chain; FastAPI only enqueues and polls.

1. **Bootstrap** Python 3.11 via `uv` / `pyenv` (host is 3.10), init git, `pyproject.toml` + `requirements.txt` with pinned versions, `.env.example`, `docker-compose.yml` (redis:7-alpine), `storage/` + `assets/` scaffolding, `app/` skeleton, `pytest` + `ruff` + `mypy` config.
2. **Transcription module first** (highest risk: rate limits + timestamp correctness) — implement `extract_audio()` (FFmpeg `aac→wav 16k mono`), `async_chunk_audio(duration=180, overlap=0.5)` (uses `ffprobe` duration, splits with `-ss`/`-t`), Celery `transcribe_chunk` with Groq client (`openai` SDK pointing to Groq base_url or `groq` SDK), `stitch_transcripts(chunks)` with offset fix.
3. **LLM module** — `MuseClient` (OpenAI-compatible `base_url` for Muse Spark), hard-coded `SYSTEM_PROMPT` constant, `analyze_transcript(words, duration)` → `ClipPlan`, Pydantic validators for 15–45s and schema.
4. **Coverr module** — `CoverrClient.search(keyword, fallback)`, SQLite cache layer, `download_preview(url, dest)` streaming, vertical filter.
5. **Render engine** — `FFmpegBuilder` that composes full `filter_complex` step-by-step in spec order: (a) audio extraction/pitch/ultra/music, (b) video auto-crop/zoom/noise, (c) dead-air cut via `select` or pre-trim concat, (d) B-Roll glitch (`xfade` with `glitch` or `tblend`), (e) kinetic typography via `drawtext` with word timestamps (or `ass` subtitle), (f) final `-map_metadata -1 -metadata creation_time=...`.
6. **Orchestration** — Celery canvas `chain(extract.s(), chord(group(transcribe_chunk.s(c) for c in chunks), stitch.s()) | analyze.s() | source_broll.s() | render.s())`, FastAPI routes `/clip` (upload → enqueue), `/jobs/{id}`, `/health`; error handling + idempotency via job_id.
7. **Validation hardening** — add `tests/` fixtures (10s sample video), mock Groq/Coverr, assert FFmpeg command contains each required filter token.

## Work Plan

### Phase 0 — Setup & Scaffolding (dependency: none)
- **Owner**: `app/` + infra
- **Tasks**:
  - Init git, `.gitignore` (`.env`, `*.db`, `storage/`, `__pycache__`), `README.md` with run instructions.
  - Resolve Python 3.11: `uv python pin 3.11` or `pyenv install 3.11` + `uv venv`; document fallback if host stays 3.10 (compat shim).
  - `pyproject.toml` + `requirements.txt`: `fastapi[standard]`, `celery[redis]`, `redis`, `groq`, `openai`, `pydantic>=2`, `pydantic-settings`, `python-dotenv`, `ffmpeg-python`, `moviepy`, `opencv-python`, `mediapipe`, `aiosqlite`, `httpx`, `pytest`, `pytest-asyncio`, `ruff`, `mypy`.
  - `docker-compose.yml`: `redis:7-alpine`, `api` (uvicorn), `worker` (celery), volume mounts.
  - Create `app/core/config.py` (Settings via `pydantic-settings`), `app/models/schemas.py` (empty stubs), `app/services/`, `app/workers/celery_app.py`, `app/api/routes.py`, `tests/`, `storage/{uploads,previews,renders,cache}`.
- **Validation**: `uv pip install -r requirements.txt` succeeds; `docker compose config` validates; `pytest --collect-only` 0 errors; `ruff check` + `mypy` pass on skeleton.

### Phase 1 — Transcription Module (depends: Phase 0)
- **Files**: `app/services/transcription.py`, `app/workers/tasks.py` (transcribe), `app/utils/audio.py`, `tests/test_transcription.py`
- **Tasks**:
  - `extract_audio(src: Path, dst: Path)` → `ffmpeg -y -i src -vn -acodec pcm_s16le -ar 16000 -ac 1 dst` via subprocess.
  - `get_duration(path)` via `ffprobe -show_format`.
  - `async_chunk_audio(audio_path, chunk_sec=180) -> list[Chunk]` splits with `ffmpeg -ss START -t DURATION`; handle <25 MB vs duration logic; return chunks with `index, start_time, path`.
  - Groq client: `client.audio.transcriptions.create(model="whisper-large-v3", file=..., response_format="verbose_json", timestamp_granularities=["word"], language="en")`; support `whisper-large-v3-turbo` fallback via config.
  - Celery `transcribe_chunk(chunk_path, offset)` with `@shared_task(bind=True, max_retries=5, rate_limit="10/m")`, catches 429 → `self.retry(countdown=2**retries * 10, exponential)`.
  - `stitch_transcripts(results: list[GroqResponse], chunks: list[Chunk]) -> Transcript` adds offset to each `word.start/end` and `segment.start/end`, sorts, validates monotonic, returns `words: list[Word]` with global timestamps.
  - Preserve word-level timestamps end-to-end (Pydantic `Word(start: float, end: float, word: str)`).
- **Validation**: Unit test with mocked Groq (2 chunks, overlapping words) asserts stitched timestamps == chunk1.words + (chunk2.words + 180.0); integration test with 10s fixture does real Groq call if `GROQ_API_KEY` present else mocked; `pytest tests/test_transcription.py -v` passes.

### Phase 2 — Semantic Brain / Muse Spark (depends: Phase 1 schemas)
- **Files**: `app/services/llm.py`, `app/models/schemas.py`, `tests/test_llm.py`
- **Tasks**:
  - Define Pydantic models:
    ```python
    class Clip(BaseModel): start_time: float; end_time: float; hook_text: str; virality_score: int = Field(ge=0, le=100)
    class DeadAir(BaseModel): start: float; end: float
    class BrollCue(BaseModel): timestamp: float; keywords_en: str; fallback_en: str
    class ClipPlan(BaseModel): clips: list[Clip]; dead_air: list[DeadAir]; broll_cues: list[BrollCue]
    ```
    Validators: `end-start in [15,45]`, `start<end`, `timestamp` within duration, `dead_air` non-overlapping.
  - Hardcode `SYSTEM_PROMPT: Final[str] = """You are a viral clip detector... Return ONLY JSON with schema {...} ... enforce 15-45s ..."""` — must be module constant, not env.
  - `MuseClient.analyze(transcript: Transcript, duration: float) -> ClipPlan` builds user prompt with word-timestamped transcript + duration, calls `openai.OpenAI(base_url=MUSE_BASE_URL, api_key=...)` with `response_format={"type":"json_object"}`, temp 0.2.
  - Parse with `ClipPlan.model_validate_json(raw)`; on `ValidationError` → repair prompt (send validation errors back) and retry once.
  - Log virality_score distribution.
- **Validation**: `pytest tests/test_llm.py` with fixtures: valid JSON passes, 14s clip rejected, 46s clip rejected, missing field triggers retry mock; manual E2E prints `ClipPlan` for sample transcript.

### Phase 3 — B-Roll Sourcing / Coverr (depends: Phase 2 for cue shape)
- **Files**: `app/services/coverr.py`, `app/utils/cache.py`, `tests/test_coverr.py`
- **Tasks**:
  - `CoverrClient.__init__(api_key, base_url="https://api.coverr.co")` with `httpx.AsyncClient` + Bearer header.
  - `search(keyword, fallback) -> CoverrVideo | None`: check `cache.get(keyword)` first; if miss, `GET /videos?query={keyword}&urls=true&sort=trending` (fallback to `fallback` keyword on empty `hits`), filter `is_vertical is True` else `height>width` fallback, pick first hit, cache `keyword→(video_id, preview_url)`.
  - `download(url, dest)` streams `mp4_preview` (fallback to `mp4`), validates content-type video/mp4, size <50 MB.
  - `Cache` via `aiosqlite` with `CREATE TABLE IF NOT EXISTS coverr_cache(keyword TEXT PRIMARY KEY, video_id TEXT, preview_url TEXT, is_vertical INTEGER, created_at INTEGER)`; TTL 24h (86400s); `get` checks expiry.
  - Download concurrency limit 3 via `asyncio.Semaphore`.
- **Validation**: Mocked `httpx` tests: cache hit returns without HTTP call; vertical filter drops horizontal; fallback keyword succeeds when primary empty; SQLite TTL expiry correctly misses; download writes file.

### Phase 4 — Video DNA Alteration & Rendering (depends: Phases 1-3)
- **Files**: `app/services/render.py`, `app/utils/ffmpeg_builder.py`, `app/utils/autoframe.py`, `tests/test_render.py`
- **Tasks** (build `FFmpegBuilder` incrementally):
  1. **Auto-Framing** (`autoframe.py`): `detect_crop_window(video_path) -> tuple[x,y,w,h]` via MediaPipe sampling; if `width/height == 16/9` ±0.1, compute 9:16 crop; else passthrough.
  2. **Visual fingerprint**: `zoompan` slow zoom: `scale=iw*1.05:ih*1.05,zoompan=d=1:s=720x1280:fps=30` or `zoompan=z='if(lte(zoom,1.0),1.0,min(zoom+0.0005,1.05))':d=1`; noise frame: `tblend=all_mode=addition:opacity=0.02:enable='eq(mod(n,210),0)'` (1 per 7s at 30fps = 210 frames) — verify transparency via `colorchannelmixer`.
  3. **Audio fingerprint**: pitch `asetrate=48000*1.01,aresample=48000,atempo=1/1.01` or `rubberband=pitch=1.01`; background music `amix` at `volume=0.1`; ultrasonic `aevalsrc=0:d=DURATION:s=48000 [ultra]` → `sine=frequency=19000:beep_factor=1` mixed via `amix`.
  4. **Jump cuts**: remove `dead_air` via `-vf select='not(between(t,12.1,12.8)+...)' ,setpts=N/FRAME_RATE/TB` or via segment concat demuxer (preferred for audio sync).
  5. **B-Roll overlay**: for each cue, `ffmpeg -i main -i broll -filter_complex "[0:v][1:v] xfade=transition=glitch:duration=0.3:offset=TS [v]"` or `overlay` with `enable='between(t,TS,TS+2)'`.
  6. **Kinetic typography**: first 3s hook words via `drawtext=text='word':x=(w-text_w)/2:y=h*0.7:enable='between(t,WORD_START,WORD_END)'` per word (generate `ass` file alternative for cleaner).
  7. **Metadata wipe**: `-map_metadata -1 -metadata creation_time=2024-03-15T12:00:00Z` (randomized per render via `datetime.utcnow` jitter).
  - `RenderEngine.render_clip(src, clip: Clip, transcript: Transcript, broll_paths: list[Path], music_path: Path | None) -> Path` composes final command, runs via `subprocess.run`, validates exit code, `ffprobe` output.
- **Validation**: Builder unit tests assert command string contains each filter token (`zoompan`, `asetrate`, `aevalsrc`, `amix`, `map_metadata`, `drawtext`/`ass`, `is_vertical` crop); golden FFprobe on fixture output shows `width==720 height==1280`, `creation_time` randomized, no `encoder` leak; manual watch of 5s sample verifies no crash.

### Phase 5 — Orchestration & API (depends: Phases 1-4)
- **Files**: `app/workers/celery_app.py`, `app/workers/tasks.py` (chain), `app/api/routes.py`, `app/core/config.py`, `tests/test_orchestration.py`
- **Tasks**:
  - `celery_app = Celery("clipper", broker="redis://redis:6379/0", backend="redis://redis:6379/1", task_acks_late=True, worker_prefetch_multiplier=1)`.
  - Tasks: `extract_and_chunk`, `transcribe_group`, `stitch`, `analyze`, `source_broll`, `render_clips` (fan-out per clip).
  - Chain: `chain(extract_and_chunk.s(src) | group(transcribe_chunk.s(c) for c in chunks) | stitch.s() | analyze.s() | source_broll.s() | render_clips.s())` using `chord` for stitch barrier.
  - FastAPI: `POST /clip` (multipart upload, saves to `storage/uploads/{uuid}.mp4`, enqueues chain, returns `job_id`), `GET /jobs/{id}` (Celery `AsyncResult` state + progress), `GET /health` (redis ping, ffmpeg check), `GET /renders/{id}` (file serve).
  - Job tracking via `job_id` (UUID) stored in Redis hash with phase status; frontend can poll.
  - Error isolation: transcription 429 retries do not fail chain; Coverr miss falls back to no B-Roll rather than fail clip.
- **Validation**: `pytest tests/test_orchestration.py` with `celery.contrib.pytest` or `task_always_eager=True` runs full chain on 10s fixture with mocked Groq/Coverr/Muse (no network) and asserts 2 renders; `curl -F file=@fixture.mp4 localhost:8000/clip` returns 202 + job_id; `GET /jobs/{id}` eventually `SUCCESS`.

### Phase 6 — Hardening & Docs (depends: Phase 5)
- **Tasks**: Add `README.md` (setup, env vars, `docker compose up`, API docs), `.env.example` (`GROQ_API_KEY`, `MUSE_API_KEY`, `MUSE_BASE_URL`, `COVERR_API_KEY`, `REDIS_URL`, `MUSIC_PATH`), `make` targets (`make dev`, `make worker`, `make test`), `ruff` + `mypy --strict` CI, loguru/structlog, Sentry optional.
- **Validation**: `make test` (pytest), `make lint` (ruff + mypy), `docker compose up --build` boots 3 services, E2E with real keys (if provided) produces TikTok-ready 720x1280 MP4s that pass `ffprobe` fingerprint checks.

## Validation Plan

| Work Unit | Focused Command / Check | Expected Evidence |
|---|---|---|
| Phase 0 | `uv pip install -r requirements.txt && docker compose config` | No resolve errors; compose validates |
| Phase 0 | `pytest --collect-only && ruff check . && mypy app/` | 0 collection errors, 0 lint, 0 type errors |
| Phase 1 | `pytest tests/test_transcription.py -v` | Stitch offset test passes: chunk2 word `start==180+local_start` |
| Phase 1 | Real Groq (if key) `python -m app.services.transcription --file fixtures/60s.mp4` | Returns `Transcript` with `len(words)>200`, `words[0].start==0.0` ±0.05 |
| Phase 2 | `pytest tests/test_llm.py -v` | 15–45s validator rejects 14s/46s; JSON repair retry succeeds |
| Phase 2 | `python -m app.services.llm --transcript fixtures/transcript.json` | Prints valid `ClipPlan` with `clips[0].virality_score` 0–100 |
| Phase 3 | `pytest tests/test_coverr.py -v` | Cache hit avoids HTTP; vertical filter test passes; TTL expiry test passes |
| Phase 3 | `python -m app.services.coverr --query "burning money"` | Downloads `mp4_preview` <50 MB, `ffprobe` shows `height>width` |
| Phase 4 | `pytest tests/test_render.py -v` | Builder string contains `zoompan`, `asetrate`, `aevalsrc`/`sine`, `amix`, `map_metadata -1`, `drawtext` |
| Phase 4 | `ffprobe -v error -select_streams v:0 -show_entries stream=width,height,codec_name -of csv render.mp4` | `720,1280,h264` |
| Phase 4 | `ffprobe -v error -show_format render.mp4 | grep creation_time` | Randomized date, not source date |
| Phase 4 | `ffprobe -v error -show_entries stream=codec_type -of csv render.mp4` + spectral check | Audio stream exists; ultrasonic tone visible in `sox spectrogram` if not stripped by AAC |
| Phase 5 | `pytest tests/test_orchestration.py -v` with `task_always_eager` | Chain produces 2 MP4s from fixture |
| Phase 5 | `curl -F file=@fixtures/10s.mp4 http://localhost:8000/clip` → `GET /jobs/{id}` | 202 + job_id; eventual `SUCCESS` + render paths |
| Phase 6 | `docker compose up --build -d && curl localhost:8000/health` | `{"status":"ok","redis":"pong","ffmpeg":"4.4.2"}` |
| E2E | Manual watch of rendered 9:16 clip on phone | Face centered, hook text animates 0–3s, B-Roll glitch at cue, no source watermark/metadata |

## Risks / Rollback

| Risk | Impact | Mitigation | Rollback |
|---|---|---|---|
| Host Python 3.10 vs required 3.11 | `typing` / `asyncio` incompatibilities | Use `uv python pin 3.11` + venv; document `pyenv` fallback; add `requires-python = ">=3.11"` | Pin to 3.10 shims if 3.11 unavailable (note in README) |
| Groq 25 MB / 429 free-tier limits | Transcription fails or incomplete | 3-min chunking keeps <25 MB; Celery `rate_limit` + exponential `retry` + `acks_late`; stitch idempotent | Fall back to sequential chunk processing with 10s delay |
| Groq `timestamp_granularities` not supported on `whisper-large-v3` (only turbo) | No word timestamps → jump-cut/kinetic broken | Try `whisper-large-v3-turbo` first; validate response has `words`; if missing, fallback to segment-level timestamps + warn | Disable kinetic per-word, use segment subtitles |
| Coverr API shape mismatch (`hits` vs `videos`, auth) | B-Roll search returns 0 hits | Log raw response; support both shapes; filter fallback `height>width`; use `fallback_en` query; degrade gracefully to no B-Roll | Skip B-Roll overlay, render main only |
| AAC strips 18–20kHz ultrasonic | Jam ineffective, wasted filter | Validate with `sox` spectrogram; if stripped, auto-downgrade to 16kHz or make `ULTRASONIC_ENABLED=false` config; document limitation | Remove `aevalsrc` branch from builder |
| MediaPipe jitter / no face | Crop jumps or mis-centers | EMA smoothing + average fallback; if no face detected, center crop; add `AUTOFRAME_ENABLED` toggle | Disable auto-framing, pure center crop |
| FFmpeg 4.4 vs 6.x filter syntax drift | `zoompan`/`rubberband` unavailable | Check `ffmpeg -filters` at startup; feature-detect and degrade (e.g., `atempo` instead of `rubberband`) | Remove missing filter, log warning |
| Redis not running | Celery broker fails | `docker-compose` provides redis; healthcheck in `config.py` raises clear error; `task_always_eager` for tests | Run API synchronously without Celery (dev mode) |
| Long render OOM (4 GB RAM) | FFmpeg killed | Limit parallel renders to 1 per worker; use `-preset fast -crf 23`; monitor `dmesg` | Serialize clips, reduce concurrency |
| Metadata wipe incomplete | TikTok detects source | Verify `ffprobe -show_format` clean; use `-map_metadata -1` + `-metadata creation_time` + strip `moov` via re-mux | Re-run `ffmpeg -i in -map_metadata -1 -c copy out` |

## Open Questions

1. **Muse Spark endpoint**: What is the exact `base_url` and auth header for Muse Spark OpenAI-compatible API? (Assumption: `https://api.muse-spark.../v1` with `Authorization: Bearer <key>`; confirm and document in `.env.example` as `MUSE_BASE_URL` + `MUSE_API_KEY`.)
2. **Coverr API key procurement**: Does the team have a Coverr API key, or should Phase 3 initially run against mocked responses? (Assumption: key in `COVERR_API_KEY` env; search without key returns 401.)
3. **Background music source**: Which trending track to mix at 10%? Local file `assets/music.mp3` or Coverr audio? (Assumption: local `assets/trending.mp3` provided; fallback to no music if missing.)
4. **Language**: Source videos are Indonesian (per prior skill) or English? Groq `language` param affects accuracy. (Assumption: auto-detect, default `id` for Indonesian with `arDiNeural`-style content.)
5. **Deployment target**: Local dev (`docker compose`) vs Kaggle vs VPS? Impacts Redis persistence and storage paths. (Assumption: local `docker compose` for v1.)

## Sources

- Groq Whisper discovery snippets: `https://github.com/viljolehmus-cyber/clipping_tool` (25 MB chunking, `whisper-large-v3` + `timestamp_granularities: ["word"]`), `https://github.com/adrbn/subifi` (5-min chunk for 25 MB limit, `verbose_json` word timestamps) — discovery, not primary docs. Primary docs to inspect before implementation: `https://console.groq.com/docs` (Whisper) and `https://api.groq.com/openai/v1/audio/transcriptions`.
- Coverr API discovery: `https://api.coverr.co/docs` (official — underlying content not yet fetched non-empty; must inspect before finalizing auth/query shape), plus `https://github.com/harry0703/MoneyPrinterTurbo/pull/1008` (Bearer auth, `GET /videos?query=&urls=true&sort=popular`, `hits` shape, `mp4_preview` field).
- FFmpeg invocation: `https://micropyramid.com/blog/converting-audio-and-video-files-using-ffmpeg-tool/` (subprocess vs ffmpeg-python tradeoffs), `https://github.com/kkroening/ffmpeg-python` (wrapper over `subprocess.Popen`), `https://ffmpeg.org/ffmpeg-filters.html` (to verify `zoompan`, `aevalsrc`, `amix`, `rubberband` syntax — inspect before render implementation).
- MediaPipe auto-framing: `https://github.com/madhavmendiratta/clipforge` (face-aware 9:16 crop via MediaPipe), `https://github.com/hector-ai-91/video-podcast-clipper` (FaceLandmarker 468-point framing) — patterns to validate against `https://developers.google.com/mediapipe/solutions/vision/face_detector`.

*Note*: All external candidates above were located via discovery/search snippets only. Before Phase 1/3/4 implementation, the underlying authoritative pages (Groq docs, Coverr docs, FFmpeg filter docs, MediaPipe docs) must be fetched and verified for exact request shape, rate-limit headers, and filter syntax; recommendations remain conditional until that inspection succeeds.
