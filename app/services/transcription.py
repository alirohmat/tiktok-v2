from __future__ import annotations

import hashlib
import json
import random
import threading
import time
import logging
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.models.schemas import AudioChunk, Segment, Transcript, Word
from app.utils.audio import chunk_audio, extract_audio, get_duration

logger = logging.getLogger(__name__)

_last_groq_call: float = 0.0
_groq_lock = threading.Lock()


def _groq_throttle() -> None:
    global _last_groq_call
    settings = get_settings()
    rpm = max(1, settings.groq_rate_limit_per_minute)
    min_interval = 60.0 / rpm
    jitter = random.uniform(0, 0.8)
    min_interval += jitter
    sleep_for = 0.0
    with _groq_lock:
        elapsed = time.time() - _last_groq_call
        if elapsed < min_interval:
            sleep_for = min_interval - elapsed
        else:
            _last_groq_call = time.time()
            return
    if sleep_for > 0:
        time.sleep(sleep_for)
    with _groq_lock:
        _last_groq_call = time.time()


def _file_sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()[:16]


def _cache_path(chunk_path: Path, model: str) -> Path | None:
    try:
        settings = get_settings()
        if not getattr(settings, "groq_enable_cache", True):
            return None
        sha = _file_sha(chunk_path)
        p = settings.storage_path / "cache" / "transcripts" / f"{sha}_{model}.json"
        return p
    except Exception:
        return None


def _load_cache(p: Path) -> dict[str, Any] | None:
    try:
        if p.exists():
            return json.loads(p.read_text())
    except Exception:
        pass
    return None


def _save_cache(p: Path, data: dict[str, Any]) -> None:
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
    except Exception:
        pass


def _extract_retry_after(exc: Exception) -> float | None:
    msg = str(exc)
    import re
    m = re.search(r"retry[_\- ]?after[^0-9]*([0-9]+)", msg.lower())
    if m:
        try:
            return float(m.group(1))
        except Exception:
            pass
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


def _is_billing_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 402:
        return True
    return any(k in msg for k in ["402", "billing_not_configured", "billing_error", "billing verification"])


def _is_payload_too_large(exc: Exception) -> bool:
    msg = str(exc).lower()
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 413:
        return True
    return "413" in msg or "payload too large" in msg or "file too large" in msg or "25 mb" in msg


def _mock_result(chunk_path: Path) -> dict[str, Any]:
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


def transcribe_file_groq(
    chunk_path: Path,
    offset: float,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    settings = get_settings()
    key = api_key or settings.groq_api_key
    mdl = model or settings.groq_whisper_model

    if not key or key == "your_groq_api_key":
        _groq_throttle()
        return _mock_result(chunk_path)

    # cache hit?
    cp = _cache_path(chunk_path, mdl)
    if cp is not None:
        cached = _load_cache(cp)
        if cached is not None:
            logger.info("Groq cache hit %s -> %s", chunk_path.name, cp.name)
            return cached

    max_retries = settings.groq_max_retries
    base_delay = settings.groq_retry_base_delay

    for attempt in range(max_retries + 1):
        try:
            _groq_throttle()
            try:
                size_mb = chunk_path.stat().st_size / (1024 * 1024)
                if size_mb > settings.groq_max_file_mb:
                    raise ValueError(f"Chunk {size_mb:.1f} MB melebihi limit {settings.groq_max_file_mb} MB — perlu rechunk lebih kecil")
            except FileNotFoundError:
                pass

            try:
                from groq import Groq  # type: ignore[import-untyped]
                client = Groq(api_key=key)
                with open(chunk_path, "rb") as f:
                    file_tuple = (chunk_path.name, f.read())
                    try:
                        transcription = client.audio.transcriptions.create(
                            file=file_tuple,
                            model=mdl,
                            response_format="verbose_json",  # type: ignore[arg-type]
                            timestamp_granularities=["word"],  # type: ignore[arg-type]
                        )
                    except TypeError as te:
                        if "timestamp_granularities" in str(te):
                            transcription = client.audio.transcriptions.create(
                                file=file_tuple,
                                model=mdl,
                                response_format="verbose_json",  # type: ignore[arg-type]
                            )
                        else:
                            raise
                if isinstance(transcription, dict):
                    res = transcription
                elif hasattr(transcription, "model_dump"):
                    res = transcription.model_dump()  # type: ignore[no-any-return]
                elif hasattr(transcription, "__dict__"):
                    res = dict(transcription.__dict__)
                else:
                    res = json.loads(str(transcription))
                if cp is not None:
                    _save_cache(cp, res)
                return res
            except ImportError:
                from openai import OpenAI  # type: ignore[import-untyped]
                client = OpenAI(api_key=key, base_url="https://api.groq.com/openai/v1")
                with open(chunk_path, "rb") as f:
                    file_tuple = (chunk_path.name, f.read())
                    try:
                        transcription = client.audio.transcriptions.create(
                            file=file_tuple,
                            model=mdl,
                            response_format="verbose_json",
                            timestamp_granularities=["word"],  # type: ignore[arg-type]
                        )
                    except TypeError as te:
                        if "timestamp_granularities" in str(te):
                            transcription = client.audio.transcriptions.create(
                                file=file_tuple,
                                model=mdl,
                                response_format="verbose_json",
                            )
                        else:
                            raise
                if isinstance(transcription, dict):
                    res = transcription
                elif hasattr(transcription, "model_dump"):
                    res = transcription.model_dump()  # type: ignore[no-any-return]
                else:
                    res = dict(transcription.__dict__)  # type: ignore[no-any-return]
                if cp is not None:
                    _save_cache(cp, res)
                return res

        except Exception as exc:
            if _is_payload_too_large(exc):
                logger.error("Groq 413 %s untuk %s", exc, chunk_path)
                raise
            if _is_billing_error(exc):
                if getattr(settings, "groq_enable_local_fallback", True):
                    logger.warning("Groq 402 billing %s -> fallback mock %s (pipeline lanjut, LLM tetap clip 55-90s)", exc, chunk_path.name)
                    mock = _mock_result(chunk_path)
                    if cp is not None:
                        _save_cache(cp, mock)
                    return mock
                raise
            is_rate = _is_rate_limit_error(exc)
            is_retryable = is_rate or "500" in str(exc) or "502" in str(exc) or "503" in str(exc) or "timeout" in str(exc).lower()
            if attempt >= max_retries or not is_retryable:
                if is_rate and getattr(settings, "groq_enable_local_fallback", True):
                    logger.warning("Groq 429 max retry %s -> fallback mock %s", exc, chunk_path.name)
                    mock = _mock_result(chunk_path)
                    if cp is not None:
                        _save_cache(cp, mock)
                    return mock
                logger.exception("Groq transcribe gagal chunk %s attempt %s: %s", chunk_path, attempt, exc)
                raise
            retry_after = _extract_retry_after(exc)
            if retry_after is not None:
                delay = retry_after
            else:
                delay = base_delay * (2 ** attempt)
                if is_rate:
                    delay = max(delay, 60.0 / max(1, settings.groq_rate_limit_per_minute))
            delay = min(delay, 120.0) + random.uniform(0, 1.5)
            logger.warning("Groq %s, retry %s/%s dalam %.1fs: %s", "429" if is_rate else "transient", attempt + 1, max_retries, delay, exc)
            time.sleep(delay)
            continue
    raise RuntimeError("Groq transcribe gagal setelah retry")


def stitch_transcripts(
    chunk_results: list[dict[str, Any]],
    chunks: list[AudioChunk],
    total_duration: float = 0.0,
) -> Transcript:
    all_words: list[Word] = []
    all_segments: list[Segment] = []
    full_text_parts: list[str] = []
    for res, chunk in zip(chunk_results, chunks):
        offset = chunk.start_time
        text = res.get("text", "")
        if text:
            full_text_parts.append(text)
        for w in res.get("words") or []:
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
    all_words.sort(key=lambda x: x.start)
    deduped: list[Word] = []
    for w in all_words:
        if deduped and abs(w.start - deduped[-1].start) < 0.01 and w.word == deduped[-1].word:
            continue
        deduped.append(w)
    all_words = deduped
    all_segments.sort(key=lambda x: x.start)
    full_text = " ".join(full_text_parts)
    if total_duration == 0 and all_words:
        total_duration = all_words[-1].end
    elif total_duration == 0 and all_segments:
        total_duration = all_segments[-1].end
    return Transcript(text=full_text, words=all_words, segments=all_segments, duration=total_duration)


def async_chunk_audio(
    audio_path: Path,
    output_dir: Path,
    chunk_sec: int | None = None,
) -> list[AudioChunk]:
    settings = get_settings()
    if chunk_sec is None:
        chunk_sec = settings.groq_chunk_seconds
    chunk_sec = max(1, min(300, chunk_sec))
    raw = chunk_audio(audio_path, output_dir, chunk_sec=chunk_sec)
    chunks: list[AudioChunk] = []
    for idx, (p, start, dur) in enumerate(raw):
        chunks.append(AudioChunk(index=idx, start_time=start, duration=dur, path=p))
    return chunks


async def async_chunk_audio_async(
    audio_path: Path,
    output_dir: Path,
    chunk_sec: int = 300,
) -> list[AudioChunk]:
    return async_chunk_audio(audio_path, output_dir, chunk_sec)
