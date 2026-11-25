#!/bin/sh
set -e
python scripts/seed_orgs_json.py \
  scripts/tula_tyuf_2026.json \
  scripts/buryatia_tyuf.json \
  || echo "seed_orgs_json skipped"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000
