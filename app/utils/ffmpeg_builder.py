from __future__ import annotations

import random
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.models.schemas import BrollCue, DeadAir, Transcript
try:
    from app.utils.autoframe import build_crop_filter, detect_crop_window
except ImportError:
    def build_crop_filter(crop):  # type: ignore[no-redef]
        if crop is None:
            return None
        x, y, cw, ch = crop
        return f"crop={cw}:{ch}:{x}:{y}"

    def detect_crop_window(*a, **kw):  # type: ignore[no-redef]
        return None


def random_creation_time() -> str:
    base = datetime(2023, 1, 1, tzinfo=timezone.utc)
    delta = timedelta(days=random.randint(0, 700), seconds=random.randint(0, 86400))
    dt = base + delta
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


class FFmpegBuilder:
    """
    Builds FFmpeg filter_complex for DNA alteration pipeline.
    Approach: Use intermediate trimmed clip as base, then apply filters sequentially.
    For testability, we expose methods that return filter strings.
    """

    def __init__(
        self,
        src: Path,
        output: Path,
        transcript: Transcript | None = None,
        music_path: Path | None = None,
        enable_ultrasonic: bool = True,
        enable_zoompan: bool = True,
        enable_noise: bool = True,
    ) -> None:
        self.src = src
        self.output = output
        self.transcript = transcript
        self.music_path = music_path
        self.enable_ultrasonic = enable_ultrasonic
        self.enable_zoompan = enable_zoompan
        self.enable_noise = enable_noise

    def build_command(
        self,
        clip_start: float,
        clip_end: float,
        dead_air: list[DeadAir],
        broll_cues: list[BrollCue],
        broll_paths: list[Path],
        hook_text: str = "",
        crop_window: tuple[int, int, int, int] | None = None,
    ) -> list[str]:
        """
        Build full ffmpeg command argv list for one clip.
        Returns argv suitable for subprocess.run.
        """
        duration = clip_end - clip_start
        creation_time = random_creation_time()

        # Base inputs: main video start/end trim
        cmd: list[str] = ["ffmpeg", "-y"]

        # Main input trimmed
        cmd += ["-ss", f"{clip_start:.3f}", "-t", f"{duration:.3f}", "-i", str(self.src)]

        # Additional inputs: B-Roll overlays and music
        for bp in broll_paths:
            cmd += ["-i", str(bp)]
        has_music = self.music_path is not None and self.music_path.exists()
        if has_music:
            cmd += ["-i", str(self.music_path)]

        # Build filter_complex
        filter_parts: list[str] = []
        # Track labels
        # [0:v] is main video, [0:a] main audio
        # We'll chain: crop -> scale -> zoompan -> noise -> broll -> subtitles
        # Audio: pitch -> ultra -> music mix

        # Video chain start
        v_label = "0:v"
        next_v = "v0"

        # Auto-framing crop
        if crop_window is not None:
            crop_f = build_crop_filter(crop_window)
            if crop_f:
                filter_parts.append(f"[{v_label}]{crop_f}[{next_v}];")
                v_label = next_v
                next_v = f"v{len(filter_parts)}"

        # Always scale to 720x1280 (vertical HD)
        # force_original_aspect_ratio + pad to avoid distortion
        filter_parts.append(f"[{v_label}]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1,fps=30[{next_v}];")
        v_label = next_v
        next_v = f"v{len(filter_parts)}"

        # Visual fingerprint: slow 5% dynamic zoom
        if self.enable_zoompan:
            # zoompan filter: d=1 duration each frame, z increment slowly to 1.05
            # Note: zoompan requires prior scale; we use zoompan on scaled frame
            # Using scale+zoompan: alternative is to use zoompan directly
            filter_parts.append(
                f"[{v_label}]zoompan=d=1:s=720x1280:fps=30:z='min(zoom+0.0015,1.05)'[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)}"

        # Transparent noise every 7 seconds (210 frames at 30fps)
        if self.enable_noise:
            # Use noise filter with enable expression: add very low opacity noise periodically
            # tblend or noise: we use "noise=alls=10:allf=t:enable='eq(mod(n,210),0)'" blended
            # Simpler: use eq and noise via select blending - approximate with noise alpha
            # For compatibility, use "noise=alls=6:allf=t:enable='eq(mod(n\\,210),0)'" would inject noise frame
            # We'll use split + overlay noise: generate noise source via color+noise
            # Simpler approach: use "noise" with temporal enable
            filter_parts.append(
                f"[{v_label}]noise=alls=8:allf=t:enable='eq(mod(n,210),0)'[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)}"

        # Dead air removal is handled via pre-trim segmentation or select filter.
        # For simplicity in MVP, we apply trim-based jump cuts by generating concat segments.
        # If dead_air present, we would need to cut segments; v1 documents and skips complex select
        # and leaves it to builder documentation. We add a comment filter placeholder:
        # No additional filter if dead_air empty; otherwise would need select.
        # For now, skip dead_air video filter and assume pre-trim handled externally.

        # B-Roll overlay via glitch transition (xfade)
        # For multiple B-Rolls, chain xfade or overlay enable
        # Simplified: overlay each B-Roll at its cue timestamp (relative to clip start)
        for idx, (cue, bpath) in enumerate(zip(broll_cues, broll_paths)):
            broll_input_idx = 1 + idx  # after main 0
            # Need to scale B-Roll to 720x1280 as well
            b_v = f"b{idx}"
            filter_parts.append(f"[{broll_input_idx}:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1[{b_v}];")
            # Overlay with glitch effect: use tblend or xfade
            # xfade requires sequential timeline; use overlay with enable between
            offset = max(0, cue.timestamp - clip_start)
            # Use overlay with enable
            out_v = f"v{len(filter_parts)+10}"  # avoid collision
            # Use glitch transition via tblend for 0.5s? Simpler overlay
            filter_parts.append(
                f"[{v_label}][{b_v}]overlay=0:0:enable='between(t,{offset:.2f},{offset+2:.2f})'[{out_v}];"
            )
            v_label = out_v

        # Kinetic typography: drawtext for hook in first 3 seconds
        # If transcript + hook provided, generate per-word drawtext or simple hook
        if hook_text:
            # Single drawtext for hook duration 0-3s (word-by-word would require ASS)
            # Use hook_text split; for MVP single centered text
            safe_hook = hook_text.replace(":", "\\:").replace("'", "").replace('"', "")
            filter_parts.append(
                f"[{v_label}]drawtext=text='{safe_hook}':fontcolor=white:fontsize=48:box=1:boxcolor=black@0.6:boxborderw=10:x=(w-text_w)/2:y=h-220:enable='between(t,0,3)'[{next_v}];"
            )
            # Also add per-word kinetic if transcript available (approximate split)
            if self.transcript and self.transcript.words:
                # For brevity, v1 uses hook only; per-word would generate ASS file
                pass
            v_label = next_v
            # No extra step needed

        final_v = v_label

        # Audio chain
        a_label = "0:a"
        next_a = "a0"
        # Pitch shift +1% via asetrate (48000*1.01) + aresample + atempo correction
        filter_parts.append(
            f"[{a_label}]asetrate=48480,aresample=48000,atempo=1/1.01[{next_a}];"
        )
        a_label = next_a
        next_a = f"a{len(filter_parts)+20}"

        # Ultrasonic 19kHz sine mixed at low volume
        if self.enable_ultrasonic:
            # sine is an audio source, not a filter; generate directly
            filter_parts.append(
                f"sine=frequency=19000:sample_rate=48000:duration={duration:.2f}:beep_factor=1[ultra];"
                f"[ultra]volume=0.015[ultra_vol];"
                f"[{a_label}][ultra_vol]amix=inputs=2:duration=longest:dropout_transition=0[{next_a}];"
            )
            a_label = next_a
            next_a = f"a{len(filter_parts)+30}"

        # Mix background music at 10%
        if has_music:
            music_idx = 1 + len(broll_paths)  # last input
            # music volume 0.1
            filter_parts.append(
                f"[{music_idx}:a]volume=0.1[bgm];"
                f"[{a_label}][bgm]amix=inputs=2:duration=first:dropout_transition=0[{next_a}];"
            )
            a_label = next_a

        # Build final filter_complex string (join, strip trailing ;)
        filter_complex = "".join(filter_parts)
        if filter_complex.endswith(";"):
            filter_complex = filter_complex[:-1]

        if filter_complex:
            cmd += ["-filter_complex", filter_complex]
            cmd += ["-map", f"[{final_v}]", "-map", f"[{a_label}]"]
        else:
            cmd += ["-map", "0:v", "-map", "0:a"]

        # Encoding
        cmd += [
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
        ]
        # Metadata wipe
        cmd += ["-map_metadata", "-1", "-metadata", f"creation_time={creation_time}"]
        cmd += [str(self.output)]

        return cmd

    def run(self, cmd: list[str]) -> None:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"FFmpeg failed: {result.stderr[-2000:]}")

    @staticmethod
    def check_command(cmd: list[str]) -> dict[str, bool]:
        """For tests: check which DNA features are present in command."""
        joined = " ".join(cmd)
        return {
            "zoompan": "zoompan" in joined,
            "asetrate": "asetrate" in joined,
            "aevalsrc_or_sine": "aevalsrc" in joined or "sine" in joined,
            "amix": "amix" in joined,
            "map_metadata": "-map_metadata -1" in joined,
            "drawtext": "drawtext" in joined,
            "noise": "noise" in joined,
            "crop_or_scale": "crop=" in joined or "scale=" in joined,
        }
