#!/bin/bash
# yt-dlp Control Center - Start Script
# Jalankan: bash start-ytdlp.sh
# atau: /tmp/yt-venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload

set -e
cd "$(dirname "$0")"

# Gunakan venv di /tmp/yt-venv (dibuat via UV_CACHE_DIR=/tmp/uv-cache)
# Jika belum ada, buat otomatis
if [ ! -x "/tmp/yt-venv/bin/uvicorn" ]; then
  echo "Membuat venv..."
  export UV_CACHE_DIR=/tmp/uv-cache
  mkdir -p /tmp/uv-cache
  uv venv --python 3.10 /tmp/yt-venv 2>&1 | tail -n 3
  export UV_CACHE_DIR=/tmp/uv-cache
  uv pip install -r requirements.txt --python /tmp/yt-venv/bin/python 2>&1 | tail -n 3
  uv pip install yt-dlp --python /tmp/yt-venv/bin/python 2>&1 | tail -n 3
  ln -sf /tmp/yt-venv .venv
fi

echo "Menjalankan server di http://127.0.0.1:8000 ..."
echo "Buka browser ke: http://localhost:8000"
echo "API docs: http://localhost:8000/docs"
echo "Health: http://localhost:8000/api/ytdlp/health"
/tmp/yt-venv/bin/uvicorn app.api.main:app --host 0.0.0.0 --port 8000 --reload
