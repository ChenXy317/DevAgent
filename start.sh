#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ! -f backend/.env ]]; then
  echo "缺少 backend/.env。可以先复制 .env.example。"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source backend/.env
set +a

if ! mysql --protocol=TCP -h "${MYSQL_HOST}" -P "${MYSQL_PORT}" -u "${MYSQL_USER}" -p"${MYSQL_PASSWORD}" -D "${MYSQL_DATABASE}" -e "SELECT 1" >/dev/null; then
  echo "无法连接 MySQL。"
  exit 1
fi

free_port() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
  fi
}

free_port 8000
free_port 5173

uv run --directory backend uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 &
back_pid=$!
npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173 &
front_pid=$!

cleanup() {
  kill "${back_pid}" "${front_pid}" >/dev/null 2>&1 || true
}
trap cleanup INT TERM EXIT
wait -n || true
