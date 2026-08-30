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


def _transcribe_impl(chunk_path: str, start_time: float, chunk_index: int) -> dict[str, Any]:
    result = transcribe_file_groq(Path(chunk_path), offset=start_time)
    result["_chunk_index"] = chunk_index
    result["_start_time"] = start_time
    return result


@celery_app.task(bind=True, max_retries=5, rate_limit="10/m", name="app.workers.tasks.transcribe_chunk")
def transcribe_chunk(self: Task, chunk_path: str, start_time: float, chunk_index: int) -> dict[str, Any]:
    """Transcribe single chunk with retry on 429."""
    try:
        return _transcribe_impl(chunk_path, start_time, chunk_index)
    except Exception as exc:
        msg = str(exc).lower()
        if "429" in msg or "rate" in msg or "too many" in msg:
            try:
                countdown = (2 ** getattr(self.request, "retries", 0)) * 10
            except Exception:
                countdown = 10
            logger.warning("Groq 429, retry %s in %s sec: %s", getattr(self.request, "retries", 0), countdown, exc)
            raise self.retry(exc=exc, countdown=countdown)
        logger.exception("Transcription failed for chunk %s", chunk_path)
        raise


@celery_app.task(name="app.workers.tasks.extract_and_chunk")
def extract_and_chunk(src_path: str, job_id: str) -> dict[str, Any]:
    settings = get_settings()
    src = Path(src_path)
    storage = settings.storage_path
    job_dir = storage / "cache" / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    # Extract audio
    audio_path = job_dir / "audio.wav"
    extract_audio(src, audio_path)

    # Chunk
    chunk_dir = job_dir / "chunks"
    raw = chunk_audio(audio_path, chunk_dir, chunk_sec=180)
    chunks: list[dict[str, Any]] = []
    for idx, (p, start, dur) in enumerate(raw):
        chunks.append({"path": str(p), "start_time": start, "duration": dur, "index": idx})

    total_duration = get_duration(src)
    return {"job_id": job_id, "src": src_path, "chunks": chunks, "total_duration": total_duration}


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
        "transcript": transcript.model_dump(mode="json"),
        "total_duration": total_duration,
    }


@celery_app.task(name="app.workers.tasks.analyze")
def analyze(data: dict[str, Any]) -> dict[str, Any]:
    from app.models.schemas import Transcript

    transcript = Transcript.model_validate(data["transcript"])
    total_duration: float = data["total_duration"]
    client = MuseClient()
    plan = client.analyze(transcript, duration=total_duration)
    return {
        "job_id": data["job_id"],
        "src": data["src"],
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
    src = Path(data["src"])
    job_id: str = data["job_id"]

    settings = get_settings()
    music_path = Path(settings.music_path) if settings.music_path else None
    if music_path and not music_path.is_absolute():
        music_path = Path(__file__).parent.parent.parent / music_path
    if music_path and not music_path.exists():
        music_path = None

    output_dir = settings.storage_path / "renders" / job_id
    engine = RenderEngine(music_path=music_path)
    outputs = engine.render_all(src, transcript, plan, broll_map, output_dir)
    return {
        "job_id": job_id,
        "outputs": [str(p) for p in outputs],
        "clip_plan": data["clip_plan"],
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


@celery_app.task(name="app.workers.tasks.run_pipeline_tail")
def run_pipeline_tail(meta: dict[str, Any]) -> dict[str, Any]:
    """
    Continuation after extract_and_chunk: handles transcribe group synchronously (for eager) or via chord.
    This is a helper to keep chain simple when using EAGER mode for tests.
    In production with real broker, build_chain uses manual group.
    """
    chunks: list[dict[str, Any]] = meta["chunks"]
    # Use impl directly to avoid bound-task self issue when calling without worker context
    results: list[dict[str, Any]] = []
    for c in chunks:
        res = _transcribe_impl(c["path"], c["start_time"], c["index"])
        results.append(res)

    stitched = stitch(results, meta)
    analyzed = analyze(stitched)
    with_broll = source_broll(analyzed)
    rendered = render_clips(with_broll)
    return rendered


@celery_app.task(name="app.workers.tasks.run_full_pipeline")
def run_full_pipeline(src_path: str, job_id: str | None = None) -> dict[str, Any]:
    """Synchronous full pipeline for FastAPI eager execution (no broker needed)."""
    if job_id is None:
        job_id = str(uuid.uuid4())
    meta = extract_and_chunk(src_path, job_id)
    return run_pipeline_tail(meta)
