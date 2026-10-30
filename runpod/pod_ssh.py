#!/usr/bin/env python3
"""管理対象 Pod への SSH 操作 (paramiko。~/comfy-lab/.venv/bin/python で実行)。
CIVITAI_TOKEN は Pod の環境変数に入れず SSH 標準入力で渡し、出力では伏字にする。

使い方: pod_ssh.py setup-model <pod> [--check-only] | lora <pod> <URL|版ID> | tunnel-start <pod> | tunnel-stop
                   | arm-watchdog <pod> | watchdog-check <pod> | volume <pod> | pose-setup <pod> | run <pod> "CMD"
"""
import json
import os
import select
import socket
import socketserver
import subprocess
import sys
import threading
import time

import paramiko

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import runpod_pod as rp  # noqa: E402
import model_info as mi  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TUNNEL_PID = os.path.join(rp.ROOT, "state", "tunnel.pid")
LOCAL_PORT = int(os.environ.get("COMFY_LOCAL_PORT", "18188"))
SDXL_VAE = {"url": "https://huggingface.co/stabilityai/sdxl-vae/resolve/6f5909a7e596173e25d4e97b07fd19cdf9611c76/sdxl_vae.safetensors",
            "size": 334641164, "sha256": "63aeecb90ff7bc1c115395962d3e803571385b61938377bc7089b36e81e92e2e"}


def connect(pod_id):
    rp.require_managed(pod_id)
    p = rp.api_or_exit("GET", f"/pods/{pod_id}")
    d = rp.ssh_direct(p)
    if not d:
        sys.exit("SSH 接続先 (ssh.direct) がまだ無い。runpod_pod.py wait を先に")
    key = os.path.expanduser(rp.CFG.get("ssh_key") or "~/.ssh/comfylab_ed25519")
    for _ in range(20):
        try:
            c = paramiko.SSHClient()
            c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            c.connect(d["host"], int(d["port"]), username=d.get("username") or "root", key_filename=key, timeout=20,
                      banner_timeout=30, auth_timeout=30, allow_agent=False, look_for_keys=False)
            return c
        except Exception as e:
            print("SSH 再試行", type(e).__name__)
            time.sleep(6)
    sys.exit("SSH 接続できない")


def run(pod_id, cmd, stdin_text=None, secret=None):
    c = connect(pod_id)
    t = c.get_transport().open_session()
    t.set_combine_stderr(True)
    t.exec_command(cmd)
    if stdin_text is not None:
        t.sendall(stdin_text.encode())
    t.shutdown_write()
    out = []
    while True:
        if t.recv_ready():
            d = t.recv(4096).decode(errors="replace")
            if secret:
                d = d.replace(secret, "***")
            sys.stdout.write(d)
            sys.stdout.flush()
            out.append(d)
        elif t.exit_status_ready():
            break
        else:
            time.sleep(0.3)
    code = t.recv_exit_status()
    c.close()
    print(f"[exit {code}]")
    return code, "".join(out)


def put(pod_id, local, remote):
    c = connect(pod_id)
    s = c.open_sftp()
    s.put(local, remote)
    s.close()
    c.close()


def required_files():
    """config.json から今回必要なファイルの一覧 (subdir, file, url, size, sha256, size_tol)。"""
    cfg = rp.CFG
    req = []
    if not cfg.get("checkpoint") or not cfg.get("checkpoint_url"):
        sys.exit("config.json の checkpoint / checkpoint_url が未設定 (model_info.py で配布ページから調べる)")
    req.append(("checkpoints", cfg["checkpoint"], cfg["checkpoint_url"], cfg.get("checkpoint_size") or "", cfg.get("checkpoint_sha256") or "",
                2048 if mi.is_civitai(cfg["checkpoint_url"]) else 0))
    vae = cfg.get("vae") or "sdxl_vae.safetensors"
    if cfg.get("vae_url"):
        req.append(("vae", vae, cfg["vae_url"], cfg.get("vae_size") or "", cfg.get("vae_sha256") or "", 2048 if mi.is_civitai(cfg["vae_url"]) else 0))
    elif vae == "sdxl_vae.safetensors":
        req.append(("vae", vae, SDXL_VAE["url"], SDXL_VAE["size"], SDXL_VAE["sha256"], 0))
    else:
        sys.exit(f"VAE {vae} の入手先 (vae_url, 分かれば vae_sha256) が config.json に無い。SDXL の中身で代用しない")
    for l in cfg.get("loras") or []:
        req.append(("loras", l["file"], l["url"], l.get("size") or "", l.get("sha256") or "", 2048 if mi.is_civitai(l["url"]) else 0))
    for sub, f, *_ in req:
        if "/" in f or "|" in f or not f:
            sys.exit(f"ファイル名が不正: {f}")
    return req


def push_and_run_setup(pod, req, check_only=False):
    put(pod, os.path.join(HERE, "pod_setup.sh"), "/root/pod_setup.sh")
    need_tok = any(mi.is_civitai(r[2]) for r in req) and not check_only
    tok = os.environ.get("CIVITAI_TOKEN", "") if need_tok else ""
    if need_tok and not tok:
        sys.exit("環境変数 CIVITAI_TOKEN が未設定 (Civitai からの取得に必要)")
    lines = "\n".join("|".join(str(x) for x in r) for r in req)
    code, _ = run(pod, "bash /root/pod_setup.sh" + (" --check-only" if check_only else ""), stdin_text=tok + "\n" + lines + "\n", secret=tok or None)
    return code


def serve_tunnel(pod):
    c = connect(pod)
    tr = c.get_transport()
    tr.set_keepalive(30)

    class H(socketserver.BaseRequestHandler):
        def handle(self):
            try:
                ch = tr.open_channel("direct-tcpip", ("127.0.0.1", 8188), self.request.getpeername())
            except Exception as e:
                print("channel 失敗", e, flush=True)
                return
            while True:
                r, _, _ = select.select([self.request, ch], [], [])
                if self.request in r:
                    d = self.request.recv(65536)
                    if not d:
                        break
                    ch.sendall(d)
                if ch in r:
                    d = ch.recv(65536)
                    if not d:
                        break
                    self.request.sendall(d)
            ch.close()
            self.request.close()

    class S(socketserver.ThreadingTCPServer):
        daemon_threads = True
        allow_reuse_address = True

    srv = S(("127.0.0.1", LOCAL_PORT), H)  # このコンピュータの中からだけ使える
    print(f"tunnel 127.0.0.1:{LOCAL_PORT} -> {pod}:8188", flush=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    while tr.is_active():
        time.sleep(5)
    srv.shutdown()
    sys.exit("SSH が切れた。tunnel-start をやり直す")


def tunnel_stop():
    pid = rp.load_json(TUNNEL_PID, None)
    if isinstance(pid, int):
        try:
            os.kill(pid, 15)
        except OSError:
            pass
    if os.path.exists(TUNNEL_PID):
        os.remove(TUNNEL_PID)


def tunnel_start(pod):
    import urllib.request
    rp.require_managed(pod)
    tunnel_stop()
    log = open(os.path.join(rp.ROOT, "state", "tunnel.log"), "a")
    p = subprocess.Popen([sys.executable, os.path.abspath(__file__), "tunnel", pod], stdin=subprocess.DEVNULL, stdout=log,
                         stderr=subprocess.STDOUT, start_new_session=True)
    rp.save_json(TUNNEL_PID, p.pid)
    base = f"http://127.0.0.1:{LOCAL_PORT}"
    for i in range(90):
        try:
            with urllib.request.urlopen(base + "/system_stats", timeout=10) as r:
                if r.status == 200:
                    print(f"ComfyUI 応答あり (トンネル経由, {i * 5}s)")
                    print(f"export COMFY_BASE_URL={base} COMFY_POD_ID={pod}")
                    for node, field in (("LoraLoader", "lora_name"), ("CheckpointLoaderSimple", "ckpt_name"), ("VAELoader", "vae_name")):
                        try:
                            with urllib.request.urlopen(f"{base}/object_info/{node}", timeout=30) as r2:
                                print(f"{node}:", ", ".join(json.loads(r2.read())[node]["input"]["required"][field][0]) or "なし")
                        except Exception as e:
                            print(f"{node}: 取得失敗 {e}")
                    return 0
        except Exception:
            pass
        if p.poll() is not None:
            sys.exit("トンネルが終了した (state/tunnel.log を確認)")
        time.sleep(5)
    sys.exit("ComfyUI が応答しない (初回は数分かかる。もう一度 tunnel-start)")


WATCHDOG_SH = r'''#!/usr/bin/env bash
# comfy-lab: 期限 (epoch) を過ぎたらこの Pod 自身を削除する。キーは PID1 の環境 (Pod 用キー) から読み、表示しない。
DEADLINE="$1"; LOG=/root/comfylab-watchdog.log
envv() { tr '\0' '\n' < /proc/1/environ | sed -n "s/^$1=//p" | head -n1; }
ID="${RUNPOD_POD_ID:-$(envv RUNPOD_POD_ID)}"; KEY="${RUNPOD_API_KEY:-$(envv RUNPOD_API_KEY)}"
while [ "$(date +%s)" -lt "$DEADLINE" ]; do sleep 20; done
echo "$(date -u +%FT%TZ) deadline reached, deleting $ID" >> "$LOG"
for i in 1 2 3 4 5; do
  RUNPOD_API_KEY="$KEY" runpodctl pod delete "$ID" >> "$LOG" 2>&1 && exit 0
  RUNPOD_API_KEY="$KEY" runpodctl remove pod "$ID" >> "$LOG" 2>&1 && exit 0
  code=$(curl -s -o /dev/null -w '%{http_code}' -X DELETE -H "Authorization: Bearer $KEY" "https://api.runpod.io/v2/pods/$ID")
  echo "REST delete -> $code" >> "$LOG"; [ "$code" = 204 ] && exit 0
  sleep 30
done
'''


def main():
    a = sys.argv[1:]
    if len(a) < 1:
        sys.exit(__doc__)
    cmd = a[0]
    if cmd == "tunnel-stop":
        tunnel_stop()
        print("トンネル停止")
        return
    if len(a) < 2:
        sys.exit(__doc__)
    pod = a[1]
    if cmd == "run":
        sys.exit(run(pod, a[2])[0])
    if cmd == "tunnel":
        serve_tunnel(pod)
    if cmd == "tunnel-start":
        sys.exit(tunnel_start(pod))
    if cmd == "setup-model":
        sys.exit(push_and_run_setup(pod, required_files(), "--check-only" in a))
    if cmd == "lora":
        src = a[2]
        url = src if src.startswith("https://") else f"https://civitai.com/api/v1/model-versions/{int(src)}"
        info = mi.lookup(url)
        if not info.get("sha256"):
            print("注意: 配布元に SHA256 が無い。サイズだけで照合する")
        name = os.path.basename(a[3]) if len(a) > 3 else info["file"]
        tol = 0 if info.get("size_exact") else 2048
        print(f"LoRA: {info.get('name')} -> {name}  size≈{info.get('size')}  sha256={'あり' if info.get('sha256') else 'なし'}  ライセンス: {info.get('license')}")
        code = push_and_run_setup(pod, [("loras", name, info["url"], info.get("size") or "", info.get("sha256") or "", tol)])
        if code == 0:
            print(f"次の生成で --set 11.lora_name={name}。続けて使うなら config.json の loras に追記 (再起動後も setup-model が確認する)")
        sys.exit(code)
    if cmd == "arm-watchdog":
        st, rec = rp.require_managed(pod)
        c = connect(pod)
        s = c.open_sftp()
        with s.open("/root/comfylab_watchdog.sh", "w") as f:
            f.write(WATCHDOG_SH)
        s.close()
        c.close()
        code, _ = run(pod, f"chmod +x /root/comfylab_watchdog.sh; pkill -f comfylab_watchdog.sh; "
                           f"nohup setsid /root/comfylab_watchdog.sh {int(rec['deadline'])} >/dev/null 2>&1 & sleep 1; "
                           f"pgrep -f comfylab_watchdog.sh >/dev/null && echo 'Pod内タイマー: 稼働中'")
        sys.exit(code)
    if cmd == "watchdog-check":
        sys.exit(run(pod, "for v in RUNPOD_POD_ID RUNPOD_API_KEY; do tr '\\0' '\\n' </proc/1/environ | grep -q \"^$v=.\" && echo \"$v: あり\" || echo \"$v: なし\"; done; "
                          "command -v runpodctl >/dev/null && runpodctl version 2>&1 | head -1 || echo 'runpodctl: なし'; "
                          "pgrep -f comfylab_watchdog.sh >/dev/null && echo 'Pod内タイマー: 稼働中' || echo 'Pod内タイマー: なし'")[0])
    if cmd == "pose-setup":
        sh = ("set -euo pipefail; R=$(dirname \"$(dirname \"$(find /workspace -maxdepth 5 -type d -path '*/models/checkpoints' | head -n1)\")\"); "
              "cd \"$R/custom_nodes\"; [ -d comfyui_controlnet_aux ] || git clone --depth 1 https://github.com/Fannovel16/comfyui_controlnet_aux.git; "
              "mkdir -p \"$R/models/controlnet\"; ls -l \"$R/models/controlnet\"; echo '(未検証) ComfyUI の再起動後に新ノードが使える'")
        sys.exit(run(pod, sh)[0])
    if cmd == "volume":
        code, out = run(pod, "df -B1 /workspace | tail -1")
        parts = out.split()
        if code == 0 and len(parts) >= 4 and parts[1].isdigit():
            total, free = int(parts[1]) / 1e9, int(parts[3]) / 1e9
            m = rp.load_json(rp.MODELS_JSON, None)
            if m is not None:
                m["volume"] = {"total_gb": round(total, 1), "free_gb": round(free, 2), "free_label": f"約{free:.1f}GB"}
                m["updated"] = time.strftime("%Y-%m-%d %H:%M")
                rp.save_json(rp.MODELS_JSON, m)
                print(f"models.json の容量を更新: 全体 {total:.1f}GB / 空き {free:.1f}GB")
        sys.exit(code)
    sys.exit(__doc__)


if __name__ == "__main__":
    main()
