#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
[ -e .env ] || cp .env.example .env
URL='http://127.0.0.1:8000'
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    docker compose up --build -d
else
    PY="$(command -v python3 || command -v python || true)"
    [ -n "$PY" ] || { echo '请先安装 Python 3.11+ 或 Docker。'; exit 1; }
    [ -x .venv/bin/python ] || "$PY" -m venv .venv
    .venv/bin/python -m pip install -r requirements.txt 'playwright>=1.55,<2'
    .venv/bin/python -m playwright install chromium || echo '浏览器组件未装上，社交平台采集暂不可用'
    nohup .venv/bin/python run.py > lead-radar.log 2>&1 &
    echo "$!" > .lead-radar.pid
fi
ok=false
for i in $(seq 1 35); do
  if curl -fsS "$URL/api/health" >/dev/null 2>&1; then ok=true; break; fi
  sleep 1
done
if [ "$ok" != true ]; then echo '启动失败，请检查 lead-radar.log 或 docker compose logs'; exit 1; fi
if command -v open >/dev/null 2>&1; then open "$URL"; elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$URL"; fi
echo "Lead Radar 已启动：$URL"
