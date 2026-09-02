from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional

import re as _re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.services.ytdlp_service import (
    JOBS,
    create_job,
    fetch_info,
    get_download_dir,
    list_downloaded_files,
    run_download_job,
)

_JOB_RE = _re.compile(r"^[a-f0-9]{12}$")
_FILENAME_RE = _re.compile(r"^[^/\\]+$")


def _is_within(child: Path, parent: Path) -> bool:
    try:
        return child.resolve().is_relative_to(parent.resolve())
    except AttributeError:
        try:
            child.resolve().relative_to(parent.resolve())
            return True
        except Exception:
            return False
    except Exception:
        return False

router = APIRouter(prefix="/api/ytdlp", tags=["ytdlp"])


class InfoRequest(BaseModel):
    url: str = Field(min_length=4)
    no_playlist: bool = True


class DownloadRequest(BaseModel):
    url: str = Field(min_length=4)
    quality: str = Field(default="best", description="best,1080,720,480,worst")
    format: str = Field(default="mp4", description="mp4,mkv,webm,mp3,m4a,opus,best")
    audio_only: bool = False
    no_playlist: bool = True
    with_subs: bool = False
    embed_thumbnail: bool = False
    extra_args: str = ""


@router.post("/info")
async def ytdlp_info(body: InfoRequest):
    try:
        data = await fetch_info(body.url, no_playlist=body.no_playlist)
        return {"ok": True, "data": data}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/download")
async def ytdlp_download(body: DownloadRequest, bg: BackgroundTasks):
    from app.services.ytdlp_service import validate_download_url as _vurl
    try:
        _vurl(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Rate-limit shared via redis set when available, fallback in-memory (#4 multi-worker)
    def _is_rate_limited() -> bool:
        try:
            import redis as _r  # type: ignore[import-untyped]
            from app.core.config import get_settings as _gs
            _s = _gs()
            _cli = _r.from_url(_s.redis_url, socket_connect_timeout=1)
            try:
                if _cli.scard("ytdlp:active") > 20:
                    return True
            finally:
                try:
                    _cli.close()
                except Exception:
                    pass
        except Exception:
            pass
        return sum(1 for j in JOBS.values() if j.status in ("queued", "downloading")) > 20

    if _is_rate_limited():
        raise HTTPException(status_code=429, detail="Terlalu banyak download aktif, coba lagi nanti")

    options = {
        "quality": body.quality,
        "format": body.format,
        "audio_only": body.audio_only,
        "no_playlist": body.no_playlist,
        "with_subs": body.with_subs,
        "embed_thumbnail": body.embed_thumbnail,
        "extra_args": body.extra_args,
    }
    job = create_job(body.url, options)

    def _runner_sync():
        import asyncio as _asyncio
        _loop = _asyncio.new_event_loop()
        try:
            _asyncio.set_event_loop(_loop)
            _loop.run_until_complete(run_download_job(job))
        except Exception as e:
            job.status = "error"
            job.error = str(e)
        finally:
            _loop.close()
            _asyncio.set_event_loop(None)

    # Use BackgroundTasks for persistence across event loop reload
    bg.add_task(_runner_sync)

    return {"ok": True, "job_id": job.job_id, "status": job.status}


@router.get("/jobs")
def list_jobs():
    items = []
    for j in sorted(JOBS.values(), key=lambda x: x.created_at, reverse=True):
        items.append(
            {
                "job_id": j.job_id,
                "url": j.url,
                "status": j.status,
                "progress": j.progress,
                "speed": j.speed,
                "eta": j.eta,
                "filename": j.filename,
                "filepath": j.filepath,
                "filesize": j.filesize,
                "error": j.error,
                "created_at": j.created_at,
                "finished_at": j.finished_at,
                "options": j.options,
                "logs_tail": j.logs[-20:],
            }
        )
    return {"ok": True, "jobs": items}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    if not _JOB_RE.match(job_id):
        raise HTTPException(status_code=400, detail="job_id tidak valid")
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan")
    return {
        "ok": True,
        "job": {
            "job_id": job.job_id,
            "url": job.url,
            "status": job.status,
            "progress": job.progress,
            "speed": job.speed,
            "eta": job.eta,
            "filename": job.filename,
            "filepath": job.filepath,
            "filesize": job.filesize,
            "error": job.error,
            "created_at": job.created_at,
            "finished_at": job.finished_at,
            "options": job.options,
            "logs": job.logs,
        },
    }


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    if not _JOB_RE.match(job_id):
        raise HTTPException(status_code=400, detail="job_id tidak valid")
    job = JOBS.pop(job_id, None)
    if not job:
        raise HTTPException(status_code=404, detail="Job tidak ditemukan")
    # try cancel process
    try:
        if job.process and job.status == "downloading":
            job.process.terminate()
    except:
        pass
    return {"ok": True}


@router.get("/files")
def get_files():
    files = list_downloaded_files()
    return {"ok": True, "files": files, "download_dir": str(get_download_dir())}


@router.get("/files/download")
def download_file(name: str):
    # name is filename, prevent path traversal
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    if not _FILENAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    dl_dir = get_download_dir()
    p = dl_dir / name
    if not _is_within(p, dl_dir):
        raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    try:
        if p.resolve().parent != dl_dir.resolve():
            raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="File tidak ditemukan")
    # Guess media type
    return FileResponse(path=p, filename=p.name, media_type="application/octet-stream")


@router.delete("/files/{name}")
def delete_file(name: str):
    if "/" in name or "\\" in name or ".." in name:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    if not _FILENAME_RE.match(name):
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    dl_dir = get_download_dir()
    p = dl_dir / name
    if not _is_within(p, dl_dir):
        raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    try:
        if p.resolve().parent != dl_dir.resolve():
            raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    if not p.exists():
        raise HTTPException(status_code=404, detail="File tidak ditemukan")
    try:
        p.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"ok": True}


@router.get("/health")
def health():
    from app.services.ytdlp_service import YTDLP_BASE
    import subprocess

    try:
        r = subprocess.run(YTDLP_BASE + ["--version"], capture_output=True, text=True, timeout=5)
        ver = r.stdout.strip() if r.returncode == 0 else r.stderr.strip()
    except Exception as e:
        ver = f"error: {e}"
    return {"ok": True, "ytdlp_version": ver, "ytdlp_cmd": " ".join(YTDLP_BASE), "download_dir": str(get_download_dir())}
