#!/usr/bin/env bash
set -e
export DATABASE_URL="${DATABASE_URL:-sqlite:///./storage/app.db}"
export STORAGE_DIR="${STORAGE_DIR:-./storage/files}"
exec .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}"
