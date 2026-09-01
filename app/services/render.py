from __future__ import annotations

import json
import re

from pathlib import Path

from app.models.schemas import Clip, ClipPlan, Transcript
from app.utils.autoframe import detect_crop_window
from app.utils.ffmpeg_builder import FFmpegBuilder


def _slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:40] or "viral-clip"


class RenderEngine:
    def __init__(
        self,
        music_path: Path | None = None,
        enable_ultrasonic: bool = True,
        enable_zoompan: bool = True,
        enable_noise: bool = True,
    ) -> None:
        self.music_path = music_path
        self.enable_ultrasonic = enable_ultrasonic
        self.enable_zoompan = enable_zoompan
        self.enable_noise = enable_noise

    def render_clip(
        self,
        src: Path,
        clip: Clip,
        transcript: Transcript,
        clip_plan: ClipPlan,
        broll_paths: list[Path],
        output: Path,
        build_only: bool = False,
    ) -> Path | list[str]:
        """
        Render a single clip with DNA alterations.
        If build_only, returns command list without executing (for tests).
        """
        output.parent.mkdir(parents=True, exist_ok=True)

        # Detect crop window for auto-framing (16:9 -> 9:16)
        crop_window = None
        try:
            crop_window = detect_crop_window(src)
        except Exception:
            crop_window = None

        # Filter broll cues to those within this clip
        relevant_cues = [
            c for c in clip_plan.broll_cues if clip.start_time <= c.timestamp <= clip.end_time
        ]
        # Match broll_paths to relevant_cues (assume ordered)
        if len(broll_paths) > len(relevant_cues):
            broll_paths = broll_paths[: len(relevant_cues)]
            relevant_cues = relevant_cues[: len(broll_paths)]
        elif len(broll_paths) < len(relevant_cues):
            relevant_cues = relevant_cues[: len(broll_paths)]

        builder = FFmpegBuilder(
            src=src,
            output=output,
            transcript=transcript,
            music_path=self.music_path if self.music_path and self.music_path.exists() else None,
            enable_ultrasonic=self.enable_ultrasonic,
            enable_zoompan=self.enable_zoompan,
            enable_noise=self.enable_noise,
        )
        cmd = builder.build_command(
            clip_start=clip.start_time,
            clip_end=clip.end_time,
            dead_air=clip_plan.dead_air,
            broll_cues=relevant_cues,
            broll_paths=broll_paths,
            hook_text=clip.hook_text,
            seo_keyword=clip.seo_keyword or _slug(clip.hook_text),
            cta_text=clip.cta_text or "Save video ini & Share ke teman →",
            crop_window=crop_window,
        )
        if build_only:
            return cmd
        builder.run(cmd)
        return output

    def render_all(
        self,
        src: Path,
        transcript: Transcript,
        clip_plan: ClipPlan,
        broll_map: dict[str, Path],
        output_dir: Path,
    ) -> list[Path]:
        """
        Render all clips in plan. broll_map: keyword -> local preview path.
        Output filename: {seo_keyword}-{idx:02d}_{start}_{end}.mp4 (SEO hyphenated)
        Also writes caption.txt + engagement.json per job.
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []

        # Write engagement pack once per job (patch 3)
        try:
            engagement = {
                "niche_tag": clip_plan.niche_tag,
                "niche_profit_tier": clip_plan.niche_profit_tier,
                "niche_approved": clip_plan.niche_approved,
                "comments": clip_plan.engagement_comments,
                "replies": clip_plan.engagement_replies,
            }
            (output_dir / "engagement.json").write_text(json.dumps(engagement, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

        for idx, clip in enumerate(clip_plan.clips):
            relevant_cues = [c for c in clip_plan.broll_cues if clip.start_time <= c.timestamp <= clip.end_time]
            paths: list[Path] = []
            for cue in relevant_cues:
                p = broll_map.get(cue.keywords_en) or broll_map.get(cue.fallback_en)
                if p and p.exists():
                    paths.append(p)
            # SEO filename
            kw = (clip.seo_keyword or _slug(clip.hook_text)).strip("-")
            kw = re.sub(r"[^a-z0-9-]+", "-", kw.lower()).strip("-")[:40] or "viral-clip"
            out = output_dir / f"{kw}-{idx:02d}_{clip.start_time:.0f}_{clip.end_time:.0f}.mp4"
            # sanitize: ensure no spaces
            self.render_clip(src, clip, transcript, clip_plan, paths, out)
            outputs.append(out)

            # Sidecar caption.txt (patch 1: keyword first 50 chars + hashtags)
            try:
                caption = clip.caption or f"{kw.replace('-',' ')} — tonton sampai akhir"
                hashtags = " ".join(clip.hashtags) if clip.hashtags else ""
                # enforce keyword in first 50 chars
                kw_words = kw.replace("-", " ")
                if kw_words.lower() not in caption.lower()[:60]:
                    caption = f"{kw_words} — {caption}"
                txt = caption.strip()
                if hashtags:
                    txt += "\n\n" + hashtags
                (out.with_suffix(".txt")).write_text(txt, encoding="utf-8")
            except Exception:
                pass

        return outputs
