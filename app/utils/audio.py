from __future__ import annotations

import json
import subprocess
from pathlib import Path


def get_duration(path: Path) -> float:
    """Return media duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "quiet",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    data = json.loads(result.stdout)
    # Try format duration first
    try:
        return float(data["format"]["duration"])
    except (KeyError, ValueError, TypeError):
        pass
    for s in data.get("streams", []):
        if "duration" in s:
            try:
                return float(s["duration"])
            except (ValueError, TypeError):
                continue
    raise ValueError(f"Cannot determine duration for {path}")


def extract_audio(src: Path, dst: Path) -> Path:
    """Extract mono 16kHz PCM wav from video."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-i",
            str(src),
            "-vn",
            "-acodec",
            "pcm_s16le",
            "-ar",
            "16000",
            "-ac",
            "1",
            str(dst),
        ],
        check=True,
        capture_output=True,
    )
    return dst


def chunk_audio(
    audio_path: Path,
    output_dir: Path,
    chunk_sec: int = 180,
    overlap_sec: float = 0.0,
) -> list[tuple[Path, float, float]]:
    """
    Split audio untuk Groq free-tier:
    - 180s default -> ~5.7 MB @16k mono, aman untuk podcast 2 jam (40 chunk)
    - jika total > 3600s, tetap 180s (jangan perbesar, jaga <25 MB)
    - tumpuk log untuk 429: chunk kecil lebih mudah retry
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    total = get_duration(audio_path)
    # clamp untuk test (3s) dan prod (180s)
    chunk_sec = max(1, min(300, chunk_sec))
    # jika podcast sangat panjang (>10k detik), jangan perbesar chunk
    chunks: list[tuple[Path, float, float]] = []
    idx = 0
    start = 0.0
    while start < total:
        dur = min(chunk_sec, total - start)
        # Apply overlap for all chunks except first: start slightly earlier
        # For simplicity, we don't overlap in this v1 (avoid duplicate words)
        chunk_path = output_dir / f"chunk_{idx:03d}.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                f"{start:.3f}",
                "-t",
                f"{dur:.3f}",
                "-i",
                str(audio_path),
                "-acodec",
                "pcm_s16le",
                "-ar",
                "16000",
                "-ac",
                "1",
                str(chunk_path),
            ],
            check=True,
            capture_output=True,
        )
        chunks.append((chunk_path, start, dur))
        start += chunk_sec - overlap_sec
        idx += 1
        if idx > 1000:
            break
    return chunks
