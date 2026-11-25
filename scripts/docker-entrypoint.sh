#!/bin/sh
set -e
python scripts/seed_tula_tyuf_2026.py || echo "seed_tula_tyuf_2026 skipped"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
