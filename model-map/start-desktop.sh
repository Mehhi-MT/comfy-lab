#!/bin/bash
# 管理画面の裏方を起動 (何度実行しても安全)。PORT 既定 8765。ブラウザ表示はボットのデスクトップで http://localhost:PORT/ を開いて F11。
D="$(cd "$(dirname "$0")" && pwd)"; PORT="${PORT:-8765}"; PY="$D/../.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$D" || exit 1
if ! pgrep -f "http.server $PORT" >/dev/null; then
  nohup python3 -m http.server "$PORT" --directory "$D" >"$D/server.log" 2>&1 &
  echo "サーバー起動 (port $PORT)"
else echo "サーバー: 稼働中 (port $PORT)"; fi
if ! pgrep -f "gen-index.py; sleep 30 # $D" >/dev/null; then
  nohup bash -c "cd '$D'; while true; do $PY gen-index.py; sleep 30 # $D
done" >"$D/gen-index.log" 2>&1 &
  echo "画像一覧の更新ループ起動"
else echo "画像一覧の更新: 稼働中"; fi
sleep 1
curl -s -o /dev/null -w "ページ HTTP %{http_code}\n" "http://localhost:$PORT/"
