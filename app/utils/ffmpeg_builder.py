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


def _escape_drawtext(text: str) -> str:
    r"""Escape drawtext text for filter_complex: \ : ' % [ ] ;"""
    # Order matters: escape \ first
    t = text.replace("\\", "\\\\")
    t = t.replace(":", "\\:")
    t = t.replace("'", "\\'")
    t = t.replace("%", "\\%")
    t = t.replace("[", "\\[")
    t = t.replace("]", "\\]")
    t = t.replace(";", "\\;")
    t = t.replace("\n", " ")
    # Remove double quotes and control chars
    t = t.replace('"', "")
    return t


def _fontfile_arg() -> str:
    cand = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if cand.exists():
        return f":fontfile={cand}"
    # fallback try other common
    for p in [Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"), Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")]:
        if p.exists():
            return f":fontfile={p}"
    return ""


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
        seo_keyword: str = "",
        cta_text: str = "",
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

        # Visual fingerprint: slow 5% dynamic zoom — smooth via zoompan
        if self.enable_zoompan:
            # Use zoompan with d=1 and small increment; feed fps 30 Ensures smoothness.
            # Use pzoom to avoid initial jump, max 1.08 (~8% over ~50 frames * 0.0015)
            filter_parts.append(
                f"[{v_label}]zoompan=z='min(pzoom+0.0015\\,1.08)':d=1:s=720x1280:fps=30[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)}"

        # Subtle noise: visible but not intrusive — apply noise for 3 frames every ~2 sec (60 frames)
        if self.enable_noise:
            filter_parts.append(
                f"[{v_label}]noise=alls=6:allf=t:enable='between(mod(n\\,60)\\,0\\,3)'[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)}"

        # PASS 2 dead_air jump-cut — video part (audio part applied after pitch)
        rel_dead_air: list[tuple[float, float]] = []
        if dead_air:
            for d in dead_air:
                s = float(d.start) - clip_start
                e = float(d.end) - clip_start
                if e <= 0 or s >= duration:
                    continue
                s = max(s, 0.0)
                e = min(e, duration)
                if e <= 3.0:
                    continue
                if s < 3.0 < e:
                    s = 3.0
                if e - s >= 0.2:
                    rel_dead_air.append((s, e))
            if rel_dead_air:
                expr_v = "+".join(f"between(t\\,{s:.3f}\\,{e:.3f})" for s, e in rel_dead_air)
                nxt_v_da = f"v{len(filter_parts)+80}"
                filter_parts.append(f"[{v_label}]select='not({expr_v})',setpts=N/FRAME_RATE/TB[{nxt_v_da}];")
                v_label = nxt_v_da

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
            safe_hook = _escape_drawtext(hook_text)
            ff = _fontfile_arg()
            filter_parts.append(
                f"[{v_label}]drawtext=text='{safe_hook}'{ff}:fontcolor=white:fontsize=60:box=1:boxcolor=black@0.75:boxborderw=12:x=(w-text_w)/2:y=80:enable='between(t,0,3)'[{next_v}];"
            )
            # Also add per-word kinetic if transcript available (approximate split)
            if self.transcript and self.transcript.words:
                # For brevity, v1 uses hook only; per-word would generate ASS file
                pass
            v_label = next_v
            # No extra step needed

        # SEO keyword overlay OCR 0.2-2.7s (riset: keyword tebal 2-3 detik pertama di-scan OCR)
        if seo_keyword:
            safe_kw = _escape_drawtext(seo_keyword.replace("-", " ").upper())
            ff2 = _fontfile_arg()
            next_kw = f"v{len(filter_parts)+44}"
            filter_parts.append(f"[{v_label}]drawtext=text='{safe_kw}'{ff2}:fontcolor=yellow:fontsize=48:box=1:boxcolor=black@0.55:boxborderw=8:x=(w-text_w)/2:y=(h*0.35):enable='between(t,0.2,2.7)'[{next_kw}];")
            v_label = next_kw

        # CTA Share/Save last 5s bottom (panah ke kiri-bawah keranjang)
        if cta_text:
            safe_cta = _escape_drawtext(cta_text)
            ff3 = _fontfile_arg()
            next_cta = f"v{len(filter_parts)+45}"
            filter_parts.append(f"[{v_label}]drawtext=text='{safe_cta}'{ff3}:fontcolor=white:fontsize=38:box=1:boxcolor=red@0.7:boxborderw=10:x=(w-text_w)/2:y=h-160:enable='gte(t,{duration-5:.1f})'[{next_cta}];")
            v_label = next_cta

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
        # PASS 2 audio jump-cut (mirror video select)
        if rel_dead_air:
            expr_a = "+".join(f"between(t\\,{s:.3f}\\,{e:.3f})" for s, e in rel_dead_air)
            nxt_a_da2 = f"a{len(filter_parts)+85}"
            filter_parts.append(f"[{a_label}]aselect='not({expr_a})',asetpts=N/SR/TB[{nxt_a_da2}];")
            a_label = nxt_a_da2
            next_a = f"a{len(filter_parts)+86}"

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
