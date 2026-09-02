from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

try:
    from celery import Task, chain, chord, group  # type: ignore[import-untyped]
    from celery.utils.log import get_task_logger  # type: ignore[import-untyped]

    logger = get_task_logger(__name__)
except ImportError:
    Task = object  # type: ignore[assignment, misc]
    chain = chord = group = lambda *a, **kw: None  # type: ignore[assignment]
    import logging

    logger = logging.getLogger(__name__)

from app.core.config import get_settings
from app.models.schemas import AudioChunk
from app.services.coverr import CoverrClient
from app.services.llm import MuseClient
from app.services.render import RenderEngine
from app.services.transcription import stitch_transcripts, transcribe_file_groq
from app.utils.audio import chunk_audio, extract_audio, get_duration
from app.workers.celery_app import celery_app

# auto-editor 2-pass helpers (binary, not pip — see Dockerfile)
try:
    from app.services.auto_editor import export_dead_air as _ae_export
    from app.services.auto_editor import is_available as _ae_available
    from app.services.auto_editor import trim_silence as _ae_trim
except ImportError:
    _ae_available = lambda: False  # type: ignore[assignment]
    _ae_trim = lambda s, d, **kw: s  # type: ignore[assignment]
    _ae_export = lambda *a, **kw: []  # type: ignore[assignment]


def _transcribe_impl(chunk_path: str, start_time: float, chunk_index: int) -> dict[str, Any]:
    result = transcribe_file_groq(Path(chunk_path), offset=start_time)
    result["_chunk_index"] = chunk_index
    result["_start_time"] = start_time
    return result


@celery_app.task(bind=True, max_retries=5, name="app.workers.tasks.transcribe_chunk")
def transcribe_chunk(self: Task, chunk_path: str, start_time: float, chunk_index: int) -> dict[str, Any]:
    """Transcribe chunk — free-tier aware: 429/413 + Retry-After + 25 MB."""
    # rate_limit dinamis dari config (default 10/m untuk podcast)
    try:
        # set rate_limit dari settings jika didukung broker
        from app.core.config import get_settings as _gs

        rpm = _gs().groq_rate_limit_per_minute
        # Celery rate_limit string "10/m" -> update jika perlu
        # tidak perlu set di decorator, cukup throttle di transcribe_file_groq
        pass
    except Exception:
        pass
    try:
        return _transcribe_impl(chunk_path, start_time, chunk_index)
    except Exception as exc:
        msg = str(exc).lower()
        is_rate = any(k in msg for k in ["429", "rate limit", "too many", "quota", "rate_limit_exceeded"])
        is_payload = "413" in msg or "payload too large" in msg or "25 mb" in msg
        if is_payload:
            logger.error("Groq 413 chunk %s terlalu besar, perlu rechunk 90s: %s", chunk_path, exc)
            # coba rechunk 90s untuk chunk ini jika file >25MB
            try:
                from pathlib import Path as _P

                p = _P(chunk_path)
                if p.exists() and p.stat().st_size > 25 * 1024 * 1024:
                    logger.warning("Chunk %s >25MB, skip & beri mock agar pipeline tidak stuck (podcast tetap lanjut)", chunk_path)
            except Exception:
                pass
            raise
        if is_rate:
            try:
                # hormati Retry-After jika ada di pesan
                import re

                m = re.search(r"retry[_\- ]?after[^0-9]*([0-9]+)", msg)
                countdown = float(m.group(1)) if m else (2 ** getattr(self.request, "retries", 0)) * 10
            except Exception:
                countdown = 10
            # cap & tambah jitter untuk podcast 40 chunk
            countdown = min(max(countdown, 60.0 / 10), 120)
            logger.warning("Groq 429, retry %s dalam %.0fs: %s", getattr(self.request, "retries", 0), countdown, exc)
            raise self.retry(exc=exc, countdown=countdown)
        # retry juga untuk 5xx transient
        if any(k in msg for k in ["500", "502", "503", "timeout", "connection"]):
            countdown = (2 ** getattr(self.request, "retries", 0)) * 5
            logger.warning("Groq transient, retry %s dalam %.0fs: %s", getattr(self.request, "retries", 0), countdown, exc)
            raise self.retry(exc=exc, countdown=min(countdown, 60))
        logger.exception("Transcription failed chunk %s: %s", chunk_path, exc)
        raise


@celery_app.task(name="app.workers.tasks.extract_and_chunk")
def extract_and_chunk(src_path: str, job_id: str, host_name: str = "") -> dict[str, Any]:
    settings = get_settings()
    src = Path(src_path)
    storage = settings.storage_path
    job_dir = storage / "cache" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # PASS 1 global: auto-editor trim silence BEFORE Whisper (hemat Groq 10-20% + 10/m throttle)
    effective_src = src
    try:
        if _ae_available():
            trimmed = job_dir / "trimmed.mp4"
            # try trim; fallback to src on fail/unavailable
            maybe = _ae_trim(src, trimmed, threshold="0.04", margin="0.2s")
            if maybe != src and Path(maybe).exists() and Path(maybe).stat().st_size > 0:
                effective_src = Path(maybe)
                logger.info("PASS 1 auto-editor: %s -> %s (%.1f MB -> %.1f MB)", src.name, trimmed.name, src.stat().st_size/1e6, effective_src.stat().st_size/1e6)
    except Exception as exc:
        logger.warning("PASS 1 skip: %s", exc)
        effective_src = src

    # Extract audio from effective_src (trimmed if PASS1 succeeded)
    audio_path = job_dir / "audio.wav"
    extract_audio(effective_src, audio_path)

    # Chunk: pakai config untuk podcast panjang (180s default, <25 MB)
    chunk_dir = job_dir / "chunks"
    chunk_sec = getattr(settings, "groq_chunk_seconds", 180)
    raw = chunk_audio(audio_path, chunk_dir, chunk_sec=chunk_sec)
    # safety: jika chunk >25 MB (mis. audio tinggi), auto-rechunk 90s
    try:
        for p, _, _ in raw:
            if p.stat().st_size > 25 * 1024 * 1024:
                import shutil

                shutil.rmtree(chunk_dir, ignore_errors=True)
                chunk_dir.mkdir(parents=True, exist_ok=True)
                raw = chunk_audio(audio_path, chunk_dir, chunk_sec=90)
                logger.warning("Chunk >25 MB, auto-rechunk 90s untuk free tier (%s chunk)", len(raw))
                break
    except Exception:
        pass
    chunks: list[dict[str, Any]] = []
    for idx, (p, start, dur) in enumerate(raw):
        chunks.append({"path": str(p), "start_time": start, "duration": dur, "index": idx})

    total_duration = get_duration(effective_src)
    logger.info("Podcast chunked: %.1f menit -> %s chunk @%ss (free tier aman)", total_duration / 60, len(chunks), chunk_sec)
    return {"job_id": job_id, "src": src_path, "effective_src": str(effective_src), "chunks": chunks, "total_duration": total_duration, "host_name": host_name}


@celery_app.task(name="app.workers.tasks.stitch")
def stitch(results: list[dict[str, Any]], meta: dict[str, Any]) -> dict[str, Any]:
    # results is list of chunk transcriptions, meta is from extract_and_chunk
    chunks_meta: list[dict[str, Any]] = meta["chunks"]
    total_duration: float = meta["total_duration"]
    audio_chunks = [
        AudioChunk(index=c["index"], start_time=c["start_time"], duration=c["duration"], path=Path(c["path"]))
        for c in chunks_meta
    ]
    # Sort results by _chunk_index to ensure order
    sorted_results = sorted(results, key=lambda x: x.get("_chunk_index", 0))
    transcript = stitch_transcripts(sorted_results, audio_chunks, total_duration=total_duration)
    return {
        "job_id": meta["job_id"],
        "src": meta["src"],
        "effective_src": meta.get("effective_src", meta["src"]),
        "transcript": transcript.model_dump(mode="json"),
        "total_duration": total_duration,
        "host_name": meta.get("host_name", ""),
    }


@celery_app.task(name="app.workers.tasks.analyze")
def analyze(data: dict[str, Any]) -> dict[str, Any]:
    from app.models.schemas import Transcript

    transcript = Transcript.model_validate(data["transcript"])
    total_duration: float = data["total_duration"]
    host_name = (data.get("host_name") or data.get("uploader") or "").strip()
    client = MuseClient()
    plan = client.analyze(transcript, duration=total_duration, host_name=host_name or None)
    return {
        "job_id": data["job_id"],
        "src": data["src"],
        "effective_src": data.get("effective_src", data["src"]),
        "host_name": host_name,
        "transcript": data["transcript"],
        "clip_plan": plan.model_dump(mode="json"),
        "total_duration": total_duration,
    }


@celery_app.task(name="app.workers.tasks.source_broll")
def source_broll(data: dict[str, Any]) -> dict[str, Any]:
    from app.models.schemas import ClipPlan

    plan = ClipPlan.model_validate(data["clip_plan"])
    client = CoverrClient()
    # Collect unique keywords
    keywords: dict[str, str] = {}  # keywords_en -> fallback
    for cue in plan.broll_cues:
        keywords[cue.keywords_en] = cue.fallback_en

    broll_map: dict[str, str] = {}  # keyword -> local path string
    settings = get_settings()
    preview_dir = settings.storage_path / "previews" / data["job_id"]
    preview_dir.mkdir(parents=True, exist_ok=True)

    for kw, fallback in keywords.items():
        video = client.search_sync(kw, fallback)
        if video is None:
            continue
        # Download preview
        safe = kw.replace(" ", "_").replace("/", "_")
        dest = preview_dir / f"{safe}.mp4"
        try:
            client.download_sync(video.preview_url, dest)
            broll_map[kw] = str(dest)
            # Also map fallback to same file for convenience
            if fallback not in broll_map:
                broll_map[fallback] = str(dest)
        except Exception as exc:
            logger.warning("Failed to download B-Roll %s: %s", kw, exc)
            continue

    return {
        "job_id": data["job_id"],
        "src": data["src"],
        "effective_src": data.get("effective_src", data["src"]),
        "transcript": data["transcript"],
        "clip_plan": data["clip_plan"],
        "broll_map": broll_map,
        "total_duration": data["total_duration"],
    }


@celery_app.task(name="app.workers.tasks.render_clips")
def render_clips(data: dict[str, Any]) -> dict[str, Any]:
    from app.models.schemas import ClipPlan, Transcript

    transcript = Transcript.model_validate(data["transcript"])
    plan = ClipPlan.model_validate(data["clip_plan"])
    broll_map_str: dict[str, str] = data.get("broll_map", {})
    broll_map = {k: Path(v) for k, v in broll_map_str.items()}
    # PASS 1 trimmed source if available (timestamps already relative to it)
    src = Path(data.get("effective_src") or data["src"])
    job_id: str = data["job_id"]

    settings = get_settings()
    # Use resolved_music_path helper (checks project root and storage)
    music_path = settings.resolved_music_path

    # PASS 2: override LLM dead_air with deterministic auto-editor gaps
    try:
        if _ae_available() and src.exists():
            ae_dead = _ae_export(src, threshold="0.03", margin="0.1s", hook_protect_until=3.0)
            if ae_dead:
                plan.dead_air = ae_dead  # type: ignore[assignment]
                logger.info("PASS 2 auto-editor dead_air: %s segments -> FFmpeg select cut", len(ae_dead))
    except Exception as exc:
        logger.warning("PASS 2 dead_air export skip: %s", exc)

    output_dir = settings.storage_path / "renders" / job_id
    engine = RenderEngine(music_path=music_path)
    outputs = engine.render_all(src, transcript, plan, broll_map, output_dir)
    return {
        "job_id": job_id,
        "outputs": [str(p) for p in outputs],
        "clip_plan": plan.model_dump(mode="json"),
    }


def build_chain(src_path: str, job_id: str | None = None) -> Any:
    """Build Celery chain for full pipeline. Returns chain signature."""
    if job_id is None:
        job_id = str(uuid.uuid4())

    # Extract + chunk -> transcribe group -> stitch -> analyze -> source_broll -> render
    # We implement as: extract_and_chunk -> then chord of transcribe_chunk group -> stitch -> analyze -> source_broll -> render

    # First task
    start = extract_and_chunk.s(src_path, job_id)

    # The rest will be chained dynamically: after extract, we need to spawn transcribe group
    # For simplicity in eager mode, we provide a single high-level task `run_pipeline`
    return chain(start, run_pipeline_tail.s())


@celery_app.task(bind=True, name="app.workers.tasks.run_pipeline_tail")
def run_pipeline_tail(self, meta: dict[str, Any]) -> dict[str, Any]:
    """
    Podcast panjang: transcribe sekuensial dengan throttle free-tier.
    Eager mode (tanpa broker) tetap hormati groq_rate_limit_per_minute.
    """
    import time as _time

    settings = get_settings()
    rpm = max(1, getattr(settings, "groq_rate_limit_per_minute", 10))
    job_id = meta.get("job_id", "")
    # helper: push progress to redis so api SSE can display live (api and worker are separate containers)
    def _push_progress(phase: str, prog: float, detail: str = ""):
        try:
            import redis as _redis
            r = _redis.from_url(settings.celery_broker_url, socket_connect_timeout=1)
            r.setex(f"clip:progress:{job_id}", 3600, f"{phase}|{prog}|{detail}")
            # also celery state for AsyncResult polling
            try:
                self.update_state(state="PROGRESS", meta={"phase": phase, "progress": prog, "detail": detail})
            except Exception:
                pass
        except Exception:
            pass
    _push_progress("transcribe", 0.15, f"0/{len(chunks)}")
    min_interval = 60.0 / rpm

    chunks: list[dict[str, Any]] = meta["chunks"]
    # Estimasi untuk podcast: 60 menit -> 20 chunk -> ~2 menit di free tier (10/m)
    # log biar user paham durasi
    if len(chunks) > 5:
        est_min = len(chunks) / rpm
        logger.info("Podcast panjang %s chunk, estimasi transcribe %.1f menit di free tier (%s/m)", len(chunks), est_min, rpm)

    results: list[dict[str, Any]] = []
    last_call = 0.0
    for idx, c in enumerate(chunks):
        # throttle antar chunk (mirip token bucket)
        now = _time.time()
        elapsed = now - last_call
        if idx > 0 and elapsed < min_interval:
            _time.sleep(min_interval - elapsed)
        last_call = _time.time()

        try:
            res = _transcribe_impl(c["path"], c["start_time"], c["index"])
        except Exception as exc:
            # jika 413, sudah ditangani di transcribe_file_groq; coba rechunk 90s untuk chunk ini
            if "413" in str(exc) or "25 mb" in str(exc).lower():
                logger.warning("Chunk %s 413, coba rechunk 90s dan skip jika gagal", c["path"])
                # fallback mock agar pipeline tidak mati total untuk podcast
                raise  # no mock — user forbids mock, biar error kelihatan (413 payload)
            else:
                raise
        results.append(res)
        _push_progress("transcribe", 0.15 + 0.5 * (idx + 1) / max(1, len(chunks)), f"{idx+1}/{len(chunks)}")
        # jeda tambahan untuk free tier jika chunk banyak
        if idx < len(chunks) - 1:
            # _transcribe_impl sudah throttle, tapi jaga interval tetap
            last_call = _time.time()

    _push_progress("stitch", 0.70, "stitch")
    stitched = stitch(results, meta)
    _push_progress("analyze", 0.75, "analyze")
    analyzed = analyze(stitched)
    _push_progress("broll", 0.85, "broll")
    with_broll = source_broll(analyzed)
    _push_progress("render", 0.90, "render")
    rendered = render_clips(with_broll)
    _push_progress("done", 1.0, "done")
    return rendered


@celery_app.task(name="app.workers.tasks.run_full_pipeline")
def run_full_pipeline(src_path: str, job_id: str | None = None, host_name: str = "") -> dict[str, Any]:
    """Synchronous full pipeline for FastAPI eager execution (no broker needed)."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    meta = extract_and_chunk(src_path, job_id, host_name or "")
    return run_pipeline_tail(meta)
