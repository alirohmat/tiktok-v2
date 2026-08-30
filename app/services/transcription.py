from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.config import get_settings
from app.models.schemas import AudioChunk, Segment, Transcript, Word
from app.utils.audio import chunk_audio, extract_audio, get_duration


def transcribe_file_groq(
    chunk_path: Path,
    offset: float,
    api_key: str | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    """
    Transcribe a single chunk via Groq Whisper.
    Returns raw JSON dict with words/segments (chunk-relative timestamps).
    If no API key, returns mock structure for testing.
    """
    settings = get_settings()
    key = api_key or settings.groq_api_key
    mdl = model or settings.groq_whisper_model

    if not key or key == "your_groq_api_key":
        # Mock: return fake words for testing without API key
        dur = 5.0
        try:
            dur = get_duration(chunk_path)
        except Exception:
            pass
        # Generate dummy words splitting duration
        n_words = max(1, int(dur * 2))
        words = []
        for i in range(n_words):
            w_start = i * dur / n_words
            w_end = (i + 1) * dur / n_words * 0.95
            words.append({"word": f"word{i}", "start": w_start, "end": w_end})
        return {"text": " ".join(f"word{i}" for i in range(n_words)), "words": words, "segments": []}

    # Real Groq call via openai SDK pointing to Groq, or groq SDK
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
        # groq returns object with .text, .words etc or dict
        if isinstance(transcription, dict):
            return transcription
        # Convert object to dict
        # Use model_dump if pydantic, else dict()
        if hasattr(transcription, "model_dump"):
            return transcription.model_dump()  # type: ignore[no-any-return]
        if hasattr(transcription, "__dict__"):
            return dict(transcription.__dict__)
        # Fallback: try json
        return json.loads(str(transcription))
    except ImportError:
        # Fallback to openai SDK
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
    chunk_sec: int = 180,
) -> list[AudioChunk]:
    """
    Split audio into 3-minute chunks for Groq (25 MB limit) and return AudioChunk list.
    This is the spec-required async_chunk_audio function (sync version for direct use).
    Celery tasks will handle async processing of each chunk.
    """
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
