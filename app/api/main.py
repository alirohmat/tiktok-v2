from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.ytdlp import router as ytdlp_router

app = FastAPI(title="TikTok v2 Clipper — yt-dlp Edition", version="0.2.0")

# CORS: never allow wildcard + credentials (browser blocks). Use env FRONTEND_ORIGIN or *
_frontend = os.getenv("FRONTEND_ORIGIN") or os.getenv("CORS_ALLOW_ORIGINS") or ""
if _frontend:
    _origins = [o.strip() for o in _frontend.split(",") if o.strip()]
else:
    # By default, allow all origins but without credentials (safe for wildcard)
    _origins = ["*"]
_allow_creds = False
if _origins != ["*"]:
    # Only allow credentials when origins are explicit
    _allow_creds = os.getenv("CORS_ALLOW_CREDENTIALS", "false").lower() in ("1", "true", "yes")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=_allow_creds,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(ytdlp_router)

# Alias for reverse proxy path-based routing: /api/renders/* -> same as /renders/*
from fastapi import HTTPException
from fastapi.responses import FileResponse as _FileResponse
from pathlib import Path as _Path
import re as _re
_JOB_RE = _re.compile(r"^[a-f0-9-]{8,}$")

def _is_within_main(child: _Path, parent: _Path) -> bool:
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

@app.get("/api/renders/{job_id}/{filename}", include_in_schema=False)
def api_get_render(job_id: str, filename: str):
    if not _JOB_RE.match(job_id):
        raise HTTPException(status_code=400, detail="job_id tidak valid")
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Nama file tidak valid")
    from app.core.config import get_settings as _gs
    settings = _gs()
    base = settings.storage_path / "renders" / job_id
    path = base / filename
    if not _is_within_main(path, settings.storage_path / "renders"):
        raise HTTPException(status_code=400, detail="Path traversal terdeteksi")
    if not path.exists() or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return _FileResponse(path, media_type="video/mp4", filename=filename)

# Serve static frontend (yt-dlp UI)
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)

# mount /static for assets if needed
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

@app.get("/", include_in_schema=False)
def serve_index():
    idx = static_dir / "index.html"
    if idx.exists():
        return FileResponse(idx, media_type="text/html")
    return {"message": "yt-dlp UI not built yet", "docs": "/docs"}

@app.get("/ytdlp", include_in_schema=False)
def serve_ytdlp():
    idx = static_dir / "index.html"
    if idx.exists():
        return FileResponse(idx, media_type="text/html")
    return {"message": "yt-dlp UI not built yet"}
