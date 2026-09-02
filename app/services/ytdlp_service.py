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


async def fetch_info(url: str, no_playlist: bool = True, timeout: float = 25.0) -> dict[str, Any]:
    """Run yt-dlp --dump-json --no-download to get metadata. Timeout & anti-bengong."""
    if not url or not url.startswith(("http://", "https://")):
        raise ValueError("URL harus diawali http:// atau https://")

    base_flags = [
        "--dump-json",
        "--no-download",
        "--no-warnings",
        "--no-playlist" if no_playlist else "--yes-playlist",
        "--skip-download",
        "--no-check-certificate",
        "--socket-timeout", "15",
        "--retries", "2",
        "--extractor-retries", "1",
        "--ignore-errors",
        # Try to bypass YouTube bot detection on datacenter IP (Koyeb)
        "--extractor-args", "youtube:player_client=android,web",
    ] + _get_proxy_args()

    # Optional cookies fallback for YouTube 403
    import os as _os
    cookies_path = _os.getenv("YTDLP_COOKIES") or _os.getenv("YTDLP_COOKIES_PATH")
    if cookies_path and Path(cookies_path).exists():
        base_flags += ["--cookies", str(Path(cookies_path))]

    async def _run(cmd: list[str]) -> tuple[int, bytes, bytes]:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            try:
                proc.kill()
                await proc.wait()
            except Exception:
                pass
            raise RuntimeError(f"yt-dlp timeout {timeout:.0f}s — YouTube mungkin memblokir IP server (datacenter) atau jaringan lambat. Coba lagi, ganti link TikTok/IG, atau langsung klik 'Download' tanpa 'Cek Info'")
        return proc.returncode or 0, stdout, stderr

    # Primary attempt
    cmd = YTDLP_BASE + base_flags + [url]
    cmd = [c for c in cmd if c]
    returncode, stdout, stderr = await _run(cmd)
    if returncode != 0:
        err = stderr.decode(errors="ignore").strip() or stdout.decode(errors="ignore").strip()
        # If YouTube bot detection, retry without android client fallback is already included, but give friendly msg
        if len(err) > 900:
            err = err[-900:]
        # Common YouTube blocks: "Sign in to confirm", "bot", "unavailable"
        lower = err.lower()
        if any(k in lower for k in ["sign in", "bot", "unavailable", "private video", "video unavailable"]):
            err = err + " — Tip: YouTube sering blokir IP datacenter (Koyeb). Coba link TikTok/Instagram, atau klik 'Download' langsung (lebih toleran) tanpa 'Cek Info'."
        raise RuntimeError(err or f"yt-dlp gagal (code {returncode})")

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


def _get_proxy_args() -> list[str]:
    """Return --proxy args if proxy env is configured (pool of 5 proxies)."""
    import os as _os
    proxy = _os.getenv("YTDLP_PROXY") or _os.getenv("HTTP_PROXY") or _os.getenv("HTTPS_PROXY") or _os.getenv("http_proxy") or _os.getenv("https_proxy")
    # Support comma-separated pool: rotate via hash of time
    if proxy:
        # If multiple proxies comma-separated, pick one deterministically
        if "," in proxy:
            proxies = [p.strip() for p in proxy.split(",") if p.strip()]
            if proxies:
                import hashlib as _hl
                idx = int(_hl.md5(str(time.time()).encode()).hexdigest(), 16) % len(proxies)
                proxy = proxies[idx]
        return ["--proxy", proxy]
    return []


def _sanitize_extra_args(extra_args: str) -> list[str]:
    """Whitelist strict for extra_args to prevent RCE."""
    if not extra_args or not extra_args.strip():
        return []
    # Block any exec / postprocessor / proxy manipulation / load-info etc
    lower = extra_args.lower()
    blocked_substrings = ["--exec", "--postprocessor-args", "--ppa", "--load-info", "--proxy", "--config-location", "--batch-file", "--exec-before", "--exec-after"]
    for b in blocked_substrings:
        if b in lower:
            return []
    # Whitelist: only these flags allowed (each takes a value)
    allowed = {"--sleep-interval", "--max-filesize", "--limit-rate", "--concurrent-fragments", "--retries", "--fragment-retries", "--socket-timeout"}
    try:
        parts = shlex.split(extra_args.strip())
    except ValueError:
        return []
    safe: list[str] = []
    i = 0
    while i < len(parts):
        p = parts[i]
        if p in allowed:
            if i + 1 < len(parts):
                val = parts[i + 1]
                # Validate value: must not start with - and must be sane
                if val.startswith("-"):
                    i += 2
                    continue
                # limit values length
                if len(val) > 20:
                    i += 2
                    continue
                safe.extend([p, val])
                i += 2
            else:
                i += 1
        else:
            # Ignore unknown flag; do not allow bare values
            i += 1
    return safe


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
    # Use --restrict-filenames to prevent subdir traversal via %(title)s containing / or ..
    # Also add %(id)s to guarantee uniqueness and avoid overwrite
    out_tmpl = str(out_path / "%(title)s [%(id)s].%(ext)s")

    cmd = YTDLP_BASE + [
        "--no-warnings",
        "--newline",
        "--no-mtime",
        "--restrict-filenames",
        "--output",
        out_tmpl,
        "--format",
        fmt,
    ]
    # Proxy pool support
    cmd += _get_proxy_args()

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

    cmd += ["--write-info-json"]
    if with_subs:
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "id,en", "--embed-subs"]

    if embed_thumbnail and not audio_only:
        cmd += ["--embed-thumbnail"]

    # extra args whitelist strict
    cmd += _sanitize_extra_args(extra_args)

    cmd.append(url)
    return cmd



def load_yt_info(src: Path) -> dict:
    """Load sidecar .info.json next to src if exists — for NLP context."""
    try:
        # src like "title [id].mp4" -> same stem .info.json
        cand = src.with_suffix(".info.json")
        # yt-dlp --write-info-json creates file with same name but .info.json extension
        # e.g. video.mp4.info.json or title [id].info.json ; also try src.parent glob
        if cand.exists():
            import json as _j
            return _j.loads(cand.read_text(encoding="utf-8", errors="ignore"))
        # fallback glob id
        import json as _j2
        for q in src.parent.glob("*.info.json"):
            # most recent
            try:
                return _j2.loads(q.read_text(encoding="utf-8", errors="ignore"))
            except: continue
    except: pass
    return {}

def _dir_stats(d: Path) -> tuple[int, int]:
    try:
        if not d.exists():
            return 0, 0
        count = 0
        total = 0
        for p in d.rglob("*"):
            if p.is_file():
                # skip partials
                if p.suffix in (".part", ".ytdl", ".temp"):
                    continue
                try:
                    total += p.stat().st_size
                    count += 1
                except Exception:
                    continue
        return count, total
    except Exception:
        return 0, 0


def _check_storage_quotas() -> str | None:
    """Check quotas for downloads/renders/uploads/cache/previews. Return error msg if exceeded."""
    settings = get_settings()
    base = settings.storage_path
    # thresholds: downloads 500/15GB, others 500/5GB, total 18GB
    limits = {
        "downloads": (500, 15 * 1024**3),
        "renders": (500, 5 * 1024**3),
        "uploads": (300, 5 * 1024**3),
        "cache": (500, 3 * 1024**3),
        "previews": (500, 2 * 1024**3),
    }
    total_all = 0
    for sub, (max_files, max_bytes) in limits.items():
        d = base / sub
        c, b = _dir_stats(d)
        total_all += b
        if c > max_files:
            return f"Disk quota: storage/{sub} terlalu banyak file ({c}>{max_files}), hapus file lama"
        if b > max_bytes:
            return f"Disk quota: storage/{sub} >{max_bytes//1024**3}GB ({b/1024**3:.1f}GB), hapus file lama"
    if total_all > 18 * 1024**3:
        return f"Disk quota: total storage >18GB ({total_all/1024**3:.1f}GB), hapus file lama"
    return None


async def run_download_job(job: YtdlpJob):
    out_dir = get_download_dir()
    err = _check_storage_quotas()
    if err:
        job.status = "error"
        job.error = err
        job.logs.append(err)
        return
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

            # Detect destination — validate stays within download_dir via is_relative_to
            if "Destination:" in decoded:
                # extract filename
                m = re.search(r"Destination:\s*(.+)", decoded)
                if m:
                    candidate = m.group(1).strip()
                    try:
                        cand_path = Path(candidate).resolve()
                        is_within = cand_path.is_relative_to(out_dir.resolve())
                    except AttributeError:
                        try:
                            cand_path.relative_to(out_dir.resolve())
                            is_within = True
                        except Exception:
                            is_within = False
                    except Exception:
                        is_within = False
                    if is_within:
                        job.filepath = candidate
                        job.filename = Path(candidate).name
                    else:
                        try:
                            job.logs.append(f"Blocked traversal dest: {candidate}")
                        except Exception:
                            pass
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
    # TTL cleanup for JOBS to avoid OOM
    now = time.time()
    stale = [jid for jid, j in list(JOBS.items()) if j.finished_at and (now - j.finished_at) > 3600]
    old_pending = [jid for jid, j in list(JOBS.items()) if not j.finished_at and (now - j.created_at) > 86400]
    for jid in stale + old_pending:
        JOBS.pop(jid, None)
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
