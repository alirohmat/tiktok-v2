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
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-API-Key", "X-Requested-With"],
)

# API-Key gate: jika CLIPPER_API_KEY/API_KEY set, POST/PUT/PATCH/DELETE butuh X-API-Key
from fastapi import Request as _Req
from fastapi.responses import JSONResponse as _JR

@app.middleware("http")
async def _api_key_gate(request: _Req, call_next):
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        # preflight bebas
        if request.method == "OPTIONS":
            return await call_next(request)
        try:
            from app.core.config import get_settings as _gs2
            need = (_gs2().api_key or "").strip()
        except Exception:
            need = ""
        if need:
            got = request.headers.get("x-api-key") or request.headers.get("X-API-Key") or ""
            # juga dukung Authorization: Bearer <key>
            if not got:
                auth = request.headers.get("authorization") or request.headers.get("Authorization") or ""
                if auth.lower().startswith("bearer "):
                    got = auth[7:].strip()
            # constant-time compare
            import hmac as _hm
            if not _hm.compare_digest(got, need):
                return _JR(status_code=401, content={"detail": "Unauthorized: X-API-Key salah"})
    return await call_next(request)

app.include_router(router)
app.include_router(ytdlp_router)

# Alias for reverse proxy path-based routing: /api/renders/* -> same as /renders/*
from fastapi import HTTPException
from fastapi.responses import FileResponse as _FileResponse
from pathlib import Path as _Path
import re as _re
_JOB_RE = _re.compile(r"^(?:[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|[a-f0-9]{8,32})$")

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

# Serve static frontend (yt-dlp UI) — prefer Svelte dist, fallback legacy index.html
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(parents=True, exist_ok=True)
dist_dir = static_dir / "dist"

# mount /static for legacy assets, /assets for Vite build
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
if dist_dir.exists():
    # Vite assets are dist/assets
    assets_dir = dist_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets-svelte")


@app.get("/", include_in_schema=False)
def serve_index():
    # Prefer Svelte SPA
    idx_dist = dist_dir / "index.html"
    if idx_dist.exists():
        return FileResponse(idx_dist, media_type="text/html")
    idx = static_dir / "index.html"
    if idx.exists():
        return FileResponse(idx, media_type="text/html")
    return {"message": "yt-dlp UI not built yet", "docs": "/docs"}


@app.get("/ytdlp", include_in_schema=False)
def serve_ytdlp():
    idx_dist = dist_dir / "index.html"
    if idx_dist.exists():
        return FileResponse(idx_dist, media_type="text/html")
    idx = static_dir / "index.html"
    if idx.exists():
        return FileResponse(idx, media_type="text/html")
    return {"message": "yt-dlp UI not built yet"}
