#!/bin/bash
# 管理画面の裏方を起動 (何度実行しても安全)。PORT 既定 8765。ブラウザ表示はボットのデスクトップで http://localhost:PORT/ を開いて F11。
# 止めるときは stop-desktop.sh。稼働の判定は PID ファイルと実行内容・作業フォルダで行う (同じ番号の別サーバーを自分のものと見なさない)。
source "$(dirname "$0")/desktop_lib.sh"
PY="$D/../.venv/bin/python"; [ -x "$PY" ] || PY=python3
cd "$D" || exit 1
if srv_ok; then echo "サーバー: 稼働中 (port $PORT, pid $SRV)"
elif port_busy; then
  echo "[中止] port $PORT は別のプロセスが使っている (このフォルダの管理画面ではない)。そのプロセスには触れない。"
  echo "  別の番号で起動するには: PORT=8767 bash $0   (表示は http://localhost:8767/)"; exit 1
else
  nohup python3 -m http.server "$PORT" --directory "$D" >"$D/server.log" 2>&1 < /dev/null &
  echo $! > "$SRV_PID"; echo "サーバー起動 (port $PORT, pid $!)"
fi
if idx_ok; then echo "画像一覧の更新: 稼働中 (pid $IDX)"
else
  nohup bash -c "cd '$D'; while true; do '$PY' gen-index.py; sleep 30 # comfy-lab-index $D
done" >"$D/gen-index.log" 2>&1 < /dev/null &
  echo $! > "$IDX_PID"; echo "画像一覧の更新ループ起動 (pid $!)"
fi
sleep 1
code=$(curl -s -o /dev/null -w "%{http_code}" "http://localhost:$PORT/")
echo "ページ HTTP $code"
srv_ok && [ "$code" = 200 ] && echo "表示OK: http://localhost:$PORT/ はこのフォルダ ($D) の管理画面"
