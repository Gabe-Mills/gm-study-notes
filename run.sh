#!/bin/zsh
# Start YT Notes on http://127.0.0.1:5210
cd "$(dirname "$0")"
exec uv run uvicorn app:app --host 127.0.0.1 --port 5210
