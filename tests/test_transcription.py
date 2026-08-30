from pathlib import Path

from app.models.schemas import AudioChunk
from app.services.transcription import async_chunk_audio, stitch_transcripts
from app.utils.audio import get_duration


def test_stitch_offset_correction():
    # Two chunks: first 0-180s, second 180-360s, with local timestamps
    chunk1 = AudioChunk(index=0, start_time=0.0, duration=180.0, path=Path("/tmp/c0.wav"))
    chunk2 = AudioChunk(index=1, start_time=180.0, duration=180.0, path=Path("/tmp/c1.wav"))

    res1 = {
        "text": "hello world",
        "words": [{"word": "hello", "start": 0.5, "end": 1.0}, {"word": "world", "start": 1.1, "end": 1.5}],
        "segments": [],
    }
    res2 = {
        "text": "foo bar",
        "words": [{"word": "foo", "start": 0.2, "end": 0.6}, {"word": "bar", "start": 0.7, "end": 1.0}],
        "segments": [],
    }
    transcript = stitch_transcripts([res1, res2], [chunk1, chunk2], total_duration=360)
    assert len(transcript.words) == 4
    assert transcript.words[0].word == "hello"
    assert transcript.words[0].start == 0.5
    assert transcript.words[2].word == "foo"
    # Second chunk offset should be 180 + local
    assert transcript.words[2].start == 180.2
    assert transcript.words[3].start == 180.7


def test_stitch_sorts_words():
    c1 = AudioChunk(index=0, start_time=0, duration=180, path=Path("/tmp/c0.wav"))
    c2 = AudioChunk(index=1, start_time=180, duration=180, path=Path("/tmp/c1.wav"))
    # Provide unsorted second chunk
    res1 = {"text": "a", "words": [{"word": "b", "start": 5, "end": 6}], "segments": []}
    res2 = {"text": "c", "words": [{"word": "a", "start": 1, "end": 2}], "segments": []}
    # Actually after offset, res2 words will be at 181, so order remains res1 then res2
    t = stitch_transcripts([res1, res2], [c1, c2])
    assert t.words[0].start < t.words[1].start


def test_async_chunk_audio(tmp_path: Path):
    # Create a 10s dummy audio via ffmpeg
    audio = tmp_path / "test.wav"
    import subprocess

    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "10", "-acodec", "pcm_s16le", str(audio)],
        check=True,
        capture_output=True,
    )
    out_dir = tmp_path / "chunks"
    chunks = async_chunk_audio(audio, out_dir, chunk_sec=3)
    # 10s with 3s chunks => 4 chunks
    assert len(chunks) == 4
    assert chunks[0].start_time == 0
    assert chunks[-1].duration > 0
    for c in chunks:
        assert c.path.exists()
        assert get_duration(c.path) > 0
