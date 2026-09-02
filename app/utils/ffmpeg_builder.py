from __future__ import annotations

import random
import subprocess
import tempfile
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
    t = text.replace("\\", "\\\\")
    t = t.replace(":", "\\:")
    t = t.replace("'", "\\'")
    t = t.replace("%", "\\%")
    t = t.replace("[", "\\[")
    t = t.replace("]", "\\]")
    t = t.replace(";", "\\;")
    t = t.replace("\n", " ")
    t = t.replace('"', "")
    return t

def _fontfile_arg() -> str:
    cand = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")
    if cand.exists():
        return f":fontfile={cand}"
    for p in [Path("/usr/share/fonts/TTF/DejaVuSans-Bold.ttf"), Path("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf")]:
        if p.exists():
            return f":fontfile={p}"
    return ""

def _fmt_ass_time(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    cs = int((t - int(t)) * 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

def _write_word_ass(words: list, clip_start: float, clip_end: float, ass_path: Path, hook_text: str = "") -> bool:
    """Generate per-word kinetic ASS (pop yellow scale 120->100). Returns True if written."""
    try:
        # filter words inside clip
        rel = []
        for w in words:
            ws = float(getattr(w, "start", 0))
            we = float(getattr(w, "end", ws + 0.3))
            if we <= clip_start or ws >= clip_end:
                continue
            rs = max(0.0, ws - clip_start)
            re = min(clip_end - clip_start, we - clip_start)
            # clamp min duration 0.18s for readability
            if re - rs < 0.12:
                re = rs + 0.18
            rel.append((str(getattr(w, "word", "")).strip(), rs, re))
        if not rel:
            return False
        ass_path.parent.mkdir(parents=True, exist_ok=True)
        # keep only first ~60 words to avoid ASS bloat for 90s clip (rare)
        if len(rel) > 70:
            rel = rel[:70]
        # build ASS
        lines = []
        lines.append("[Script Info]")
        lines.append("ScriptType: v4.00+")
        lines.append("PlayResX: 720")
        lines.append("PlayResY: 1280")
        lines.append("ScaledBorderAndShadow: yes")
        lines.append("")
        lines.append("[V4+ Styles]")
        lines.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
        # white with black outline, shadow, centered middle (an5 pos 360,650), yellow highlight via override
        lines.append("Style: Default,DejaVu Sans,52,&H00FFFFFF,&H00FFFF00,&H00000000,&H96000000,1,0,0,0,100,100,0,0,1,4,2,5,15,15,35,1")
        lines.append("Style: Hook,DejaVu Sans,56,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,6,2,5,15,15,200,1")
        lines.append("")
        lines.append("[Events]")
        lines.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
        # Optional hook as first 0-3s center top? keep drawtext hook + ASS hook duplicate? skip duplicate if hook_text used in drawtext
        dur = clip_end - clip_start
        for txt, rs, re in rel:
            if not txt:
                continue
            txt_esc = txt.replace("{", "(").replace("}", ")").replace("\n", " ")
            # pop animation: start big yellow -> white, centered at (360, 820) lower-middle (keeps y=80 hook free)
            # Use an5 middle, pos 360x820 (about 64% height) so SEO y 0.35*1280=448 not overlap
            tag = r"{\an5\pos(360,820)\fscx125\fscy125\c&H00FFFF&\t(0,120,\fscx100\fscy100\c&HFFFFFF&)}"
            lines.append(f"Dialogue: 0,{_fmt_ass_time(rs)},{_fmt_ass_time(re)},Default,,0,0,0,,{tag}{txt_esc}")
        ass_path.write_text("\n".join(lines), encoding="utf-8")
        return True
    except Exception:
        return False

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

        cmd: list[str] = ["ffmpeg", "-y"]
        cmd += ["-ss", f"{clip_start:.3f}", "-t", f"{duration:.3f}", "-i", str(self.src)]
        for bp in broll_paths:
            cmd += ["-i", str(bp)]
        has_music = self.music_path is not None and self.music_path.exists()
        if has_music:
            cmd += ["-i", str(self.music_path)]

        filter_parts: list[str] = []
        v_label = "0:v"
        next_v = "v0"

        if crop_window is not None:
            crop_f = build_crop_filter(crop_window)
            if crop_f:
                filter_parts.append(f"[{v_label}]{crop_f}[{next_v}];")
                v_label = next_v
                next_v = f"v{len(filter_parts)}"

        filter_parts.append(f"[{v_label}]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1,fps=30[{next_v}];")
        v_label = next_v
        next_v = f"v{len(filter_parts)}"

        if self.enable_zoompan:
            filter_parts.append(
                f"[{v_label}]zoompan=z='min(pzoom+0.0015\\,1.08)':d=1:s=720x1280:fps=30[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)}"

        if self.enable_noise:
            filter_parts.append(
                f"[{v_label}]noise=alls=6:allf=t:enable='between(mod(n\\,60)\\,0\\,3)'[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)}"

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

        # B-Roll overlay — glitch-ish via tblend? simple overlay with scale + slight blur edge
        for idx, (cue, bpath) in enumerate(zip(broll_cues, broll_paths)):
            broll_input_idx = 1 + idx
            b_v = f"b{idx}"
            filter_parts.append(f"[{broll_input_idx}:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1,fps=30,format=yuv420p[{b_v}];")
            offset = max(0, cue.timestamp - clip_start)
            # xfade-like: overlay 2s with soft alpha blend via tblend would need extra; keep overlay enable + fade
            out_v = f"v{len(filter_parts)+10}"
            # fade in/out 0.15s via alpha? use overlay + format; keep simple but add format
            filter_parts.append(
                f"[{v_label}][{b_v}]overlay=0:0:enable='between(t,{offset:.2f},{offset+2:.2f})',format=yuv420p[{out_v}];"
            )
            v_label = out_v

        # Hook drawtext top-third y=80 0-3s
        if hook_text:
            safe_hook = _escape_drawtext(hook_text)
            ff = _fontfile_arg()
            filter_parts.append(
                f"[{v_label}]drawtext=text='{safe_hook}'{ff}:fontcolor=white:fontsize=60:box=1:boxcolor=black@0.75:boxborderw=12:x=(w-text_w)/2:y=80:enable='between(t,0,3)'[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)+1}"
            # ponytail: per-word kinetic uses ASS below, not drawtext loop

        # SEO keyword 0.2-2.7s yellow
        if seo_keyword:
            safe_kw = _escape_drawtext(seo_keyword.replace("-", " ").upper())
            ff2 = _fontfile_arg()
            next_kw = f"v{len(filter_parts)+44}"
            filter_parts.append(f"[{v_label}]drawtext=text='{safe_kw}'{ff2}:fontcolor=yellow:fontsize=48:box=1:boxcolor=black@0.55:boxborderw=8:x=(w-text_w)/2:y=(h*0.35):enable='between(t,0.2,2.7)'[{next_kw}];")
            v_label = next_kw

        # CTA last 5s
        if cta_text:
            safe_cta = _escape_drawtext(cta_text)
            ff3 = _fontfile_arg()
            next_cta = f"v{len(filter_parts)+45}"
            filter_parts.append(f"[{v_label}]drawtext=text='{safe_cta}'{ff3}:fontcolor=white:fontsize=38:box=1:boxcolor=red@0.7:boxborderw=10:x=(w-text_w)/2:y=h-160:enable='gte(t,{duration-5:.1f})'[{next_cta}];")
            v_label = next_cta

        # Kinetic per-word ASS (pop yellow) — after all drawtext so words on top
        ass_added = False
        if self.transcript and self.transcript.words and duration > 2:
            try:
                ass_path = self.output.with_suffix(".ass")
                # unique per clip when same output stem reused: append start
                if ass_path.exists():
                    ass_path = self.output.parent / f"{self.output.stem}_{int(clip_start)}.ass"
                ok = _write_word_ass(self.transcript.words, clip_start, clip_end, ass_path, hook_text)
                if ok and ass_path.exists():
                    # escape colon and single quote for filter_complex
                    ass_str = str(ass_path).replace(":", "\\:").replace("'", "")
                    next_ass = f"v{len(filter_parts)+90}"
                    filter_parts.append(f"[{v_label}]ass='{ass_str}'[{next_ass}];")
                    v_label = next_ass
                    ass_added = True
            except Exception:
                pass

        final_v = v_label

        a_label = "0:a"
        next_a = "a0"
        filter_parts.append(
            f"[{a_label}]asetrate=48480,aresample=48000,atempo=1/1.01[{next_a}];"
        )
        a_label = next_a
        next_a = f"a{len(filter_parts)+20}"
        if rel_dead_air:
            expr_a = "+".join(f"between(t\\,{s:.3f}\\,{e:.3f})" for s, e in rel_dead_air)
            nxt_a_da2 = f"a{len(filter_parts)+85}"
            filter_parts.append(f"[{a_label}]aselect='not({expr_a})',asetpts=N/SR/TB[{nxt_a_da2}];")
            a_label = nxt_a_da2
            next_a = f"a{len(filter_parts)+86}"

        if self.enable_ultrasonic:
            filter_parts.append(
                f"sine=frequency=19000:sample_rate=48000:duration={duration:.2f}:beep_factor=1[ultra];"
                f"[ultra]volume=0.015[ultra_vol];"
                f"[{a_label}][ultra_vol]amix=inputs=2:duration=longest:dropout_transition=0[{next_a}];"
            )
            a_label = next_a
            next_a = f"a{len(filter_parts)+30}"

        if has_music:
            music_idx = 1 + len(broll_paths)
            filter_parts.append(
                f"[{music_idx}:a]volume=0.1[bgm];"
                f"[{a_label}][bgm]amix=inputs=2:duration=first:dropout_transition=0[{next_a}];"
            )
            a_label = next_a

        filter_complex = "".join(filter_parts)
        if filter_complex.endswith(";"):
            filter_complex = filter_complex[:-1]
        if filter_complex:
            cmd += ["-filter_complex", filter_complex]
            cmd += ["-map", f"[{final_v}]", "-map", f"[{a_label}]"]
        else:
            cmd += ["-map", "0:v", "-map", "0:a"]

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
            "ass": "ass=" in joined,
        }
