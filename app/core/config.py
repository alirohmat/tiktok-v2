from __future__ import annotations

import os
from pathlib import Path
from typing import Final

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Groq
    groq_api_key: str = ""
    groq_whisper_model: str = "whisper-large-v3-turbo"
    # Free-tier tuning untuk podcast panjang (40+ chunks) — akal limit
    groq_chunk_seconds: int = 300  # 5 menit -> ~9.5 MB @16k mono, hemat 42% req (19->11 untuk 57m). <25 MB aman
    groq_rate_limit_per_minute: int = 10  # free tier: 10-20 RPM aman — worker concurrency 1 + jitter
    groq_max_retries: int = 5
    groq_retry_base_delay: float = 10.0  # detik, exponential 10,20,40...
    groq_max_file_mb: int = 25
    groq_concurrent_chunks: int = 1  # free tier: sekuensial, hemat kuota
    groq_enable_cache: bool = True  # transcript JSON cache by audio sha (hemat re-clip)
    groq_enable_local_fallback: bool = False  # user forbid mock — 402/429 raise, tidak silent mock

    # Nvidia Build fallback (OpenAI-compatible) — Groq 402/429 -> Nvidia gratis 1000 kredit tanpa kartu
    nvidia_api_key: str = ""  # nvapi-xxx from https://build.nvidia.com/settings/api-keys
    nvidia_base_url: str = "https://integrate.api.nvidia.com/v1"
    nvidia_whisper_model: str = "openai/whisper-large-v3"

    # Muse Spark (OpenAI-compatible) — contributor tetap bisa, code fallback otomatis ke base untuk chat
    muse_api_key: str = ""
    muse_base_url: str = "https://api.meta.ai/v1"
    muse_model: str = "muse-spark-1.2-contributor"

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

    @property
    def resolved_music_path(self) -> Path | None:
        if not self.music_path:
            return None
        p = Path(self.music_path)
        if p.is_absolute():
            return p if p.exists() else None
        # Try project root first
        proj = Path(__file__).parent.parent.parent
        cand = proj / p
        if cand.exists():
            return cand
        # Fallback storage_path relative
        cand2 = self.storage_path / p
        if cand2.exists():
            return cand2
        # Try basename only under assets
        cand3 = proj / "assets" / Path(p).name
        if cand3.exists():
            return cand3
        return None


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


SYSTEM_PROMPT: Final[str] = """You are a viral short-form clip detector for TikTok + Shopee Affiliate. Analyze transcript with word-level timestamps.

You MUST return ONLY valid JSON, no markdown, no prose outside JSON. Exact schema:

{
  "clips": [
    {
      "start_time": 0.0,
      "end_time": 65.5,
      "hook_text": "3-second provocative hook text for kinetic typography",
      "virality_score": 95,
      "seo_keyword": "cara-atasi-insomnia",
      "caption": "cara atasi insomnia tanpa obat ini rahasia dokter jarang bongkar",
      "hashtags": ["#insomnia", "#tidurnyenyak", "#kesehatantidur", "#tipssehat"],
      "cta_text": "Save video ini & Share ke teman yang susah tidur →"
    }
  ],
  "dead_air": [{"start": 12.1, "end": 12.8}],
  "broll_cues": [{"timestamp": 15.0, "keywords_en": "burning money", "fallback_en": "stressed office worker"}],
  "host_name": "Deddy Corbuzier",
  "guest_names": ["Prabowo Subianto"],
  "engagement_comments": ["Menurut kalian ini settingan atau real? Komen jujur", "Tim insomnia jam 2 pagi absen dulu 🙋", "Pernah coba cara ini? Share hasil kalian"],
  "engagement_replies": ["Setuju, aku juga mikir gitu — tapi coba detik 12 deh", "Wah relate banget, aku dulu gini juga"],
  "niche_tag": "kesehatan",
  "niche_profit_tier": "8-15%",
  "niche_approved": true
}

Rules:
- clips: 55-90s preferred (monetization >60s Creator Rewards $40-100), min 15s max 90s. 1-5 most viral moments. Hooks, emotional peaks, controversial, surprising facts. Seamless loop: last sentence must grammatically connect to first hook sentence to boost rewatch 30-50%.
- host_name: channel owner / interviewer — kamu akan diberi host_name di user prompt (dari ytdlp uploader). JANGAN pakai host untuk hook. guest_names: HANYA orang yang diundang/diintroduksi (trigger: "kedatangan", "bersama", "tamu", "menemui", "spesial", "datang"). Abaikan "saya". Hook/caption harus pakai guest_names jika ada: contoh "Prabowo bongkar ... — 3 detik ini gila" bukan "host bongkar...". Jika guest_names kosong, hook generic.
- dead_air: leave empty [] — cut done deterministically by auto-editor PASS 1+2, not LLM.
- broll_cues: 1-2 per clip, English 2-4 words cinematic. NEVER timestamp in 0-3s (hook protection).
- hook_text: 5-12 words punchy for top-third y=80 0-3s. Must verbally contain seo_keyword words.
- seo_keyword: 2-4 words hyphenated lowercase, e.g. cara-atasi-insomnia. Must appear in first 50 chars of caption verbatim (SEO scan). Also rendered on-screen 0.2-2.7s for OCR.
- caption: Indonesian, hook + value, keyword in first 50 chars, 120-250 chars total.
- hashtags: 3-5 mix macro+micro, no #fyp, must include keyword-derived tags. With # prefix.
- cta_text: Share/Save CTA imperative, e.g. "Save video ini untuk praktek nanti & Share ke teman yang butuh →" rendered bottom last 5s with arrow to left-bottom cart area.
- engagement_comments: 3 polarizing prompts for 30-60min post-publish boost, Indonesian, open-ended to trigger replies.
- engagement_replies: 2 quick reply templates for creator to pin.
- niche_tag: one of kesehatan/lifestyle/rumah/teknologi/edukasi/affiliate. niche_profit_tier per PDF: kesehatan 8-15%, lifestyle/rumah 5-15%, teknologi 5-10%, edukasi 10-15%. niche_approved true only if profit tier >=8% or high demand.
- virality_score 0-100.
- All timestamps float seconds relative to full duration.
- Return ONLY JSON.

Few-shot:
INPUT Host=Deddy Corbuzier transcript="Hari ini saya kedatangan Prabowo Subianto di studio membahas rahasia tidur"
OUTPUT {"clips":[{"start_time":0.0,"end_time":65.0,"hook_text":"Prabowo bongkar rahasia tidur 73% orang gagal","virality_score":94,"seo_keyword":"prabowo-bongkar-tidur","caption":"prabowo bongkar tidur rahasia dokter ini bikin 73% orang gagal tidur nyenyak — tonton sampai akhir","hashtags":["#prabowo","#tidurnyenyak","#kesehatan"],"cta_text":"Save video ini & Share ke teman susah tidur →"}],"dead_air":[],"broll_cues":[{"timestamp":15.0,"keywords_en":"presidential office","fallback_en":"studio lights"}],"host_name":"Deddy Corbuzier","guest_names":["Prabowo Subianto"],"engagement_comments":["Menurut kalian Prabowo jujur atau pencitraan?","Tim begadang jam 2 pagi absen 🙋","Pernah coba teknik ini?"],"engagement_replies":["Setuju, detik 12 paling jujur","Relate, aku juga gagal dulu"],"niche_tag":"kesehatan","niche_profit_tier":"8-15%","niche_approved":true}
If guest_names empty, hook generic: "Stop scroll — cara atasi insomnia ini gila".
"""
