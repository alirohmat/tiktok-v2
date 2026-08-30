from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import FileResponse

from app.core.config import get_settings
from app.models.schemas import JobStatus

router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    settings = get_settings()
    # Check ffmpeg
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        ffmpeg = "ok"
    except Exception as e:
        ffmpeg = f"error: {e}"
    # Check redis (optional)
    try:
        import redis  # type: ignore[import-untyped]

        r = redis.from_url(settings.redis_url, socket_connect_timeout=2)
        r.ping()
        redis_status = "pong"
    except Exception as e:
        redis_status = f"unavailable: {e}"
    return {"status": "ok", "ffmpeg": ffmpeg, "redis": redis_status}


@router.post("/clip", response_model=JobStatus)
async def clip_video(file: UploadFile = File(...)) -> JobStatus:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
        raise HTTPException(status_code=400, detail=f"Unsupported format {ext}")

    settings = get_settings()
    job_id = str(uuid.uuid4())
    uploads = settings.storage_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / f"{job_id}{ext}"
    content = await file.read()
    dest.write_bytes(content)

    # Try Celery async if redis + celery available; otherwise eager fallback
    try:
        import redis as redis_lib  # type: ignore[import-untyped]

        r = redis_lib.from_url(settings.celery_broker_url, socket_connect_timeout=1)
        r.ping()
        from app.workers.tasks import run_full_pipeline

        async_result = run_full_pipeline.delay(str(dest), job_id)  # type: ignore[attr-defined]
        return JobStatus(job_id=job_id, status="PENDING", phase="queued", progress=0.0)
    except Exception:
        # Eager fallback: run synchronously and return SUCCESS
        from app.workers.tasks import run_full_pipeline
        from app.workers.celery_app import celery_app

        # If stub conf supports attribute, set eager; otherwise ignore
        try:
            celery_app.conf.task_always_eager = True  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            result = run_full_pipeline(str(dest), job_id)  # type: ignore[call-arg]
            outputs: list[str] = result.get("outputs", [])
            return JobStatus(job_id=job_id, status="SUCCESS", phase="render", progress=1.0, result=outputs)
        except Exception as e:
            return JobStatus(job_id=job_id, status="FAILURE", phase="error", error=str(e))
        finally:
            try:
                celery_app.conf.task_always_eager = False  # type: ignore[attr-defined]
            except Exception:
                pass


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    # Try Celery result backend (optional)
    try:
        from app.workers.celery_app import celery_app  # type: ignore[import-untyped]
        from celery.result import AsyncResult  # type: ignore[import-untyped]
    except ImportError:
        celery_app = None  # type: ignore[assignment]
        AsyncResult = None  # type: ignore[assignment]

    # Check storage first (primary)
    settings = get_settings()
    renders = list((settings.storage_path / "renders" / job_id).glob("*.mp4"))
    if renders:
        return JobStatus(job_id=job_id, status="SUCCESS", phase="render", progress=1.0, result=[str(p) for p in renders])
    upload_exists = any((settings.storage_path / "uploads").glob(f"{job_id}.*"))
    if upload_exists:
        return JobStatus(job_id=job_id, status="STARTED", phase="processing", progress=0.5)
    # Fallback to celery state if available
    if AsyncResult is not None and celery_app is not None:
        try:
            result = AsyncResult(job_id, app=celery_app)
            state = result.state if result else "PENDING"
            return JobStatus(job_id=job_id, status=state, phase="unknown", progress=0.0)
        except Exception:
            pass
    return JobStatus(job_id=job_id, status="PENDING", phase="unknown", progress=0.0)


@router.get("/renders/{job_id}/{filename}")
def get_render(job_id: str, filename: str) -> FileResponse:
    settings = get_settings()
    path = settings.storage_path / "renders" / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)
