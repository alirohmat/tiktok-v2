from __future__ import annotations

from pathlib import Path

from app.models.schemas import Clip, ClipPlan, Transcript
from app.utils.autoframe import detect_crop_window
from app.utils.ffmpeg_builder import FFmpegBuilder


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
        # If more cues than paths, truncate; if fewer, pad
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
        """
        output_dir.mkdir(parents=True, exist_ok=True)
        outputs: list[Path] = []
        for idx, clip in enumerate(clip_plan.clips):
            # Collect B-Roll paths for this clip's cues
            relevant_cues = [c for c in clip_plan.broll_cues if clip.start_time <= c.timestamp <= clip.end_time]
            paths: list[Path] = []
            for cue in relevant_cues:
                p = broll_map.get(cue.keywords_en) or broll_map.get(cue.fallback_en)
                if p and p.exists():
                    paths.append(p)
            out = output_dir / f"clip_{idx:02d}_{clip.start_time:.0f}_{clip.end_time:.0f}.mp4"
            self.render_clip(src, clip, transcript, clip_plan, paths, out)
            outputs.append(out)
        return outputs
