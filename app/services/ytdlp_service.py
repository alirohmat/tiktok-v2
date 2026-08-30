from __future__ import annotations

import asyncio
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.core.config import get_settings


def _yt_dlp_cmd() -> list[str]:
    # Prefer venv python -m yt_dlp for reliability
    # Fall back to yt-dlp binary
    py = Path(sys.executable)
    # Try sys.executable -m yt_dlp
    # Check if yt_dlp importable
    try:
        import yt_dlp  # type: ignore

        return [str(py), "-m", "yt_dlp"]
    except Exception:
        pass
    bin_path = shutil.which("yt-dlp")
    if bin_path:
        return [bin_path]
    # fallback to /tmp/yt-venv
    cand = Path("/tmp/yt-venv/bin/yt-dlp")
    if cand.exists():
        return [str(cand)]
    return [str(py), "-m", "yt_dlp"]


YTDLP_BASE = _yt_dlp_cmd()


@dataclass
class YtdlpJob:
    job_id: str
    url: str
    status: str = "queued"  # queued, downloading, completed, error
    progress: float = 0.0
    speed: str = ""
    eta: str = ""
    filename: str = ""
    filepath: str = ""
    filesize: str = ""
    title: str = ""
    error: str = ""
    logs: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    options: dict[str, Any] = field(default_factory=dict)
    process: Any = None


JOBS: dict[str, YtdlpJob] = {}


def get_download_dir() -> Path:
    settings = get_settings()
    d = settings.storage_path / "downloads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def safe_filename(name: str) -> str:
    # yt-dlp handles templating; this is for display
    return re.sub(r'[\\/*?:"<>|]', "_", name)


def build_format_selector(quality: str, audio_only: bool, format_pref: str) -> str:
    if audio_only:
        if format_pref in ("mp3", "aac", "m4a", "opus", "wav"):
            return "bestaudio/best"
        return "bestaudio/best"
    if format_pref == "mp3" or format_pref == "m4a":
        return "bestaudio/best"
    # video quality mapping
    qmap = {
        "best": "bv*+ba/b",
        "1080": "bv*[height<=1080]+ba/b[height<=1080] / bv*+ba/b",
        "720": "bv*[height<=720]+ba/b[height<=720] / bv*+ba/b",
        "480": "bv*[height<=480]+ba/b[height<=480] / bv*+ba/b",
        "worst": "worst",
    }
    return qmap.get(quality, "bv*+ba/b")


def parse_progress_line(line: str) -> dict[str, Any] | None:
    # yt-dlp progress: [download]  45.2% of 12.34MiB at  1.23MiB/s ETA 00:05
    # or [download] 100% ...
    if "[download]" not in line:
        return None
    m = re.search(r"(\d+\.?\d*)%\s+of\s+([~\d\.]+\w+)?", line)
    pct = None
    if m:
        try:
            pct = float(m.group(1))
        except:
            pct = None
    speed = ""
    eta = ""
    sm = re.search(r"at\s+([^\s]+/s)", line)
    if sm:
        speed = sm.group(1)
    em = re.search(r"ETA\s+([^\s]+)", line)
    if em:
        eta = em.group(1)
    dest = ""
    dm = re.search(r"Destination:\s*(.+)", line)
    if dm:
        dest = dm.group(1).strip()
    # merging etc
    if pct is not None or speed or eta:
        return {"pct": pct, "speed": speed, "eta": eta, "dest": dest}
    return None


async def fetch_info(url: str, no_playlist: bool = True) -> dict[str, Any]:
    """Run yt-dlp --dump-json --no-download to get metadata."""
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("URL harus diawali http:// atau https://")

    cmd = YTDLP_BASE + [
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--no-playlist" if no_playlist else "--yes-playlist",
        "--skip-download",
        url,
    ]
    # Remove empty?
    cmd = [c for c in cmd if c]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        err = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        # trim long
        if len(err) > 800:
            err = err[-800:]
        raise RuntimeError(err or f"yt-dlp gagal (code {proc.returncode})")

    text = stdout.decode(errors="ignore").strip()
    # yt-dlp outputs one JSON per video; for playlist it outputs multiple lines => take first
    first_line = text.splitlines()[0] if text else ""
    if not first_line:
        raise RuntimeError("Tidak ada data ditemukan")
    try:
        data = json.loads(first_line)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Gagal parse info: {e}") from e

    # Normalize key fields
    formats = data.get("formats", []) or []
    # sort formats by height desc
    formats_sorted = sorted(formats, key=lambda f: (f.get("height") or 0, f.get("tbr") or 0), reverse=True)
    # Keep lightweight subset for UI
    fmt_list = []
    for f in formats_sorted[:30]:
        fmt_list.append(
            {
                "format_id": f.get("format_id"),
                "ext": f.get("ext"),
                "resolution": f.get("resolution") or f"{f.get('width', '')}x{f.get('height', '')}",
                "height": f.get("height"),
                "fps": f.get("fps"),
                "vcodec": f.get("vcodec"),
                "acodec": f.get("acodec"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
                "tbr": f.get("tbr"),
                "protocol": f.get("protocol"),
                "format_note": f.get("format_note"),
            }
        )

    return {
        "id": data.get("id"),
        "title": data.get("title"),
        "uploader": data.get("uploader") or data.get("channel"),
        "uploader_id": data.get("uploader_id"),
        "duration": data.get("duration"),
        "duration_string": data.get("duration_string"),
        "view_count": data.get("view_count"),
        "like_count": data.get("like_count"),
        "thumbnail": data.get("thumbnail"),
        "thumbnails": data.get("thumbnails", [])[-3:] if data.get("thumbnails") else [],
        "description": (data.get("description") or "")[:2000],
        "webpage_url": data.get("webpage_url") or url,
        "extractor": data.get("extractor"),
        "extractor_key": data.get("extractor_key"),
        "is_live": data.get("is_live"),
        "was_live": data.get("was_live"),
        "formats": fmt_list,
        "formats_count": len(formats),
        "requested_formats": data.get("requested_formats"),
        "width": data.get("width"),
        "height": data.get("height"),
        "fps": data.get("fps"),
        "vcodec": data.get("vcodec"),
        "acodec": data.get("acodec"),
        "ext": data.get("ext"),
    }


def build_download_cmd(
    url: str,
    out_path: Path,
    quality: str = "best",
    format_pref: str = "mp4",
    audio_only: bool = False,
    no_playlist: bool = True,
    with_subs: bool = False,
    embed_thumbnail: bool = False,
    extra_args: str = "",
) -> list[str]:
    fmt = build_format_selector(quality, audio_only, format_pref)
    # Output template: use out_path as directory + %(title)s.%(ext)s but sanitize
    # We'll let yt-dlp handle title sanitizing
    out_tmpl = str(out_path / "%(title)s [%(id)s].%(ext)s")

    cmd = YTDLP_BASE + [
        "--no-warnings",
        "--newline",
        "--no-mtime",
        "--output",
        out_tmpl,
        "--format",
        fmt,
    ]

    if no_playlist:
        cmd.append("--no-playlist")
    else:
        cmd.append("--yes-playlist")

    # Merge / recode
    if audio_only:
        if format_pref == "mp3":
            cmd += ["--extract-audio", "--audio-format", "mp3", "--audio-quality", "0"]
        elif format_pref in ("m4a", "aac"):
            cmd += ["--extract-audio", "--audio-format", "m4a"]
        elif format_pref == "opus":
            cmd += ["--extract-audio", "--audio-format", "opus"]
        else:
            cmd += ["--extract-audio", "--audio-format", "best"]
    else:
        # Ensure mp4 when requested, else let yt-dlp merge
        if format_pref == "mp4":
            cmd += ["--merge-output-format", "mp4"]
        elif format_pref == "mkv":
            cmd += ["--merge-output-format", "mkv"]
        elif format_pref == "webm":
            cmd += ["--merge-output-format", "webm"]

    if with_subs:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "id,en", "--embed-subs"]

    if embed_thumbnail and not audio_only:
        cmd += ["--embed-thumbnail"]

    # extra args safe split
    if extra_args and extra_args.strip():
        try:
            parts = shlex.split(extra_args.strip())
            # block dangerous flags that escape output dir
            blocked = {"--exec", "--exec-before-download", "--load-info-json"}
            filtered = [p for p in parts if p not in blocked]
            cmd += filtered
        except ValueError:
            # fallback: ignore malformed extra args
            pass

    cmd.append(url)
    return cmd


async def run_download_job(job: YtdlpJob):
    out_dir = get_download_dir()
    # Ensure unique per job subfolder? Use flat downloads dir; yt-dlp already dedupes with id
    cmd = build_download_cmd(
        job.url,
        out_dir,
        quality=job.options.get("quality", "best"),
        format_pref=job.options.get("format", "mp4"),
        audio_only=job.options.get("audio_only", False),
        no_playlist=job.options.get("no_playlist", True),
        with_subs=job.options.get("with_subs", False),
        embed_thumbnail=job.options.get("embed_thumbnail", False),
        extra_args=job.options.get("extra_args", ""),
    )
    job.status = "downloading"
    job.logs.append(f"$ {' '.join(shlex.quote(c) for c in cmd)}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        job.process = proc
        assert proc.stdout is not None

        # Read line by line
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            decoded = line.decode(errors="ignore").rstrip()
            if not decoded:
                continue
            job.logs.append(decoded)
            # keep last 400 lines
            if len(job.logs) > 400:
                job.logs = job.logs[-400:]

            # progress parse
            prog = parse_progress_line(decoded)
            if prog:
                if prog.get("pct") is not None:
                    job.progress = float(prog["pct"])
                if prog.get("speed"):
                    job.speed = prog["speed"]
                if prog.get("eta"):
                    job.eta = prog["eta"]

            # Detect destination
            if "Destination:" in decoded:
                # extract filename
                m = re.search(r"Destination:\s*(.+)", decoded)
                if m:
                    job.filepath = m.group(1).strip()
                    job.filename = Path(job.filepath).name
            # Detect already downloaded
            if "[download] " in decoded and "has already been downloaded" in decoded:
                job.progress = 100.0
            # Extracted audio
            if "Destination:" not in decoded and "Merging formats into" in decoded:
                m = re.search(r'"(.+)"', decoded)
                if m:
                    job.filepath = m.group(1)
                    job.filename = Path(job.filepath).name

        await proc.wait()
        if proc.returncode == 0:
            job.status = "completed"
            job.progress = 100.0
            job.finished_at = time.time()
            # Try to find file if not captured
            if not job.filepath or not Path(job.filepath).exists():
                # search latest file in download dir
                files = sorted(out_dir.glob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
                if files:
                    # find most recent that matches job timing (within 2 minutes)
                    for f in files[:5]:
                        if f.is_file() and f.stat().st_mtime >= job.created_at - 1:
                            job.filepath = str(f)
                            job.filename = f.name
                            break
            # filesize
            try:
                if job.filepath and Path(job.filepath).exists():
                    sz = Path(job.filepath).stat().st_size
                    job.filesize = f"{sz / (1024*1024):.2f} MB" if sz > 1024*1024 else f"{sz/1024:.1f} KB"
            except:
                pass
        else:
            job.status = "error"
            job.error = job.logs[-1] if job.logs else f"yt-dlp exit {proc.returncode}"
            job.finished_at = time.time()

    except asyncio.CancelledError:
        try:
            if job.process:
                job.process.terminate()
        except:
            pass
        job.status = "error"
        job.error = "Cancelled"
        raise
    except Exception as e:
        job.status = "error"
        job.error = str(e)
        job.logs.append(f"ERROR: {e}")


def create_job(url: str, options: dict[str, Any]) -> YtdlpJob:
    job_id = uuid.uuid4().hex[:12]
    job = YtdlpJob(job_id=job_id, url=url, options=options)
    JOBS[job_id] = job
    return job


def list_downloaded_files() -> list[dict[str, Any]]:
    d = get_download_dir()
    files: list[dict[str, Any]] = []
    for p in sorted(d.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if not p.is_file():
            continue
        # skip hidden / partial
        if p.suffix in (".part", ".ytdl", ".temp"):
            continue
        try:
            stat = p.stat()
            files.append(
                {
                    "name": p.name,
                    "path": str(p),
                    "size": stat.st_size,
                    "size_human": f"{stat.st_size/(1024*1024):.2f} MB" if stat.st_size > 1024*1024 else f"{stat.st_size/1024:.1f} KB",
                    "mtime": stat.st_mtime,
                    "mtime_human": time.strftime("%d %b %Y %H:%M", time.localtime(stat.st_mtime)),
                    "ext": p.suffix.lstrip("."),
                }
            )
        except:
            continue
    return files
