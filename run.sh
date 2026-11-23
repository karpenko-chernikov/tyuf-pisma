#!/bin/bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  .venv/bin/pip install -r requirements.txt
fi

if [ ! -f ".env" ]; then
  cp .env.example .env
  echo ""
  echo "Создан .env — задайте SECRET_KEY и пароли пользователей (один раз)."
  echo "После первого входа пароли меняются в Кабинете."
  echo ""
fi

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"
.venv/bin/uvicorn app.main:app --host "$HOST" --port "$PORT"
