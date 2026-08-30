from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes import router
from app.api.ytdlp import router as ytdlp_router

app = FastAPI(title="TikTok v2 Clipper — yt-dlp Edition", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
app.include_router(ytdlp_router)

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
