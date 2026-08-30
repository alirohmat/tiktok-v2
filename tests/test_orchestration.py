from pathlib import Path
import subprocess


def test_full_pipeline_eager(tmp_path: Path, monkeypatch):
    # Create a 6s sample video 1280x720 with audio
    src = tmp_path / "sample.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=1280x720:d=6:r=30",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            "6",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            "-pix_fmt",
            "yuv420p",
            "-shortest",
            str(src),
        ],
        check=True,
        capture_output=True,
    )
    assert src.exists()

    # Mock env to use eager + mock APIs (no keys needed)
    monkeypatch.setenv("GROQ_API_KEY", "your_groq_api_key")
    monkeypatch.setenv("MUSE_API_KEY", "your_muse_spark_key")
    monkeypatch.setenv("COVERR_API_KEY", "your_coverr_api_key")

    # Need to reconfigure celery eager
    from app.workers.celery_app import celery_app

    celery_app.conf.update(task_always_eager=True, task_eager_propagates=True)

    from app.workers.tasks import run_full_pipeline
    from app.core.config import get_settings

    # Patch storage to tmp
    settings = get_settings()
    orig_storage = settings.storage_dir
    settings.storage_dir = str(tmp_path / "storage")

    try:
        # Use tiny chunk size for test speed
        # Direct pipeline
        result = run_full_pipeline(str(src), job_id="test_eager")
        assert "outputs" in result or "job_id" in result
        # Check renders created if full render executed
        # In eager mode with mock Groq, transcription will be mock; LLM mock creates clips
        # Render may still run (requires FFmpeg) - check output count >=1
        if "outputs" in result:
            outputs = result["outputs"]
            assert len(outputs) >= 1
            for p in outputs:
                assert Path(p).exists()
                # Verify 9:16 via ffprobe
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", p],
                    capture_output=True,
                    text=True,
                    check=True,
                )
                import json

                data = json.loads(probe.stdout)
                for s in data["streams"]:
                    if s["codec_type"] == "video":
                        assert s["width"] == 720
                        assert s["height"] == 1280
    finally:
        settings.storage_dir = orig_storage
        celery_app.conf.task_always_eager = False
