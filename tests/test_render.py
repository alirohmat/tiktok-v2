from pathlib import Path

from app.models.schemas import BrollCue, Clip, ClipPlan, DeadAir, Transcript, Word
from app.services.render import RenderEngine
from app.utils.ffmpeg_builder import FFmpegBuilder


def test_ffmpeg_builder_contains_filters(tmp_path: Path):
    src = tmp_path / "src.mp4"
    out = tmp_path / "out.mp4"
    # Create dummy src via lavfi
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=1280x720:d=1:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(src)],
        check=True,
        capture_output=True,
    )
    transcript = Transcript(text="hello", words=[Word(word="hello", start=0.1, end=0.5)], duration=1)
    builder = FFmpegBuilder(src=src, output=out, transcript=transcript)
    cmd = builder.build_command(
        clip_start=0,
        clip_end=1,
        dead_air=[],
        broll_cues=[],
        broll_paths=[],
        hook_text="viral hook test",
        crop_window=None,
    )
    checks = builder.check_command(cmd)
    assert checks["zoompan"] is True
    assert checks["asetrate"] is True
    assert checks["aevalsrc_or_sine"] is True
    assert checks["amix"] is True
    assert checks["map_metadata"] is True
    assert checks["drawtext"] is True
    assert checks["noise"] is True
    assert checks["crop_or_scale"] is True
    # Ensure output and metadata wipe present
    assert "-map_metadata" in cmd
    assert str(out) in cmd


def test_ffmpeg_builder_with_broll(tmp_path: Path):
    src = tmp_path / "src.mp4"
    broll = tmp_path / "broll.mp4"
    out = tmp_path / "out2.mp4"
    import subprocess

    for p in [src, broll]:
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=blue:s=720x1280:d=1:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(p)],
            check=True,
            capture_output=True,
        )
    builder = FFmpegBuilder(src=src, output=out)
    cue = BrollCue(timestamp=0.5, keywords_en="burning money", fallback_en="office")
    cmd = builder.build_command(0, 1, [], [cue], [broll], hook_text="hook")
    joined = " ".join(cmd)
    assert "overlay" in joined
    assert "scale=720:1280" in joined


def test_render_engine_build_only(tmp_path: Path):
    src = tmp_path / "src.mp4"
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=red:s=1280x720:d=2:r=30", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "2", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", "-shortest", str(src)],
        check=True,
        capture_output=True,
    )
    transcript = Transcript(text="test", words=[Word(word="test", start=0, end=0.5)], duration=20)
    plan = ClipPlan.model_validate(
        {
            "clips": [{"start_time": 0, "end_time": 15, "hook_text": "hook", "virality_score": 90}],
            "dead_air": [],
            "broll_cues": [],
        }
    )
    # Need duration >=15 for Clip validation, but render test uses 2s clip - use manual Clip bypass?
    # Create clip directly without validation for render test
    from app.models.schemas import Clip

    clip = Clip.model_construct(start_time=0, end_time=1.5, hook_text="hook", virality_score=90)
    engine = RenderEngine(enable_ultrasonic=False)  # disable for speed
    out = tmp_path / "clip.mp4"
    cmd = engine.render_clip(src, clip, transcript, plan, [], out, build_only=True)
    assert isinstance(cmd, list)
    assert "ffmpeg" in cmd
