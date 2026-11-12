#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

MODE="postgres"
CMD="up"

for arg in "$@"; do
  case "$arg" in
    --sqlite) MODE="sqlite" ;;
    --down) CMD="down" ;;
    --logs) CMD="logs" ;;
    *) echo "Использование: ./run.sh [--sqlite] [--down|--logs]"; exit 1 ;;
  esac
done

if [ "$MODE" = "sqlite" ]; then
  COMPOSE=(docker compose -f docker-compose.sqlite.yml)
else
  COMPOSE=(docker compose)
fi

case "$CMD" in
  down) "${COMPOSE[@]}" down ;;
  logs) "${COMPOSE[@]}" logs -f app ;;
  up)
    "${COMPOSE[@]}" up --build -d
    echo "CheckStock запущен: http://localhost:8000"
    ;;
esac
