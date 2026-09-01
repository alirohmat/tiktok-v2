from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from app.models.schemas import DeadAir


def _binary() -> str | None:
    p = shutil.which("auto-editor")
    if p:
        return p
    cand = Path("/usr/local/bin/auto-editor")
    if cand.exists() and cand.stat().st_mode & 0o111:
        return str(cand)
    # local dev
    local = Path("./auto-editor")
    if local.exists():
        return str(local)
    return None


def is_available() -> bool:
    return _binary() is not None


def trim_silence(
    src: Path,
    dst: Path,
    threshold: str = "0.04",
    margin: str = "0.2s",
    min_clip: str = "0.2s",
) -> Path:
    """PASS 1 global: cut silence before Whisper. Returns dst (or src if unavailable/fail)."""
    bin_path = _binary()
    if bin_path is None:
        return src
    dst.parent.mkdir(parents=True, exist_ok=True)
    # auto-editor --edit audio:threshold=X --margin M --output dst
    cmd = [
        bin_path,
        str(src),
        "--edit",
        f"audio:threshold={threshold}",
        "--margin",
        margin,
        "--silent-speed",
        "99999",
        "--video-speed",
        "99999",
        "-o",
        str(dst),
        "--no-open",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            # fallback: try dB variant
            return src
        if dst.exists() and dst.stat().st_size > 0:
            return dst
        return src
    except Exception:
        return src


def export_dead_air(
    src: Path,
    threshold: str = "0.04",
    margin: str = "0.2s",
    hook_protect_until: float = 3.0,
    clip_start: float = 0.0,
    clip_end: float | None = None,
) -> list[DeadAir]:
    """PASS 2: export timeline json -> derive dead_air gaps. Exclude 0-hook_protect_until."""
    bin_path = _binary()
    if bin_path is None:
        return []
    tmp_json = src.parent / f".ae_{src.stem}.json"
    cmd = [
        bin_path,
        str(src),
        "--edit",
        f"audio:threshold={threshold}",
        "--margin",
        margin,
        "--export",
        "json",
        "-o",
        str(tmp_json),
        "--no-open",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if r.returncode != 0 or not tmp_json.exists():
            return []
        data = json.loads(tmp_json.read_text())
        # auto-editor json: {"chunks": [[s,e],...]} or {"timeline": ...} handle both
        chunks = None
        if isinstance(data, dict):
            chunks = data.get("chunks") or data.get("timeline") or data.get("clips")
            # some versions wrap in {"v":..., "chunks": [[s_frame,e_frame],...]}
            if chunks is None and "v" in data:
                chunks = data.get("chunks")
        elif isinstance(data, list):
            chunks = data
        if not chunks:
            return []
        # normalize chunks to seconds (auto-editor may emit frame numbers if fps known)
        # Try detect: if values > 10000 likely frames -> divide by fps 30
        # We probe via reading first chunk
        normalized = []
        for c in chunks:
            if isinstance(c, (list, tuple)) and len(c) >= 2:
                s, e = float(c[0]), float(c[1])
                # heuristic: if src duration < 3600 and values > 3600*30, treat as frames
                if s > 5000 and e > 5000:
                    # assume 30 fps
                    s, e = s / 30.0, e / 30.0
                normalized.append((s, e))
        if not normalized:
            return []
        normalized.sort()
        # dead_air = gaps between kept chunks, clipped to [clip_start, clip_end]
        start = clip_start
        end = clip_end if clip_end is not None else normalized[-1][1]
        gaps: list[DeadAir] = []
        # gap before first chunk
        if normalized[0][0] > start + 0.05:
            gs, ge = start, normalized[0][0]
            if ge - gs >= 0.2 and ge > hook_protect_until:
                # clip gap that overlaps hook protect
                if gs < hook_protect_until < ge:
                    gs = hook_protect_until
                if ge - gs >= 0.2:
                    gaps.append(DeadAir(start=round(gs, 3), end=round(ge, 3)))
        for i in range(len(normalized) - 1):
            gs = normalized[i][1]
            ge = normalized[i + 1][0]
            if ge - gs < 0.2:
                continue
            # skip gap fully inside hook protect
            if ge <= hook_protect_until:
                continue
            if gs < hook_protect_until < ge:
                gs = hook_protect_until
            # clip to window
            gs = max(gs, start)
            ge = min(ge, end)
            if ge - gs >= 0.2:
                try:
                    gaps.append(DeadAir(start=round(gs, 3), end=round(ge, 3)))
                except Exception:
                    continue
        # gap after last chunk
        if normalized[-1][1] < end - 0.05:
            gs, ge = normalized[-1][1], end
            if ge - gs >= 0.2 and ge > hook_protect_until:
                if gs < hook_protect_until:
                    gs = hook_protect_until
                if ge - gs >= 0.2:
                    try:
                        gaps.append(DeadAir(start=round(gs, 3), end=round(ge, 3)))
                    except Exception:
                        pass
        return gaps
    except Exception:
        return []
    finally:
        try:
            if tmp_json.exists():
                tmp_json.unlink()
        except Exception:
            pass


def cut_clip_file(
    clip_path: Path,
    threshold: str = "0.03",
    margin: str = "0.1s",
) -> bool:
    """PASS 2 in-place: run auto-editor on rendered clip to jump-cut internal silence. Returns True if changed."""
    bin_path = _binary()
    if bin_path is None or not clip_path.exists():
        return False
    tmp_out = clip_path.with_suffix(".ae.mp4")
    cmd = [
        bin_path,
        str(clip_path),
        "--edit",
        f"audio:threshold={threshold}",
        "--margin",
        margin,
        "--silent-speed",
        "99999",
        "--video-speed",
        "99999",
        "-o",
        str(tmp_out),
        "--no-open",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
        if r.returncode == 0 and tmp_out.exists() and tmp_out.stat().st_size > 0:
            tmp_out.replace(clip_path)
            return True
        if tmp_out.exists():
            tmp_out.unlink(missing_ok=True)
        return False
    except Exception:
        return False
