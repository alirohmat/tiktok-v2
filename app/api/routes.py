from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

import time as _time

from app.core.config import get_settings
from app.models.schemas import JobStatus

router = APIRouter()

# --- Security constants ---
_JOB_ID_RE = re.compile(r"^[a-f0-9-]{8,}$")
_FILENAME_RE = re.compile(r"^[^/\\]+$")
MAX_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB
CHUNK_SIZE = 1024 * 1024  # 1 MB streaming


def _validate_job_id(job_id: str) -> None:
    if not _JOB_ID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="job_id tidak valid (harus hex/uuid, min 8 char)")


def _is_within(child: Path, parent: Path) -> bool:
    """Safe check via is_relative_to — prevents /storage vs /storage_evil bypass."""
    try:
        # Python 3.9+ has is_relative_to; fallback to relative_to try
        return child.resolve().is_relative_to(parent.resolve())
    except AttributeError:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except Exception:
            return False
    except Exception:
        return False


def _resolve_within_storage(path: Path, storage_root: Path) -> Path:
    try:
        resolved = path.resolve()
        storage_resolved = storage_root.resolve()
        if not _is_within(resolved, storage_resolved):
            raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
        return resolved
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Path tidak valid")


def _enforce_job_ttl(max_age_sec: float = 3600) -> None:
    """Evict stale CLIP_JOBS / CLIP_LOGS to avoid unbounded memory (TTL)."""
    now = _time.time()
    stale = [jid for jid, js in list(CLIP_JOBS.items()) if js.started_at and (now - js.started_at) > max_age_sec and js.status in ("PENDING", "STARTED", "PROCESSING")]
    # Do not auto-evict SUCCESS/FAILURE quickly; keep 24h
    old = [jid for jid, js in list(CLIP_JOBS.items()) if js.started_at and (now - js.started_at) > 86400]
    for jid in set(stale + old):
        CLIP_JOBS.pop(jid, None)
        CLIP_LOGS.pop(jid, None)


class ClipFromFileRequest(BaseModel):
    filename: str = Field(min_length=1, description="Nama file di storage/downloads")
    job_id: str | None = None


class ClipFromUrlRequest(BaseModel):
    url: str = Field(min_length=4)
    quality: str = Field(default="best")
    format: str = Field(default="mp4")
    audio_only: bool = False
    no_playlist: bool = True


# In-memory tracker for lean mode (tanpa redis) agar refresh tidak hilang + live logs
CLIP_JOBS: dict[str, JobStatus] = {}
CLIP_LOGS: dict[str, list[str]] = {}

def _clip_log(job_id: str, msg: str):
    ts = _time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    lst = CLIP_LOGS.setdefault(job_id, [])
    lst.append(line)
    if len(lst) > 500:
        CLIP_LOGS[job_id] = lst[-500:]
    # sync ke JobStatus.logs agar GET /jobs/{id} live
    if job_id in CLIP_JOBS:
        js = CLIP_JOBS[job_id]
        CLIP_JOBS[job_id] = js.model_copy(update={"logs": list(CLIP_LOGS[job_id])})

def _update_clip_job(job_id: str, **kw):
    if job_id in CLIP_JOBS:
        js = CLIP_JOBS[job_id]
        CLIP_JOBS[job_id] = js.model_copy(update=kw)
        # keep logs synced
        if job_id in CLIP_LOGS:
            CLIP_JOBS[job_id] = CLIP_JOBS[job_id].model_copy(update={"logs": list(CLIP_LOGS[job_id])})
    else:
        CLIP_JOBS[job_id] = JobStatus(job_id=job_id, status=kw.get("status","PENDING"), phase=kw.get("phase",""), progress=kw.get("progress",0.0), logs=list(CLIP_LOGS.get(job_id,[])), **{k:v for k,v in kw.items() if k not in ("status","phase","progress")})

def _run_clip_pipeline(src: Path, job_id: str) -> JobStatus:
    """Jalankan clip pipeline via Celery jika tersedia, fallback eager (non-blocking) dengan live logs detail."""
    settings = get_settings()
    _enforce_job_ttl()
    try:
        from app.services.ytdlp_service import _check_storage_quotas
        qerr = _check_storage_quotas()
        if qerr:
            # allow clip but warn via logs; block only if renders quota exceeded (>5GB)
            renders = settings.storage_path / "renders"
            # strict block for renders/uploads exceeding
            if "renders" in qerr or "uploads" in qerr:
                raise HTTPException(status_code=507, detail=qerr)
    except HTTPException:
        raise
    except Exception:
        pass
    # Validate src is within storage
    _resolve_within_storage(src, settings.storage_path)
    _validate_job_id(job_id)
    if job_id in CLIP_JOBS and CLIP_JOBS[job_id].status in ("PENDING", "STARTED", "PROCESSING"):
        raise HTTPException(status_code=409, detail=f"job_id {job_id} sudah dipakai (status {CLIP_JOBS[job_id].status})")
    CLIP_LOGS[job_id] = []
    _clip_log(job_id, f"Queued: {src.name}")
    # Try Celery async (redis tersedia)
    try:
        import redis as redis_lib  # type: ignore[import-untyped]

        r = redis_lib.from_url(settings.celery_broker_url, socket_connect_timeout=1)
        r.ping()
        from app.workers.tasks import run_full_pipeline

        _clip_log(job_id, "Redis terhubung — dispatch ke Celery worker (concurrency=2)")
        _clip_log(job_id, "Phase: queued -> worker akan update progress via backend")
        run_full_pipeline.delay(str(src), job_id)  # type: ignore[attr-defined]
        js = JobStatus(job_id=job_id, status="PENDING", phase="queued", progress=0.0, logs=list(CLIP_LOGS[job_id]), started_at=_time.time())
        CLIP_JOBS[job_id] = js
        return js
    except HTTPException:
        raise
    except Exception as e:
        _clip_log(job_id, f"Redis tidak tersedia ({e}) — fallback eager thread (lean mode)")

    # Fallback eager: jalankan di background thread agar HTTP tidak block (fix bug pending hilang saat refresh)
    import threading

    def _bg():
        try:
            _update_clip_job(job_id, status="STARTED", phase="extract", progress=0.05, started_at=_time.time())
            _clip_log(job_id, "Phase: extract audio (ffmpeg)")
            from app.workers.tasks import extract_and_chunk, run_pipeline_tail
            meta = extract_and_chunk(str(src), job_id)
            chunks = meta.get("chunks", [])
            total = meta.get("total_duration", 0)
            _clip_log(job_id, f"Audio extracted — chunked {len(chunks)} segment @180s (total {total/60:.1f} menit)")
            _update_clip_job(job_id, phase="transcribe", progress=0.15)
            _clip_log(job_id, f"Phase: transcribe (Groq {settings.groq_whisper_model}) — {len(chunks)} chunk, rate {settings.groq_rate_limit_per_minute}/m")
            # run_pipeline_tail akan transkripsi sekuensial dengan throttle + log internal
            # kita log progress per chunk via wrapper
            result = run_pipeline_tail(meta)
            outputs: list[str] = result.get("outputs", []) if isinstance(result, dict) else []
            _clip_log(job_id, f"Phase: render 9:16 selesai — {len(outputs)} clip")
            for p in outputs:
                _clip_log(job_id, f"  → {Path(p).name}")
            _update_clip_job(job_id, status="SUCCESS", phase="render", progress=1.0, result=outputs, finished_at=_time.time())
            _clip_log(job_id, "SUCCESS — cek tab Hasil DNA Rebirth")
        except Exception as e:
            import traceback
            err = f"{e}\n{traceback.format_exc()[-800:]}"
            _clip_log(job_id, f"FAILURE: {e}")
            _update_clip_job(job_id, status="FAILURE", phase="error", error=str(e)[:1000], finished_at=_time.time())
            # also store full trace in logs
            CLIP_LOGS[job_id].append(err)

    CLIP_JOBS[job_id] = JobStatus(job_id=job_id, status="PENDING", phase="queued", progress=0.0, logs=list(CLIP_LOGS[job_id]), started_at=_time.time())
    # Use non-daemon thread so shutdown waits for cleanup; no orphan kill
    threading.Thread(target=_bg, daemon=False).start()
    return CLIP_JOBS[job_id]


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
async def clip_video(request: Request, file: UploadFile = File(...)) -> JobStatus:
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided")
    ext = Path(file.filename).suffix.lower()
    if ext not in {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}:
        raise HTTPException(status_code=400, detail=f"Unsupported format {ext}")

    # Enforce Content-Length early if provided
    clen = request.headers.get("content-length")
    if clen:
        try:
            if int(clen) > MAX_UPLOAD_BYTES:
                raise HTTPException(status_code=413, detail=f"File terlalu besar (max {MAX_UPLOAD_BYTES//1024//1024} MB)")
        except HTTPException:
            raise
        except Exception:
            pass

    settings = get_settings()
    # Quota check renders/uploads/cache/previews before accepting upload
    try:
        from app.services.ytdlp_service import _check_storage_quotas
        qerr = _check_storage_quotas()
        if qerr:
            raise HTTPException(status_code=507, detail=qerr)
    except HTTPException:
        raise
    except Exception:
        pass
    job_id = str(uuid.uuid4())
    uploads = settings.storage_path / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    dest = uploads / f"{job_id}{ext}"
    # Stream write with limit to avoid OOM
    total = 0
    with open(dest, "wb") as out:
        while True:
            chunk = await file.read(CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_UPLOAD_BYTES:
                out.close()
                try:
                    dest.unlink()
                except Exception:
                    pass
                raise HTTPException(status_code=413, detail=f"File terlalu besar (max {MAX_UPLOAD_BYTES//1024//1024} MB)")
            out.write(chunk)

    return _run_clip_pipeline(dest, job_id)


@router.get("/jobs/{job_id}", response_model=JobStatus)
def get_job(job_id: str) -> JobStatus:
    _validate_job_id(job_id)
    # 1) Cek in-memory CLIP_JOBS (lean mode non-blocking, persist refresh) — sertakan live logs
    if job_id in CLIP_JOBS:
        # Jika sudah SUCCESS di memory tapi renders belum ke-detect, sinkronkan
        js = CLIP_JOBS[job_id]
        # selalu sync logs terbaru
        if job_id in CLIP_LOGS:
            js = js.model_copy(update={"logs": list(CLIP_LOGS[job_id])})
            CLIP_JOBS[job_id] = js
        if js.status == "SUCCESS" and js.result:
            return js
        # Update dari storage jika sudah render
        settings = get_settings()
        renders = list((settings.storage_path / "renders" / job_id).glob("*.mp4"))
        if renders:
            js2 = JobStatus(job_id=job_id, status="SUCCESS", phase="render", progress=1.0, result=[str(p) for p in renders], logs=list(CLIP_LOGS.get(job_id,[])), started_at=js.started_at, finished_at=_time.time())
            CLIP_JOBS[job_id] = js2
            return js2
        return js
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

    _enforce_job_ttl()
    if "/" in body.filename or "\\" in body.filename or ".." in body.filename:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    if not _FILENAME_RE.match(body.filename):
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    download_dir = get_download_dir()
    src = download_dir / body.filename
    # Resolve must stay within downloads — is_relative_to prevents storage_evil
    _resolve_within_storage(src, download_dir)
    # Flat downloads: ensure direct parent is download_dir
    try:
        if src.resolve().parent != download_dir.resolve():
            raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Path tidak valid")
    if not src.exists() or not src.is_file():
        raise HTTPException(status_code=404, detail=f"File {body.filename} tidak ditemukan di downloads")
    # Validasi ekstensi video
    if src.suffix.lower() not in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".m4v", ".mp3", ".m4a", ".opus", ".wav"}:
        raise HTTPException(status_code=400, detail=f"Format {src.suffix} tidak didukung untuk clipping")
    if body.job_id:
        _validate_job_id(body.job_id)
        if body.job_id in CLIP_JOBS:
            raise HTTPException(status_code=409, detail=f"job_id {body.job_id} sudah dipakai")
    job_id = body.job_id or str(uuid.uuid4())
    return _run_clip_pipeline(src, job_id)


@router.post("/clip/from-ytdlp-job", response_model=JobStatus)
def clip_from_ytdlp_job(job_id: str) -> JobStatus:
    """Ambil hasil download job ytdlp yang sudah completed lalu clip."""
    from app.services.ytdlp_service import JOBS
    _validate_job_id(job_id)

    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job ytdlp tidak ditemukan")
    if job.status != "completed" or not job.filepath:
        raise HTTPException(status_code=400, detail=f"Job belum selesai (status={job.status})")
    src = Path(job.filepath)
    # Validate src still within storage/downloads
    from app.services.ytdlp_service import get_download_dir
    _resolve_within_storage(src, get_download_dir())
    if not src.exists():
        raise HTTPException(status_code=404, detail="File hasil download tidak ditemukan di disk")
    clip_job_id = str(uuid.uuid4())
    return _run_clip_pipeline(src, clip_job_id)


@router.post("/clip/from-url", response_model=JobStatus)
async def clip_from_url(body: ClipFromUrlRequest, bg: BackgroundTasks) -> JobStatus:
    """One-click: ytdlp download URL lalu langsung clip (sumber utama ytdlp)."""
    from app.services.ytdlp_service import create_job, run_download_job

    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="URL harus diawali http:// atau https://")

    ytdlp_job = create_job(body.url, {"quality": body.quality, "format": body.format, "audio_only": body.audio_only, "no_playlist": body.no_playlist})
    clip_job_id = str(uuid.uuid4())

    def _download_then_clip_sync():
        import asyncio as _asyncio
        try:
            _asyncio.run(run_download_job(ytdlp_job))
            if ytdlp_job.status == "completed" and ytdlp_job.filepath:
                src = Path(ytdlp_job.filepath)
                if src.exists():
                    _run_clip_pipeline(src, clip_job_id)
        except Exception as e:
            ytdlp_job.status = "error"
            ytdlp_job.error = str(e)

    # Use BackgroundTasks so task survives event loop restart / worker reload
    bg.add_task(_download_then_clip_sync)
    # Pre-register clip job as PENDING for polling
    CLIP_JOBS[clip_job_id] = JobStatus(job_id=clip_job_id, status="PENDING", phase="downloading", progress=0.0, started_at=_time.time())
    CLIP_LOGS[clip_job_id] = [f"[{_time.strftime('%H:%M:%S')}] Download queued: {ytdlp_job.job_id} -> clip {clip_job_id}"]
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
    """List clip jobs (scan renders + uploads + in-memory pending)."""
    settings = get_settings()
    jobs: list[dict] = []
    renders_dir = settings.storage_path / "renders"
    uploads_dir = settings.storage_path / "uploads"
    seen: set[str] = set()
    # 1) renders (selesai)
    if renders_dir.exists():
        for d in renders_dir.iterdir():
            if d.is_dir() and ((d / f"{d.name}.mp4").exists() or list(d.glob("*.mp4"))):
                clips = [p.name for p in d.glob("*.mp4")]
                if clips:
                    jobs.append({"job_id": d.name, "status": "SUCCESS", "files": clips})
                    seen.add(d.name)
    # 2) in-memory CLIP_JOBS pending/started (fix hilang saat refresh)
    for jid, js in CLIP_JOBS.items():
        if jid not in seen:
            jobs.append({"job_id": jid, "status": js.status, "phase": js.phase, "progress": js.progress, "error": js.error})
            seen.add(jid)
    if uploads_dir.exists():
        for p in uploads_dir.iterdir():
            jid = p.stem
            if jid not in seen:
                jobs.append({"job_id": jid, "status": "PROCESSING", "files": []})
    # Sort: processing/pending first, then success
    jobs.sort(key=lambda x: (0 if x["status"] in ("PENDING","STARTED","PROCESSING") else 1, x["job_id"]), reverse=False)
    return {"ok": True, "jobs": jobs}


@router.get("/renders/{job_id}/{filename}")
def get_render(job_id: str, filename: str) -> FileResponse:
    _validate_job_id(job_id)
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    if not _FILENAME_RE.match(filename):
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    settings = get_settings()
    # Validate must be within renders/job_id — is_relative_to safe
    base = settings.storage_path / "renders" / job_id
    path = base / filename
    _resolve_within_storage(path, settings.storage_path / "renders")
    try:
        if path.resolve().parent != base.resolve():
            raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Path tidak valid")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path, media_type="video/mp4", filename=filename)
