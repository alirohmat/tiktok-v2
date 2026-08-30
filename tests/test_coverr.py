from pathlib import Path

import pytest

from app.services.coverr import CoverrClient
from app.utils.cache import CoverrCache


@pytest.mark.asyncio
async def test_cache_hit_avoids_http(tmp_path: Path, monkeypatch):
    db = tmp_path / "cache.db"
    cache = CoverrCache(db, ttl=86400)
    await cache.init()
    await cache.set("burning money", "vid123", "https://mock.coverr.co/burning_preview.mp4", is_vertical=True)

    client = CoverrClient(api_key="your_coverr_api_key", cache=cache)
    # Should return cached without HTTP
    video = await client.search("burning money")
    assert video is not None
    assert video.video_id == "vid123"
    assert video.is_vertical is True


@pytest.mark.asyncio
async def test_cache_ttl_expiry(tmp_path: Path):
    db = tmp_path / "cache.db"
    cache = CoverrCache(db, ttl=1)
    await cache.init()
    await cache.set("test key", "vid1", "http://example.com/a.mp4")
    import asyncio

    await asyncio.sleep(1.2)
    result = await cache.get("test key")
    assert result is None


def test_coverr_mock_vertical_filter(tmp_path: Path):
    db = tmp_path / "cache.db"
    cache = CoverrCache(db)
    client = CoverrClient(api_key="your_coverr_api_key", cache=cache)
    video = client.search_sync("burning money", fallback="office")
    assert video is not None
    assert video.is_vertical is True
    assert "mock.coverr.co" in video.preview_url


@pytest.mark.asyncio
async def test_coverr_download_mock(tmp_path: Path):
    db = tmp_path / "cache.db"
    cache = CoverrCache(db)
    client = CoverrClient(api_key="your_coverr_api_key", cache=cache)
    dest = tmp_path / "preview.mp4"
    # Mock url
    url = "https://mock.coverr.co/test_preview.mp4"
    result = await client.download(url, dest)
    assert result.exists()
    assert result.stat().st_size > 0


def test_coverr_sync_download_mock(tmp_path: Path):
    db = tmp_path / "cache.db"
    cache = CoverrCache(db)
    client = CoverrClient(api_key="your_coverr_api_key", cache=cache)
    dest = tmp_path / "preview_sync.mp4"
    url = "https://mock.coverr.co/test2_preview.mp4"
    result = client.download_sync(url, dest)
    assert result.exists()
