from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from app.core.config import SYSTEM_PROMPT, get_settings
from app.models.schemas import ClipPlan, Transcript


def _strip_markdown_fences(text: str) -> str:
    """Remove ```json ... ``` fences if present."""
    text = text.strip()
    # Remove leading ```json or ```
    if text.startswith("```"):
        # Find first newline after fence
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
        text = text.strip()
    return text


def _extract_json(text: str) -> str:
    """Try to extract JSON object from LLM output that may contain prose."""
    text = _strip_markdown_fences(text)
    # If already valid JSON, return
    try:
        json.loads(text)
        return text
    except Exception:
        pass
    # Find first { and last } substring
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start : end + 1]
        try:
            json.loads(candidate)
            return candidate
        except Exception:
            return candidate
    return text


class MuseClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.muse_api_key
        self.base_url = base_url or settings.muse_base_url
        self.model = model or settings.muse_model

    def analyze(self, transcript: Transcript, duration: float | None = None) -> ClipPlan:
        dur = duration or transcript.duration or 60.0
        # Build user prompt with word timestamps (truncated if too long)
        words_preview = transcript.words[:300]  # avoid token blowup
        words_text = " ".join(f"{w.word}[{w.start:.1f}-{w.end:.1f}]" for w in words_preview)
        if len(transcript.words) > 300:
            words_text += f" ... (+{len(transcript.words)-300} more words)"
        user_prompt = (
            f"Video duration: {dur:.1f}s\n"
            f"Transcript with word timestamps (word[start-end]):\n{words_text}\n\n"
            f"Full text: {transcript.text[:2000]}\n\n"
            f"Select best clips 15-45s each, identify dead_air and broll_cues. Return ONLY JSON."
        )

        # If no key, return mock for testing
        if not self.api_key or self.api_key == "your_muse_spark_key":
            return self._mock_plan(dur)

        # Real call via OpenAI-compatible API
        from openai import OpenAI  # type: ignore[import-untyped]

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)
        response = client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        raw = response.choices[0].message.content or ""
        return self._parse_and_validate(raw, dur, client, user_prompt)

    def _parse_and_validate(self, raw: str, dur: float, client: Any, user_prompt: str) -> ClipPlan:
        cleaned = _extract_json(raw)
        try:
            data = json.loads(cleaned)
            plan = ClipPlan.model_validate(data)
            return plan
        except (json.JSONDecodeError, ValidationError) as e:
            # Repair: try stripping fences again, then retry LLM once with error feedback
            error_msg = str(e)
            # Second attempt: ask LLM to fix
            try:
                repair_prompt = (
                    f"Your previous JSON was invalid: {error_msg}\n"
                    f"Fix it and return ONLY valid JSON matching schema. Previous output:\n{raw[:3000]}"
                )
                from openai import OpenAI  # type: ignore[import-untyped]

                # Reuse client if provided, else create
                if client is None:
                    settings = get_settings()
                    client = OpenAI(api_key=settings.muse_api_key, base_url=settings.muse_base_url)
                resp = client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_prompt},
                        {"role": "assistant", "content": raw},
                        {"role": "user", "content": repair_prompt},
                    ],
                    temperature=0.1,
                    response_format={"type": "json_object"},
                )
                raw2 = resp.choices[0].message.content or ""
                cleaned2 = _extract_json(raw2)
                data2 = json.loads(cleaned2)
                return ClipPlan.model_validate(data2)
            except Exception:
                # Re-raise original
                raise ValueError(f"LLM JSON validation failed: {error_msg}\nRaw: {raw[:1000]}") from e

    def _mock_plan(self, duration: float) -> ClipPlan:
        """Deterministic mock when no API key."""
        # Create 2 clips covering duration, 15-30s each
        clips = []
        if duration >= 30:
            mid = duration / 2
            clips.append(
                {"start_time": 0.0, "end_time": min(30.0, duration * 0.6), "hook_text": "You won't believe what happens next", "virality_score": 92}
            )
            if duration > 35:
                clips.append(
                    {"start_time": mid, "end_time": min(duration, mid + 25.0), "hook_text": "This changes everything you thought", "virality_score": 88}
                )
        else:
            clips.append(
                {"start_time": 0.0, "end_time": duration, "hook_text": "Watch until the end", "virality_score": 85}
            )
            # Ensure duration 15-45, if shorter than 15 pad not needed - but validator requires >=15
            # If duration <15, mock still must satisfy validator -> adjust to 15
            if duration < 15:
                clips[0]["end_time"] = 15.0
                clips[0]["start_time"] = 0.0

        return ClipPlan.model_validate(
            {
                "clips": clips,
                "dead_air": [{"start": 5.0, "end": 5.6}] if duration > 10 else [],
                "broll_cues": [
                    {"timestamp": (clips[0]["end_time"] + clips[0]["start_time"]) / 2, "keywords_en": "burning money", "fallback_en": "stressed office worker"}
                ],
            }
        )
