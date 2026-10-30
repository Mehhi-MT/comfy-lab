#!/bin/bash
# comfy-lab 導入・更新: bash install.sh   (導入先は ~/comfy-lab。引数の導入先指定は検証用)
# 利用者データ (config.json, model-map/models.json, out/, state/, .venv, ~/.ssh の鍵) は上書きしない。
set -Eeuo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
TARGET="${1:-$HOME/comfy-lab}"
NEW_VER="$(cat "$SRC/VERSION")"
fail() { echo "導入失敗: $*" >&2; exit 1; }
trap 'echo "導入失敗: $LINENO 行目のコマンドが失敗しました ($BASH_COMMAND)" >&2' ERR
step() { echo "== $*"; }

step "前提の確認"
command -v python3 >/dev/null || fail "python3 がありません"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' || fail "python3 3.9 以上が必要です ($(python3 -V 2>&1))"
python3 -c 'import venv, ensurepip' 2>/dev/null || fail "python3 の venv/ensurepip がありません (例: apt install python3-venv)"
command -v curl >/dev/null || fail "curl がありません"

step "配布物の照合 (MANIFEST.sha256)"
( cd "$SRC" && sha256sum --quiet -c MANIFEST.sha256 ) || fail "配布物のファイルが MANIFEST.sha256 と一致しません。展開をやり直すか、配布物を取り直してください"

mkdir -p "$TARGET"; TARGET="$(cd "$TARGET" && pwd)"
OLD_VER=""; [ -f "$TARGET/VERSION" ] && OLD_VER="$(cat "$TARGET/VERSION")"
FILES="$(cut -c67- "$SRC/MANIFEST.sha256")"

if [ -n "$OLD_VER" ]; then
  B="$TARGET/.backup/$OLD_VER-$(date +%Y%m%d-%H%M%S)"
  step "既存コード ($OLD_VER) を $B に退避"
  mkdir -p "$B"
  for f in $FILES MANIFEST.sha256; do
    [ -f "$TARGET/$f" ] && { mkdir -p "$B/$(dirname "$f")"; cp -p "$TARGET/$f" "$B/$f"; }
  done
fi

step "コードを配置 ($NEW_VER → $TARGET)"
for f in $FILES MANIFEST.sha256; do
  mkdir -p "$TARGET/$(dirname "$f")"
  cp "$SRC/$f" "$TARGET/$f.new.$$" && mv -f "$TARGET/$f.new.$$" "$TARGET/$f"
done
chmod +x "$TARGET"/runpod/*.py "$TARGET"/runpod/*.sh "$TARGET"/model-map/*.sh "$TARGET/install.sh"

step "利用者データ (無いものだけ作る)"
mkdir -p "$TARGET/out" "$TARGET/state"
if [ -f "$TARGET/config.json" ]; then echo "config.json: 既存を維持"; else cp "$SRC/templates/config.example.json" "$TARGET/config.json"; echo "config.json: ひな形から作成"; fi
if [ -f "$TARGET/model-map/models.json" ]; then echo "models.json: 既存を維持"; else cp "$SRC/templates/models.example.json" "$TARGET/model-map/models.json"; echo "models.json: ひな形から作成"; fi

step "Python 環境 (.venv) と依存関係"
[ -x "$TARGET/.venv/bin/python" ] || python3 -m venv "$TARGET/.venv"
"$TARGET/.venv/bin/pip" install -q --disable-pip-version-check -r "$TARGET/requirements.txt" || fail "依存関係を入れられません (ネットワークか pip の問題)。requirements.txt を確認してください"

step "SSH 鍵 (無いときだけ作る)"
"$TARGET/.venv/bin/python" - <<'PYEOF'
import os
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives import serialization as s
p = os.path.expanduser("~/.ssh/comfylab_ed25519")
if not os.path.exists(p):
    os.makedirs(os.path.dirname(p), mode=0o700, exist_ok=True)
    k = ed25519.Ed25519PrivateKey.generate()
    with open(os.open(p, os.O_WRONLY | os.O_CREAT, 0o600), "wb") as f:
        f.write(k.private_bytes(s.Encoding.PEM, s.PrivateFormat.OpenSSH, s.NoEncryption()))
    with open(p + ".pub", "wb") as f:
        f.write(k.public_key().public_bytes(s.Encoding.OpenSSH, s.PublicFormat.OpenSSH) + b" comfy-lab" + bytes([10]))
    print("SSH鍵: 作成", p, "(秘密鍵は表示しない)")
else:
    print("SSH鍵: 既存を維持", p)
PYEOF

step "導入確認"
cd "$TARGET"
sha256sum --quiet -c MANIFEST.sha256 || fail "導入先のコードが MANIFEST.sha256 と一致しません"
PY="$TARGET/.venv/bin/python"
"$PY" -m py_compile runpod/runpod_pod.py runpod/pod_ssh.py runpod/model_info.py runpod/comfy_run.py model-map/gen-index.py model-map/update_models.py
bash -n runpod/pod_setup.sh && bash -n model-map/start-desktop.sh && bash -n install.sh
for j in runpod/workflow-api.base.json runpod/workflow-api.lora1.json runpod/workflow-api.lora2.json config.json model-map/models.json; do
  "$PY" -c 'import json,sys; json.load(open(sys.argv[1]))' "$j" || fail "JSON が読めません: $j"
done
"$PY" -c 'import paramiko, PIL' || fail "paramiko / pillow を読み込めません"
python3 runpod/runpod_pod.py --help >/dev/null || fail "runpod_pod.py --help"
python3 runpod/comfy_run.py --help >/dev/null || fail "comfy_run.py --help"
# pod_ssh.py / model_info.py は引数なしで使い方を表示して終了コード 1 (元の動作のまま)
U="$("$PY" runpod/pod_ssh.py --help 2>&1 || true)"; case "$U" in *使い方*) ;; *) fail "pod_ssh.py が起動しません";; esac
"$PY" -c 'import sys; sys.path.insert(0, "runpod"); import model_info' || fail "model_info.py を読み込めません"
trap - ERR
echo "導入OK: comfy-lab $NEW_VER → $TARGET${OLD_VER:+ (更新前: $OLD_VER、退避: .backup/)}"
