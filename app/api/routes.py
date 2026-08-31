from __future__ import annotations

import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.core.config import get_settings
from app.models.schemas import JobStatus

router = APIRouter()


class ClipFromFileRequest(BaseModel):
    filename: str = Field(min_length=1, description="Nama file di storage/downloads")
    job_id: str | None = None


class ClipFromUrlRequest(BaseModel):
    url: str = Field(min_length=4)
    quality: str = Field(default="best")
    format: str = Field(default="mp4")
    audio_only: bool = False
    no_playlist: bool = True


def _run_clip_pipeline(src: Path, job_id: str) -> JobStatus:
    """Jalankan clip pipeline via Celery jika tersedia, fallback eager."""
    settings = get_settings()
    # Try Celery async
    try:
        import redis as redis_lib  # type: ignore[import-untyped]

        r = redis_lib.from_url(settings.celery_broker_url, socket_connect_timeout=1)
        r.ping()
        from app.workers.tasks import run_full_pipeline

        run_full_pipeline.delay(str(src), job_id)  # type: ignore[attr-defined]
        return JobStatus(job_id=job_id, status="PENDING", phase="queued", progress=0.0)
    except Exception:
        from app.workers.tasks import run_full_pipeline
        from app.workers.celery_app import celery_app

        try:
            celery_app.conf.task_always_eager = True  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            result = run_full_pipeline(str(src), job_id)  # type: ignore[call-arg]
            outputs: list[str] = result.get("outputs", [])
            return JobStatus(job_id=job_id, status="SUCCESS", phase="render", progress=1.0, result=outputs)
        except Exception as e:
            return JobStatus(job_id=job_id, status="FAILURE", phase="error", error=str(e))
        finally:
            try:
                celery_app.conf.task_always_eager = False  # type: ignore[attr-defined]
            except Exception:
                pass


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

    return _run_clip_pipeline(dest, job_id)


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


@router.post("/clip/from-download", response_model=JobStatus)
def clip_from_download(body: ClipFromFileRequest) -> JobStatus:
    """Clip sumber utama ytdlp: ambil file dari storage/downloads lalu jalankan DNA Engine."""
    from app.services.ytdlp_service import get_download_dir

    if "/" in body.filename or "\\" in body.filename or ".." in body.filename:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    src = get_download_dir() / body.filename
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail=f"File {body.filename} tidak ditemukan di downloads")
    # Validasi ekstensi video
    if src.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".opus", ".wav"}:
        raise HTTPException(status_code=400, detail=f"Format {src.suffix} tidak didukung untuk clipping")
    job_id = body.job_id or str(uuid.uuid4())
    return _run_clip_pipeline(src, job_id)


@router.post("/clip/from-ytdlp-job", response_model=JobStatus)
def clip_from_ytdlp_job(job_id: str) -> JobStatus:
    """Ambil hasil download job ytdlp yang sudah completed lalu clip."""
    from app.services.ytdlp_service import JOBS

    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ytdlp tidak ditemukan")
    if job.status != "completed" or not job.filepath:
        raise HTTPException(status_code=400, detail=f"Job belum selesai (status={job.status})")
    src = Path(job.filepath)
    if not src.exists():
        raise HTTPException(status_code=404, detail="File hasil download tidak ditemukan di disk")
    clip_job_id = str(uuid.uuid4())
    return _run_clip_pipeline(src, clip_job_id)


@router.post("/clip/from-url", response_model=JobStatus)
async def clip_from_url(body: ClipFromUrlRequest, bg: BackgroundTasks) -> JobStatus:
    """One-click: ytdlp download URL lalu langsung clip (sumber utama ytdlp)."""
    import asyncio

    from app.services.ytdlp_service import JOBS, create_job, run_download_job

    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL harus diawali http:// atau https://")

    ytdlp_job = create_job(body.url, {"quality": body.quality, "format": body.format, "audio_only": body.audio_only, "no_playlist": body.no_playlist})
    clip_job_id = str(uuid.uuid4())

    async def _download_then_clip():
        try:
            await run_download_job(ytdlp_job)
            if ytdlp_job.status == "completed" and ytdlp_job.filepath:
                src = Path(ytdlp_job.filepath)
                if src.exists():
                    _run_clip_pipeline(src, clip_job_id)
        except Exception as e:
            ytdlp_job.status = "error"
            ytdlp_job.error = str(e)

    asyncio.create_task(_download_then_clip())
    # Return clip job id segera; frontend polling /jobs/{clip_job_id} dan /api/ytdlp/jobs/{ytdlp_job.job_id}
    return JobStatus(job_id=clip_job_id, status="PENDING", phase="downloading", progress=0.0)


@router.get("/clip/sources")
def clip_sources() -> dict:
    """List file ytdlp yang siap di-clip (sumber utama)."""
    from app.services.ytdlp_service import list_downloaded_files

    files = list_downloaded_files()
    video_exts = {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v"}
    video_files = [f for f in files if Path(f["name"]).suffix.lower() in video_exts]
    return {"ok": True, "files": video_files, "total": len(video_files), "all_files": files}


@router.get("/clip/renders")
def clip_renders() -> dict:
    """List semua hasil clip DNA (storage/renders/*/*.mp4)."""
    settings = get_settings()
    renders_dir = settings.storage_path / "renders"
    items: list[dict] = []
    if renders_dir.exists():
        for job_dir in sorted(renders_dir.iterdir(), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
            if not job_dir.is_dir():
                continue
            for p in sorted(job_dir.glob("*.mp4"), key=lambda x: x.stat().st_mtime, reverse=True):
                try:
                    stat = p.stat()
                    items.append({
                        "job_id": job_dir.name,
                        "filename": p.name,
                        "path": str(p),
                        "size_human": f"{stat.st_size/(1024*1024):.2f} MB",
                        "mtime": stat.st_mtime,
                        "url": f"/renders/{job_dir.name}/{p.name}",
                    })
                except Exception:
                    continue
    return {"ok": True, "renders": items, "total": len(items)}


@router.get("/clip/jobs")
def clip_jobs() -> dict:
    """List clip jobs (scan renders + uploads)."""
    settings = get_settings()
    jobs: list[dict] = []
    renders_dir = settings.storage_path / "renders"
    uploads_dir = settings.storage_path / "uploads"
    seen: set[str] = set()
    if renders_dir.exists():
        for d in renders_dir.iterdir():
            if d.is_dir() and (d / f"{d.name}.mp4").exists() or list(d.glob("*.mp4")):
                clips = [p.name for p in d.glob("*.mp4")]
                if clips:
                    jobs.append({"job_id": d.name, "status": "SUCCESS", "files": clips})
                    seen.add(d.name)
    if uploads_dir.exists():
        for p in uploads_dir.iterdir():
            jid = p.stem
            if jid not in seen:
                jobs.append({"job_id": jid, "status": "PROCESSING", "files": []})
    return {"ok": True, "jobs": jobs}


@router.get("/renders/{job_id}/{filename}")
def get_render(job_id: str, filename: str) -> FileResponse:
    settings = get_settings()
    path = settings.storage_path / "renders" / job_id / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)
