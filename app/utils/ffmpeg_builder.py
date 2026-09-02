from __future__ import annotations

import re
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


def _wrap_text(text: str, max_chars: int = 22) -> str:
    words = text.strip().split()
    lines: list[str] = []
    cur = ""
    for w in words:
        if len(cur) + len(w) + (1 if cur else 0) <= max_chars:
            cur = cur + (" " if cur else "") + w
        else:
            if cur:
                lines.append(cur)
            cur = w
            while len(cur) > max_chars:
                lines.append(cur[:max_chars])
                cur = cur[max_chars:]
    if cur:
        lines.append(cur)
    return "\n".join(lines)

def _escape_drawtext(text: str) -> str:
    r"""Escape drawtext text for filter_complex: \ : ' % [ ] ; — keeps \n for wrap"""
    parts = text.split("\n")
    out: list[str] = []
    for part in parts:
        t = part.replace("\\", "\\\\")
        t = t.replace(":", "\\:")
        t = t.replace("'", "\\'")
        t = t.replace("%", "\\%")
        t = t.replace("[", "\\[")
        t = t.replace("]", "\\]")
        t = t.replace(";", "\\;")
        t = t.replace('"', "")
        out.append(t)
    return "\n".join(out) if len(out) > 1 else out[0] if out else ""

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

def _write_word_ass(words: list, clip_start: float, clip_end: float, ass_path: Path, hook_text: str = "", dead_air: list[tuple[float,float]] | None = None) -> bool:
    """Generate per-word kinetic ASS (pop yellow scale 125->100). Returns True if written."""
    try:
        rel = []
        da = dead_air or []
        # normalize da to list of (s,e) relative to clip_start already or absolute? caller passes rel_dead_air relative
        # da is already rel_dead_air (s,e) in clip timeline 0..dur
        def _compressed(tm: float) -> float:
            off = 0.0
            for s, e in da:
                if tm >= e:
                    off += e - s
                elif tm > s:
                    off += tm - s
                    break
                else:
                    break
            return max(0.0, tm - off)
        for w in words:
            ws = float(getattr(w, "start", 0))
            we = float(getattr(w, "end", ws + 0.3))
            if we <= clip_start or ws >= clip_end:
                continue
            rs_raw = max(0.0, ws - clip_start)
            re_raw = min(clip_end - clip_start, we - clip_start)
            rs = _compressed(rs_raw)
            re = _compressed(re_raw)
            if re - rs < 0.12:
                re = rs + 0.18
            rel.append((str(getattr(w, "word", "")).strip(), rs, re))
        if not rel:
            return False
        ass_path.parent.mkdir(parents=True, exist_ok=True)
        if len(rel) > 70:
            rel = rel[:70]
        lines = []
        lines.append("[Script Info]")
        lines.append("ScriptType: v4.00+")
        lines.append("PlayResX: 720")
        lines.append("PlayResY: 1280")
        lines.append("ScaledBorderAndShadow: yes")
        lines.append("")
        lines.append("[V4+ Styles]")
        lines.append("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding")
        lines.append("Style: Default,DejaVu Sans,52,&H00FFFFFF,&H00FFFF00,&H00000000,&H96000000,1,0,0,0,100,100,0,0,1,4,2,5,15,15,35,1")
        lines.append("Style: Hook,DejaVu Sans,56,&H00FFFFFF,&H00FFFFFF,&H00000000,&HAA000000,1,0,0,0,100,100,0,0,1,6,2,5,15,15,200,1")
        lines.append("")
        lines.append("[Events]")
        lines.append("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text")
        for txt, rs, re in rel:
            if not txt:
                continue
            txt_esc = txt.replace("{", "(").replace("}", ")").replace("\n", " ")
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
        enable_ultrasonic: bool = False,
        enable_zoompan: bool = True,
        enable_noise: bool = True,
        enable_audio_alter: bool = False,
    ) -> None:
        self.src = src
        self.output = output
        self.transcript = transcript
        self.music_path = music_path
        self.enable_ultrasonic = enable_ultrasonic
        self.enable_zoompan = enable_zoompan
        self.enable_noise = enable_noise
        self.enable_audio_alter = enable_audio_alter
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
        source_channel: str = "",
        tiktok_handle: str = "brogalanblora",
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

        # B-Roll overlay — Coverr auto fetch (source_broll) + xfade 0.25s alpha (PDF 2-3s stok)
        for idx, (cue, bpath) in enumerate(zip(broll_cues, broll_paths)):
            broll_input_idx = 1 + idx
            b_v = f"b{idx}"
            # b-roll chain: scale -> yuva420p -> fade in/out alpha (0.25s) so overlay xfade smooth, not hard cut
            filter_parts.append(
                f"[{broll_input_idx}:v]scale=720:1280:force_original_aspect_ratio=increase,crop=720:1280,setsar=1,fps=30,format=yuva420p,fade=t=in:st=0:d=0.25:alpha=1,fade=t=out:st=1.75:d=0.25:alpha=1,format=yuva420p[{b_v}];"
            )
            offset = max(0, cue.timestamp - clip_start)
            out_v = f"v{len(filter_parts)+10}"
            filter_parts.append(
                f"[{v_label}][{b_v}]overlay=0:0:enable='between(t,{offset:.2f},{offset+2:.2f})',format=yuv420p[{out_v}];"
            )
            v_label = out_v

        # Hook drawtext top-third y=80 0-3s — wrap 2 baris anti-potong 720px
        if hook_text:
            wrapped_hook = _wrap_text(hook_text, 22)
            nlines = wrapped_hook.count("\n") + 1
            hook_fs = 44 if nlines > 1 or len(hook_text) > 32 else 60
            hook_y = 50 if nlines > 1 else 80
            ls = ":line_spacing=10" if nlines > 1 else ""
            safe_hook = _escape_drawtext(wrapped_hook)
            ff = _fontfile_arg()
            filter_parts.append(
                f"[{v_label}]drawtext=text='{safe_hook}'{ff}:fontcolor=white:fontsize={hook_fs}:box=1:boxcolor=black@0.75:boxborderw=12:x=(w-text_w)/2:y={hook_y}{ls}:enable='between(t,0,3)'[{next_v}];"
            )
            v_label = next_v
            next_v = f"v{len(filter_parts)+1}"
            # ponytail: per-word kinetic uses ASS below, not drawtext loop

        # SEO keyword 0.2-2.7s yellow — wrap juga
        if seo_keyword:
            wrapped_kw = _wrap_text(seo_keyword.replace("-", " ").upper(), 20)
            nkw = wrapped_kw.count("\n") + 1
            kw_fs = 36 if nkw > 1 else 48
            ls2 = ":line_spacing=8" if nkw > 1 else ""
            safe_kw = _escape_drawtext(wrapped_kw)
            ff2 = _fontfile_arg()
            next_kw = f"v{len(filter_parts)+44}"
            filter_parts.append(f"[{v_label}]drawtext=text='{safe_kw}'{ff2}:fontcolor=yellow:fontsize={kw_fs}:box=1:boxcolor=black@0.55:boxborderw=8:x=(w-text_w)/2:y=(h*0.35){ls2}:enable='between(t,0.2,2.7)'[{next_kw}];")
            v_label = next_kw

        # CTA last 5s — wrap jika >28 char
        if cta_text:
            wrapped_cta = _wrap_text(cta_text, 28) if len(cta_text) > 28 else cta_text
            ncta = wrapped_cta.count("\n") + 1
            cta_fs = 32 if ncta > 1 else 38
            cta_y = "h-190" if ncta > 1 else "h-160"
            ls3 = ":line_spacing=6" if ncta > 1 else ""
            safe_cta = _escape_drawtext(wrapped_cta)
            ff3 = _fontfile_arg()
            next_cta = f"v{len(filter_parts)+45}"
            filter_parts.append(f"[{v_label}]drawtext=text='{safe_cta}'{ff3}:fontcolor=white:fontsize={cta_fs}:box=1:boxcolor=red@0.7:boxborderw=10:x=(w-text_w)/2:y={cta_y}{ls3}:enable='gte(t,{duration-5:.1f})'[{next_cta}];")
            v_label = next_cta

        
        # Watermark subtle fixed: @handle • src: channel (fair use atribusi, bukan random besar)
        # fontsize 16 alpha 0.18 di h-28, tidak nutup hook y=50 / CTA h-160 / ASS 360,820
        try:
            handle = (tiktok_handle or "brogalanblora").strip().lstrip("@")[:24] or "brogalanblora"
            ch = (source_channel or "").strip()[:36]
            # clean channel: remove special chars that break drawtext
            if ch:
                # keep alnum space - _
                ch = re.sub(r"[^\w\s\-]", "", ch).strip()
                ch = " ".join(ch.split())  # collapse
                wm_text = f"@{handle} \u2022 src: {ch}" if ch else f"@{handle}"
            else:
                wm_text = f"@{handle}"
            if wm_text:
                safe_wm = _escape_drawtext(wm_text)
                ff_wm = _fontfile_arg()
                next_wm = f"v{len(filter_parts)+90}"
                # alpha 0.18 subtle, box@0.35 small border 4
                filter_parts.append(f"[{v_label}]drawtext=text='{safe_wm}'{ff_wm}:fontcolor=white@0.18:fontsize=16:box=1:boxcolor=black@0.35:boxborderw=4:x=w-text_w-14:y=h-28:enable='gte(t,0)'[{next_wm}];")
                v_label = next_wm
        except Exception:
            pass

        # Kinetic per-word ASS (pop yellow) — after all drawtext so words on top
        ass_added = False
        if self.transcript and self.transcript.words and duration > 2:
            try:
                ass_path = self.output.with_suffix(".ass")
                # unique per clip when same output stem reused: append start
                if ass_path.exists():
                    ass_path = self.output.parent / f"{self.output.stem}_{int(clip_start)}.ass"
                ok = _write_word_ass(self.transcript.words, clip_start, clip_end, ass_path, hook_text, rel_dead_air)
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
        if self.enable_audio_alter:
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
            _dead_sum = sum(e - s for s, e in rel_dead_air) if rel_dead_air else 0.0
            _ultra_dur = max(1.0, duration - _dead_sum)
            filter_parts.append(
                f"sine=frequency=19000:sample_rate=48000:duration={_ultra_dur:.2f}:beep_factor=1[ultra];"
                f"[ultra]volume=0.015[ultra_vol];"
                f"[{a_label}][ultra_vol]amix=inputs=2:duration=first:dropout_transition=0[{next_a}];"
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
            cmd += ["-map", f"[{final_v}]"]
            # a_label may still be raw 0:a (no audio filter) -> map without brackets
            if a_label == "0:a":
                cmd += ["-map", "0:a"]
            else:
                cmd += ["-map", f"[{a_label}]"]
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
