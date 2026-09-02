from __future__ import annotations

import json
import re
import time
from typing import Any

from pydantic import ValidationError

from app.core.config import SYSTEM_PROMPT, get_settings
from app.models.schemas import ClipPlan, Transcript


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_nl = text.find("\n")
        if first_nl != -1:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()
    return text


def _extract_json(text: str) -> str:
    text = _strip_markdown_fences(text)
    try:
        json.loads(text)
        return text
    except Exception:
        pass
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


def _slugify(s: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")
    return s[:32] or "viral-hook"


def _enforce_seo(plan: ClipPlan) -> ClipPlan:
    """Post-validate: seo_keyword hyphen + caption 50char contains keyword."""
    for c in plan.clips:
        if not c.seo_keyword or "-" not in c.seo_keyword:
            c.seo_keyword = _slugify(c.hook_text or c.caption or "viral-hook")
            if "-" not in c.seo_keyword:
                c.seo_keyword = c.seo_keyword + "-viral"
        # caption first 50 chars must contain keyword words
        kw_words = c.seo_keyword.replace("-", " ").lower().split()
        cap_low = (c.caption or "").lower()
        if c.caption and not any(w in cap_low[:60] for w in kw_words):
            # prepend keyword
            prefix = c.seo_keyword.replace("-", " ")
            c.caption = f"{prefix} {c.caption}".strip()[:500]
        if not c.caption:
            c.caption = c.seo_keyword.replace("-", " ") + " — tonton sampai akhir"
        if not c.hashtags:
            c.hashtags = [f"#{c.seo_keyword.split('-')[0]}", "#tipssehat", "#viral"]  # type: ignore[assignment]
        if not c.cta_text:
            c.cta_text = "Save video ini & Share ke teman →"
    return plan


def _apply_niche_advisory(plan: "ClipPlan") -> "ClipPlan":
    """Advisory gate: tidak block, hanya tag skor. 90 high 8-15%, 70 mid, 50 low."""
    tier = (plan.niche_profit_tier or "").strip()
    tag = (plan.niche_tag or "").lower()
    # tier parsing
    score = 70
    advisory = ""
    if "8-15" in tier or "10-15" in tier:
        score = 90
    elif "5-15" in tier:
        score = 70
        if tag in ("teknologi",):
            score = 60
    elif "5-10" in tier:
        score = 50
    else:
        score = 70 if plan.niche_approved else 45
    if not plan.niche_approved or score < 60:
        advisory = f"Advisory: {tag or 'umum'} {tier or '?'} — viral boleh tapi komisi tipis, tetap dirender"
    elif score >= 80:
        advisory = f"High-profit {tag} {tier} ✓"
    else:
        advisory = f"Mid-profit {tag} {tier}"
    try:
        plan.niche_score = int(score)
        plan.niche_advisory = advisory
    except Exception:
        pass
    return plan


class MuseClient:
    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str | None = None) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.muse_api_key
        self.base_url = base_url or settings.muse_base_url
        self.model = model or settings.muse_model

    def analyze(self, transcript: Transcript, duration: float | None = None, host_name: str | None = None, yt_meta: dict | None = None) -> ClipPlan:
        dur = duration or transcript.duration or 60.0
        # TPM fix: gpt-oss-120b limit 8000 on_demand incl reasoning -> keep <6500 to avoid 413 (was 600w+4000c=8821)
        max_w = 320 if len(transcript.words) > 500 else 400
        words_preview = transcript.words[:max_w]
        words_text = " ".join(f"{w.word}[{w.start:.1f}-{w.end:.1f}]" for w in words_preview)
        if len(transcript.words) > max_w:
            words_text += f" ... (+{len(transcript.words)-max_w} more words)"
        host = (host_name or "").strip()
        yt = yt_meta or {}
        yt_title = (yt.get("title") or yt.get("fulltitle") or "").strip()[:120]
        yt_channel = (yt.get("uploader") or yt.get("channel") or yt.get("uploader_id") or "").strip()[:80]
        yt_desc = (yt.get("description") or "").strip()[:400]
        # NLP context only — hook must be NEW from transcript, not copy title verbatim (curiosity gap 5-12w)
        yt_block = ""
        if yt_title or yt_channel:
            yt_block = f"YT metadata (konteks NLP, JANGAN copy verbatim jadi hook — buat hook baru dari transcript):\nTitle: {yt_title or '-'}\nChannel: {yt_channel or '-'}\nDesc: {yt_desc[:300] if yt_desc else '-'}\n"
        user_prompt = (
            f"{yt_block}"
            f"Host channel: {host or '-'} (jangan pakai host untuk hook)\n"
            f"Video duration: {dur:.1f}s\n"
            f"Transcript with word timestamps (word[start-end]):\n{words_text}\n\n"
            f"Full text: {transcript.text[:2200]}\n\n"
            f"Select best clips 55-90s each (min15 max90), guest_names=only invited people (trigger: kedatangan/bersama/tamu/menemui/spesial), hook pakai guest_names jika ada, seo_keyword hyphenated, caption keyword first 50 chars, 3-5 hashtags, CTA Share/Save, engagement 3+2, niche profit tier, host_name/guest_names. Return ONLY JSON."
        )
        if not self.api_key or self.api_key == "your_muse_spark_key":
            plan = self._mock_plan(dur, host_name=host)
            return _apply_niche_advisory(_enforce_seo(plan))
        from openai import OpenAI  # type: ignore[import-untyped]

        client = OpenAI(api_key=self.api_key, base_url=self.base_url)

        def _try_create(m):
            return client.chat.completions.create(
                model=m,
                messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}],
                temperature=0.4,
                response_format={"type": "json_object"},
            )

        # retry 3x 5s + 413 TPM truncate retry (56m transcript -> 413 on gpt-oss-120b 8000 TPM)
        response = None
        last_err = None
        for attempt in range(3):
            try:
                response = _try_create(self.model)
                break
            except Exception as e:
                last_err = e
                msg = str(e).lower()
                if "413" in msg or "rate_limit_exceeded" in msg or "tokens per minute" in msg or "tpm" in msg:
                    # truncate further and retry immediately (not mock — error biar kelihatan if still fail)
                    if attempt == 0:
                        # cut words_text in half for retry
                        words_text = words_text[:1200] + " ... [truncated for TPM]"
                        import time as _t
                        _t.sleep(1)
                        continue
                is_retryable = any(k in msg for k in ("timeout", "timed out", "429", "500", "502", "503", "504", "connection", "http"))
                is_404 = "404" in str(e) or "model_not_found" in msg or "notfounderror" in type(e).__name__.lower()
                if is_404:
                    # model fallback, not retry same
                    import logging

                    alt = self.model
                    for suffix in ("-contributor-free", "-contributor", "-free"):
                        if alt.endswith(suffix):
                            alt = alt[: -len(suffix)]
                            break
                    if alt != self.model:
                        try:
                            logging.getLogger(__name__).warning("Muse model %r 404, coba fallback ke %r", self.model, alt)
                            response = _try_create(alt)
                            break
                        except Exception as e2:
                            logging.getLogger(__name__).warning("Fallback %r juga 404 (%s), fallback mock", alt, e2)
                            return _apply_niche_advisory(_enforce_seo(self._mock_plan(dur, host_name=host)))
                    else:
                        logging.getLogger(__name__).warning("Muse model %r 404 di %s, fallback mock", self.model, self.base_url)
                        return _apply_niche_advisory(_enforce_seo(self._mock_plan(dur, host_name=host)))
                if "401" in msg or "403" in msg:
                    import logging

                    logging.getLogger(__name__).warning("Muse API auth %s fallback mock: %s", type(e).__name__, e)
                    return _apply_niche_advisory(_enforce_seo(self._mock_plan(dur, host_name=host)))
                if is_retryable and attempt < 2:
                    time.sleep(5)
                    continue
                if attempt >= 2:
                    # last attempt failed
                    if is_retryable:
                        import logging

                        logging.getLogger(__name__).warning("LLM retry 3x failed (%s), fallback mock", e)
                        return _apply_niche_advisory(_enforce_seo(self._mock_plan(dur, host_name=host)))
                    raise
                # non-retryable
                raise
        if response is None:
            if last_err:
                raise last_err
            return _apply_niche_advisory(_enforce_seo(self._mock_plan(dur, host_name=host)))
        raw = response.choices[0].message.content or ""
        try:
            plan = self._parse_and_validate(raw, dur, client, user_prompt)
            if host and plan.guest_names:
                plan.guest_names = [g for g in plan.guest_names if g.strip().lower() != host.lower()]
                if plan.host_name and plan.host_name.lower() == host.lower():
                    pass
                else:
                    plan.host_name = host or plan.host_name
            elif host:
                plan.host_name = host
            return _apply_niche_advisory(_enforce_seo(plan))
        except Exception:
            raise

    def _parse_and_validate(self, raw: str, dur: float, client: Any, user_prompt: str) -> ClipPlan:
        cleaned = _extract_json(raw)
        try:
            data = json.loads(cleaned)
            plan = ClipPlan.model_validate(data)
            return plan
        except (json.JSONDecodeError, ValidationError) as e:
            error_msg = str(e)
            try:
                repair_prompt = f"Your previous JSON was invalid: {error_msg}\nFix it and return ONLY valid JSON matching schema. Previous output:\n{raw[:3000]}"
                from openai import OpenAI  # type: ignore[import-untyped]

                if client is None:
                    settings = get_settings()
                    client = OpenAI(api_key=settings.muse_api_key, base_url=settings.muse_base_url)
                try:
                    resp = client.chat.completions.create(
                        model=self.model,
                        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_prompt}, {"role": "assistant", "content": raw}, {"role": "user", "content": repair_prompt}],
                        temperature=0.1,
                        response_format={"type": "json_object"},
                    )
                except Exception as e2:
                    if "404" in str(e2) or "model_not_found" in str(e2):
                        import logging

                        logging.getLogger(__name__).warning("Repair 404, fallback mock: %s", e2)
                        return self._mock_plan(dur)
                    raise
                raw2 = resp.choices[0].message.content or ""
                cleaned2 = _extract_json(raw2)
                data2 = json.loads(cleaned2)
                return ClipPlan.model_validate(data2)
            except Exception:
                raise ValueError(f"LLM JSON validation failed: {error_msg}\nRaw: {raw[:1000]}") from e

    def _mock_plan(self, duration: float, host_name: str | None = None) -> ClipPlan:
        _host = (host_name or "").strip()
        clips: list[dict] = []
        if duration >= 60:
            mid = duration / 2
            kw1 = "cara-atasi-insomnia"
            clips.append({"start_time": 0.0, "end_time": min(70.0, duration * 0.6), "hook_text": "Stop scroll — cara atasi insomnia ini gila", "virality_score": 92, "seo_keyword": kw1, "caption": f"{kw1.replace('-',' ')} tanpa obat ini rahasia dokter jarang bongkar — tonton sampai akhir", "hashtags": ["#insomnia", "#tidurnyenyak", "#kesehatantidur", "#tipssehat"], "cta_text": "Save video ini & Share ke teman yang susah tidur →"})
            if duration > 80:
                kw2 = "tips-tidur-nyenyak"
                clips.append({"start_time": mid, "end_time": min(duration, mid + 60.0), "hook_text": "Ini ubah tidurmu malam ini juga", "virality_score": 88, "seo_keyword": kw2, "caption": f"{kw2.replace('-',' ')} 3 langkah simpel buktikan malam ini", "hashtags": ["#tipssehat", "#tidurnyenyak", "#kesehatan"], "cta_text": "Share ke teman begadang & Save buat nanti →"})
        elif duration >= 30:
            kw = "tips-tidur-nyenyak"
            clips.append({"start_time": 0.0, "end_time": min(60.0, duration), "hook_text": "Cara tidur nyenyak tanpa obat", "virality_score": 88, "seo_keyword": kw, "caption": f"{kw.replace('-',' ')} ini wajib coba malam ini", "hashtags": ["#tidurnyenyak", "#kesehatan", "#tipssehat"], "cta_text": "Save & Share ke yang butuh →"})
        else:
            kw = _slugify("Watch until the end")
            clips.append({"start_time": 0.0, "end_time": duration, "hook_text": "Watch until the end", "virality_score": 85, "seo_keyword": kw, "caption": f"{kw.replace('-',' ')} tonton sampai habis", "hashtags": ["#viral", "#fyp", "#tips"], "cta_text": "Save & Share →"})
            if duration < 15:
                clips[0]["end_time"] = 15.0
                clips[0]["start_time"] = 0.0
        for c in clips:
            if not c.get("seo_keyword"):
                c["seo_keyword"] = _slugify(c["hook_text"])
            if not c.get("caption"):
                c["caption"] = c["seo_keyword"].replace("-", " ") + " — tonton sampai akhir"
            if not c.get("hashtags"):
                c["hashtags"] = [f"#{c['seo_keyword'].split('-')[0]}", "#tipssehat", "#viral"]
            if not c.get("cta_text"):
                c["cta_text"] = "Save video ini & Share ke teman →"
        return ClipPlan.model_validate({"clips": clips, "dead_air": [], "broll_cues": [{"timestamp": (clips[0]["end_time"] + clips[0]["start_time"]) / 2, "keywords_en": "burning money", "fallback_en": "stressed office worker"}], "host_name": _host, "guest_names": [], "engagement_comments": ["Menurut kalian ini settingan atau real? Komen jujur", "Tim insomnia jam 2 pagi absen dulu 🙋", "Pernah coba cara ini? Share hasil kalian"], "engagement_replies": ["Setuju, aku juga mikir gitu — coba detik 12", "Relate banget, aku dulu gini juga"], "niche_tag": "kesehatan", "niche_profit_tier": "8-15%", "niche_approved": True})
