# start-desktop.sh / stop-desktop.sh 共通: このフォルダの管理画面サーバーと画像一覧の更新ループを
# PID ファイルと /proc/PID/cmdline・cwd で確かめる (プロセス名の部分一致は使わない)。
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"; PORT="${PORT:-8765}"
RUN="$D/../state"; mkdir -p "$RUN"
SRV_PID="$RUN/desktop-server.pid"; IDX_PID="$RUN/desktop-index.pid"
_argv() { local A=(); { mapfile -d "" -t A < "/proc/$1/cmdline"; } 2>/dev/null || return 1; [ "${#A[@]}" -gt 0 ] || return 1; printf '%s\n' "${A[@]}"; }
_cwd() { readlink "/proc/$1/cwd" 2>/dev/null; }
# 管理画面サーバー: argv が「python3 -m http.server <PORT> --directory <D>」で cwd が D
srv_ok() { local p a; p=$(cat "$SRV_PID" 2>/dev/null) || return 1; [[ "$p" =~ ^[0-9]+$ ]] || return 1
  a="$(_argv "$p")" || return 1
  [[ "$(sed -n 2,3p <<<"$a" | tr '\n' ' ')" == "-m http.server " ]] && [ "$(sed -n 4p <<<"$a")" = "$PORT" ] \
    && [ "$(sed -n 5p <<<"$a")" = "--directory" ] && [ "$(sed -n 6p <<<"$a")" = "$D" ] && [ "$(_cwd "$p")" = "$D" ] || return 1
  SRV=$p; }
# 画像一覧の更新ループ: argv が「bash -c <このフォルダ用のループ本文>」で cwd が D
idx_ok() { local p a; p=$(cat "$IDX_PID" 2>/dev/null) || return 1; [[ "$p" =~ ^[0-9]+$ ]] || return 1
  a="$(_argv "$p")" || return 1
  [[ "$(sed -n 1p <<<"$a")" == *bash ]] && [ "$(sed -n 2p <<<"$a")" = "-c" ] && grep -qF "gen-index.py; sleep 30 # comfy-lab-index $D" <<<"$a" \
    && [ "$(_cwd "$p")" = "$D" ] || return 1
  IDX=$p; }
port_busy() { python3 - "$PORT" <<'PY'
import socket, sys
s = socket.socket(); s.settimeout(1)
sys.exit(0 if s.connect_ex(("127.0.0.1", int(sys.argv[1]))) == 0 else 1)
PY
}
