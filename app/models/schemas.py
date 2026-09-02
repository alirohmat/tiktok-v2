from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


# --- Transcription models ---


class Word(BaseModel):
    word: str
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @field_validator("end")
    @classmethod
    def end_after_start(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        start = info.data.get("start")
        if start is not None and v < start:
            raise ValueError("end must be >= start")
        return v


class Segment(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = ""


class Transcript(BaseModel):
    text: str = ""
    words: list[Word] = Field(default_factory=list)
    segments: list[Segment] = Field(default_factory=list)
    duration: float = Field(ge=0, default=0.0)


class AudioChunk(BaseModel):
    index: int = Field(ge=0)
    start_time: float = Field(ge=0)
    duration: float = Field(ge=0)
    path: Path


# --- LLM models (Muse Spark) ---


class Clip(BaseModel):
    start_time: float = Field(ge=0)
    end_time: float = Field(ge=0)
    hook_text: str = Field(min_length=1, max_length=200)
    virality_score: int = Field(ge=0, le=100)
    # SEO + CTA (patch 1-2)
    seo_keyword: str = Field(default="", max_length=80, description="2-4 word hyphenated keyword, e.g. cara-atasi-insomnia")
    caption: str = Field(default="", max_length=500, description="keyword in first 50 chars")
    hashtags: list[str] = Field(default_factory=list, description="3-5 hashtags")
    cta_text: str = Field(default="", max_length=200, description="Save/Share CTA")

    @field_validator("end_time")
    @classmethod
    def check_duration(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        start = info.data.get("start_time")
        if start is not None:
            dur = v - start
            if dur < 15 or dur > 90:
                raise ValueError(f"clip duration must be 15-90s, got {dur:.2f}")
            if v <= start:
                raise ValueError("end_time must be > start_time")
        return v

    @field_validator("hashtags")
    @classmethod
    def check_hashtags(cls, v: list[str]) -> list[str]:  # type: ignore[no-untyped-def]
        # normalize: ensure # prefix, max 5
        out = []
        for h in v[:5]:
            h = h.strip()
            if not h:
                continue
            if not h.startswith("#"):
                h = "#" + h
            out.append(h)
        return out


class DeadAir(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(ge=0)

    @field_validator("end")
    @classmethod
    def check_end(cls, v: float, info) -> float:  # type: ignore[no-untyped-def]
        s = info.data.get("start")
        if s is not None and v <= s:
            raise ValueError("dead_air end must be > start")
        if s is not None and (v - s) < 0.2:
            raise ValueError("dead_air segment too short (<0.2s)")
        return v


class BrollCue(BaseModel):
    timestamp: float = Field(ge=0)
    keywords_en: str = Field(min_length=1)
    fallback_en: str = Field(min_length=1)


class ClipPlan(BaseModel):
    clips: list[Clip] = Field(min_length=1)
    dead_air: list[DeadAir] = Field(default_factory=list)
    broll_cues: list[BrollCue] = Field(default_factory=list)
    # Host vs guest (NLP role-aware) — host jangan jadi hook tamu
    host_name: str = Field(default="", description="channel owner / interviewer, jangan pakai untuk hook")
    guest_names: list[str] = Field(default_factory=list, description="tamu yang diundang/diintroduksi, dipakai untuk hook trigger")
    # Engagement + niche (patch 3-4)
    engagement_comments: list[str] = Field(default_factory=list, description="3 polarizing prompts e.g. Menurut kalian settingan?")
    engagement_replies: list[str] = Field(default_factory=list, description="reply templates for 30-60min window")
    niche_tag: str = Field(default="", description="mikro-niche, e.g. kesehatan")
    niche_profit_tier: str = Field(default="", description="8-15% / 5-15% etc")
    niche_approved: bool = Field(default=True, description="LLM filter profit 8-15%")
    niche_score: int = Field(default=70, ge=0, le=100, description="advisory 0-100 profit+demand")
    niche_advisory: str = Field(default="", description="advisory note jika low-profit")

    @model_validator(mode="after")
    def check_timestamps(self) -> ClipPlan:
        # Ensure broll cues within max clip end
        if self.clips:
            max_end = max(c.end_time for c in self.clips)
            for cue in self.broll_cues:
                if cue.timestamp > max_end + 1:
                    # allow small overflow but warn - don't fail
                    pass
        # dead_air should not overlap clips? Allow but validate no overlap within dead_air itself
        sorted_da = sorted(self.dead_air, key=lambda x: x.start)
        for i in range(1, len(sorted_da)):
            if sorted_da[i].start < sorted_da[i - 1].end:
                raise ValueError("dead_air segments overlap")
        return self


# --- Coverr models ---


class CoverrVideo(BaseModel):
    video_id: str
    preview_url: str  # mp4_preview
    download_url: str = ""  # mp4 or mp4_download
    is_vertical: bool = False
    width: int = 0
    height: int = 0
    title: str = ""


# --- Job models ---


class JobStatus(BaseModel):
    job_id: str
    status: str  # PENDING, STARTED, PROGRESS, SUCCESS, FAILURE
    phase: str = ""
    progress: float = 0.0
    result: list[str] | None = None
    error: str | None = None
    logs: list[str] | None = None
    started_at: float | None = None
    finished_at: float | None = None
