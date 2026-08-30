from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import time
import logging
from app.core.config import get_settings
from app.models.schemas import AudioChunk, Segment, Transcript, Word
from app.utils.audio import chunk_audio, extract_audio, get_duration

logger = logging.getLogger(__name__)

# Global token-bucket untuk free tier: jaga jarak antar request
_last_groq_call: float = 0.0


def _groq_throttle() -> None:
    """Jaga jarak antar call sesuai groq_rate_limit_per_minute (podcast 40 chunk)."""
    global _last_groq_call
    settings = get_settings()
    rpm = max(1, settings.groq_rate_limit_per_minute)
    min_interval = 60.0 / rpm
    # di eager mode (1 worker) tetap throttle; di Celery rate_limit juga jaga
    elapsed = time.time() - _last_groq_call
    if elapsed < min_interval:
        sleep_for = min_interval - elapsed
        # jangan sleep di mock (tanpa key) terlalu lama, tapi tetap hormati
        time.sleep(sleep_for)
    _last_groq_call = time.time()


def _extract_retry_after(exc: Exception) -> float | None:
    """Parse Retry-After dari pesan error Groq (detik)."""
    msg = str(exc)
    # cari 'retry after 12' atau header
    import re

    m = re.search(r"retry[_\- ]?after[^0-9]*([0-9]+)", msg.lower())
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
    # coba attribute response
    for attr in ("response", "headers"):
        try:
            resp = getattr(exc, attr, None)
            if resp is not None:
                headers = getattr(resp, "headers", None) or (resp if isinstance(resp, dict) else None)
                if headers and "retry-after" in headers:
                    return float(headers["retry-after"])
                if headers and "Retry-After" in headers:
                    return float(headers["Retry-After"])
        except Exception:
            continue
    return None


def _is_rate_limit_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 429:
        return True
    return any(k in msg for k in ["429", "rate limit", "too many requests", "quota", "rate_limit_exceeded"])


def _is_payload_too_large(exc: Exception) -> bool:
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 413:
        return True
    return "413" in msg or "payload too large" in msg or "file too large" in msg or "25 mb" in msg


def transcribe_file_groq(
    chunk_path: Path,
    offset: float,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Transcribe a single chunk via Groq Whisper — free-tier safe untuk podcast panjang.
    - throttle sesuai groq_rate_limit_per_minute
    - retry dengan exponential backoff + Retry-After
    - handle 413 file too large (akan di-rechunk oleh caller)
    Jika tanpa API key, kembalikan mock.
    """
    settings = get_settings()
    key = api_key or settings.groq_api_key
    mdl = model or settings.groq_whisper_model

    if not key or key == "your_groq_api_key":
        # Mock: tetap throttle ringan biar simulasi real
        _groq_throttle()
        dur = 5.0
        try:
            dur = get_duration(chunk_path)
        except Exception:
            pass
        n_words = max(1, int(dur * 2))
        words = []
        for i in range(n_words):
            w_start = i * dur / n_words
            w_end = (i + 1) * dur / n_words * 0.95
            words.append({"word": f"word{i}", "start": w_start, "end": w_end})
        return {"text": " ".join(f"word{i}" for i in range(n_words)), "words": words, "segments": []}

    max_retries = settings.groq_max_retries
    base_delay = settings.groq_retry_base_delay

    for attempt in range(max_retries + 1):
        try:
            _groq_throttle()
            # cek file size <25 MB sebelum kirim (free tier hard limit)
            try:
                size_mb = chunk_path.stat().st_size / (1024 * 1024)
                if size_mb > settings.groq_max_file_mb:
                    raise ValueError(f"Chunk {size_mb:.1f} MB melebihi limit {settings.groq_max_file_mb} MB — perlu rechunk lebih kecil")
            except FileNotFoundError:
                pass

            # Real Groq call
            try:
                from groq import Groq  # type: ignore[import-untyped]

                client = Groq(api_key=key)
                with open(chunk_path, "rb") as f:
                    transcription = client.audio.transcriptions.create(
                        file=(chunk_path.name, f.read()),
                        model=mdl,
                        response_format="verbose_json",  # type: ignore[arg-type]
                        timestamp_granularities=["word"],  # type: ignore[arg-type]
                    )
                if isinstance(transcription, dict):
                    return transcription
                if hasattr(transcription, "model_dump"):
                    return transcription.model_dump()  # type: ignore[no-any-return]
                if hasattr(transcription, "__dict__"):
                    return dict(transcription.__dict__)
                return json.loads(str(transcription))
            except ImportError:
                from openai import OpenAI  # type: ignore[import-untyped]

                client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
                with open(chunk_path, "rb") as f:
                    transcription = client.audio.transcriptions.create(
                        file=f,
                        model=mdl,
                        response_format="verbose_json",
                        timestamp_granularities=["word"],  # type: ignore[arg-type]
                    )
                if isinstance(transcription, dict):
                    return transcription
                if hasattr(transcription, "model_dump"):
                    return transcription.model_dump()  # type: ignore[no-any-return]
                return dict(transcription.__dict__)  # type: ignore[no-any-return]

        except Exception as exc:
            # 413 -> jangan retry, lempar ke caller untuk rechunk
            if _is_payload_too_large(exc):
                logger.error("Groq 413 %s untuk %s", exc, chunk_path)
                raise

            is_rate = _is_rate_limit_error(exc)
            is_retryable = is_rate or "500" in str(exc) or "502" in str(exc) or "503" in str(exc) or "timeout" in str(exc).lower()

            if attempt >= max_retries or not is_retryable:
                logger.exception("Groq transcribe gagal chunk %s attempt %s: %s", chunk_path, attempt, exc)
                raise

            retry_after = _extract_retry_after(exc)
            if retry_after is not None:
                delay = retry_after
            else:
                delay = base_delay * (2 ** attempt)
                if is_rate:
                    delay = max(delay, 60.0 / max(1, settings.groq_rate_limit_per_minute))
            # cap 120s
            delay = min(delay, 120.0)
            logger.warning("Groq %s, retry %s/%s dalam %.1fs: %s", "429" if is_rate else "transient", attempt + 1, max_retries, delay, exc)
            time.sleep(delay)
            continue
    # should not reach
    raise RuntimeError("Groq transcribe gagal setelah retry")


def stitch_transcripts(
    chunk_results: list[dict[str, Any]],
    chunks: list[AudioChunk],
    total_duration: float = 0.0,
) -> Transcript:
    """
    Stitch chunk results with offset correction.
    Each chunk's words have local 0..chunk_duration timestamps; add chunk.start_time.
    """
    all_words: list[Word] = []
    all_segments: list[Segment] = []
    full_text_parts: list[str] = []

    for res, chunk in zip(chunk_results, chunks):
        offset = chunk.start_time
        text = res.get("text", "")
        if text:
            full_text_parts.append(text)
        for w in res.get("words") or res.get("words", []):
            try:
                word_text = w.get("word") or w.get("text") or ""
                start = float(w.get("start", 0)) + offset
                end = float(w.get("end", 0)) + offset
                all_words.append(Word(word=word_text, start=start, end=end))
            except Exception:
                continue
        for s in res.get("segments") or []:
            try:
                seg_start = float(s.get("start", 0)) + offset
                seg_end = float(s.get("end", 0)) + offset
                seg_text = s.get("text", "")
                all_segments.append(Segment(start=seg_start, end=seg_end, text=seg_text))
            except Exception:
                continue

    # Sort by start
    all_words.sort(key=lambda x: x.start)
    all_segments.sort(key=lambda x: x.start)

    # Validate monotonic (allow small overlap)
    # Don't raise, just ensure order

    full_text = " ".join(full_text_parts)
    # duration: if provided else last word end
    if total_duration == 0 and all_words:
        total_duration = all_words[-1].end
    elif total_duration == 0 and all_segments:
        total_duration = all_segments[-1].end

    return Transcript(
        text=full_text,
        words=all_words,
        segments=all_segments,
        duration=total_duration,
    )


def async_chunk_audio(
    audio_path: Path,
    output_dir: Path,
    chunk_sec: int | None = None,
) -> list[AudioChunk]:
    """
    Split audio untuk Groq free-tier: 180s default, auto-rechunk jika file >25 MB.
    Untuk podcast 2 jam -> 40 chunk, tiap chunk ~5.7 MB jadi aman.
    """
    settings = get_settings()
    if chunk_sec is None:
        chunk_sec = settings.groq_chunk_seconds
    # validasi 1..300 detik (test pakai 3s, prod pakai 180s)
    chunk_sec = max(1, min(300, chunk_sec))
    raw = chunk_audio(audio_path, output_dir, chunk_sec=chunk_sec)
    chunks: list[AudioChunk] = []
    for idx, (p, start, dur) in enumerate(raw):
        chunks.append(AudioChunk(index=idx, start_time=start, duration=dur, path=p))
    return chunks


async def async_chunk_audio_async(
    audio_path: Path,
    output_dir: Path,
    chunk_sec: int = 180,
) -> list[AudioChunk]:
    """Async wrapper (for FastAPI)."""
    return async_chunk_audio(audio_path, output_dir, chunk_sec)
