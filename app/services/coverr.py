from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.core.config import get_settings
from app.models.schemas import CoverrVideo
from app.utils.cache import CoverrCache


class CoverrClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        cache: CoverrCache | None = None,
    ) -> None:
        settings = get_settings()
        self.api_key = api_key or settings.coverr_api_key
        self.base_url = (base_url or settings.coverr_base_url).rstrip("/")
        if cache is None:
            db_path = settings.storage_path / "cache" / "coverr_cache.db"
            cache = CoverrCache(db_path)
        self.cache = cache

    def _headers(self) -> dict[str, str]:
        if self.api_key and self.api_key != "your_coverr_api_key":
            return {"Authorization": f"Bearer {self.api_key}"}
        return {}

    async def search(
        self,
        keyword: str,
        fallback: str = "office worker",
        sort: str = "trending",
    ) -> CoverrVideo | None:
        """
        Search Coverr for keyword, filter vertical, cache result.
        Returns CoverrVideo with mp4_preview or None if not found.
        """
        # Check cache first (async)
        try:
            await self.cache.init()
        except Exception:
            pass
        cached = await self.cache.get(keyword)
        if cached:
            return CoverrVideo(
                video_id=cached["video_id"],
                preview_url=cached["preview_url"],
                download_url=cached.get("download_url", ""),
                is_vertical=bool(cached["is_vertical"]),
            )

        # If no API key, return mock
        if not self.api_key or self.api_key == "your_coverr_api_key":
            mock = self._mock_video(keyword)
            await self.cache.set(keyword, mock.video_id, mock.preview_url, mock.download_url, mock.is_vertical)
            return mock

        async with httpx.AsyncClient(timeout=20.0) as client:
            result = await self._search_query(client, keyword, sort)
            if result is None and fallback and fallback != keyword:
                result = await self._search_query(client, fallback, sort)
            if result is None:
                return None
            # Cache
            await self.cache.set(keyword, result.video_id, result.preview_url, result.download_url, result.is_vertical)
            return result

    async def _search_query(
        self, client: httpx.AsyncClient, keyword: str, sort: str
    ) -> CoverrVideo | None:
        url = f"{self.base_url}/videos"
        params = {"query": keyword, "urls": "true", "sort": sort}
        try:
            resp = await client.get(url, params=params, headers=self._headers())
        except Exception:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except Exception:
            return None

        # Support both shapes: {"hits": [...]} or {"videos": [...]} or list
        items: list[dict[str, Any]] = []
        if isinstance(data, dict):
            if "hits" in data:
                items = data["hits"]  # type: ignore[assignment]
            elif "videos" in data:
                items = data["videos"]  # type: ignore[assignment]
            elif "data" in data:
                items = data["data"]  # type: ignore[assignment]
            else:
                # Try to find list inside
                for v in data.values():
                    if isinstance(v, list):
                        items = v  # type: ignore[assignment]
                        break
        elif isinstance(data, list):
            items = data  # type: ignore[assignment]

        # Filter vertical
        for item in items:
            is_vert = item.get("is_vertical")
            if is_vert is None:
                # Fallback check width/height
                w = item.get("width") or item.get("video_width") or 0
                h = item.get("height") or item.get("video_height") or 0
                if w and h:
                    is_vert = h > w
                else:
                    # Check urls dict
                    is_vert = False
            # Only select vertical if spec requires
            if is_vert is not True:
                continue
            video_id = str(item.get("id") or item.get("video_id") or item.get("uuid") or keyword)
            # Extract preview url
            urls = item.get("urls") or item.get("video_urls") or {}
            preview = ""
            download = ""
            if isinstance(urls, dict):
                preview = urls.get("mp4_preview") or urls.get("preview") or ""
                download = urls.get("mp4") or urls.get("mp4_download") or preview
            else:
                preview = item.get("mp4_preview") or item.get("preview_url") or item.get("url") or ""
                download = item.get("mp4") or preview
            if not preview:
                preview = download
            if not preview:
                continue
            return CoverrVideo(
                video_id=video_id,
                preview_url=preview,
                download_url=download or preview,
                is_vertical=True,
                width=int(item.get("width", 720)),
                height=int(item.get("height", 1280)),
                title=str(item.get("title", "")),
            )

        # If no vertical found, return first item if any (fallback)
        if items:
            first = items[0]
            urls = first.get("urls") or {}
            if isinstance(urls, dict):
                preview = urls.get("mp4_preview") or urls.get("mp4") or ""
            else:
                preview = first.get("mp4_preview") or ""
            if preview:
                return CoverrVideo(
                    video_id=str(first.get("id", keyword)),
                    preview_url=preview,
                    download_url=preview,
                    is_vertical=False,
                    title=str(first.get("title", "")),
                )
        return None

    def _mock_video(self, keyword: str) -> CoverrVideo:
        # Deterministic mock: use placeholder preview URL (will be mocked in download)
        safe = keyword.replace(" ", "_")
        return CoverrVideo(
            video_id=f"mock_{safe}",
            preview_url=f"https://mock.coverr.co/{safe}_preview.mp4",
            download_url=f"https://mock.coverr.co/{safe}.mp4",
            is_vertical=True,
            width=720,
            height=1280,
            title=f"Mock {keyword}",
        )

    async def download(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        # Mock handling
        if "mock.coverr.co" in url:
            # Create a tiny placeholder mp4 (black 720x1280 1s) via ffmpeg if not exists
            if not dest.exists():
                import subprocess

                subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-f",
                        "lavfi",
                        "-i",
                        "color=c=black:s=720x1280:d=2:r=30",
                        "-c:v",
                        "libx264",
                        "-pix_fmt",
                        "yuv420p",
                        str(dest),
                    ],
                    check=True,
                    capture_output=True,
                )
            return dest

        async with httpx.AsyncClient(timeout=60.0) as client:
            async with client.stream("GET", url, headers=self._headers()) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes():
                        f.write(chunk)
        return dest

    # Sync versions for Celery

    def search_sync(self, keyword: str, fallback: str = "office worker") -> CoverrVideo | None:
        # Use sync cache + httpx sync client
        cached = self.cache.get_sync(keyword)
        if cached:
            return CoverrVideo(
                video_id=cached["video_id"],
                preview_url=cached["preview_url"],
                download_url=cached.get("download_url", ""),
                is_vertical=bool(cached["is_vertical"]),
            )
        if not self.api_key or self.api_key == "your_coverr_api_key":
            mock = self._mock_video(keyword)
            self.cache.set_sync(keyword, mock.video_id, mock.preview_url, mock.download_url, mock.is_vertical)
            return mock

        # Use httpx sync
        import httpx as httpx_sync

        with httpx_sync.Client(timeout=20.0) as client:
            # Reuse _search_query logic but sync
            url = f"{self.base_url}/videos"
            params = {"query": keyword, "urls": "true", "sort": "trending"}
            try:
                resp = client.get(url, params=params, headers=self._headers())
            except Exception:
                return None
            if resp.status_code != 200:
                if fallback and fallback != keyword:
                    params["query"] = fallback
                    try:
                        resp = client.get(url, params=params, headers=self._headers())
                    except Exception:
                        return None
                    if resp.status_code != 200:
                        return None
                else:
                    return None
            try:
                data = resp.json()
            except Exception:
                return None
            # Same parsing as async
            items: list[dict[str, Any]] = []
            if isinstance(data, dict):
                if "hits" in data:
                    items = data["hits"]  # type: ignore[assignment]
                elif "videos" in data:
                    items = data["videos"]  # type: ignore[assignment]
                elif "data" in data:
                    items = data["data"]  # type: ignore[assignment]
                else:
                    for v in data.values():
                        if isinstance(v, list):
                            items = v  # type: ignore[assignment]
                            break
            elif isinstance(data, list):
                items = data  # type: ignore[assignment]

            for item in items:
                is_vert = item.get("is_vertical")
                if is_vert is None:
                    w = item.get("width") or 0
                    h = item.get("height") or 0
                    if w and h:
                        is_vert = h > w
                    else:
                        is_vert = False
                if is_vert is not True:
                    continue
                video_id = str(item.get("id") or keyword)
                urls = item.get("urls") or {}
                preview = ""
                if isinstance(urls, dict):
                    preview = urls.get("mp4_preview") or urls.get("preview") or ""
                else:
                    preview = item.get("mp4_preview") or ""
                if not preview:
                    continue
                cv = CoverrVideo(
                    video_id=video_id,
                    preview_url=preview,
                    download_url=preview,
                    is_vertical=True,
                    width=int(item.get("width", 720)),
                    height=int(item.get("height", 1280)),
                    title=str(item.get("title", "")),
                )
                self.cache.set_sync(keyword, cv.video_id, cv.preview_url, cv.download_url, cv.is_vertical)
                return cv
            if items:
                first = items[0]
                urls = first.get("urls") or {}
                preview = urls.get("mp4_preview") or first.get("mp4_preview") or ""  # type: ignore[union-attr]
                if preview:
                    cv = CoverrVideo(video_id=str(first.get("id", keyword)), preview_url=preview, download_url=preview, is_vertical=False)
                    self.cache.set_sync(keyword, cv.video_id, cv.preview_url, cv.download_url, cv.is_vertical)
                    return cv
        return None

    def download_sync(self, url: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if "mock.coverr.co" in url:
            if not dest.exists():
                import subprocess

                subprocess.run(
                    ["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=720x1280:d=2:r=30", "-c:v", "libx264", "-pix_fmt", "yuv420p", str(dest)],
                    check=True,
                    capture_output=True,
                )
            return dest
        import httpx as httpx_sync

        with httpx_sync.Client(timeout=60.0) as client:
            with client.stream("GET", url, headers=self._headers()) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    for chunk in resp.iter_bytes():
                        f.write(chunk)
        return dest
