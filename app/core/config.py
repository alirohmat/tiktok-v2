from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Groq
    groq_api_key: str = ""
    groq_whisper_model: str = "whisper-large-v3"

    # Muse Spark (OpenAI-compatible)
    muse_api_key: str = ""
    muse_base_url: str = "https://api.muse-spark.example/v1"
    muse_model: str = "meta-llama/Meta-Llama-3-70B-Instruct"

    # Coverr
    coverr_api_key: str = ""
    coverr_base_url: str = "https://api.coverr.co"

    # Redis / Celery
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"

    # App
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    storage_dir: str = "storage"
    music_path: str = "assets/trending.mp3"

    @property
    def storage_path(self) -> Path:
        p = Path(self.storage_dir)
        if not p.is_absolute():
            # Resolve relative to project root (one level above app/)
            p = Path(__file__).parent.parent.parent / p
        return p


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


SYSTEM_PROMPT: Final[str] = """You are a viral short-form clip detector for TikTok and Shopee Affiliate content. Analyze the provided transcript with word-level timestamps.

You MUST return ONLY a valid JSON object, no markdown, no explanation, no prose outside JSON. The JSON must have exactly this schema:

{
  "clips": [
    {
      "start_time": 0.0,
      "end_time": 35.5,
      "hook_text": "3-second provocative hook text for kinetic typography",
      "virality_score": 95
    }
  ],
  "dead_air": [{"start": 12.1, "end": 12.8}, {"start": 25.0, "end": 25.5}],
  "broll_cues": [
    {"timestamp": 15.0, "keywords_en": "burning money", "fallback_en": "stressed office worker"}
  ]
}

Rules:
- clips: Each clip must be 15 to 45 seconds long (end_time - start_time between 15 and 45). Select 1-5 most viral moments. Prioritize hooks, emotional peaks, controversial statements, surprising facts.
- dead_air: Identify silent or filler segments (um, uh, long pauses) to jump-cut. Each segment 0.3s minimum.
- broll_cues: For each clip, suggest 1-2 B-Roll insertion points with primary keywords_en and fallback_en (always English, 2-4 words, cinematic).
- hook_text: 5-12 words, punchy, provocative, suitable for kinetic typography in first 3 seconds.
- virality_score: integer 0-100, higher means more viral potential.
- All timestamps in seconds (float), relative to full video duration.
- Return ONLY JSON.
"""
