#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then docker compose down || true; fi
if [ -f .lead-radar.pid ]; then
  pid=$(cat .lead-radar.pid)
  if ps -p "$pid" -o args= 2>/dev/null | grep -q 'run.py'; then kill "$pid" || true; fi
  rm -f .lead-radar.pid
fi
echo '已停止本项目服务，数据不会被删除。'
