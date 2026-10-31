#!/bin/bash
# 管理画面サーバーと画像一覧の更新ループを止める (start-desktop.sh が起動したものだけ)。
# PID ファイルの PID について実行内容と作業フォルダを確かめてから止める。名前の部分一致では止めない。
source "$(dirname "$0")/desktop_lib.sh"
stop_one() { # $1=名前 $2=判定関数 $3=PIDファイル $4=変数名
  if "$2"; then local p="${!4}"; kill "$p"
    for i in 1 2 3 4 5 6 7 8 9 10; do "$2" || break; sleep 0.3; done
    "$2" && kill -9 "$p"; echo "$1: 停止 (pid $p)"
  else echo "$1: 動いていない (または別のプロセス。触れない)"; fi
  rm -f "$3"; }
stop_one "サーバー (port $PORT)" srv_ok "$SRV_PID" SRV
stop_one "画像一覧の更新" idx_ok "$IDX_PID" IDX
port_busy && echo "注意: port $PORT はまだ別のプロセスが使っている (このフォルダのものではないので止めていない)" || echo "port $PORT: 空き"
